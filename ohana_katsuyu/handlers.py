"""Deterministic and explicitly allowlisted Katsuyu handlers."""

from __future__ import annotations

import ctypes
import gzip
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Event
from time import monotonic, process_time, sleep
from typing import Any, Protocol

from ohana_katsuyu.models import (
    BackupCompressParameters,
    BackupCompressResult,
    BackupEncryptParameters,
    BackupEncryptResult,
    BackupVerifyParameters,
    BackupVerifyResult,
    InfraBackupParameters,
    InfraBackupResult,
    JobProgress,
    SystemHealthIssue,
    SystemHealthParameters,
    SystemHealthResult,
)

CHUNK_SIZE = 1024 * 1024
HANDLER_TYPES = (
    "system.health",
    "backup.compress",
    "backup.encrypt",
    "backup.verify",
    "backup.infra",
)


class JobCancelledError(RuntimeError):
    """Raised at a safe interruption point after Tsunade cancels a job."""


class JobTimeoutError(RuntimeError):
    """Raised at a safe interruption point after the global timeout."""


@dataclass(slots=True)
class HandlerContext:
    cancelled: Event = field(default_factory=Event)
    deadline: float | None = None
    progress_callback: Callable[[JobProgress], None] = lambda _value: None
    job_id: str | None = None
    worker_id: str | None = None
    attempt: int = 0

    def check(self) -> None:
        if self.cancelled.is_set():
            raise JobCancelledError("job cancelled by Tsunade")
        if self.deadline is not None and monotonic() >= self.deadline:
            raise JobTimeoutError("job timeout elapsed")

    def report(self, percent: float, stage: str, message: str | None = None) -> None:
        self.check()
        self.progress_callback(
            JobProgress(percent=percent, stage=stage, message=message)
        )


@dataclass(slots=True)
class KatsuyuWorkspace:
    """Confine every input, output and temporary file to one local root."""

    root: Path

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def input_file(self, relative: str) -> Path:
        path = self._resolve(relative)
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"workspace input is not a regular file: {relative}")
        return path

    def output_file(self, relative: str) -> Path:
        path = self._resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and (not path.is_file() or path.is_symlink()):
            raise ValueError(f"workspace output is not a regular file: {relative}")
        return path

    def _resolve(self, relative: str) -> Path:
        portable = PurePosixPath(relative)
        if (
            not portable.parts
            or portable.is_absolute()
            or ".." in portable.parts
            or "\\" in relative
            or ":" in relative
        ):
            raise ValueError("job paths must be relative to the Katsuyu workspace")
        resolved = self.root.joinpath(*portable.parts).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("job path escapes the Katsuyu workspace")
        return resolved


class InfraBackupTransferClient(Protocol):
    def download_job_input(
        self,
        job_id: str,
        worker_id: str,
        attempt: int,
        destination: Path,
        context: HandlerContext,
    ) -> tuple[str, int]: ...

    def upload_job_artifact(
        self,
        job_id: str,
        worker_id: str,
        attempt: int,
        source: Path,
        sha256: str,
        context: HandlerContext,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    platform: str
    cpu_percent: float
    memory_total_bytes: int
    memory_available_bytes: int
    temperature_c: float | None = None


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    @property
    def value(self) -> int:
        return (self.high << 32) + self.low


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("memory_load", ctypes.c_uint32),
        ("total_physical", ctypes.c_uint64),
        ("available_physical", ctypes.c_uint64),
        ("total_page_file", ctypes.c_uint64),
        ("available_page_file", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("available_virtual", ctypes.c_uint64),
        ("available_extended_virtual", ctypes.c_uint64),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("page_fault_count", ctypes.c_uint32),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
    ]


def _peak_working_set_bytes() -> int | None:
    if os.name != "nt":
        return None
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
    if not ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
        process, ctypes.byref(counters), counters.cb
    ):
        return None
    return int(counters.peak_working_set_size)


@dataclass(slots=True)
class SystemHealthProbe:
    """Collect CPU and memory with Windows APIs or procfs, without Agent."""

    sample_wait: Callable[[float], None] = sleep

    def collect(self) -> SystemMetrics:
        first = self._cpu_times()
        self.sample_wait(0.1)
        second = self._cpu_times()
        total_delta = (second[1] + second[2]) - (first[1] + first[2])
        idle_delta = second[0] - first[0]
        cpu_percent = (
            max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100))
            if total_delta > 0
            else 0.0
        )
        memory_total, memory_available = self._memory()
        return SystemMetrics(
            platform=f"{platform.system()} {platform.release()}".strip(),
            cpu_percent=cpu_percent,
            memory_total_bytes=memory_total,
            memory_available_bytes=memory_available,
        )

    @staticmethod
    def _cpu_times() -> tuple[int, int, int]:
        if os.name == "nt":
            idle = _FileTime()
            kernel = _FileTime()
            user = _FileTime()
            if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                raise OSError("GetSystemTimes failed")
            return idle.value, kernel.value, user.value
        values = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
        fields = [int(value) for value in values.split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return idle, sum(fields), 0

    @staticmethod
    def _memory() -> tuple[int, int]:
        if os.name == "nt":
            status = _MemoryStatus()
            status.length = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
                ctypes.byref(status)
            ):
                raise OSError("GlobalMemoryStatusEx failed")
            return int(status.total_physical), int(status.available_physical)
        fields: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            name, value = line.split(":", 1)
            fields[name] = int(value.strip().split()[0]) * 1024
        return fields["MemTotal"], fields["MemAvailable"]


@dataclass(slots=True)
class SystemHealthHandler:
    workspace: KatsuyuWorkspace
    probe: SystemHealthProbe = field(default_factory=SystemHealthProbe)
    disk_usage: Callable[[str], Any] = shutil.disk_usage

    def execute(
        self, parameters: dict[str, Any], context: HandlerContext | None = None
    ) -> dict[str, Any]:
        context = context or HandlerContext()
        SystemHealthParameters.model_validate(parameters)
        context.report(10, "system.sample")
        metrics = self.probe.collect()
        context.check()
        disk = self.disk_usage(str(self.workspace.root))
        issues = self._issues(metrics, disk)
        result = SystemHealthResult(
            status="DEGRADED" if issues else "OK",
            collected_at=datetime.now(UTC),
            platform=metrics.platform[:100],
            cpu_percent=metrics.cpu_percent,
            memory_total_bytes=metrics.memory_total_bytes,
            memory_available_bytes=metrics.memory_available_bytes,
            disk_total_bytes=int(disk.total),
            disk_free_bytes=int(disk.free),
            temperature_c=metrics.temperature_c,
            issues=issues,
        )
        context.report(100, "system.complete")
        return result.model_dump(mode="json")

    @staticmethod
    def _issues(metrics: SystemMetrics, disk: Any) -> list[SystemHealthIssue]:
        issues: list[SystemHealthIssue] = []
        memory_percent = (
            1 - metrics.memory_available_bytes / metrics.memory_total_bytes
        ) * 100
        disk_percent = disk.used / disk.total * 100 if disk.total else 100
        for code, label, value, limit in (
            ("resource.cpu.high", "CPU élevé", metrics.cpu_percent, 85),
            ("resource.memory.high", "Mémoire élevée", memory_percent, 85),
            ("resource.disk.high", "Disque occupé", disk_percent, 85),
        ):
            if value >= limit:
                issues.append(
                    SystemHealthIssue(
                        code=code,
                        message=f"{label}: {value:.1f} % (seuil {limit} %)",
                    )
                )
        if metrics.temperature_c is not None and metrics.temperature_c >= 75:
            issues.append(
                SystemHealthIssue(
                    code="resource.temperature.high",
                    message=f"Température élevée: {metrics.temperature_c:.1f} °C",
                )
            )
        return issues


def _hash_file(path: Path, context: HandlerContext, *, stage: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = path.stat().st_size
    completed = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            context.check()
            digest.update(chunk)
            completed += len(chunk)
            context.report(100 if size == 0 else completed / size * 100, stage)
    return digest.hexdigest(), completed


@dataclass(slots=True)
class BackupCompressHandler:
    workspace: KatsuyuWorkspace

    def execute(
        self, parameters: dict[str, Any], context: HandlerContext | None = None
    ) -> dict[str, Any]:
        context = context or HandlerContext()
        request = BackupCompressParameters.model_validate(parameters)
        source = self.workspace.input_file(request.source)
        destination = self.workspace.output_file(request.destination)
        if source == destination:
            raise ValueError("source and destination must differ")
        source_sha256, source_size = _hash_file(
            source, context, stage="compress.hash-source"
        )
        if not destination.exists():
            temporary = _temporary_path(destination)
            try:
                completed = 0
                with source.open("rb") as input_stream, temporary.open("wb") as raw:
                    with gzip.GzipFile(
                        filename="",
                        mode="wb",
                        compresslevel=request.compression_level,
                        fileobj=raw,
                        mtime=0,
                    ) as output_stream:
                        while chunk := input_stream.read(CHUNK_SIZE):
                            context.check()
                            output_stream.write(chunk)
                            completed += len(chunk)
                            percent = (
                                100
                                if source_size == 0
                                else completed / source_size * 100
                            )
                            context.report(percent, "compress.write")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        destination_sha256, destination_size = _hash_file(
            destination, context, stage="compress.verify"
        )
        return BackupCompressResult(
            source_sha256=source_sha256,
            source_size=source_size,
            destination_sha256=destination_sha256,
            destination_size=destination_size,
        ).model_dump(mode="json")


@dataclass(slots=True)
class BackupEncryptHandler:
    workspace: KatsuyuWorkspace
    age_binary: Path = Path("age.exe")

    def execute(
        self, parameters: dict[str, Any], context: HandlerContext | None = None
    ) -> dict[str, Any]:
        context = context or HandlerContext()
        request = BackupEncryptParameters.model_validate(parameters)
        source = self.workspace.input_file(request.source)
        destination = self.workspace.output_file(request.destination)
        if source == destination:
            raise ValueError("source and destination must differ")
        source_sha256, source_size = _hash_file(
            source, context, stage="encrypt.hash-source"
        )
        if not destination.exists():
            temporary = _temporary_path(destination)
            creation_flags = (
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # type: ignore[attr-defined]
            )
            process = subprocess.Popen(  # noqa: S603
                [
                    str(self.age_binary),
                    "--encrypt",
                    "--recipient",
                    request.recipient,
                    "--output",
                    str(temporary),
                    str(source),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creation_flags,
            )
            try:
                while process.poll() is None:
                    context.report(50, "encrypt.write")
                    sleep(0.2)
                stderr = (process.stderr.read() if process.stderr else "")[:1000]
                if process.returncode != 0:
                    raise RuntimeError(
                        "age encryption failed "
                        f"({process.returncode}): {stderr.strip()}"
                    )
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise RuntimeError("age did not produce an encrypted artifact")
                os.replace(temporary, destination)
            except (JobCancelledError, JobTimeoutError):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise
            finally:
                temporary.unlink(missing_ok=True)
        destination_sha256, destination_size = _hash_file(
            destination, context, stage="encrypt.verify"
        )
        return BackupEncryptResult(
            source_sha256=source_sha256,
            source_size=source_size,
            destination_sha256=destination_sha256,
            destination_size=destination_size,
            recipient=request.recipient,
        ).model_dump(mode="json")


@dataclass(slots=True)
class BackupVerifyHandler:
    workspace: KatsuyuWorkspace

    def execute(
        self, parameters: dict[str, Any], context: HandlerContext | None = None
    ) -> dict[str, Any]:
        context = context or HandlerContext()
        request = BackupVerifyParameters.model_validate(parameters)
        path = self.workspace.input_file(request.path)
        sha256, size = _hash_file(path, context, stage="verify.hash")
        sha256_matches = sha256 == request.expected_sha256
        size_matches = (
            size == request.expected_size if request.expected_size is not None else None
        )
        return BackupVerifyResult(
            valid=sha256_matches and size_matches is not False,
            sha256=sha256,
            size=size,
            sha256_matches=sha256_matches,
            size_matches=size_matches,
        ).model_dump(mode="json")


@dataclass(slots=True)
class InfraBackupHandler:
    """Fetch, compress, encrypt and return one Agent-owned INFRA backup."""

    workspace: KatsuyuWorkspace
    client: InfraBackupTransferClient
    age_binary: Path = Path("age.exe")

    def execute(
        self, parameters: dict[str, Any], context: HandlerContext | None = None
    ) -> dict[str, Any]:
        context = context or HandlerContext()
        request = InfraBackupParameters.model_validate(parameters)
        started_at = monotonic()
        cpu_started_at = process_time()
        if not context.job_id or not context.worker_id or context.attempt < 1:
            raise ValueError("distributed backup context is incomplete")
        prefix = f"jobs/{context.job_id}"
        source_relative = f"{prefix}/source.tar"
        compressed_relative = f"{prefix}/source.tar.gz"
        encrypted_relative = f"{prefix}/{request.backup_id}.tar.gz.age"
        source = self.workspace.output_file(source_relative)
        compressed = self.workspace.output_file(compressed_relative)
        encrypted = self.workspace.output_file(encrypted_relative)
        for path in (source, compressed, encrypted):
            path.unlink(missing_ok=True)
        try:
            context.report(1, "infra.download")
            source_sha256, source_size = self.client.download_job_input(
                context.job_id,
                context.worker_id,
                context.attempt,
                source,
                context,
            )
            compression = BackupCompressHandler(self.workspace).execute(
                {
                    "source": source_relative,
                    "destination": compressed_relative,
                    "compression_level": request.compression_level,
                },
                context,
            )
            encryption = BackupEncryptHandler(
                self.workspace, self.age_binary
            ).execute(
                {
                    "source": compressed_relative,
                    "destination": encrypted_relative,
                    "recipient": request.recipient,
                },
                context,
            )
            receipt = self.client.upload_job_artifact(
                context.job_id,
                context.worker_id,
                context.attempt,
                encrypted,
                str(encryption["destination_sha256"]),
                context,
            )
            result = InfraBackupResult(
                backup_id=request.backup_id,
                remote_path=receipt["remote_path"],
                source_sha256=source_sha256,
                source_size=source_size,
                compressed_size=int(compression["destination_size"]),
                sha256=receipt["sha256"],
                size_bytes=int(receipt["size_bytes"]),
                deleted_remote_backups=int(receipt["deleted_remote_backups"]),
                duration_seconds=monotonic() - started_at,
                cpu_seconds=process_time() - cpu_started_at,
                peak_working_set_bytes=_peak_working_set_bytes(),
                logical_io_read_bytes=(
                    source_size
                    + source_size
                    + int(compression["destination_size"])
                    + int(encryption["destination_size"])
                ),
                logical_io_written_bytes=(
                    source_size
                    + int(compression["destination_size"])
                    + int(encryption["destination_size"])
                    + int(receipt["size_bytes"])
                ),
            )
            context.report(100, "infra.complete")
            return result.model_dump(mode="json")
        finally:
            for path in (source, compressed, encrypted):
                path.unlink(missing_ok=True)
            try:
                source.parent.rmdir()
            except OSError:
                pass


def _temporary_path(destination: Path) -> Path:
    descriptor, value = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    return Path(value)

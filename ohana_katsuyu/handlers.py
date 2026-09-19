"""Deterministic and explicitly allowlisted Katsuyu handlers."""

from __future__ import annotations

import ctypes
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import tarfile
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Event
from time import monotonic, process_time, sleep
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from websocket import WebSocketException, create_connection

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
    LogBaseline,
    LogCorrelation,
    LogFinding,
    LogsHealthCheckParameters,
    LogsHealthCheckResult,
    LogsInvestigateParameters,
    LogsInvestigateResult,
    LogSourceHealth,
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
    "logs.health_check",
    "logs.investigate",
)
INFRA_REQUIRED_MEMBERS = frozenset(
    {
        "etc/ohana-agent",
        "etc/ohana-vision",
        "etc/dnsmasq.d",
        "etc/chrony/chrony.conf",
        "var/lib/ohana-vision/vision.db",
    }
)
INFRA_DIRECTORY_ROOTS = frozenset(
    {"etc/ohana-agent", "etc/ohana-vision", "etc/dnsmasq.d"}
)
INFRA_FORBIDDEN_MEMBERS = frozenset(
    {"etc/ohana-agent/tls/ca.key", "etc/ohana-agent/tls/ca.srl"}
)
INFRA_DESCRIPTOR = "ohana-backup/descriptor.json"


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


LogSourceProvider = Callable[[str, str, int, str], dict[str, Any]]
_TIMESTAMP = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)"
)
_ANOMALY = re.compile(
    r"\b(error|exception|traceback|timeout|timed out|failed|failure|"
    r"disconnect(?:ed)?|reconnect(?:ed|ing)?|restart(?:ed|ing)?|unavailable|"
    r"connection refused|econnrefused|shutdown requested|stopping|stopped|"
    r"starting|started|dead|checksum invalid|"
    r"transmission failed)\b",
    re.IGNORECASE,
)
_VARIABLE = re.compile(
    r"\b(?:[0-9a-f]{8}-[0-9a-f-]{27,}|0x[0-9a-f]+|"
    r"\d{1,3}(?:\.\d{1,3}){3}|\d+)\b",
    re.IGNORECASE,
)
_ENTITY_ID = re.compile(r"\b[a-z][a-z0-9_]*\.[a-z0-9_]+\b", re.IGNORECASE)
_SESSION_PATH = re.compile(r"(/stok=)[^/\s\"'<>]+", re.IGNORECASE)


def _safe_log_text(line: str) -> str:
    """Remove camera session credentials before grouping or extracting references."""
    return _SESSION_PATH.sub(r"\1[redacted]", line)


def _is_log_anomaly(source: str, line: str) -> bool:
    """Known plain INFO activity messages alone do not establish a fault."""
    if source == "zwave-01":
        message = _TIMESTAMP.sub("", line, count=1).strip()
        if re.fullmatch(
            r"INFO\s+(?:Z-WAVE-SERVER:\s+Client disconnected|"
            r"Z-WAVE:\s+Starting bulk firmware update check for all nodes|"
            r"BACKUP:\s+Backup store started)\.?",
            message,
            re.I,
        ):
            return False
    return bool(_ANOMALY.search(line))


def _parse_log_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP.search(line)
    if match is None:
        return None
    candidate = match.group("timestamp").replace(" ", "T").replace(",", ".")
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        value = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _category(source: str, line: str) -> str:
    lowered = line.lower()
    if source == "zwave-01" and any(
        term in lowered for term in ("node", "transmission", "interview", "routing")
    ):
        return "zwave"
    if source == "linky-01" and any(
        term in lowered for term in ("teleinfo", "serial", "frame", "checksum")
    ):
        return "serial"
    for category, terms in (
        ("zwave", ("z-wave", "zwave", "node dead", "interview", "routing")),
        ("serial", ("serial", "teleinfo", "frame", "checksum")),
        ("mqtt", ("mqtt",)),
        ("automation", ("automation",)),
        ("timeout", ("timeout", "timed out")),
        ("network", ("network", "disconnect", "reconnect", "unavailable")),
        (
            "restart",
            (
                "restart",
                "shutdown requested",
                "stopping",
                "stopped",
                "starting",
                "started",
            ),
        ),
        ("exception", ("exception", "traceback")),
    ):
        if any(term in lowered for term in terms):
            return category
    return "other"


def _signature(line: str) -> str:
    normalized = _TIMESTAMP.sub("<timestamp>", _safe_log_text(line)).lower()
    normalized = _VARIABLE.sub("<value>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized[:160] or "unclassified log anomaly"


def _references(line: str) -> list[str]:
    """Keep bounded Home Assistant entity IDs without exposing raw log lines."""
    return list(
        dict.fromkeys(
            match.casefold() for match in _ENTITY_ID.findall(_safe_log_text(line))
        )
    )[:16]


def _severity(line: str) -> str:
    lowered = line.lower()
    if "critical" in lowered or "fatal" in lowered:
        return "critical"
    if any(term in lowered for term in ("error", "exception", "failed", "dead")):
        return "error"
    return "warning"


class _DirectLogReader:
    def __init__(self, source_provider: LogSourceProvider) -> None:
        self.source_provider = source_provider

    def read(
        self,
        source: str,
        max_bytes: int,
        context: HandlerContext,
    ) -> tuple[list[str], int, bool]:
        if not context.job_id or not context.worker_id or context.attempt < 1:
            raise RuntimeError("log retrieval requires an owning job attempt")
        descriptor = self.source_provider(
            context.job_id, context.worker_id, context.attempt, source
        )
        if descriptor.get("source") != source:
            raise RuntimeError("Agent returned a mismatched log source")
        if descriptor.get("transport") == "inline":
            content = descriptor.get("content")
            if not isinstance(content, str):
                raise RuntimeError("Agent returned invalid inline log content")
            payload = content.encode("utf-8")
            truncated = bool(descriptor.get("truncated")) or len(payload) > max_bytes
            bounded = payload[-max_bytes:]
            return (
                bounded.decode("utf-8", errors="replace").splitlines(),
                len(bounded),
                truncated,
            )
        url = descriptor.get("url")
        token = descriptor.get("access_token")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise RuntimeError("Agent returned an invalid Home Assistant log URL")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Agent returned an invalid Home Assistant token")
        tls_context = None
        if url.startswith("https://") and descriptor.get("verify_tls", True) is False:
            tls_context = ssl._create_unverified_context()  # noqa: SLF001
        request = Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "text/plain"},
            method="GET",
        )
        timeout = float(descriptor.get("timeout_seconds", 30))
        base_url = descriptor.get("base_url")
        if isinstance(base_url, str):
            try:
                payload = self._read_supervisor(
                    base_url,
                    token,
                    descriptor.get("addon_name_patterns", []),
                    max_bytes,
                    min(max(timeout, 5), 60),
                    verify_tls=descriptor.get("verify_tls", True) is not False,
                )
                return (
                    payload.decode("utf-8", errors="replace").splitlines(),
                    len(payload),
                    len(payload) >= max_bytes,
                )
            except (OSError, RuntimeError, ValueError, WebSocketException) as error:
                # Keep the primary Supervisor/proxy failure visible when the
                # compatibility URL is used as a bounded fallback.
                supervisor_warning = (
                    "ERROR Katsuyu Supervisor log access unavailable: "
                    f"{type(error).__name__}: {error}; using Home Assistant "
                    "core log fallback\n"
                ).encode("utf-8", errors="replace")
            else:  # pragma: no cover - return above documents the successful branch.
                supervisor_warning = b""
        else:
            supervisor_warning = b""
        context.check()
        with urlopen(  # noqa: S310 - Agent supplies an allowlisted configured URL.
            request,
            timeout=min(max(timeout, 5), 60),
            context=tls_context,
        ) as response:
            payload = response.read(max_bytes + 1)
        context.check()
        truncated = len(supervisor_warning) + len(payload) > max_bytes
        bounded = (supervisor_warning + payload)[:max_bytes]
        return (
            bounded.decode("utf-8", errors="replace").splitlines(),
            len(bounded),
            truncated,
        )

    @staticmethod
    def _read_supervisor(
        base_url: str,
        token: str,
        addon_patterns: object,
        max_bytes: int,
        timeout: float,
        *,
        verify_tls: bool,
    ) -> bytes:
        normalized_base_url = base_url.rstrip("/")
        tls_context = None
        if normalized_base_url.startswith("https://") and not verify_tls:
            tls_context = ssl._create_unverified_context()  # noqa: SLF001

        def read_text(path: str, params: dict[str, object]) -> bytes:
            query = urlencode(params)
            request = Request(
                f"{normalized_base_url}/api/hassio/{path.lstrip('/')}?{query}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/plain",
                },
                method="GET",
            )
            with urlopen(  # noqa: S310 - Agent supplies an allowlisted HA URL.
                request,
                timeout=timeout,
                context=tls_context,
            ) as response:
                return response.read(max_bytes + 1)

        fragments = [
            read_text(
                "/core/logs/latest",
                {"lines": 10_000, "no_colors": 1},
            )
        ]
        patterns = (
            [str(value).casefold() for value in addon_patterns]
            if isinstance(addon_patterns, list)
            else []
        )
        if not patterns:
            return b"\n".join(fragments)[:max_bytes]

        websocket_url = re.sub(r"^http", "ws", normalized_base_url)
        ssl_options = {} if verify_tls else {"cert_reqs": ssl.CERT_NONE}
        connection = create_connection(
            f"{websocket_url}/api/websocket",
            timeout=timeout,
            sslopt=ssl_options,
        )
        try:
            challenge = json.loads(connection.recv())
            if challenge.get("type") != "auth_required":
                raise RuntimeError("unexpected Home Assistant WebSocket challenge")
            connection.send(json.dumps({"type": "auth", "access_token": token}))
            authenticated = json.loads(connection.recv())
            if authenticated.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant rejected Supervisor log access")

            connection.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": "supervisor/api",
                        "endpoint": "/addons",
                        "method": "get",
                        "timeout": timeout,
                    }
                )
            )
            response = json.loads(connection.recv())
            if not response.get("success"):
                error = response.get("error")
                raise RuntimeError(
                    f"Home Assistant Supervisor request failed: /addons ({error})"
                )
            addons_result = response.get("result")
            addons = (
                addons_result.get("addons", [])
                if isinstance(addons_result, dict)
                else []
            )
            for addon in addons:
                if not isinstance(addon, dict):
                    continue
                slug = str(addon.get("slug", ""))
                searchable = f"{slug} {addon.get('name', '')}".casefold()
                if slug and any(pattern in searchable for pattern in patterns):
                    fragments.append(
                        read_text(
                            f"/addons/{quote(slug, safe='')}/logs",
                            {"lines": 10_000, "no_colors": 1},
                        )
                    )
            return b"\n".join(fragments)[:max_bytes]
        finally:
            connection.close()


class LogsHealthCheckHandler:
    """Fetch each target directly and group deterministic anomalies."""

    def __init__(self, source_provider: LogSourceProvider) -> None:
        self.reader = _DirectLogReader(source_provider)

    def execute(
        self,
        parameters: dict[str, Any],
        context: HandlerContext | None = None,
    ) -> dict[str, Any]:
        request = LogsHealthCheckParameters.model_validate(parameters)
        runtime = context or HandlerContext()
        baseline: Counter[tuple[str, str]] = Counter()
        for entry in request.baseline:
            # Older workers grouped rotating session credentials separately.
            baseline[(entry.source, _safe_log_text(entry.signature))] += (
                entry.occurrences
            )
        results: list[LogSourceHealth] = []
        for index, source in enumerate(request.sources):
            runtime.report(
                5 + (index * 80 / len(request.sources)),
                "logs.fetching",
                source,
            )
            lines, fetched_bytes, truncated = self.reader.read(
                source, request.max_bytes_per_source, runtime
            )
            results.append(
                self._analyze_source(
                    source,
                    lines,
                    fetched_bytes,
                    truncated,
                    request.window_started_at,
                    request.window_ended_at,
                    baseline,
                )
            )
        findings = [finding for result in results for finding in result.findings]
        current_keys = {(finding.source, finding.signature) for finding in findings}
        disappeared = [
            LogBaseline(source=source, signature=signature, occurrences=occurrences)
            for (source, signature), occurrences in baseline.items()
            if (source, signature) not in current_keys
        ][:192]
        correlations: list[LogCorrelation] = []
        timed = [finding for finding in findings if finding.last_at is not None]
        for index, left in enumerate(timed):
            for right in timed[index + 1 :]:
                if left.source == right.source:
                    continue
                assert left.last_at is not None and right.last_at is not None
                if abs((left.last_at - right.last_at).total_seconds()) <= 10:
                    correlations.append(
                        LogCorrelation(
                            sources=sorted({left.source, right.source}),
                            occurred_at=max(left.last_at, right.last_at),
                            summary=(
                                "Events are temporally correlated; "
                                "no causality is inferred."
                            ),
                        )
                    )
                if len(correlations) >= 32:
                    break
            if len(correlations) >= 32:
                break
        result = LogsHealthCheckResult(
            status="KO" if findings else "OK",
            analyzed_at=datetime.now(UTC),
            window_started_at=request.window_started_at,
            window_ended_at=request.window_ended_at,
            sources=results,
            new_anomaly_count=sum(finding.trend == "new" for finding in findings),
            worsening_anomaly_count=sum(
                finding.trend == "increasing" for finding in findings
            ),
            disappeared_anomalies=disappeared,
            correlations=correlations,
            recommended_investigations=[
                f"Investigate {finding.source}: {finding.signature}"
                for finding in sorted(
                    findings,
                    key=lambda item: (item.severity, item.occurrences),
                    reverse=True,
                )[:16]
            ],
        )
        runtime.report(100, "logs.complete")
        return result.model_dump(mode="json")

    @staticmethod
    def _analyze_source(
        source: str,
        lines: list[str],
        fetched_bytes: int,
        truncated: bool,
        started_at: datetime,
        ended_at: datetime,
        baseline: dict[tuple[str, str], int],
    ) -> LogSourceHealth:
        grouped: Counter[str] = Counter()
        samples: dict[str, str] = {}
        times: dict[str, list[datetime]] = defaultdict(list)
        analyzed_lines = 0
        for line in lines[:200_000]:
            occurred_at = _parse_log_timestamp(line)
            if occurred_at is not None and not (started_at <= occurred_at <= ended_at):
                continue
            analyzed_lines += 1
            if not _is_log_anomaly(source, line):
                continue
            signature = _signature(line)
            grouped[signature] += 1
            samples.setdefault(signature, line.strip()[:500])
            if occurred_at is not None:
                times[signature].append(occurred_at)
        findings: list[LogFinding] = []
        for signature, occurrences in grouped.most_common(64):
            previous = baseline.get((source, signature))
            if previous is None:
                trend = "new"
            elif occurrences > max(previous + 2, int(previous * 1.5)):
                trend = "increasing"
            elif occurrences < int(previous * 0.5):
                trend = "decreasing"
            else:
                trend = "stable" if occurrences == previous else "known"
            observed = times.get(signature, [])
            sample = samples[signature]
            findings.append(
                LogFinding(
                    source=source,
                    signature=signature,
                    category=_category(source, sample),
                    severity=_severity(sample),
                    summary=f"{signature} ({occurrences} occurrence(s))",
                    references=_references(sample),
                    occurrences=occurrences,
                    reference_occurrences=previous,
                    first_at=min(observed) if observed else None,
                    last_at=max(observed) if observed else None,
                    trend=trend,
                )
            )
        return LogSourceHealth(
            source=source,
            status="KO" if findings else "OK",
            fetched_bytes=fetched_bytes,
            truncated=truncated,
            analyzed_lines=analyzed_lines,
            findings=findings,
        )


class LogsInvestigateHandler:
    """Return bounded context after Tsunade authorizes one plain pattern."""

    def __init__(self, source_provider: LogSourceProvider) -> None:
        self.reader = _DirectLogReader(source_provider)

    def execute(
        self,
        parameters: dict[str, Any],
        context: HandlerContext | None = None,
    ) -> dict[str, Any]:
        request = LogsInvestigateParameters.model_validate(parameters)
        runtime = context or HandlerContext()
        runtime.report(10, "logs.fetching", request.source)
        lines, _fetched_bytes, truncated = self.reader.read(
            request.source, request.max_bytes, runtime
        )
        eligible: list[str] = []
        for line in lines:
            occurred_at = _parse_log_timestamp(line)
            if occurred_at is None or (
                request.window_started_at <= occurred_at <= request.window_ended_at
            ):
                eligible.append(line)
        grouped: Counter[str] = Counter()
        samples: dict[str, str] = {}
        times: dict[str, list[datetime]] = defaultdict(list)
        matched_lines = 0
        needle = request.pattern.casefold()
        for line in eligible[:200_000]:
            if needle in line.casefold():
                matched_lines += 1
                if not _is_log_anomaly(request.source, line):
                    continue
                signature = _signature(line)
                grouped[signature] += 1
                samples.setdefault(signature, line)
                occurred_at = _parse_log_timestamp(line)
                if occurred_at is not None:
                    times[signature].append(occurred_at)
        findings: list[LogFinding] = []
        for signature, occurrences in grouped.most_common(64):
            observed = times.get(signature, [])
            sample = samples[signature]
            findings.append(
                LogFinding(
                    source=request.source,
                    signature=signature,
                    category=_category(request.source, sample),
                    severity=_severity(sample),
                    summary=f"{signature} ({occurrences} occurrence(s))",
                    references=_references(sample),
                    occurrences=occurrences,
                    first_at=min(observed) if observed else None,
                    last_at=max(observed) if observed else None,
                    trend="new",
                )
            )
        result = LogsInvestigateResult(
            status="KO" if findings else "OK",
            analyzed_at=datetime.now(UTC),
            source=request.source,
            pattern=request.pattern,
            matched_lines=matched_lines,
            findings=findings,
            truncated=truncated or len(eligible) > 200_000 or len(grouped) > 64,
        )
        runtime.report(100, "logs.complete")
        return result.model_dump(mode="json")


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
            _validate_infra_source_archive(source, request.backup_id)
            context.report(20, "infra.validate")
            compression = BackupCompressHandler(self.workspace).execute(
                {
                    "source": source_relative,
                    "destination": compressed_relative,
                    "compression_level": request.compression_level,
                },
                context,
            )
            encryption = BackupEncryptHandler(self.workspace, self.age_binary).execute(
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


def _validate_infra_source_archive(source: Path, backup_id: str) -> None:
    """Reject truncated or incomplete Agent source archives before encryption."""

    size = source.stat().st_size
    if size < 1024:
        raise ValueError(
            "Agent backup source is too small to be a complete tar archive"
        )
    with source.open("rb") as stream:
        stream.seek(-1024, os.SEEK_END)
        if stream.read(1024) != bytes(1024):
            raise ValueError("Agent backup source has no complete tar trailer")
    try:
        with tarfile.open(source, mode="r:") as archive:
            members = archive.getmembers()
            names = [member.name.rstrip("/") for member in members]
            if len(names) != len(set(names)):
                raise ValueError("Agent backup source contains duplicate members")
            required = INFRA_REQUIRED_MEMBERS | {INFRA_DESCRIPTOR}
            missing = sorted(required - set(names))
            if missing:
                raise ValueError(
                    "Agent backup source is incomplete; missing: " + ", ".join(missing)
                )
            for member in members:
                name = member.name.rstrip("/")
                allowed = (
                    name == INFRA_DESCRIPTOR
                    or name in INFRA_REQUIRED_MEMBERS
                    or any(
                        name.startswith(f"{root}/") for root in INFRA_DIRECTORY_ROOTS
                    )
                )
                if (
                    not allowed
                    or name in INFRA_FORBIDDEN_MEMBERS
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError(
                        f"Agent backup source member is not allowed: {name}"
                    )
            descriptor_member = archive.getmember(INFRA_DESCRIPTOR)
            descriptor_stream = archive.extractfile(descriptor_member)
            if descriptor_stream is None or descriptor_member.size > 65536:
                raise ValueError("Agent backup descriptor is invalid")
            descriptor = json.loads(descriptor_stream.read())
    except (OSError, tarfile.TarError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Agent backup source is not a valid tar archive: {error}"
        ) from error
    contents = descriptor.get("contents") if isinstance(descriptor, dict) else None
    if (
        not isinstance(descriptor, dict)
        or descriptor.get("schema_version") != 1
        or descriptor.get("profile") != "infra-01"
        or descriptor.get("backup_id") != backup_id
        or not isinstance(contents, list)
        or not all(isinstance(item, str) for item in contents)
        or len(contents) != len(set(contents))
        or set(contents) != INFRA_REQUIRED_MEMBERS
    ):
        raise ValueError("Agent backup descriptor does not match the requested backup")


def _temporary_path(destination: Path) -> Path:
    descriptor, value = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    return Path(value)

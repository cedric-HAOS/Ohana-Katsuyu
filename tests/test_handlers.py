"""Tests for Katsuyu's deterministic local handlers."""

from __future__ import annotations

import gzip
import hashlib
from io import StringIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ohana_katsuyu.handlers import (
    BackupCompressHandler,
    BackupEncryptHandler,
    BackupVerifyHandler,
    HandlerContext,
    JobCancelledError,
    KatsuyuWorkspace,
    SystemHealthHandler,
    SystemMetrics,
)


class FakeProbe:
    def collect(self) -> SystemMetrics:
        return SystemMetrics(
            platform="Windows 11",
            cpu_percent=12.5,
            memory_total_bytes=8 * 1024**3,
            memory_available_bytes=5 * 1024**3,
        )


def test_system_health_is_local_and_strict(tmp_path: Path) -> None:
    result = SystemHealthHandler(
        KatsuyuWorkspace(tmp_path),
        probe=FakeProbe(),  # type: ignore[arg-type]
        disk_usage=lambda _path: SimpleNamespace(total=1000, used=250, free=750),
    ).execute({})

    assert result["status"] == "OK"
    assert result["platform"] == "Windows 11"
    assert result["memory_available_bytes"] == 5 * 1024**3


def test_compress_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "incoming" / "backup.tar"
    source.parent.mkdir()
    source.write_bytes(b"ohana-backup" * 1000)
    handler = BackupCompressHandler(KatsuyuWorkspace(tmp_path))
    parameters = {
        "source": "incoming/backup.tar",
        "destination": "artifacts/backup.tar.gz",
        "compression_level": 6,
    }

    first = handler.execute(parameters)
    second = handler.execute(parameters)

    destination = tmp_path / "artifacts" / "backup.tar.gz"
    assert gzip.decompress(destination.read_bytes()) == source.read_bytes()
    assert first == second
    assert first["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_verify_reports_mismatch_as_a_structured_result(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.age"
    artifact.write_bytes(b"encrypted")
    result = BackupVerifyHandler(KatsuyuWorkspace(tmp_path)).execute(
        {
            "path": "artifact.age",
            "expected_sha256": "0" * 64,
            "expected_size": 10,
        }
    )

    assert result["valid"] is False
    assert result["sha256_matches"] is False
    assert result["size_matches"] is False


@pytest.mark.parametrize("value", ["../secret", "C:/Windows/secret", r"..\secret"])
def test_workspace_rejects_escape_paths(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="relative|escapes"):
        KatsuyuWorkspace(tmp_path).output_file(value)


def test_handler_stops_when_cancelled(tmp_path: Path) -> None:
    (tmp_path / "backup.tar").write_bytes(b"x" * 100)
    cancelled = Event()
    cancelled.set()
    with pytest.raises(JobCancelledError):
        BackupCompressHandler(KatsuyuWorkspace(tmp_path)).execute(
            {"source": "backup.tar", "destination": "backup.tar.gz"},
            HandlerContext(cancelled=cancelled),
        )


def test_encrypt_uses_only_the_configured_age_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "backup.tar.gz").write_bytes(b"compressed")
    calls: list[list[str]] = []

    class FakeProcess:
        returncode = 0
        stderr = StringIO("")

        def __init__(self, command: list[str], **_kwargs: object) -> None:
            calls.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(b"encrypted")

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: int) -> int:
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr("ohana_katsuyu.handlers.subprocess.Popen", FakeProcess)
    recipient = "age1" + "q" * 58
    result = BackupEncryptHandler(
        KatsuyuWorkspace(tmp_path), Path(r"C:\Tools\age.exe")
    ).execute(
        {
            "source": "backup.tar.gz",
            "destination": "backup.tar.gz.age",
            "recipient": recipient,
        }
    )

    assert calls[0][0] == r"C:\Tools\age.exe"
    assert result["recipient"] == recipient
    assert result["destination_sha256"] == hashlib.sha256(b"encrypted").hexdigest()

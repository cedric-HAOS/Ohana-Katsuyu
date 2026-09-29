"""Phase 5: Katsuyu tells the Agent which local runtimes are usable."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError

import pytest

from ohana_katsuyu.ai import AiInferenceHandler
from ohana_katsuyu.handlers import (
    BackupEncryptHandler,
    HandlerContext,
    KatsuyuWorkspace,
)
from ohana_katsuyu.models import JobClaimResult, WorkerDocument, WorkerRuntime
from ohana_katsuyu.runtimes import executable_runtime
from ohana_katsuyu.worker import AgentClient, KatsuyuWorker


class RuntimeClient:
    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.reports: list[dict[str, Any]] = []

    def register(self, payload: dict[str, Any]) -> WorkerDocument:
        now = datetime.now(UTC)
        return WorkerDocument(**payload, registered_at=now, last_seen_at=now)

    def report_runtimes(self, payload: dict[str, Any]) -> bool:
        self.reports.append(payload)
        return self.accepts

    def claim(self, _payload: dict[str, Any]) -> JobClaimResult:
        return JobClaimResult(job=None)


class SwitchableRuntime:
    def __init__(self) -> None:
        self.state = WorkerRuntime(state="ready")

    def execute(
        self, _parameters: dict[str, Any], _context: HandlerContext | None = None
    ) -> dict[str, Any]:
        return {}

    def runtime_status(self) -> WorkerRuntime:
        return self.state


class NoRuntime:
    def execute(
        self, _parameters: dict[str, Any], _context: HandlerContext | None = None
    ) -> dict[str, Any]:
        return {}


def _worker(client: RuntimeClient, runtime: SwitchableRuntime) -> KatsuyuWorker:
    return KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={"ai.inference": runtime, "system.health": NoRuntime()},
        runtime_refresh_seconds=0,
    )


def test_registration_reports_only_capabilities_with_a_runtime() -> None:
    client = RuntimeClient()
    _worker(client, SwitchableRuntime()).register()

    assert client.reports == [
        {
            "protocol_version": 1,
            "worker_id": "katsuyu-bubule",
            "runtimes": {"ai.inference": {"state": "ready", "detail": ""}},
        }
    ]


def test_runtime_is_reported_again_only_when_it_changes() -> None:
    client = RuntimeClient()
    runtime = SwitchableRuntime()
    worker = _worker(client, runtime)
    worker.register()

    worker.run_once()
    runtime.state = WorkerRuntime(state="missing", detail="modèle IA absent")
    worker.run_once()
    worker.run_once()

    assert [r["runtimes"]["ai.inference"]["state"] for r in client.reports] == [
        "ready",
        "missing",
    ]


def test_idle_checks_wait_for_the_refresh_interval() -> None:
    client = RuntimeClient()
    runtime = SwitchableRuntime()
    worker = _worker(client, runtime)
    worker.runtime_refresh_seconds = 3600
    worker.register()

    runtime.state = WorkerRuntime(state="missing")
    worker.run_once()

    assert len(client.reports) == 1


def test_an_older_agent_is_asked_again_only_hourly(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("ohana_katsuyu.worker.monotonic", lambda: clock[0])
    client = RuntimeClient(accepts=False)
    runtime = SwitchableRuntime()
    worker = _worker(client, runtime)
    worker.register()
    worker.run_once()
    clock[0] += 3599
    worker.run_once()
    assert len(client.reports) == 1

    # The Agent was updated meanwhile: accepted without restarting Katsuyu.
    client.accepts = True
    clock[0] += 2
    worker.run_once()
    runtime.state = WorkerRuntime(state="missing")
    worker.run_once()
    assert [r["runtimes"]["ai.inference"]["state"] for r in client.reports] == [
        "ready",
        "ready",
        "missing",
    ]


@pytest.mark.parametrize("status", [401, 404])
def test_client_maps_an_unknown_endpoint_to_false(monkeypatch, status) -> None:
    def post(_self, path, _payload):
        raise RuntimeError("unknown") from HTTPError(path, status, "test", {}, None)

    monkeypatch.setattr(AgentClient, "_post", post)
    assert AgentClient("http://localhost", "test-only").report_runtimes({}) is False


def test_client_keeps_other_errors(monkeypatch) -> None:
    def post(_self, path, _payload):
        raise RuntimeError("refused") from HTTPError(path, 500, "test", {}, None)

    monkeypatch.setattr(AgentClient, "_post", post)
    with pytest.raises(RuntimeError):
        AgentClient("http://localhost", "test-only").report_runtimes({})


def test_executable_runtime_checks_configured_paths(tmp_path: Path) -> None:
    binary = tmp_path / "age.exe"
    assert executable_runtime(binary, "age").state == "missing"
    binary.write_bytes(b"")
    assert executable_runtime(binary, "age").state == "ready"
    assert executable_runtime(Path("no-such-age-binary.exe"), "age").state == (
        "missing"
    )


def test_backup_encrypt_reports_age(tmp_path: Path) -> None:
    handler = BackupEncryptHandler(KatsuyuWorkspace(tmp_path), tmp_path / "age.exe")
    assert handler.runtime_status().state == "missing"


def test_ai_runtime_states_follow_files_and_verification(tmp_path: Path) -> None:
    runtime = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    content = b"model"
    handler = AiInferenceHandler(
        runtime=runtime,
        model=model,
        model_id="ministral",
        model_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert handler.runtime_status().detail == "moteur llama-server absent"
    runtime.write_bytes(b"")
    assert handler.runtime_status().detail == "modèle IA absent"
    model.write_bytes(content)
    assert handler.runtime_status().state == "unverified"

    handler._verify_payload(HandlerContext())
    assert handler.runtime_status().state == "ready"


def test_ai_runtime_reports_a_bad_model_digest(tmp_path: Path) -> None:
    runtime = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    runtime.write_bytes(b"")
    model.write_bytes(b"corrupted")
    handler = AiInferenceHandler(
        runtime=runtime,
        model=model,
        model_id="ministral",
        model_sha256="0" * 64,
    )

    with pytest.raises(RuntimeError, match="SHA-256"):
        handler._verify_payload(HandlerContext())

    status = handler.runtime_status()
    assert status.state == "failed"
    assert status.detail == "empreinte SHA-256 du modèle invalide"


class StrictOlderAgent(RuntimeClient):
    """Agent 1.40: the runtimes route exists but rejects the host section."""

    def report_runtimes(self, payload: dict[str, Any]) -> bool:
        if "host" in payload:
            raise RuntimeError("invalid") from HTTPError("r", 422, "test", {}, None)
        return super().report_runtimes(payload)


def test_workspace_detail_is_reported_with_the_runtimes(tmp_path: Path) -> None:
    (tmp_path / "job").mkdir()
    (tmp_path / "job" / "part").write_bytes(b"x" * 10)
    client = RuntimeClient()
    worker = _worker(client, SwitchableRuntime())
    worker.workspace_root = tmp_path
    worker.register()

    workspace = client.reports[0]["host"]["workspace"]
    assert workspace["path"] == str(tmp_path)
    # Rounded to 100 MiB so that every written byte is not a new report.
    assert workspace["used_bytes"] == 0
    assert workspace["free_bytes"] % (100 * 1024 * 1024) == 0


def test_an_agent_rejecting_the_host_detail_still_gets_the_runtimes(
    tmp_path: Path,
) -> None:
    client = StrictOlderAgent()
    runtime = SwitchableRuntime()
    worker = _worker(client, runtime)
    worker.workspace_root = tmp_path
    worker.register()
    runtime.state = WorkerRuntime(state="missing")
    worker.run_once()

    assert [sorted(report) for report in client.reports] == [
        ["protocol_version", "runtimes", "worker_id"],
        ["protocol_version", "runtimes", "worker_id"],
    ]


def test_ai_host_detail_never_starts_the_model(tmp_path: Path) -> None:
    runtime = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf" * 8)
    handler = AiInferenceHandler(
        runtime=runtime,
        model=model,
        model_id="ministral-3-14b",
        model_sha256=hashlib.sha256(b"gguf" * 8).hexdigest(),
    )

    detail = handler.host_detail()
    assert detail.model == "ministral-3-14b"
    assert detail.model_bytes == 32
    assert detail.model_verified is False
    assert detail.runtime is None
    assert detail.last_inference_at is None

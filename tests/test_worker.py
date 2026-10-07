"""Tests for Katsuyu's authenticated worker loop."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread
from time import sleep
from typing import Any, cast
from urllib.error import HTTPError
from uuid import uuid4

import pytest

from ohana_katsuyu.handlers import HandlerContext
from ohana_katsuyu.models import (
    JobClaimResult,
    JobDocument,
    JobStatus,
    WorkerDocument,
)
from ohana_katsuyu.power import ShutdownVeto
from ohana_katsuyu.status import StatusStore
from ohana_katsuyu.worker import (
    AgentClient,
    KatsuyuWorker,
    apply_configuration,
    build_parser,
)


def test_agent_client_sends_previous_worker_identity_only_for_migration(
    monkeypatch: Any,
) -> None:
    requests: list[Any] = []
    now = datetime.now(UTC).isoformat()

    class Response(BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_urlopen(request: Any, **_kwargs: object) -> Response:
        requests.append(request)
        body = json.dumps(
            {
                "protocol_version": 1,
                "worker_id": "katsuyu-bubule",
                "capabilities": ["system.health"],
                "platform": "Windows 11",
                "worker_version": "0.6.4",
                "registered_at": now,
                "last_seen_at": now,
                "availability": "AVAILABLE",
            }
        ).encode()
        return Response(body)

    monkeypatch.setattr("ohana_katsuyu.worker.urlopen", fake_urlopen)
    client = AgentClient("http://infra-01.ohana.lan:8766", "worker-secret")
    client.register(
        {
            "worker_id": "katsuyu-bubule",
            "capabilities": ["system.health"],
            "platform": "Windows 11",
            "worker_version": "0.6.4",
        },
        previous_worker_id="katsuyu-Bubule",
    )

    assert requests[0].get_header("X-ohana-previous-worker-id") == "katsuyu-Bubule"


@pytest.mark.parametrize(
    ("source_id", "configured_timeout", "expected_timeout"),
    [("infra-01", 10.0, 45.0), ("infra-01", 60.0, 60.0), ("ha-01", 10.0, 10.0)],
)
def test_infra_log_read_allows_agent_journal_deadline(
    monkeypatch: Any,
    source_id: str,
    configured_timeout: float,
    expected_timeout: float,
) -> None:
    def fake_urlopen(request: Any, *, timeout: float, context: Any) -> BytesIO:
        assert timeout == expected_timeout
        assert request.get_header("X-ohana-worker-id") == "worker-1"
        assert request.get_header("X-ohana-attempt") == "2"
        if source_id == "infra-01" and timeout <= 30:
            raise TimeoutError("Agent journal read still running")
        return BytesIO(b'{"transport":"inline","content":"journal"}')

    monkeypatch.setattr("ohana_katsuyu.worker.urlopen", fake_urlopen)
    client = AgentClient("http://infra-01:8766", "secret", configured_timeout)
    assert client.read_log_source("job-1", "worker-1", 2, source_id) == {
        "transport": "inline",
        "content": "journal",
    }


def test_worker_loads_bounded_setup_configuration(tmp_path) -> None:
    paths = {
        "token_file": tmp_path / "katsuyu.token",
        "ca_file": tmp_path / "agent-ca.pem",
        "workspace": tmp_path / "workspace",
        "log_file": tmp_path / "logs" / "katsuyu.log",
        "status_file": tmp_path / "status.json",
        "age_binary": tmp_path / "age.exe",
    }
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "base_url": "https://infra-01.ohana.lan:8766",
                "worker_id": "katsuyu-Bubule",
                **{name: str(path) for name, path in paths.items()},
            }
        ),
        encoding="utf-8",
    )
    arguments = build_parser().parse_args(["--config-file", str(config)])

    apply_configuration(arguments)

    assert arguments.base_url == "https://infra-01.ohana.lan:8766"
    assert arguments.worker_id == "katsuyu-bubule"
    assert arguments.previous_worker_id == "katsuyu-Bubule"
    for name, path in paths.items():
        assert getattr(arguments, name) == path


def job_document() -> JobDocument:
    now = datetime.now(UTC)
    return JobDocument(
        job_id=uuid4(),
        type="system.health",
        created_at=now,
        parameters={},
        timeout=60,
        status=JobStatus.RUNNING,
        started_at=now,
        worker_id="katsuyu-bubule",
        attempt=1,
        lease_expires_at=now,
    )


class FakeClient:
    def __init__(self, job: JobDocument | None) -> None:
        self.job = job
        self.registrations: list[dict[str, Any]] = []
        self.claims: list[dict[str, Any]] = []
        self.heartbeats: list[tuple[str, dict[str, Any]]] = []
        self.completions: list[tuple[str, dict[str, Any]]] = []
        self.power_reports: list[dict[str, Any]] = []

    def report_power(self, payload: dict[str, Any]) -> bool:
        self.power_reports.append(payload)
        return True

    def register(self, payload: dict[str, Any]) -> WorkerDocument:
        self.registrations.append(payload)
        now = datetime.now(UTC)
        return WorkerDocument(**payload, registered_at=now, last_seen_at=now)

    def claim(self, payload: dict[str, Any]) -> JobClaimResult:
        self.claims.append(payload)
        return JobClaimResult(job=self.job)

    def heartbeat(self, job_id: str, payload: dict[str, Any]) -> JobDocument:
        self.heartbeats.append((job_id, payload))
        return cast(JobDocument, self.job)

    def complete(self, job_id: str, payload: dict[str, Any]) -> JobDocument:
        self.completions.append((job_id, payload))
        return cast(JobDocument, self.job)


class SuccessHandler:
    def execute(
        self, _parameters: dict[str, Any], context: HandlerContext | None = None
    ) -> dict[str, Any]:
        assert context is not None
        context.report(100, "system.complete")
        return {"status": "OK"}


@pytest.mark.parametrize("status", [404, 401])
def test_polling_legacy_fallback_is_only_used_for_missing_endpoint(monkeypatch, status):
    calls = []

    def post(_self, path, _payload):
        calls.append(path)
        if path == "/v1/jobs/next":
            raise RuntimeError("endpoint response") from HTTPError(
                path, status, "test", {}, None
            )
        return {"job": None}

    monkeypatch.setattr(AgentClient, "_post", post)
    client = AgentClient("http://localhost", "test-only")
    if status == 404:
        result = client.claim({})
        assert not result.shutdown_requested
        assert calls == ["/v1/jobs/next", "/v1/jobs/claim"]
    else:
        with pytest.raises(RuntimeError):
            client.claim({})
        assert calls == ["/v1/jobs/next"]


def test_worker_registers_and_claims_only_its_allowlist() -> None:
    client = FakeClient(job_document())
    worker = KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SuccessHandler()},
    )

    worker.register()
    assert worker.run_once() is True

    assert client.registrations[0]["capabilities"] == ["system.health"]
    assert client.claims[0]["supported_types"] == ["system.health"]
    assert client.heartbeats[-1][1]["progress"]["percent"] == 100
    assert client.completions[-1][1]["status"] == "SUCCEEDED"


class OlderAgentClient(FakeClient):
    """Agent 1.38: rejects a registration naming a job type it does not know."""

    def register(self, payload: dict[str, Any]) -> WorkerDocument:
        unknown = [c for c in payload["capabilities"] if c == "trends.history_backfill"]
        if unknown:
            self.registrations.append(payload)
            raise RuntimeError(
                "Agent rejected the worker request with HTTP 400: "
                '{"error": "unsupported worker capabilities: '
                + ", ".join(unknown)
                + '"}'
            )
        return super().register(payload)


def test_worker_registers_known_types_with_an_older_agent() -> None:
    client = OlderAgentClient(None)
    worker = KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={
            "system.health": SuccessHandler(),
            "trends.history_backfill": SuccessHandler(),
        },
    )

    worker.register()

    assert [r["capabilities"] for r in client.registrations] == [
        ["system.health", "trends.history_backfill"],
        ["system.health"],
    ]


def test_other_registration_errors_are_not_hidden() -> None:
    class Refusing(FakeClient):
        def register(self, payload: dict[str, Any]) -> WorkerDocument:
            raise RuntimeError("Agent rejected the worker request with HTTP 401")

    worker = KatsuyuWorker(
        client=cast(AgentClient, Refusing(None)),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SuccessHandler()},
    )

    with pytest.raises(RuntimeError, match="401"):
        worker.register()


def test_worker_ignores_shutdown_granted_before_completion() -> None:
    calls: list[str] = []
    client = FakeClient(
        job_document().model_copy(update={"shutdown_after_completion": True})
    )
    worker = KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SuccessHandler()},
        shutdown_requester=lambda: calls.append("shutdown"),
    )

    assert worker.run_once() is True

    assert client.completions[-1][1]["status"] == "SUCCEEDED"
    assert calls == []


def test_worker_shuts_down_only_after_agent_settles_the_queue() -> None:
    calls: list[str] = []

    class SettledClient(FakeClient):
        def claim(self, payload: dict[str, Any]) -> JobClaimResult:
            return JobClaimResult(shutdown_requested=True)

    worker = KatsuyuWorker(
        client=cast(AgentClient, SettledClient(None)),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SuccessHandler()},
        shutdown_requester=lambda: calls.append("shutdown"),
    )
    assert worker.run_once() is False
    assert worker.shutdown_requested
    assert calls == ["shutdown"]


def test_worker_reports_the_shutdown_it_starts() -> None:
    class SettledClient(FakeClient):
        def claim(self, payload: dict[str, Any]) -> JobClaimResult:
            return JobClaimResult(shutdown_requested=True)

    client = SettledClient(None)
    worker = KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SuccessHandler()},
        shutdown_requester=lambda: None,
        shutdown_veto=lambda: None,
    )

    worker.run_once()

    assert client.power_reports == [
        {
            "protocol_version": 1,
            "worker_id": "katsuyu-bubule",
            "outcome": "shutdown_started",
        }
    ]


def test_worker_keeps_the_pc_on_when_someone_is_signed_in() -> None:
    calls: list[str] = []

    class SettledClient(FakeClient):
        def claim(self, payload: dict[str, Any]) -> JobClaimResult:
            return JobClaimResult(shutdown_requested=True)

    client = SettledClient(None)
    worker = KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SuccessHandler()},
        shutdown_requester=lambda: calls.append("shutdown"),
        shutdown_veto=lambda: ShutdownVeto("interactive_session", 1),
    )

    assert worker.run_once() is False

    assert calls == []
    # The loop goes on: the worker stays available and keeps polling.
    assert not worker.shutdown_requested
    assert client.power_reports == [
        {
            "protocol_version": 1,
            "worker_id": "katsuyu-bubule",
            "outcome": "shutdown_vetoed",
            "reason": "interactive_session",
            "sessions": 1,
        }
    ]


def test_a_lost_report_never_blocks_the_shutdown() -> None:
    calls: list[str] = []

    class SettledClient(FakeClient):
        def claim(self, payload: dict[str, Any]) -> JobClaimResult:
            return JobClaimResult(shutdown_requested=True)

        def report_power(self, payload: dict[str, Any]) -> bool:
            raise RuntimeError("Agent unreachable")

    worker = KatsuyuWorker(
        client=cast(AgentClient, SettledClient(None)),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SuccessHandler()},
        shutdown_requester=lambda: calls.append("shutdown"),
    )

    worker.run_once()

    assert calls == ["shutdown"]


@pytest.mark.parametrize("status", [401, 404])
def test_power_report_is_ignored_by_an_older_agent(monkeypatch, status):
    def post(_self, _path, _payload):
        raise RuntimeError("rejected") from HTTPError("u", status, "x", {}, None)

    monkeypatch.setattr(AgentClient, "_post", post)
    client = AgentClient(base_url="http://agent", token="t")

    assert client.report_power({"worker_id": "w"}) is False


def test_worker_keeps_local_status_fresh_when_no_job_is_available(
    tmp_path: Path,
) -> None:
    client = FakeClient(None)
    status_store = StatusStore(tmp_path / "status.json")
    worker = KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SuccessHandler()},
        status_store=status_store,
    )

    first = worker.run_once()
    first_status = status_store.read()
    second = worker.run_once()
    second_status = status_store.read()

    assert first is False
    assert second is False
    assert first_status.state == "connected"
    assert second_status.state == "connected"
    assert first_status.last_connection_at is not None
    assert second_status.last_connection_at is not None
    assert second_status.updated_at >= first_status.updated_at
    assert client.completions == []


def test_agent_client_streams_backup_input_and_exact_artifact_length(
    tmp_path: Path,
) -> None:
    source = b"uncompressed source tar"
    artifact = b"encrypted artifact"
    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            assert self.headers["Authorization"] == "Bearer worker-secret"
            assert self.headers["X-Ohana-Worker-Id"] == "bubule"
            self.send_response(200)
            self.send_header("Content-Length", str(len(source)))
            self.end_headers()
            self.wfile.write(source)

        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers["Content-Length"])
            received.append(self.rfile.read(size))
            body = json.dumps(
                {
                    "remote_path": "icloud:Ohana/Backups/infra-01/test",
                    "sha256": hashlib.sha256(artifact).hexdigest(),
                    "size_bytes": len(artifact),
                    "deleted_remote_backups": 0,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = AgentClient(
        f"http://127.0.0.1:{server.server_address[1]}", "worker-secret"
    )
    destination = tmp_path / "source.tar"
    encrypted = tmp_path / "artifact.age"
    encrypted.write_bytes(artifact)
    context = HandlerContext()
    try:
        sha256, size = client.download_job_input(
            "job-1", "bubule", 1, destination, context
        )
        receipt = client.upload_job_artifact(
            "job-1",
            "bubule",
            1,
            encrypted,
            hashlib.sha256(artifact).hexdigest(),
            context,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert destination.read_bytes() == source
    assert sha256 == hashlib.sha256(source).hexdigest()
    assert size == len(source)
    assert received == [artifact]
    assert receipt["size_bytes"] == len(artifact)


def test_agent_client_exposes_backup_source_error_detail_in_french(
    tmp_path: Path,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(
                {
                    "detail": (
                        "Distributed backup source preparation failed: "
                        "Impossible de créer le snapshot compact de Vision : "
                        "database is locked"
                    )
                }
            ).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = AgentClient(
        f"http://127.0.0.1:{server.server_address[1]}", "worker-secret"
    )
    try:
        with pytest.raises(
            RuntimeError,
            match=(
                "Agent a refusé la source de sauvegarde.*"
                "Impossible de créer le snapshot compact de Vision.*"
                "database is locked"
            ),
        ):
            client.download_job_input(
                "job-1",
                "bubule",
                1,
                tmp_path / "source.tar",
                HandlerContext(),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_worker_keeps_running_status_fresh_until_agent_cancellation(
    tmp_path: Path,
) -> None:
    published = []

    class RecordingStatusStore(StatusStore):
        def write(self, *, state: str, **changes: object):
            status = super().write(state=state, **changes)
            published.append(status)
            return status

    class SlowHandler:
        def execute(
            self, _parameters: dict[str, Any], context: HandlerContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            while True:
                context.check()
                sleep(0.001)

    class CancellingClient(FakeClient):
        def heartbeat(self, job_id: str, payload: dict[str, Any]) -> JobDocument:
            super().heartbeat(job_id, payload)
            assert self.job is not None
            if len(self.heartbeats) == 1:
                return self.job
            return self.job.model_copy(update={"status": JobStatus.CANCELLED})

    client = CancellingClient(job_document())
    status_store = RecordingStatusStore(tmp_path / "status.json")
    worker = KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SlowHandler()},
        heartbeat_seconds=0.01,
        status_store=status_store,
    )

    assert worker.run_once() is True
    assert len(client.heartbeats) == 2
    assert client.completions == []
    running = [status for status in published if status.state == "running"]
    assert len(running) == 2
    assert running[1].updated_at >= running[0].updated_at
    assert running[1].current_job_type == "system.health"
    status = status_store.read()
    assert status.state == "connected"
    assert status.current_job_id is None
    assert status.current_job_type is None

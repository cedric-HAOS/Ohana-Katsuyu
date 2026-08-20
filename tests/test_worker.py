"""Tests for Katsuyu's authenticated worker loop."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import sleep
from typing import Any, cast
from uuid import uuid4

from ohana_katsuyu.handlers import HandlerContext
from ohana_katsuyu.models import (
    JobClaimResult,
    JobDocument,
    JobStatus,
    WorkerDocument,
)
from ohana_katsuyu.status import StatusStore
from ohana_katsuyu.worker import (
    AgentClient,
    KatsuyuWorker,
    apply_configuration,
    build_parser,
)


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
    assert arguments.worker_id == "katsuyu-Bubule"
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


def test_worker_stops_after_agent_cancellation() -> None:
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
            return self.job.model_copy(update={"status": JobStatus.CANCELLED})

    client = CancellingClient(job_document())
    worker = KatsuyuWorker(
        client=cast(AgentClient, client),
        worker_id="katsuyu-bubule",
        handlers={"system.health": SlowHandler()},
        heartbeat_seconds=0.01,
    )

    assert worker.run_once() is True
    assert client.heartbeats
    assert client.completions == []

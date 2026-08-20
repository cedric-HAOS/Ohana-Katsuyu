"""Authenticated Katsuyu worker loop for Ohana-Agent protocol v1."""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import socket
import ssl
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ohana_katsuyu import __version__
from ohana_katsuyu.handlers import (
    BackupCompressHandler,
    BackupEncryptHandler,
    BackupVerifyHandler,
    HandlerContext,
    JobCancelledError,
    JobTimeoutError,
    KatsuyuWorkspace,
    SystemHealthHandler,
)
from ohana_katsuyu.models import (
    JobClaim,
    JobClaimResult,
    JobCompletion,
    JobDocument,
    JobError,
    JobHeartbeat,
    JobProgress,
    JobStatus,
    WorkerDocument,
    WorkerRegistration,
)
from ohana_katsuyu.status import StatusStore
from ohana_katsuyu.updates import refresh_update_status

LOGGER = logging.getLogger(__name__)


class KatsuyuHandler(Protocol):
    def execute(
        self, parameters: dict[str, Any], context: HandlerContext | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AgentClient:
    """Use Agent's existing worker endpoints and bearer authentication."""

    base_url: str
    token: str
    timeout_seconds: float = 10.0
    ca_certificate_file: Path | None = None

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("base_url cannot be empty")
        if not self.token.strip():
            raise ValueError("worker token cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

    def register(self, payload: dict[str, Any]) -> WorkerDocument:
        return WorkerDocument.model_validate(
            self._post("/v1/jobs/workers/register", payload)
        )

    def claim(self, payload: dict[str, Any]) -> JobClaimResult:
        return JobClaimResult.model_validate(self._post("/v1/jobs/claim", payload))

    def heartbeat(self, job_id: str, payload: dict[str, Any]) -> JobDocument:
        path = f"/v1/jobs/{quote(job_id, safe='')}/heartbeat"
        return JobDocument.model_validate(self._post(path, payload))

    def complete(self, job_id: str, payload: dict[str, Any]) -> JobDocument:
        path = f"/v1/jobs/{quote(job_id, safe='')}/complete"
        return JobDocument.model_validate(self._post(path, payload))

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            url=f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            context = None
            if self.base_url.lower().startswith("https://"):
                if self.ca_certificate_file is None:
                    raise RuntimeError("Katsuyu HTTPS CA certificate is missing")
                context = ssl.create_default_context(cafile=self.ca_certificate_file)
            with urlopen(
                request,
                timeout=self.timeout_seconds,
                context=context,
            ) as response:
                body = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(
                f"Agent rejected the worker request with HTTP {error.code}: {detail}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError(
                f"Agent worker endpoint is unavailable: {error}"
            ) from error
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Agent returned an invalid worker response") from error
        if not isinstance(value, dict):
            raise RuntimeError("Agent returned a non-object worker response")
        return value


@dataclass(slots=True)
class KatsuyuWorker:
    client: AgentClient
    worker_id: str
    handlers: dict[str, KatsuyuHandler]
    heartbeat_seconds: float = 5.0
    status_store: StatusStore | None = None

    def register(self) -> WorkerDocument:
        request = WorkerRegistration(
            worker_id=self.worker_id,
            capabilities=sorted(self.handlers),
            platform=f"{platform.system()} {platform.release()}".strip(),
            worker_version=__version__,
        )
        document = self.client.register(request.model_dump(mode="json"))
        if self.status_store is not None:
            self.status_store.write(
                state="connected",
                last_connection_at=datetime.now(UTC).isoformat(),
                current_job_id=None,
                current_job_type=None,
                error=None,
            )
        return document

    def run_once(self) -> bool:
        claim = JobClaim(
            worker_id=self.worker_id,
            supported_types=sorted(self.handlers),
        )
        job = self.client.claim(claim.model_dump(mode="json")).job
        if job is None:
            return False
        if self.status_store is not None:
            self.status_store.write(
                state="running",
                last_connection_at=datetime.now(UTC).isoformat(),
                current_job_id=str(job.job_id),
                current_job_type=job.type,
                error=None,
            )
        handler = self.handlers.get(job.type)
        if handler is None:
            raise RuntimeError(f"Agent leased unsupported job type {job.type}")

        progress_lock = Lock()
        progress = JobProgress(percent=0, stage="job.starting")

        def update_progress(value: JobProgress) -> None:
            nonlocal progress
            with progress_lock:
                progress = value

        remaining_seconds = (
            job.created_at.timestamp() + job.timeout - datetime.now(UTC).timestamp()
        )
        context = HandlerContext(
            deadline=monotonic() + max(0, remaining_seconds),
            progress_callback=update_progress,
        )
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="katsuyu-job")
        future: Future[dict[str, Any]] = executor.submit(
            handler.execute, job.parameters, context
        )
        try:
            result = self._await_job(
                job, future, context, progress_lock, lambda: progress
            )
            if result is None:
                self._publish_connected_status()
                return True
            completion = JobCompletion(
                worker_id=self.worker_id,
                attempt=job.attempt,
                status=JobStatus.SUCCEEDED,
                result=result,
            )
        except (JobCancelledError, JobTimeoutError):
            self._publish_connected_status()
            return True
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("Katsuyu job %s failed", job.job_id)
            completion = JobCompletion(
                worker_id=self.worker_id,
                attempt=job.attempt,
                status=JobStatus.FAILED,
                error=JobError(
                    code="handler.failed",
                    message=(str(error).strip() or error.__class__.__name__)[:1000],
                    retryable=False,
                ),
            )
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        current = self.client.heartbeat(
            str(job.job_id),
            JobHeartbeat(
                worker_id=self.worker_id,
                attempt=job.attempt,
                progress=JobProgress(percent=100, stage="job.complete"),
            ).model_dump(mode="json"),
        )
        if current.status in {JobStatus.CANCELLED, JobStatus.TIMEOUT}:
            self._publish_connected_status()
            return True
        self.client.complete(str(job.job_id), completion.model_dump(mode="json"))
        self._publish_connected_status()
        return True

    def _publish_connected_status(self) -> None:
        if self.status_store is not None:
            self.status_store.write(
                state="connected",
                last_connection_at=datetime.now(UTC).isoformat(),
                current_job_id=None,
                current_job_type=None,
                error=None,
            )

    def _await_job(
        self,
        job: JobDocument,
        future: Future[dict[str, Any]],
        context: HandlerContext,
        progress_lock: Lock,
        read_progress: Callable[[], JobProgress],
    ) -> dict[str, Any] | None:
        while True:
            try:
                return future.result(timeout=self.heartbeat_seconds)
            except FutureTimeout:
                with progress_lock:
                    progress = read_progress()
                try:
                    current = self.client.heartbeat(
                        str(job.job_id),
                        JobHeartbeat(
                            worker_id=self.worker_id,
                            attempt=job.attempt,
                            progress=progress,
                        ).model_dump(mode="json"),
                    )
                except RuntimeError:
                    context.cancelled.set()
                    self._settle_cancelled_future(future)
                    raise
                if current.status in {JobStatus.CANCELLED, JobStatus.TIMEOUT}:
                    context.cancelled.set()
                    self._settle_cancelled_future(future)
                    return None

    @staticmethod
    def _settle_cancelled_future(future: Future[dict[str, Any]]) -> None:
        try:
            future.result(timeout=5)
        except (FutureTimeout, JobCancelledError, JobTimeoutError):
            pass


def _default_root() -> Path:
    return Path(os.environ.get("PROGRAMDATA", "/var/lib")) / "Ohana" / "Katsuyu"


def build_parser() -> argparse.ArgumentParser:
    root = _default_root()
    parser = argparse.ArgumentParser(description="Ohana Katsuyu worker")
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--token-file", type=Path, default=root / "katsuyu.token")
    parser.add_argument("--workspace", type=Path, default=root / "workspace")
    parser.add_argument("--log-file", type=Path, default=root / "katsuyu.log")
    parser.add_argument("--status-file", type=Path, default=root / "status.json")
    parser.add_argument("--age-binary", type=Path, default=Path("age.exe"))
    parser.add_argument("--worker-id", default=socket.gethostname())
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    return parser


def apply_configuration(arguments: argparse.Namespace) -> None:
    """Load the bounded worker settings written by KatsuyuSetup."""
    if arguments.config_file is None:
        return
    try:
        document = json.loads(arguments.config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to read worker configuration: {error}") from error
    if not isinstance(document, dict):
        raise SystemExit("Worker configuration must be a JSON object")
    string_fields = ("base_url", "worker_id")
    path_fields = (
        "token_file",
        "ca_file",
        "workspace",
        "log_file",
        "status_file",
        "age_binary",
    )
    for field in string_fields:
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"Worker configuration field {field} is missing")
        setattr(arguments, field, value.strip())
    for field in path_fields:
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"Worker configuration field {field} is missing")
        setattr(arguments, field, Path(value))


def main() -> None:
    arguments = build_parser().parse_args()
    apply_configuration(arguments)
    if arguments.poll_seconds <= 0 or arguments.heartbeat_seconds <= 0:
        raise SystemExit("poll and heartbeat delays must be greater than zero")
    if arguments.base_url is None:
        raise SystemExit("Katsuyu Agent URL is missing")
    if not arguments.base_url.lower().startswith("https://"):
        raise SystemExit("Katsuyu requires an HTTPS Agent URL")
    if arguments.ca_file is None or not arguments.ca_file.is_file():
        raise SystemExit("Katsuyu HTTPS CA certificate is missing")
    try:
        token = arguments.token_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SystemExit(f"Unable to read worker token: {error}") from error
    if not token:
        raise SystemExit("Worker token cannot be empty")

    arguments.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            RotatingFileHandler(
                arguments.log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        ],
    )
    workspace = KatsuyuWorkspace(arguments.workspace)
    worker = KatsuyuWorker(
        client=AgentClient(
            arguments.base_url,
            token,
            ca_certificate_file=arguments.ca_file,
        ),
        worker_id=arguments.worker_id,
        handlers={
            "system.health": SystemHealthHandler(workspace),
            "backup.compress": BackupCompressHandler(workspace),
            "backup.encrypt": BackupEncryptHandler(workspace, arguments.age_binary),
            "backup.verify": BackupVerifyHandler(workspace),
        },
        heartbeat_seconds=arguments.heartbeat_seconds,
        status_store=StatusStore(arguments.status_file),
    )
    while True:
        try:
            worker.register()
            break
        except RuntimeError:
            LOGGER.exception("Katsuyu registration failed; retrying")
            worker.status_store.write(
                state="error",
                error="Agent inaccessible pendant l’enregistrement",
            )
            if arguments.once:
                raise
            sleep(arguments.poll_seconds)
    refresh_update_status(worker.status_store)
    LOGGER.info("Katsuyu %s started with %s", worker.worker_id, sorted(worker.handlers))
    while True:
        try:
            processed = worker.run_once()
        except RuntimeError:
            LOGGER.exception("Katsuyu polling cycle failed")
            worker.status_store.write(
                state="error",
                error="Agent inaccessible pendant le cycle de travail",
            )
            processed = False
        if arguments.once:
            return
        if not processed:
            sleep(arguments.poll_seconds)


if __name__ == "__main__":
    main()

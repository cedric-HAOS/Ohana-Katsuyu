"""Validate Katsuyu protocol v1 against a sibling Ohana-Agent checkout."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

WORKSPACE = Path(__file__).resolve().parents[2]
AGENT_ROOT = WORKSPACE / "Ohana-Agent"
KATSUYU_ROOT = WORKSPACE / "Ohana-Katsuyu"
sys.path[:0] = [str(AGENT_ROOT), str(KATSUYU_ROOT)]

from administration import (  # noqa: E402
    AdministrationHTTPServer,
    AdministrationService,
    DistributedJobRepository,
    InfrastructureConfigurationRepository,
)

from ohana_katsuyu.handlers import HandlerContext  # noqa: E402
from ohana_katsuyu.worker import AgentClient, KatsuyuWorker  # noqa: E402


class ContractHealthHandler:
    """Return one deterministic document accepted by Agent's strict model."""

    def execute(
        self, _parameters: dict[str, Any], _context: HandlerContext | None = None
    ) -> dict[str, Any]:
        return {
            "status": "OK",
            "collected_at": datetime.now(UTC).isoformat(),
            "platform": "Windows 11 contract test",
            "cpu_percent": 12.5,
            "memory_total_bytes": 8 * 1024**3,
            "memory_available_bytes": 5 * 1024**3,
            "disk_total_bytes": 1000,
            "disk_free_bytes": 750,
            "temperature_c": None,
            "issues": [],
        }


def main() -> None:
    if not AGENT_ROOT.is_dir():
        raise SystemExit(f"Sibling Ohana-Agent checkout not found: {AGENT_ROOT}")
    with TemporaryDirectory(prefix="ohana-katsuyu-contract-") as temporary:
        repository = DistributedJobRepository(Path(temporary) / "jobs.db")
        server = AdministrationHTTPServer(
            service=AdministrationService(
                infrastructure_repository=InfrastructureConfigurationRepository(
                    AGENT_ROOT / "config" / "infrastructure.yaml"
                ),
                job_repository=repository,
            ),
            token="tsunade-contract-secret",
            worker_token="katsuyu-contract-secret",
            port=0,
        )
        server.start()
        try:
            identifier = uuid4()
            repository.create(
                {
                    "protocol_version": 1,
                    "job_id": str(identifier),
                    "type": "system.health",
                    "created_at": datetime.now(UTC).isoformat(),
                    "parameters": {},
                    "timeout": 60,
                }
            )
            assert server.address is not None
            host, port = server.address
            worker = KatsuyuWorker(
                client=AgentClient(
                    f"http://{host}:{port}", "katsuyu-contract-secret"
                ),
                worker_id="katsuyu-bubule",
                handlers={"system.health": ContractHealthHandler()},
                heartbeat_seconds=0.1,
            )
            worker.register()
            assert worker.run_once() is True
            completed = repository.get(str(identifier))
            assert completed.status.value == "SUCCEEDED"
            assert completed.result_sha256 is not None
        finally:
            server.stop()
            repository.close()
    print("Agent/Katsuyu protocol v1: OK")


if __name__ == "__main__":
    main()

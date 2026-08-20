"""Short-lived Katsuyu pairing through Agent's existing worker contract."""

from __future__ import annotations

import json
import platform
import socket
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ohana_katsuyu import __version__


def normalize_agent_url(value: str) -> str:
    """Turn the installer's single address field into Agent's API base URL."""
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise ValueError("L’adresse d’Agent est obligatoire.")
    if "://" not in normalized:
        normalized = f"http://{normalized}"
    scheme, separator, remainder = normalized.partition("://")
    if separator != "://" or scheme not in {"http", "https"} or not remainder:
        raise ValueError("L’adresse d’Agent doit utiliser HTTP ou HTTPS.")
    authority = remainder.split("/", 1)[0]
    if ":" not in authority and not authority.startswith("["):
        normalized = f"{normalized}:8765"
    return normalized


@dataclass(frozen=True, slots=True)
class PairingSession:
    pairing_id: str
    polling_secret: str
    verification_code: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class PairingClient:
    base_url: str
    timeout_seconds: float = 10.0

    def create(self, worker_id: str, capabilities: list[str]) -> PairingSession:
        payload = self._post(
            "/v1/jobs/workers/pairings",
            {
                "protocol_version": 1,
                "worker_id": worker_id,
                "capabilities": capabilities,
                "platform": f"{platform.system()} {platform.release()}".strip(),
                "worker_version": __version__,
            },
        )
        return PairingSession(
            pairing_id=str(payload["pairing_id"]),
            polling_secret=str(payload["polling_secret"]),
            verification_code=str(payload["verification_code"]),
            expires_at=str(payload["expires_at"]),
        )

    def poll(self, session: PairingSession) -> tuple[str, str | None]:
        payload = self._post(
            f"/v1/jobs/workers/pairings/{session.pairing_id}/poll",
            {
                "protocol_version": 1,
                "polling_secret": session.polling_secret,
            },
        )
        token = payload.get("worker_token")
        return str(payload["status"]), str(token) if token else None

    def wait_for_approval(
        self,
        session: PairingSession,
        *,
        maximum_seconds: float = 600,
        poll_seconds: float = 2,
    ) -> str:
        deadline = monotonic() + maximum_seconds
        while monotonic() < deadline:
            status, token = self.poll(session)
            if token is not None and status == "CONSUMED":
                return token
            if status in {"EXPIRED", "REJECTED", "CONSUMED"}:
                raise RuntimeError(f"La demande d’appairage est {status.lower()}.")
            sleep(poll_seconds)
        raise RuntimeError("La validation de l’appairage a expiré.")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.load(response)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(
                f"Agent a refusé l’appairage (HTTP {error.code}) : {detail}"
            ) from error
        except (OSError, URLError) as error:
            raise RuntimeError(f"Agent est inaccessible : {error}") from error
        if not isinstance(result, dict):
            raise RuntimeError("Agent a retourné une réponse d’appairage invalide.")
        return result


def default_worker_id() -> str:
    """Use a stable, contract-compatible worker identity for this Windows host."""
    hostname = socket.gethostname().strip() or "bubule"
    safe = "".join(
        character if character.isalnum() or character in "_.:-" else "-"
        for character in hostname
    )
    return f"katsuyu-{safe}"[:80]

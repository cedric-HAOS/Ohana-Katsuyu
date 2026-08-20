"""Short-lived Katsuyu pairing through Agent's existing worker contract."""

from __future__ import annotations

import hashlib
import json
import platform
import socket
import ssl
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ohana_katsuyu import __version__


def normalize_agent_url(value: str) -> str:
    """Turn the installer's single address field into Agent's API base URL."""
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise ValueError("L’adresse d’Agent est obligatoire.")
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as error:
        raise ValueError("L’adresse d’Agent est invalide.") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("L’adresse d’Agent doit utiliser HTTPS.")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"https://{host}:{port or 8766}"


def certificate_sha256(pem: str) -> str:
    """Return the lowercase SHA-256 of one PEM certificate."""
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
    except ValueError as error:
        raise RuntimeError(
            "Agent a retourné un certificat d’autorité invalide."
        ) from error
    return hashlib.sha256(der).hexdigest()


def format_fingerprint(value: str) -> str:
    """Render a certificate fingerprint for an explicit human comparison."""
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("invalid SHA-256 fingerprint")
    return ":".join(normalized[index : index + 2].upper() for index in range(0, 64, 2))


@dataclass(frozen=True, slots=True)
class WorkerTrust:
    ca_certificate_pem: str
    ca_sha256: str


@dataclass(frozen=True, slots=True)
class PairingSession:
    pairing_id: str
    polling_secret: str
    verification_code: str
    expires_at: str
    tls_ca_sha256: str


@dataclass(frozen=True, slots=True)
class PairingClient:
    base_url: str
    trust: WorkerTrust
    timeout_seconds: float = 10.0

    @classmethod
    def bootstrap(cls, base_url: str, timeout_seconds: float = 10.0) -> PairingClient:
        """Fetch public trust material, pin it, then verify Agent's HTTPS identity."""
        unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        payload = cls._request_json(
            base_url,
            "/v1/jobs/workers/trust",
            timeout_seconds=timeout_seconds,
            context=unverified,
        )
        pem = payload.get("ca_certificate_pem")
        claimed_fingerprint = payload.get("ca_sha256")
        if not isinstance(pem, str) or not isinstance(claimed_fingerprint, str):
            raise RuntimeError("Agent a retourné une confiance HTTPS invalide.")
        calculated_fingerprint = certificate_sha256(pem)
        if calculated_fingerprint != claimed_fingerprint.lower():
            raise RuntimeError("L’empreinte HTTPS annoncée par Agent est incohérente.")
        client = cls(
            base_url=base_url,
            trust=WorkerTrust(pem, calculated_fingerprint),
            timeout_seconds=timeout_seconds,
        )
        verified = client._get("/v1/jobs/workers/trust")
        if verified.get("ca_sha256") != calculated_fingerprint:
            raise RuntimeError("La validation HTTPS d’Agent a échoué.")
        return client

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
        if payload.get("tls_ca_sha256") != self.trust.ca_sha256:
            raise RuntimeError(
                "L’appairage ne correspond pas au certificat HTTPS d’Agent."
            )
        return PairingSession(
            pairing_id=str(payload["pairing_id"]),
            polling_secret=str(payload["polling_secret"]),
            verification_code=str(payload["verification_code"]),
            expires_at=str(payload["expires_at"]),
            tls_ca_sha256=str(payload["tls_ca_sha256"]),
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

    def _context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cadata=self.trust.ca_certificate_pem)

    def _get(self, path: str) -> dict[str, Any]:
        return self._request_json(
            self.base_url,
            path,
            timeout_seconds=self.timeout_seconds,
            context=self._context(),
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        return self._open(request, context=self._context())

    @classmethod
    def _request_json(
        cls,
        base_url: str,
        path: str,
        *,
        timeout_seconds: float,
        context: ssl.SSLContext,
    ) -> dict[str, Any]:
        request = Request(
            f"{base_url.rstrip('/')}{path}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        return cls._open_request(
            request, timeout_seconds=timeout_seconds, context=context
        )

    def _open(self, request: Request, *, context: ssl.SSLContext) -> dict[str, Any]:
        return self._open_request(
            request,
            timeout_seconds=self.timeout_seconds,
            context=context,
        )

    @staticmethod
    def _open_request(
        request: Request,
        *,
        timeout_seconds: float,
        context: ssl.SSLContext,
    ) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=timeout_seconds, context=context) as response:
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

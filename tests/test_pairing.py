"""Tests for the installer-to-Agent pairing exchange."""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from ohana_katsuyu.pairing import (
    PairingClient,
    WorkerTrust,
    certificate_sha256,
    normalize_agent_url,
)


def test_normalize_agent_url_keeps_installer_input_minimal() -> None:
    assert normalize_agent_url("infra-01.ohana.lan") == (
        "https://infra-01.ohana.lan:8766"
    )
    assert normalize_agent_url("https://infra-01.ohana.lan") == (
        "https://infra-01.ohana.lan:8766"
    )
    assert normalize_agent_url("https://192.168.1.10:9000/") == (
        "https://192.168.1.10:9000"
    )
    with pytest.raises(ValueError):
        normalize_agent_url("")
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_agent_url("http://192.168.1.10:8765")


def test_pairing_never_uses_an_administration_or_worker_token(
    monkeypatch: Any,
) -> None:
    requests: list[Any] = []
    responses = iter(
        [
            {
                "pairing_id": "11111111-1111-4111-8111-111111111111",
                "polling_secret": "s" * 43,
                "verification_code": "ABCD-2345",
                "expires_at": "2026-08-20T12:10:00Z",
                "tls_ca_sha256": "a" * 64,
            },
            {
                "pairing_id": "11111111-1111-4111-8111-111111111111",
                "status": "CONSUMED",
                "expires_at": "2026-08-20T12:10:00Z",
                "worker_token": "t" * 64,
            },
        ]
    )

    marker = object()

    def fake_urlopen(request: Any, *, timeout: float, context: Any) -> BytesIO:
        assert timeout == 10
        assert context is marker
        requests.append(request)
        return BytesIO(json.dumps(next(responses)).encode("utf-8"))

    monkeypatch.setattr("ohana_katsuyu.pairing.urlopen", fake_urlopen)
    monkeypatch.setattr(PairingClient, "_context", lambda _self: marker)
    client = PairingClient(
        "https://infra-01.ohana.lan:8766",
        WorkerTrust("test-ca", "a" * 64),
    )
    session = client.create("katsuyu-bubule", ["system.health"])
    status, token = client.poll(session)

    assert status == "CONSUMED"
    assert token == "t" * 64
    assert session.verification_code == "ABCD-2345"
    assert session.tls_ca_sha256 == "a" * 64
    assert all(request.get_header("Authorization") is None for request in requests)


def test_pairing_bootstrap_pins_the_real_https_authority(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        candidates = (
            Path(r"C:\Program Files\Git\usr\bin\openssl.exe"),
            Path(r"C:\Program Files\Git\mingw64\bin\openssl.exe"),
        )
        openssl = next((str(path) for path in candidates if path.is_file()), None)
    if openssl is None:
        pytest.skip("OpenSSL is unavailable")

    ca_key = tmp_path / "ca.key"
    ca_certificate = tmp_path / "ca.crt"
    server_key = tmp_path / "server.key"
    server_request = tmp_path / "server.csr"
    server_certificate = tmp_path / "server.crt"
    commands = (
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=Ohana Test CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_certificate),
        ],
        [
            openssl,
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-addext",
            "basicConstraints=critical,CA:FALSE",
            "-addext",
            "keyUsage=critical,digitalSignature,keyEncipherment",
            "-addext",
            "extendedKeyUsage=serverAuth",
            "-keyout",
            str(server_key),
            "-out",
            str(server_request),
        ],
        [
            openssl,
            "x509",
            "-req",
            "-in",
            str(server_request),
            "-CA",
            str(ca_certificate),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-copy_extensions",
            "copy",
            "-out",
            str(server_certificate),
        ],
    )
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)

    ca_pem = ca_certificate.read_text(encoding="ascii")
    fingerprint = certificate_sha256(ca_pem)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._send(
                {
                    "schema_version": 1,
                    "ca_certificate_pem": ca_pem,
                    "ca_sha256": fingerprint,
                }
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path.endswith("/poll"):
                self._send(
                    {
                        "pairing_id": "11111111-1111-4111-8111-111111111111",
                        "status": "CONSUMED",
                        "expires_at": "2026-08-20T12:10:00Z",
                        "worker_token": "t" * 64,
                    }
                )
                return
            self._send(
                {
                    "pairing_id": "11111111-1111-4111-8111-111111111111",
                    "polling_secret": "s" * 43,
                    "verification_code": "ABCD-2345",
                    "expires_at": "2026-08-20T12:10:00Z",
                    "tls_ca_sha256": fingerprint,
                }
            )

        def _send(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(server_certificate, server_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        client = PairingClient.bootstrap(f"https://{host}:{port}")
        session = client.create("katsuyu-bubule", ["system.health"])
        status, token = client.poll(session)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert client.trust.ca_sha256 == fingerprint
    assert status == "CONSUMED"
    assert token == "t" * 64

"""Tests for the installer-to-Agent pairing exchange."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any

import pytest

from ohana_katsuyu.pairing import PairingClient, normalize_agent_url


def test_normalize_agent_url_keeps_installer_input_minimal() -> None:
    assert normalize_agent_url("infra-01.ohana.lan") == (
        "http://infra-01.ohana.lan:8765"
    )
    assert normalize_agent_url("https://infra-01.ohana.lan") == (
        "https://infra-01.ohana.lan:8765"
    )
    assert normalize_agent_url("http://192.168.1.10:9000/") == (
        "http://192.168.1.10:9000"
    )
    with pytest.raises(ValueError):
        normalize_agent_url("")


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
            },
            {
                "pairing_id": "11111111-1111-4111-8111-111111111111",
                "status": "CONSUMED",
                "expires_at": "2026-08-20T12:10:00Z",
                "worker_token": "t" * 64,
            },
        ]
    )

    def fake_urlopen(request: Any, *, timeout: float) -> BytesIO:
        assert timeout == 10
        requests.append(request)
        return BytesIO(json.dumps(next(responses)).encode("utf-8"))

    monkeypatch.setattr("ohana_katsuyu.pairing.urlopen", fake_urlopen)
    client = PairingClient("http://infra-01.ohana.lan:8765")
    session = client.create("katsuyu-bubule", ["system.health"])
    status, token = client.poll(session)

    assert status == "CONSUMED"
    assert token == "t" * 64
    assert session.verification_code == "ABCD-2345"
    assert all(request.get_header("Authorization") is None for request in requests)

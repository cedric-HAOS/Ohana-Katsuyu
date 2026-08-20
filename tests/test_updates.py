"""Tests for bounded, informational release discovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from ohana_katsuyu import updates
from ohana_katsuyu.status import StatusStore
from ohana_katsuyu.tray import tooltip
from ohana_katsuyu.updates import StableRelease, UpdateCheckError


def test_read_latest_release_accepts_only_the_official_stable_release(
    monkeypatch: Any,
) -> None:
    payload = {
        "tag_name": "v0.2.0",
        "html_url": (
            "https://github.com/cedric-HAOS/Ohana-Katsuyu/releases/tag/v0.2.0"
        ),
        "draft": False,
        "prerelease": False,
    }
    requests: list[Any] = []

    def fake_urlopen(request: Any, *, timeout: float) -> BytesIO:
        requests.append((request, timeout))
        return BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(updates, "urlopen", fake_urlopen)

    release = updates.read_latest_release(timeout_seconds=3)

    assert release == StableRelease(
        version="0.2.0",
        url="https://github.com/cedric-HAOS/Ohana-Katsuyu/releases/tag/v0.2.0",
    )
    assert requests[0][1] == 3
    assert requests[0][0].get_header("User-agent") == "Ohana-Katsuyu/0.3.0"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "tag_name": "v0.2.0",
            "html_url": "https://example.com",
            "draft": False,
            "prerelease": False,
        },
        {
            "tag_name": "v0.2.0-rc1",
            "html_url": updates.RELEASE_PAGE_PREFIX + "v0.2.0-rc1",
            "draft": False,
            "prerelease": False,
        },
        {
            "tag_name": "v0.2.0",
            "html_url": updates.RELEASE_PAGE_PREFIX + "v0.2.0",
            "draft": False,
            "prerelease": True,
        },
    ],
)
def test_read_latest_release_rejects_untrusted_or_unstable_payloads(
    payload: dict[str, object], monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        updates,
        "urlopen",
        lambda _request, *, timeout: BytesIO(json.dumps(payload).encode("utf-8")),
    )

    with pytest.raises(UpdateCheckError):
        updates.read_latest_release()


def test_refresh_is_cached_for_24_hours_and_preserves_worker_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = StatusStore(tmp_path / "status.json")
    store.write(state="connected")
    calls: list[bool] = []
    monkeypatch.setattr(
        updates,
        "read_latest_release",
        lambda **_kwargs: (
            calls.append(True)
            or StableRelease(
                "0.4.0",
                "https://github.com/cedric-HAOS/Ohana-Katsuyu/releases/tag/v0.4.0",
            )
        ),
    )
    now = datetime(2026, 8, 20, 12, tzinfo=UTC)

    first = updates.refresh_update_status(store, now=now)
    second = updates.refresh_update_status(store, now=now + timedelta(hours=23))

    assert first.state == "connected"
    assert first.update_state == "available"
    assert first.latest_version == "0.4.0"
    assert second == first
    assert calls == [True]
    assert "mise à jour 0.4.0 disponible" in tooltip(first)


def test_failed_check_is_informational_and_does_not_mark_agent_as_failed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = StatusStore(tmp_path / "status.json")
    store.write(state="connected", last_connection_at="2026-08-20T12:00:00+00:00")
    monkeypatch.setattr(
        updates,
        "read_latest_release",
        lambda **_kwargs: (_ for _ in ()).throw(UpdateCheckError("offline")),
    )

    status = updates.refresh_update_status(
        store, now=datetime(2026, 8, 20, 12, tzinfo=UTC)
    )

    assert status.state == "connected"
    assert status.error is None
    assert status.update_state == "unavailable"


def test_version_comparison_rejects_non_stable_tags() -> None:
    assert updates.version_key("v1.2.3") == (1, 2, 3)
    with pytest.raises(ValueError):
        updates.version_key("1.2.3-rc1")

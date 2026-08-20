"""Tests for the strictly informative notification-area presentation."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta

from ohana_katsuyu.icon_data import OFFICIAL_OHANA_ICON_BASE64
from ohana_katsuyu.status import LocalStatus
from ohana_katsuyu.tray import effective_state, icon_variants, tooltip


def test_embedded_icon_is_the_official_ohana_favicon() -> None:
    digest = hashlib.sha256(base64.b64decode(OFFICIAL_OHANA_ICON_BASE64)).hexdigest()
    assert digest == "c6da7e4a1da1594962fd49706cc2986deeea2ff8e83af9b43e1df59a11b24c3c"


def test_tray_has_four_distinct_clockwise_job_frames() -> None:
    variants = icon_variants()
    frames = variants["running"]

    assert set(variants) == {"connected", "running", "error", "stopped"}
    assert len(frames) == 4
    assert len({frame.tobytes() for frame in frames}) == 4
    assert variants["connected"][0].tobytes() != variants["error"][0].tobytes()
    assert variants["connected"][0].tobytes() != variants["stopped"][0].tobytes()


def test_connected_tooltip_is_explicit() -> None:
    now = datetime.now(UTC).isoformat()
    status = LocalStatus(
        state="connected",
        updated_at=now,
        last_connection_at=now,
        update_state="current",
    )

    assert effective_state(status) == "connected"
    assert "· connecté ·" in tooltip(status)


def test_stale_tooltip_does_not_claim_a_current_connection() -> None:
    old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    status = LocalStatus(
        state="connected",
        updated_at=old,
        last_connection_at=old,
        update_state="current",
    )

    assert effective_state(status) == "stopped"
    assert "· état périmé ·" in tooltip(status)

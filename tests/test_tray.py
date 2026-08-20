"""Tests for the strictly informative notification-area presentation."""

import base64
import hashlib

from ohana_katsuyu.icon_data import OFFICIAL_OHANA_ICON_BASE64
from ohana_katsuyu.tray import icon_variants


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

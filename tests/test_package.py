"""Package metadata checks for the standalone Katsuyu distribution."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_package_is_windows_specific_and_has_bounded_runtime_dependencies() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert project["name"] == "ohana-katsuyu"
    assert project["version"] == "0.1.0"
    assert project["dependencies"] == [
        "Pillow>=11,<13",
        "pydantic>=2,<3",
        "pystray>=0.19,<1",
    ]
    assert project["scripts"] == {
        "ohana-katsuyu": "ohana_katsuyu.worker:main",
        "ohana-katsuyu-setup": "ohana_katsuyu.setup:main",
        "ohana-katsuyu-tray": "ohana_katsuyu.tray:main",
        "ohana-katsuyu-windows": "ohana_katsuyu.windows:main",
    }

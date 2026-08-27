"""Bounded local power operations for Katsuyu."""

from __future__ import annotations

import os
import subprocess


def request_system_shutdown() -> None:
    """Ask Windows to shut down after Agent marked the final job complete."""
    if os.name != "nt":
        raise RuntimeError("automatic shutdown is only supported on Windows")
    subprocess.run(  # noqa: S603
        ["shutdown.exe", "/s", "/t", "0"],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
    )

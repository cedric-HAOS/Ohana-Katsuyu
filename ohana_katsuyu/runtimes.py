"""Synthetic checks of the local runtimes some capabilities need (Phase 5)."""

from __future__ import annotations

import shutil
from pathlib import Path

from ohana_katsuyu.models import WorkerRuntime


def executable_runtime(path: Path, label: str) -> WorkerRuntime:
    """Report whether a configured executable can be started as configured.

    A bare name ("age.exe") is resolved like the job would: through PATH.
    """
    if path.parent == Path(".") and not path.is_file():
        found = shutil.which(str(path))
        if found is None:
            return WorkerRuntime(state="missing", detail=f"{label} introuvable")
        return WorkerRuntime(state="ready", detail=f"{label} : {found}")
    if not path.is_file():
        return WorkerRuntime(state="missing", detail=f"{label} absent : {path}")
    return WorkerRuntime(state="ready", detail=f"{label} : {path}")

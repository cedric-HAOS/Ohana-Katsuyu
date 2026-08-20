"""Bounded local status shared read-only with Katsuyu's tray application."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ohana_katsuyu import __version__


@dataclass(frozen=True, slots=True)
class LocalStatus:
    version: str = __version__
    state: str = "stopped"
    updated_at: str = ""
    last_connection_at: str | None = None
    current_job_id: str | None = None
    current_job_type: str | None = None
    error: str | None = None
    update_state: str = "unknown"
    latest_version: str | None = None
    update_checked_at: str | None = None
    update_url: str | None = None


class StatusStore:
    """Atomically publish a tiny document; never expose credentials or payloads."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, *, state: str, **changes: object) -> LocalStatus:
        previous = self.read()
        values = asdict(previous)
        values.update(changes)
        values["state"] = state
        values["version"] = __version__
        values["updated_at"] = datetime.now(UTC).isoformat()
        status = LocalStatus(**values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(asdict(status), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return status

    def read(self) -> LocalStatus:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            allowed = set(LocalStatus.__dataclass_fields__)
            filtered = {key: item for key, item in value.items() if key in allowed}
            return LocalStatus(**filtered)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return LocalStatus()

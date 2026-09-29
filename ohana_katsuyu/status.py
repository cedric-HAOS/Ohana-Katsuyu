"""Bounded local status shared read-only with Katsuyu's tray application."""

from __future__ import annotations

import json
import os
import time
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
    # Automatic update: last error and last attempt (version, UTC ISO time).
    update_error: str | None = None
    update_attempted_version: str | None = None
    update_attempted_at: str | None = None


REPLACE_ATTEMPTS = 5
REPLACE_PAUSE_SECONDS = 0.1


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
        try:
            temporary.write_text(
                json.dumps(asdict(status), ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            # Windows refuses the replace while the tray reads the file.
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    temporary.replace(self.path)
                    break
                except PermissionError:
                    if attempt == REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(REPLACE_PAUSE_SECONDS)
        finally:
            temporary.unlink(missing_ok=True)
        return status

    def remove_stale_temporaries(self) -> int:
        """Delete leftovers of writes interrupted before this fix."""
        removed = 0
        for path in self.path.parent.glob(f"{self.path.name}.*.tmp"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

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

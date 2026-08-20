"""Tests for the bounded worker-to-tray local status document."""

from pathlib import Path

from ohana_katsuyu.status import StatusStore


def test_status_is_atomic_bounded_and_contains_no_credentials(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    store = StatusStore(path)
    written = store.write(
        state="running",
        current_job_id="job-id",
        current_job_type="system.health",
        last_connection_at="2026-08-20T12:00:00+00:00",
    )

    assert store.read() == written
    assert "token" not in path.read_text(encoding="utf-8").lower()
    assert not list(tmp_path.glob("*.tmp"))

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


def test_failed_replace_leaves_no_temporary_and_stale_ones_are_removed(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    import pytest

    from ohana_katsuyu import status as status_module
    from ohana_katsuyu.status import StatusStore

    store = StatusStore(tmp_path / "status.json")
    (tmp_path / "status.json.13688.tmp").write_text("{}", encoding="utf-8")
    assert store.remove_stale_temporaries() == 1

    monkeypatch.setattr(status_module, "REPLACE_PAUSE_SECONDS", 0)
    original = Path.replace
    calls = []

    def locked(self, target):
        calls.append(target)
        if len(calls) < 3:
            raise PermissionError("the tray is reading the file")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", locked)
    assert store.write(state="connected").state == "connected"
    assert len(calls) == 3

    monkeypatch.setattr(
        Path, "replace", lambda self, target: (_ for _ in ()).throw(PermissionError())
    )
    with pytest.raises(PermissionError):
        store.write(state="idle")
    assert not list(tmp_path.glob("status.json.*.tmp"))

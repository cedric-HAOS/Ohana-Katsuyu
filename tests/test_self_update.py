"""Automatic update started by the worker, the setup and the tray."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from ohana_katsuyu import self_update, setup, tray
from ohana_katsuyu.models import JobClaimResult, WorkerDocument
from ohana_katsuyu.self_update import (
    UPDATE_TASK_NAME,
    AutoUpdater,
    UpdateError,
    launch_update,
    remove_update_leftovers,
    stage_update,
)
from ohana_katsuyu.status import LocalStatus, StatusStore
from ohana_katsuyu.worker import AgentClient, KatsuyuWorker

SETUP = b"new katsuyu setup"
RELEASES_API = "https://api.github.com/repos/cedric-HAOS/Ohana-Katsuyu/releases"
PREFIX = "https://github.com/cedric-HAOS/Ohana-Katsuyu/releases/download/v9.0.0/"


def _github(sums: str | None = None, assets_prefix: str = PREFIX):
    documents = {
        f"{RELEASES_API}/tags/v9.0.0": json.dumps(
            {
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": name,
                        "browser_download_url": f"{assets_prefix}{name}",
                    }
                    for name in ("KatsuyuSetup.exe", "SHA256SUMS")
                ],
            }
        ).encode(),
        f"{PREFIX}SHA256SUMS": (
            sums
            if sums is not None
            else f"{hashlib.sha256(SETUP).hexdigest()}  KatsuyuSetup.exe\n"
        ).encode(),
        f"{PREFIX}KatsuyuSetup.exe": SETUP,
    }

    def read(url: str, _limit: int) -> bytes:
        return documents[url]

    return read


def test_stage_keeps_the_setup_only_when_its_digest_matches(tmp_path: Path) -> None:
    staged = stage_update("9.0.0", tmp_path, read=_github())

    assert staged == tmp_path / "KatsuyuSetup-9.0.0.exe"
    assert staged.read_bytes() == SETUP

    with pytest.raises(UpdateError, match="SHA-256"):
        stage_update(
            "9.0.0",
            tmp_path / "bad",
            read=_github(sums="0" * 64 + "  KatsuyuSetup.exe"),
        )
    assert not (tmp_path / "bad").exists()


def test_assets_outside_the_official_release_are_ignored(tmp_path: Path) -> None:
    other = "https://github.com/someone/else/releases/download/v9.0.0/"
    with pytest.raises(UpdateError, match="assets absents"):
        stage_update("9.0.0", tmp_path, read=_github(assets_prefix=other))


def test_setup_runs_in_its_own_system_task() -> None:
    calls: list[list[str]] = []
    launch_update(
        Path(r"C:\ProgramData\Ohana\Katsuyu\updates\KatsuyuSetup-9.0.0.exe"),
        schtasks=calls.append,
    )

    create, run = calls
    assert create[:2] == ["/Create", "/F"]
    assert create[create.index("/RU") + 1] == "SYSTEM"
    assert create[create.index("/RL") + 1] == "HIGHEST"
    assert create[create.index("/TN") + 1] == UPDATE_TASK_NAME
    command = create[create.index("/TR") + 1]
    assert command.endswith("KatsuyuSetup-9.0.0.exe --update-existing --background")
    assert run == ["/Run", "/TN", UPDATE_TASK_NAME]


def _available(store: StatusStore, version: str = "9.0.0"):
    def check(_store: StatusStore) -> LocalStatus:
        current = _store.read()
        if current.update_state in {"installing", "failed"}:
            return current
        return _store.write(
            state=current.state, update_state="available", latest_version=version
        )

    return check


def test_updater_installs_a_newer_release_and_records_the_attempt(
    tmp_path: Path,
) -> None:
    store = StatusStore(tmp_path / "status.json")
    store.write(state="connected")
    launched: list[Path] = []
    updater = AutoUpdater(
        store,
        tmp_path / "updates",
        check=_available(store),
        stage=lambda version, directory: directory / f"KatsuyuSetup-{version}.exe",
        launch=launched.append,
    )

    assert updater.maybe_update() is True
    status = store.read()
    assert launched == [tmp_path / "updates" / "KatsuyuSetup-9.0.0.exe"]
    assert status.update_state == "installing"
    assert status.update_attempted_version == "9.0.0"
    assert status.state == "connected"
    assert "mise à jour vers 9.0.0 en cours" in tray.tooltip(status)


def test_updater_does_nothing_when_disabled_or_current(tmp_path: Path) -> None:
    store = StatusStore(tmp_path / "status.json")
    staged: list[str] = []
    disabled = AutoUpdater(
        store,
        tmp_path,
        enabled=False,
        check=_available(store),
        stage=lambda version, _directory: staged.append(version),
    )
    assert disabled.maybe_update() is False
    older = AutoUpdater(
        store,
        tmp_path,
        check=_available(store, version="0.0.1"),
        stage=lambda version, _directory: staged.append(version),
    )
    store.write(state="connected", update_state="unknown")
    assert older.maybe_update() is False
    assert staged == []


def test_a_failed_update_is_retried_only_after_24_hours(tmp_path: Path) -> None:
    store = StatusStore(tmp_path / "status.json")
    now = [datetime(2026, 9, 28, 20, tzinfo=UTC)]
    attempts: list[str] = []

    def failing_stage(version: str, _directory: Path) -> Path:
        attempts.append(version)
        raise UpdateError("téléchargement impossible")

    def check(_store: StatusStore) -> LocalStatus:
        current = _store.read()
        return _store.write(
            state=current.state, update_state="available", latest_version="9.0.0"
        )

    updater = AutoUpdater(
        store, tmp_path, check=check, stage=failing_stage, clock=lambda: now[0]
    )

    assert updater.maybe_update() is False
    failed = store.read()
    assert failed.update_state == "failed"
    assert failed.update_error == "téléchargement impossible"
    assert "échouée" in tray.tooltip(failed)
    now[0] += timedelta(hours=23)
    assert updater.maybe_update() is False
    now[0] += timedelta(hours=2)
    updater.maybe_update()
    assert attempts == ["9.0.0", "9.0.0"]


def test_restart_on_the_same_version_marks_the_attempt_failed(tmp_path: Path) -> None:
    store = StatusStore(tmp_path / "status.json")
    store.write(
        state="connected",
        update_state="installing",
        update_attempted_version="9.0.0",
    )
    AutoUpdater(store, tmp_path).settle_previous_attempt()

    assert store.read().update_state == "failed"
    assert "katsuyu-update.log" in (store.read().update_error or "")


def test_leftovers_are_removed_after_the_restart(tmp_path: Path) -> None:
    updates, program = tmp_path / "updates", tmp_path / "program"
    updates.mkdir()
    program.mkdir()
    (updates / "KatsuyuSetup-9.0.0.exe").write_bytes(b"x")
    (program / "KatsuyuTray.exe.old-42").write_bytes(b"x")
    (program / "KatsuyuTray.exe").write_bytes(b"x")
    calls: list[list[str]] = []

    remove_update_leftovers(updates, program, schtasks=calls.append)

    assert calls == [["/Delete", "/F", "/TN", UPDATE_TASK_NAME]]
    assert list(updates.iterdir()) == []
    assert [path.name for path in program.iterdir()] == ["KatsuyuTray.exe"]


class IdleClient:
    def __init__(self, *, shutdown: bool = False) -> None:
        self.shutdown = shutdown

    def register(self, payload: dict[str, Any]) -> WorkerDocument:
        now = datetime.now(UTC)
        return WorkerDocument(**payload, registered_at=now, last_seen_at=now)

    def claim(self, _payload: dict[str, Any]) -> JobClaimResult:
        return JobClaimResult(job=None, shutdown_requested=self.shutdown)

    def report_power(self, _payload: dict[str, Any]) -> bool:
        return True


class CountingUpdater:
    def __init__(self) -> None:
        self.calls = 0

    def maybe_update(self) -> bool:
        self.calls += 1
        return False


@pytest.mark.parametrize(("shutdown", "expected"), [(False, 1), (True, 0)])
def test_worker_updates_only_when_idle_and_staying_on(
    shutdown: bool, expected: int
) -> None:
    updater = CountingUpdater()
    worker = KatsuyuWorker(
        client=cast(AgentClient, IdleClient(shutdown=shutdown)),
        worker_id="katsuyu-bubule",
        handlers={"system.health": object()},  # type: ignore[dict-item]
        updater=cast(AutoUpdater, updater),
        shutdown_requester=lambda: None,
    )

    worker.run_once()

    assert updater.calls == expected


def test_locked_executable_is_renamed_then_replaced(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "new.exe"
    source.write_bytes(b"new")
    destination = tmp_path / "KatsuyuTray.exe"
    destination.write_bytes(b"old")
    real_copy = setup.shutil.copy2
    calls = []

    def copy(src: Path, dst: Path) -> None:
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("in use")
        real_copy(src, dst)

    monkeypatch.setattr(setup.shutil, "copy2", copy)
    setup.replace_file(source, destination)

    assert destination.read_bytes() == b"new"
    retired = [path for path in tmp_path.iterdir() if ".old-" in path.name]
    assert len(retired) == 1 and retired[0].read_bytes() == b"old"


def test_background_update_keeps_the_tray_and_the_auto_update_choice(
    tmp_path: Path, monkeypatch: Any
) -> None:
    payload, program, state = (
        tmp_path / "payload",
        tmp_path / "program",
        tmp_path / "state",
    )
    for directory in (payload, program, state):
        directory.mkdir()
    for name in ("KatsuyuWorker.exe", "KatsuyuTray.exe", "age.exe", "age-LICENSE.txt"):
        (payload / name).write_bytes(b"new")
    (state / "agent-ca.pem").write_text("ca", encoding="utf-8")
    (state / "config.json").write_text('{"auto_update": false}', encoding="utf-8")
    setup_source = tmp_path / "KatsuyuSetup.exe"
    setup_source.write_bytes(b"setup")
    existing = setup.ExistingInstallation(
        "https://infra-01.ohana.lan:8766",
        "katsuyu-bubule",
        "token",
        state / "agent-ca.pem",
    )
    stops: list[dict[str, Any]] = []
    started: list[Any] = []

    class Agent:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def register(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    monkeypatch.setattr(setup, "require_administrator", lambda: None)
    monkeypatch.setattr(setup, "installed_version", lambda: "0.10.0")
    monkeypatch.setattr(setup, "read_existing_installation", lambda: existing)
    monkeypatch.setattr(setup, "payload_root", lambda: payload)
    monkeypatch.setattr(setup, "program_root", lambda: program)
    monkeypatch.setattr(setup, "data_root", lambda: state)
    monkeypatch.setattr(setup.sys, "executable", str(setup_source))
    monkeypatch.setattr(setup, "secure_paths", lambda *_args: None)
    monkeypatch.setattr(setup, "AgentClient", Agent)
    monkeypatch.setattr(setup, "wake_on_lan_mac_address", lambda _url: None)
    monkeypatch.setattr(
        setup, "stop_running_components", lambda **kwargs: stops.append(kwargs)
    )
    monkeypatch.setattr(setup, "install_windows_startup", lambda _args: None)
    monkeypatch.setattr(setup, "register_uninstaller", lambda _path: None)
    monkeypatch.setattr(setup, "_run_checked", lambda _command: None)
    monkeypatch.setattr(
        setup.subprocess, "Popen", lambda *args, **_kwargs: started.append(args)
    )

    setup.install(existing.base_url, background=True)

    assert stops == [{"keep_tray": True}]
    assert started == []  # the tray restarts itself in the user's session
    configuration = json.loads((state / "config.json").read_text(encoding="utf-8"))
    assert configuration["auto_update"] is False


@pytest.mark.parametrize(
    ("installed", "expected"),
    [(None, False), ("0.0.1", False), ("99.0.0", True), ("garbage", False)],
)
def test_tray_restarts_only_for_a_newer_installed_version(
    installed: str | None, expected: bool
) -> None:
    assert tray.newer_installed(lambda: installed) is expected


def test_background_flag_requires_update_existing(monkeypatch: Any) -> None:
    monkeypatch.setattr(setup.sys, "argv", ["KatsuyuSetup.exe", "--background"])
    with pytest.raises(SystemExit):
        setup.main()


def test_worker_configuration_reads_auto_update(tmp_path: Path) -> None:
    from ohana_katsuyu import worker as worker_module

    config = tmp_path / "config.json"
    document = {
        "base_url": "https://infra-01.ohana.lan:8766",
        "worker_id": "katsuyu-bubule",
        "token_file": "t",
        "ca_file": "c",
        "workspace": "w",
        "log_file": "l",
        "status_file": "s",
        "age_binary": "a",
        "auto_update": False,
    }
    config.write_text(json.dumps(document), encoding="utf-8")
    arguments = worker_module.build_parser().parse_args(["--config-file", str(config)])
    assert arguments.auto_update is True
    worker_module.apply_configuration(arguments)
    assert arguments.auto_update is False
    assert self_update.RETRY_AFTER == timedelta(hours=24)

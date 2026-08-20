"""Tests for fresh-install versus in-place-upgrade behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ohana_katsuyu import setup


def test_existing_installation_reuses_private_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(setup, "data_root", lambda: tmp_path)
    (tmp_path / "katsuyu.token").write_text("secret", encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "base_url": "http://infra-01.ohana.lan:8765/",
                "worker_id": "bubule",
            }
        ),
        encoding="utf-8",
    )

    existing = setup.read_existing_installation()

    assert existing == setup.ExistingInstallation(
        "http://infra-01.ohana.lan:8765", "bubule", "secret"
    )


def test_replace_payload_keeps_a_rollback_copy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "source"
    binary_root = tmp_path / "program"
    state_root = tmp_path / "state"
    source.mkdir()
    binary_root.mkdir()
    state_root.mkdir()
    (source / "KatsuyuWorker.exe").write_bytes(b"new")
    (binary_root / "KatsuyuWorker.exe").write_bytes(b"old")
    setup_source = tmp_path / "KatsuyuSetup.exe"
    setup_source.write_bytes(b"setup")
    monkeypatch.setattr(setup.sys, "executable", str(setup_source))

    backup = setup.replace_payload(
        source, binary_root, state_root, ["KatsuyuWorker.exe"]
    )

    assert (binary_root / "KatsuyuWorker.exe").read_bytes() == b"new"
    assert (binary_root / "KatsuyuUninstall.exe").read_bytes() == b"setup"
    assert (backup / "KatsuyuWorker.exe").read_bytes() == b"old"

    setup.restore_payload(backup, binary_root, ["KatsuyuWorker.exe"])

    assert (binary_root / "KatsuyuWorker.exe").read_bytes() == b"old"
    assert not (binary_root / "KatsuyuUninstall.exe").exists()
    assert not backup.exists()


def test_installer_refuses_to_downgrade_existing_katsuyu(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(setup, "require_administrator", lambda: None)
    monkeypatch.setattr(setup, "installed_version", lambda: "0.2.0")

    with pytest.raises(RuntimeError, match="plus récente"):
        setup.install("infra-01.ohana.lan")


def test_upgrade_does_not_pair_again_and_preserves_status(
    tmp_path: Path, monkeypatch: Any
) -> None:
    payload = tmp_path / "payload"
    program = tmp_path / "program"
    state = tmp_path / "state"
    payload.mkdir()
    program.mkdir()
    state.mkdir()
    required = ["KatsuyuWorker.exe", "KatsuyuTray.exe", "age.exe", "age-LICENSE.txt"]
    for name in required:
        (payload / name).write_bytes(f"new-{name}".encode())
        (program / name).write_bytes(f"old-{name}".encode())
    setup_source = tmp_path / "KatsuyuSetup.exe"
    setup_source.write_bytes(b"setup")
    (state / "status.json").write_text('{"state":"connected"}', encoding="utf-8")
    existing = setup.ExistingInstallation(
        "http://infra-01.ohana.lan:8765", "bubule", "existing-token"
    )
    registrations: list[tuple[str, dict[str, object]]] = []
    stopped: list[bool] = []

    class FakeAgentClient:
        def __init__(self, _base_url: str, token: str) -> None:
            self.token = token

        def register(self, document: dict[str, object]) -> None:
            registrations.append((self.token, document))

    monkeypatch.setattr(setup, "require_administrator", lambda: None)
    monkeypatch.setattr(setup, "installed_version", lambda: "0.1.0")
    monkeypatch.setattr(setup, "read_existing_installation", lambda: existing)
    monkeypatch.setattr(setup, "payload_root", lambda: payload)
    monkeypatch.setattr(setup, "program_root", lambda: program)
    monkeypatch.setattr(setup, "data_root", lambda: state)
    monkeypatch.setattr(setup.sys, "executable", str(setup_source))
    monkeypatch.setattr(setup, "secure_paths", lambda *_args: None)
    monkeypatch.setattr(setup, "AgentClient", FakeAgentClient)
    monkeypatch.setattr(setup, "stop_running_components", lambda: stopped.append(True))
    monkeypatch.setattr(setup, "install_windows_startup", lambda _args: None)
    monkeypatch.setattr(setup, "register_uninstaller", lambda _path: None)
    monkeypatch.setattr(setup, "_run_checked", lambda _command: None)
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        setup,
        "PairingClient",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected pairing")),
    )

    setup.install("ignored.example")

    assert stopped == [True]
    assert registrations[0][0] == "existing-token"
    assert registrations[0][1]["worker_version"] == "0.1.0"
    assert (state / "status.json").read_text(encoding="utf-8") == (
        '{"state":"connected"}'
    )
    assert (program / "KatsuyuWorker.exe").read_bytes() == (b"new-KatsuyuWorker.exe")
    assert not (state / "update-backup").exists()

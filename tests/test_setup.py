"""Tests for fresh-install versus in-place-upgrade behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ohana_katsuyu import setup
from ohana_katsuyu.ai_install import AiInstallation


@pytest.mark.parametrize("paired", [True, False])
def test_update_existing_requires_paired_installation(tmp_path, monkeypatch, paired):
    monkeypatch.setattr("sys.argv", ["KatsuyuSetup", "--update-existing"])
    calls = []
    monkeypatch.setattr(setup, "require_administrator", lambda: calls.append("admin"))
    certificate = tmp_path / "ca.pem"
    certificate.write_text("public")
    existing = setup.ExistingInstallation(
        "https://agent:8766", "worker", "secret", certificate
    )
    monkeypatch.setattr(
        setup, "read_existing_installation", lambda: existing if paired else None
    )
    monkeypatch.setattr(setup, "install", lambda address: calls.append(address))
    if paired:
        setup.main()
        assert calls == ["admin", "https://agent:8766"]
    else:
        with pytest.raises(SystemExit):
            setup.main()
        assert calls == ["admin"]


def test_download_progress_is_clear_and_bounded() -> None:
    assert setup.format_download_progress("model.gguf", 1024**3, 2 * 1024**3) == (
        "model.gguf : 50.0 % (1.00/2.00 Gio)"
    )


def test_existing_installation_reuses_private_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(setup, "data_root", lambda: tmp_path)
    (tmp_path / "katsuyu.token").write_text("secret", encoding="utf-8")
    ca_file = tmp_path / "agent-ca.pem"
    ca_file.write_text("public certificate", encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "base_url": "https://infra-01.ohana.lan:8766/",
                "worker_id": "bubule",
                "ca_file": str(ca_file),
            }
        ),
        encoding="utf-8",
    )

    existing = setup.read_existing_installation()

    assert existing == setup.ExistingInstallation(
        "https://infra-01.ohana.lan:8766", "bubule", "secret", ca_file
    )


def test_existing_installation_preserves_complete_ai_configuration(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(setup, "data_root", lambda: tmp_path)
    (tmp_path / "katsuyu.token").write_text("secret", encoding="utf-8")
    ca_file = tmp_path / "agent-ca.pem"
    ca_file.write_text("public certificate", encoding="utf-8")
    runtime = tmp_path / "ai" / "runtime" / "KatsuyuAiServer.exe"
    model = tmp_path / "ai" / "models" / "model.gguf"
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "base_url": "https://infra-01.ohana.lan:8766",
                "worker_id": "bubule",
                "ca_file": str(ca_file),
                "ai_runtime": str(runtime),
                "ai_model": str(model),
                "ai_model_id": "pinned-model",
                "ai_model_sha256": "a" * 64,
                "ai_context_size": 8192,
            }
        ),
        encoding="utf-8",
    )

    existing = setup.read_existing_installation()

    assert existing is not None
    assert existing.ai == AiInstallation(runtime, model, "pinned-model", "a" * 64, 8192)


def test_partial_ai_configuration_does_not_discard_worker_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(setup, "data_root", lambda: tmp_path)
    (tmp_path / "katsuyu.token").write_text("secret", encoding="utf-8")
    ca_file = tmp_path / "agent-ca.pem"
    ca_file.write_text("public certificate", encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "base_url": "https://infra-01.ohana.lan:8766",
                "worker_id": "bubule",
                "ca_file": str(ca_file),
                "ai_model": "incomplete.gguf",
            }
        ),
        encoding="utf-8",
    )

    existing = setup.read_existing_installation()

    assert existing is not None
    assert existing.token == "secret"
    assert existing.ai is None


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
    monkeypatch.setattr(setup, "installed_version", lambda: "0.9.0")

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
        "https://infra-01.ohana.lan:8766",
        "katsuyu-Bubule",
        "existing-token",
        state / "agent-ca.pem",
    )
    (state / "agent-ca.pem").write_text("public certificate", encoding="utf-8")
    registrations: list[tuple[str, dict[str, object], str | None]] = []
    stopped: list[bool] = []

    class FakeAgentClient:
        def __init__(
            self,
            _base_url: str,
            token: str,
            *,
            ca_certificate_file: Path,
        ) -> None:
            self.token = token
            assert ca_certificate_file == state / "agent-ca.pem"

        def register(
            self,
            document: dict[str, object],
            *,
            previous_worker_id: str | None = None,
        ) -> None:
            registrations.append((self.token, document, previous_worker_id))

    monkeypatch.setattr(setup, "require_administrator", lambda: None)
    monkeypatch.setattr(setup, "installed_version", lambda: "0.1.0")
    monkeypatch.setattr(setup, "read_existing_installation", lambda: existing)
    monkeypatch.setattr(setup, "payload_root", lambda: payload)
    monkeypatch.setattr(setup, "program_root", lambda: program)
    monkeypatch.setattr(setup, "data_root", lambda: state)
    monkeypatch.setattr(setup.sys, "executable", str(setup_source))
    monkeypatch.setattr(setup, "secure_paths", lambda *_args: None)
    monkeypatch.setattr(setup, "AgentClient", FakeAgentClient)
    monkeypatch.setattr(
        setup,
        "wake_on_lan_mac_address",
        lambda _base_url: "AA:BB:CC:DD:EE:FF",
    )
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
    assert registrations[0][1]["worker_version"] == "0.8.17"
    assert registrations[0][1]["worker_id"] == "katsuyu-bubule"
    assert registrations[0][1]["wake_on_lan_mac_address"] == "AA:BB:CC:DD:EE:FF"
    assert registrations[0][2] == "katsuyu-Bubule"
    configuration = json.loads((state / "config.json").read_text(encoding="utf-8"))
    assert configuration["worker_id"] == "katsuyu-bubule"
    assert (state / "status.json").read_text(encoding="utf-8") == (
        '{"state":"connected"}'
    )
    assert (program / "KatsuyuWorker.exe").read_bytes() == (b"new-KatsuyuWorker.exe")
    assert not (state / "update-backup").exists()


def test_upgrade_can_provision_and_advertise_optional_ai(
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
        (payload / name).write_bytes(name.encode())
    setup_source = tmp_path / "KatsuyuSetup.exe"
    setup_source.write_bytes(b"setup")
    ca_file = state / "agent-ca.pem"
    ca_file.write_text("public certificate", encoding="utf-8")
    existing = setup.ExistingInstallation(
        "https://infra-01.ohana.lan:8766", "bubule", "token", ca_file
    )
    ai = AiInstallation(
        state / "ai" / "runtime" / "KatsuyuAiServer.exe",
        state / "ai" / "models" / "model.gguf",
        "pinned-model",
        "b" * 64,
        8192,
    )
    registrations: list[dict[str, object]] = []

    class FakeAgentClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def register(self, document: dict[str, object]) -> None:
            registrations.append(document)

    monkeypatch.setattr(setup, "require_administrator", lambda: None)
    monkeypatch.setattr(setup, "installed_version", lambda: "0.3.1")
    monkeypatch.setattr(setup, "read_existing_installation", lambda: existing)
    monkeypatch.setattr(setup, "payload_root", lambda: payload)
    monkeypatch.setattr(setup, "program_root", lambda: program)
    monkeypatch.setattr(setup, "data_root", lambda: state)
    monkeypatch.setattr(setup.sys, "executable", str(setup_source))
    monkeypatch.setattr(setup, "provision_ai", lambda *_args: ai)
    monkeypatch.setattr(setup, "secure_paths", lambda *_args: None)
    monkeypatch.setattr(setup, "AgentClient", FakeAgentClient)
    monkeypatch.setattr(
        setup,
        "wake_on_lan_mac_address",
        lambda _base_url: "AA:BB:CC:DD:EE:FF",
    )
    monkeypatch.setattr(setup, "stop_running_components", lambda: None)
    monkeypatch.setattr(setup, "install_windows_startup", lambda _args: None)
    monkeypatch.setattr(setup, "register_uninstaller", lambda _path: None)
    monkeypatch.setattr(setup, "_run_checked", lambda _command: None)
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *_args, **_kwargs: None)

    setup.install("ignored.example", install_ai=True)

    configuration = json.loads((state / "config.json").read_text(encoding="utf-8"))
    assert configuration["ai_runtime"] == str(ai.runtime)
    assert configuration["ai_model_sha256"] == "b" * 64
    assert "ai.inference" in registrations[0]["capabilities"]
    assert registrations[0]["wake_on_lan_mac_address"] == "AA:BB:CC:DD:EE:FF"

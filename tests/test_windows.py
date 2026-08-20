"""Tests for Katsuyu's Windows startup integration."""

from __future__ import annotations

from pathlib import Path

from ohana_katsuyu import windows


def test_install_creates_only_the_expected_system_task(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []
    token = tmp_path / "katsuyu.token"
    token.write_text("worker-secret", encoding="utf-8")
    ca_file = tmp_path / "agent-ca.pem"
    ca_file.write_text("public certificate", encoding="utf-8")
    age_binary = tmp_path / "age.exe"
    age_binary.write_bytes(b"test executable")
    monkeypatch.setattr(
        windows,
        "_worker_executable",
        lambda: Path(r"C:\Python\Scripts\ohana-katsuyu.exe"),
    )
    monkeypatch.setattr(
        windows,
        "_run_schtasks",
        lambda arguments: commands.append(arguments),
    )
    monkeypatch.setattr(
        windows,
        "_tray_executable",
        lambda: Path(r"C:\Python\Scripts\KatsuyuTray.exe"),
    )
    tray_commands: list[str] = []
    monkeypatch.setattr(windows, "_register_tray", tray_commands.append)
    arguments = windows.build_parser().parse_args(
        [
            "install",
            "--base-url",
            "https://infra-01.ohana.lan:8766",
            "--token-file",
            str(token),
            "--ca-file",
            str(ca_file),
            "--workspace",
            str(tmp_path / "workspace"),
            "--log-file",
            str(tmp_path / "katsuyu.log"),
            "--age-binary",
            str(age_binary),
            "--worker-id",
            "katsuyu-bubule",
        ]
    )

    windows.install(arguments)

    assert commands[0][:8] == [
        "/Create",
        "/F",
        "/SC",
        "ONSTART",
        "/RU",
        "SYSTEM",
        "/RL",
        "HIGHEST",
    ]
    assert commands[0][commands[0].index("/TN") + 1] == "Ohana-Katsuyu"
    assert "KatsuyuTray.exe" in tray_commands[0]


def test_uninstall_targets_only_katsuyu_task(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        windows,
        "_run_schtasks",
        lambda arguments: commands.append(arguments),
    )
    removed: list[bool] = []
    monkeypatch.setattr(windows, "_unregister_tray", lambda: removed.append(True))

    windows.uninstall(windows.build_parser().parse_args(["uninstall"]))

    assert commands == [["/Delete", "/F", "/TN", "Ohana-Katsuyu"]]
    assert removed == [True]

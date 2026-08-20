"""Install or remove Katsuyu's native Windows startup task."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

TASK_NAME = "Ohana-Katsuyu"
TRAY_RUN_VALUE = "Ohana-Katsuyu-Tray"


def _default_root() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Ohana" / "Katsuyu"


def _worker_executable() -> Path:
    discovered = shutil.which("ohana-katsuyu")
    if discovered:
        return Path(discovered).resolve()
    for name in ("KatsuyuWorker.exe", "ohana-katsuyu.exe"):
        candidate = Path(sys.executable).with_name(name)
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("ohana-katsuyu.exe is not installed beside Python or in PATH")


def _tray_executable() -> Path:
    for name in ("KatsuyuTray.exe", "ohana-katsuyu-tray.exe"):
        candidate = Path(sys.executable).with_name(name)
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("KatsuyuTray.exe is not installed beside the worker")


def _run_schtasks(arguments: list[str]) -> None:
    completed = subprocess.run(  # noqa: S603
        ["schtasks.exe", *arguments],
        check=False,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"Task Scheduler rejected the operation: {detail}")


def _register_tray(command: str) -> None:
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, TRAY_RUN_VALUE, 0, winreg.REG_SZ, command)


def _unregister_tray() -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, TRAY_RUN_VALUE)
    except FileNotFoundError:
        pass


def install(arguments: argparse.Namespace) -> None:
    if not arguments.token_file.is_file() or arguments.token_file.stat().st_size == 0:
        raise RuntimeError(
            f"worker token file is missing or empty: {arguments.token_file}"
        )
    if not arguments.ca_file.is_file() or arguments.ca_file.stat().st_size == 0:
        raise RuntimeError(
            f"worker CA certificate is missing or empty: {arguments.ca_file}"
        )
    if arguments.age_binary.is_absolute():
        if not arguments.age_binary.is_file():
            raise RuntimeError(f"age executable not found: {arguments.age_binary}")
        age_binary = arguments.age_binary.resolve()
    else:
        discovered_age = shutil.which(str(arguments.age_binary))
        if discovered_age is None:
            raise RuntimeError(
                f"age executable not found in PATH: {arguments.age_binary}"
            )
        age_binary = Path(discovered_age).resolve()
    arguments.workspace.mkdir(parents=True, exist_ok=True)
    arguments.log_file.parent.mkdir(parents=True, exist_ok=True)
    worker = (
        arguments.worker_executable.resolve()
        if arguments.worker_executable is not None
        else _worker_executable()
    )
    command = subprocess.list2cmdline(
        [
            str(worker),
            "--base-url",
            arguments.base_url,
            "--token-file",
            str(arguments.token_file.resolve()),
            "--ca-file",
            str(arguments.ca_file.resolve()),
            "--workspace",
            str(arguments.workspace.resolve()),
            "--log-file",
            str(arguments.log_file.resolve()),
            "--status-file",
            str(arguments.status_file.resolve()),
            "--age-binary",
            str(age_binary),
            "--worker-id",
            arguments.worker_id,
        ]
    )
    _run_schtasks(
        [
            "/Create",
            "/F",
            "/SC",
            "ONSTART",
            "/RU",
            "SYSTEM",
            "/RL",
            "HIGHEST",
            "/TN",
            TASK_NAME,
            "/TR",
            command,
        ]
    )
    if not getattr(arguments, "without_tray", False):
        tray = (
            arguments.tray_executable.resolve()
            if arguments.tray_executable is not None
            else _tray_executable()
        )
        _register_tray(
            subprocess.list2cmdline(
                [
                    str(tray),
                    "--status-file",
                    str(arguments.status_file.resolve()),
                    "--log-file",
                    str(arguments.log_file.resolve()),
                ]
            )
        )


def uninstall(_arguments: argparse.Namespace) -> None:
    _run_schtasks(["/Delete", "/F", "/TN", TASK_NAME])
    _unregister_tray()


def build_parser() -> argparse.ArgumentParser:
    root = _default_root()
    parser = argparse.ArgumentParser(description="Katsuyu Windows startup task")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--base-url", required=True)
    install_parser.add_argument(
        "--token-file", type=Path, default=root / "katsuyu.token"
    )
    install_parser.add_argument("--ca-file", type=Path, required=True)
    install_parser.add_argument("--workspace", type=Path, default=root / "workspace")
    install_parser.add_argument("--log-file", type=Path, default=root / "katsuyu.log")
    install_parser.add_argument(
        "--status-file", type=Path, default=root / "status.json"
    )
    install_parser.add_argument("--age-binary", type=Path, default=Path("age.exe"))
    install_parser.add_argument(
        "--worker-id", default=os.environ.get("COMPUTERNAME", "bubule")
    )
    install_parser.add_argument("--without-tray", action="store_true")
    install_parser.add_argument("--worker-executable", type=Path)
    install_parser.add_argument("--tray-executable", type=Path)
    install_parser.set_defaults(handler=install)
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.set_defaults(handler=uninstall)
    return parser


def main() -> None:
    if os.name != "nt":
        raise SystemExit("This command is only supported on Windows")
    arguments = build_parser().parse_args()
    try:
        arguments.handler(arguments)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()

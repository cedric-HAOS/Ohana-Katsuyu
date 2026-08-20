"""Single-window Windows installer and classic uninstaller for Katsuyu."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from tkinter import Button, Entry, Label, StringVar, Tk, messagebox

from ohana_katsuyu import __version__
from ohana_katsuyu.handlers import HANDLER_TYPES
from ohana_katsuyu.pairing import (
    PairingClient,
    default_worker_id,
    format_fingerprint,
    normalize_agent_url,
)
from ohana_katsuyu.updates import version_key
from ohana_katsuyu.windows import build_parser as build_windows_parser
from ohana_katsuyu.windows import install as install_windows_startup
from ohana_katsuyu.windows import uninstall as uninstall_windows_startup
from ohana_katsuyu.worker import AgentClient

PRODUCT_NAME = "Ohana Katsuyu"
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Ohana-Katsuyu"


@dataclass(frozen=True, slots=True)
class ExistingInstallation:
    base_url: str
    worker_id: str
    token: str
    ca_file: Path | None = None


def program_root() -> Path:
    base = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    return base / "Ohana" / "Katsuyu"


def data_root() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Ohana" / "Katsuyu"


def payload_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "payload"
    return Path(__file__).resolve().parents[1] / "dist" / "payload"


def require_administrator() -> None:
    if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError(
            "L’installation doit être exécutée en tant qu’administrateur."
        )


def _run_checked(command: list[str]) -> None:
    completed = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"La configuration Windows a échoué : {detail}")


def secure_paths(
    root: Path,
    token_file: Path,
    shared_files: list[Path],
    private_directories: list[Path],
) -> None:
    """Remove inherited ACLs and grant only the access each local component needs."""
    _run_checked(
        [
            "icacls.exe",
            str(root),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
            "*S-1-5-32-545:(OI)(CI)RX",
        ]
    )
    for directory in private_directories:
        _run_checked(
            [
                "icacls.exe",
                str(directory),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
            ]
        )
    _run_checked(
        [
            "icacls.exe",
            str(token_file),
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-18:F",
            "*S-1-5-32-544:F",
        ]
    )
    for path in shared_files:
        if path.exists():
            _run_checked(
                [
                    "icacls.exe",
                    str(path),
                    "/grant:r",
                    "*S-1-5-18:F",
                    "*S-1-5-32-544:F",
                    "*S-1-5-32-545:R",
                ]
            )


def register_uninstaller(uninstaller: Path) -> None:
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        values = {
            "DisplayName": PRODUCT_NAME,
            "DisplayVersion": __version__,
            "Publisher": "Ohana",
            "InstallLocation": str(program_root()),
            "DisplayIcon": str(program_root() / "KatsuyuTray.exe"),
            "UninstallString": subprocess.list2cmdline(
                [str(uninstaller), "--uninstall"]
            ),
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def installed_version() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "DisplayVersion")
    except (FileNotFoundError, OSError):
        return None
    return value if isinstance(value, str) else None


def read_existing_installation() -> ExistingInstallation | None:
    state_root = data_root()
    token_file = state_root / "katsuyu.token"
    config_file = state_root / "config.json"
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        value = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not token:
        return None
    base_url = value.get("base_url")
    worker_id = value.get("worker_id")
    configured_ca_file = value.get("ca_file")
    if not isinstance(base_url, str) or not isinstance(worker_id, str):
        return None
    try:
        normalized_url = normalize_agent_url(base_url)
    except ValueError:
        return None
    if not worker_id.strip():
        return None
    ca_file = (
        Path(configured_ca_file)
        if isinstance(configured_ca_file, str) and configured_ca_file.strip()
        else None
    )
    return ExistingInstallation(normalized_url, worker_id, token, ca_file)


def stop_running_components() -> None:
    for command in (
        ["schtasks.exe", "/End", "/TN", "Ohana-Katsuyu"],
        ["taskkill.exe", "/F", "/IM", "KatsuyuWorker.exe"],
        ["taskkill.exe", "/F", "/IM", "KatsuyuTray.exe"],
    ):
        subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        )


def replace_payload(
    source: Path,
    binary_root: Path,
    state_root: Path,
    required: list[str],
) -> Path:
    """Replace stopped executables and roll them back if a copy fails."""
    backup_root = state_root / "update-backup"
    if backup_root.exists():
        resolved_backup = backup_root.resolve()
        if not resolved_backup.is_relative_to(state_root.resolve()):
            raise RuntimeError("Le répertoire de sauvegarde sort de Katsuyu.")
        shutil.rmtree(backup_root)
    backup_root.mkdir(parents=True)
    destinations = [binary_root / name for name in required]
    uninstaller = binary_root / "KatsuyuUninstall.exe"
    destinations.append(uninstaller)
    for destination in destinations:
        if destination.is_file():
            shutil.copy2(destination, backup_root / destination.name)
    try:
        for name in required:
            shutil.copy2(source / name, binary_root / name)
        shutil.copy2(Path(sys.executable), uninstaller)
    except Exception:  # noqa: BLE001
        for destination in destinations:
            destination.unlink(missing_ok=True)
        for backup in backup_root.iterdir():
            shutil.copy2(backup, binary_root / backup.name)
        raise
    return backup_root


def restore_payload(
    backup_root: Path,
    binary_root: Path,
    required: list[str],
) -> None:
    """Restore exactly the executable set saved before an upgrade attempt."""
    destinations = [binary_root / name for name in required]
    destinations.append(binary_root / "KatsuyuUninstall.exe")
    for destination in destinations:
        destination.unlink(missing_ok=True)
    for backup in backup_root.iterdir():
        shutil.copy2(backup, binary_root / backup.name)
    shutil.rmtree(backup_root)


def install(
    agent_address: str,
    on_code: Callable[[str, str], None] | None = None,
) -> None:
    require_administrator()
    current_installed_version = installed_version()
    if current_installed_version is not None:
        try:
            if version_key(current_installed_version) > version_key(__version__):
                raise RuntimeError(
                    "Une version plus récente de Katsuyu est déjà installée."
                )
        except ValueError as error:
            raise RuntimeError("La version Katsuyu installée est invalide.") from error
    existing = read_existing_installation()
    secure_existing = (
        existing is not None
        and existing.base_url.lower().startswith("https://")
        and existing.ca_file is not None
        and existing.ca_file.is_file()
    )
    pairing_client = None
    if not secure_existing:
        base_url = normalize_agent_url(agent_address)
        worker_id = default_worker_id()
        pairing_client = PairingClient.bootstrap(base_url)
        session = pairing_client.create(worker_id, sorted(HANDLER_TYPES))
        if on_code is not None:
            on_code(
                session.verification_code,
                format_fingerprint(session.tls_ca_sha256),
            )
        token = pairing_client.wait_for_approval(session)
    else:
        assert existing is not None
        base_url = existing.base_url
        worker_id = existing.worker_id
        token = existing.token

    source = payload_root()
    required = ["KatsuyuWorker.exe", "KatsuyuTray.exe", "age.exe", "age-LICENSE.txt"]
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        raise RuntimeError(
            "Le programme d’installation est incomplet : " + ", ".join(missing)
        )
    binary_root = program_root()
    state_root = data_root()
    logs = state_root / "logs"
    workspace = state_root / "workspace"
    for directory in (binary_root, logs, workspace):
        directory.mkdir(parents=True, exist_ok=True)

    token_file = state_root / "katsuyu.token"
    token_file.write_text(token, encoding="utf-8")
    ca_file = state_root / "agent-ca.pem"
    if pairing_client is not None:
        ca_file.write_text(pairing_client.trust.ca_certificate_pem, encoding="ascii")
    elif existing is not None and existing.ca_file != ca_file:
        assert existing.ca_file is not None
        shutil.copy2(existing.ca_file, ca_file)
    config_file = state_root / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "worker_id": worker_id,
                "ca_file": str(ca_file),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    status_file = state_root / "status.json"
    log_file = logs / "katsuyu.log"
    if not status_file.exists():
        status_file.write_text("{}", encoding="utf-8")
    log_file.touch(exist_ok=True)
    secure_paths(
        state_root,
        token_file,
        [ca_file, config_file, status_file, log_file],
        [workspace],
    )

    AgentClient(base_url, token, ca_certificate_file=ca_file).register(
        {
            "protocol_version": 1,
            "worker_id": worker_id,
            "capabilities": sorted(HANDLER_TYPES),
            "platform": "Windows",
            "worker_version": __version__,
        }
    )
    if existing is not None:
        stop_running_components()
    backup_root = replace_payload(source, binary_root, state_root, required)
    uninstaller = binary_root / "KatsuyuUninstall.exe"
    startup = build_windows_parser().parse_args(
        [
            "install",
            "--base-url",
            base_url,
            "--token-file",
            str(token_file),
            "--ca-file",
            str(ca_file),
            "--workspace",
            str(workspace),
            "--log-file",
            str(log_file),
            "--status-file",
            str(status_file),
            "--age-binary",
            str(binary_root / "age.exe"),
            "--worker-id",
            worker_id,
            "--worker-executable",
            str(binary_root / "KatsuyuWorker.exe"),
            "--tray-executable",
            str(binary_root / "KatsuyuTray.exe"),
        ]
    )
    try:
        install_windows_startup(startup)
        register_uninstaller(uninstaller)
        _run_checked(["schtasks.exe", "/Run", "/TN", "Ohana-Katsuyu"])
        subprocess.Popen(  # noqa: S603
            [str(binary_root / "KatsuyuTray.exe")], close_fds=True
        )
    except Exception:  # noqa: BLE001
        restore_payload(backup_root, binary_root, required)
        if existing is not None:
            subprocess.run(  # noqa: S603
                ["schtasks.exe", "/Run", "/TN", "Ohana-Katsuyu"],
                check=False,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
            )
            subprocess.Popen(  # noqa: S603
                [str(binary_root / "KatsuyuTray.exe")], close_fds=True
            )
        raise
    else:
        shutil.rmtree(backup_root)


def uninstall() -> str:
    require_administrator()
    uninstall_windows_startup(build_windows_parser().parse_args(["uninstall"]))
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY)
    except FileNotFoundError:
        pass
    subprocess.run(  # noqa: S603
        ["taskkill.exe", "/F", "/IM", "KatsuyuTray.exe"],
        check=False,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
    )
    binary_root = program_root().resolve()
    current_executable = Path(sys.executable).resolve()
    for child in binary_root.iterdir():
        resolved = child.resolve()
        if resolved == current_executable:
            continue
        if not resolved.is_relative_to(binary_root):
            raise RuntimeError(
                "Un chemin de désinstallation sort du répertoire Katsuyu."
            )
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    for name in ("katsuyu.token", "agent-ca.pem", "config.json", "status.json"):
        (data_root() / name).unlink(missing_ok=True)
    move_file_delay_until_reboot = 4
    ctypes.windll.kernel32.MoveFileExW(
        str(current_executable), None, move_file_delay_until_reboot
    )
    ctypes.windll.kernel32.MoveFileExW(
        str(binary_root), None, move_file_delay_until_reboot
    )
    return (
        "Katsuyu est arrêté et désenregistré. Les fichiers seront supprimés "
        "au prochain redémarrage de Windows. Les logs et le workspace sont conservés."
    )


class InstallerWindow:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("Installation d’Ohana Katsuyu")
        self.root.geometry("520x260")
        existing = read_existing_installation()
        address = (
            existing.base_url
            if existing is not None
            else "http://infra-01.ohana.lan:8765"
        )
        self.address = StringVar(value=address)
        self.status = StringVar(
            value=(
                "La configuration existante sera conservée."
                if existing is not None
                else "Saisissez uniquement l’adresse d’Ohana-Agent."
            )
        )
        Label(self.root, text="Adresse d’Ohana-Agent").pack(pady=(24, 4))
        self.entry = Entry(self.root, textvariable=self.address, width=55)
        self.entry.pack(pady=4)
        if existing is not None:
            self.entry.configure(state="disabled")
        self.code = Label(self.root, text="", font=("Segoe UI", 20, "bold"))
        self.code.pack(pady=(12, 4))
        self.fingerprint = Label(
            self.root,
            text="",
            font=("Consolas", 8),
            justify="center",
            wraplength=470,
        )
        self.fingerprint.pack(pady=(0, 8))
        Label(self.root, textvariable=self.status, wraplength=470).pack(pady=4)
        action = (
            "Mettre à jour Katsuyu" if existing is not None else "Installer Katsuyu"
        )
        self.button = Button(self.root, text=action, command=self.start)
        self.button.pack(pady=12)

    def start(self) -> None:
        self.button.configure(state="disabled")
        self.status.set("Connexion à Agent…")
        Thread(target=self._install, daemon=True).start()

    def _install(self) -> None:
        try:
            install(self.address.get(), self._show_code)
        except Exception as error:  # noqa: BLE001
            detail = str(error)
            self.root.after(0, lambda: self._failed(detail))
            return
        self.root.after(0, self._complete)

    def _show_code(self, value: str, fingerprint: str) -> None:
        self.root.after(0, lambda: self.code.configure(text=value))
        self.root.after(
            0,
            lambda: self.fingerprint.configure(text=f"SHA-256\n{fingerprint}"),
        )
        self.root.after(
            0,
            lambda: self.status.set(
                "Dans Vision > Workers Katsuyu, comparez le code et toute "
                "l’empreinte SHA-256, puis cliquez sur Autoriser."
            ),
        )

    def _failed(self, detail: str) -> None:
        self.button.configure(state="normal")
        self.status.set(detail)
        messagebox.showerror(PRODUCT_NAME, detail)

    def _complete(self) -> None:
        messagebox.showinfo(PRODUCT_NAME, "Katsuyu est installé, connecté et démarré.")
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ohana Katsuyu Setup")
    parser.add_argument("--uninstall", action="store_true")
    arguments = parser.parse_args()
    if arguments.uninstall:
        try:
            detail = uninstall()
        except RuntimeError as error:
            messagebox.showerror(PRODUCT_NAME, str(error))
        else:
            messagebox.showinfo(PRODUCT_NAME, detail)
        return
    InstallerWindow().run()


if __name__ == "__main__":
    main()

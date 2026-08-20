"""Strictly informative Windows notification-area UI for Katsuyu."""

from __future__ import annotations

import argparse
import base64
import io
import os
import subprocess
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pystray
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from ohana_katsuyu.icon_data import OFFICIAL_OHANA_ICON_BASE64
from ohana_katsuyu.status import LocalStatus, StatusStore


def official_icon() -> Image.Image:
    return (
        Image.open(io.BytesIO(base64.b64decode(OFFICIAL_OHANA_ICON_BASE64)))
        .convert("RGBA")
        .resize((64, 64), Image.Resampling.LANCZOS)
    )


def icon_variants() -> dict[str, list[Image.Image]]:
    normal = official_icon()
    gray = ImageOps.grayscale(normal).convert("RGBA")
    gray.putalpha(normal.getchannel("A"))
    error = normal.copy()
    draw = ImageDraw.Draw(error)
    draw.line((9, 9, 55, 55), fill="#D91E36", width=10)
    draw.line((9, 9, 55, 55), fill="white", width=3)
    dimmed = ImageEnhance.Brightness(normal).enhance(0.28)
    masks = []
    for polygon in (
        [(0, 0), (64, 0), (32, 32)],
        [(64, 0), (64, 64), (32, 32)],
        [(64, 64), (0, 64), (32, 32)],
        [(0, 64), (0, 0), (32, 32)],
    ):
        mask = Image.new("L", normal.size)
        ImageDraw.Draw(mask).polygon(polygon, fill=255)
        frame = dimmed.copy()
        frame.paste(normal, mask=mask)
        masks.append(frame)
    return {
        "connected": [normal],
        "running": masks,
        "error": [error],
        "stopped": [gray],
    }


def effective_state(status: LocalStatus) -> str:
    if status.state not in {"connected", "running", "error"}:
        return "stopped"
    try:
        updated_at = datetime.fromisoformat(status.updated_at)
    except ValueError:
        return "stopped"
    if datetime.now(UTC) - updated_at.astimezone(UTC) > timedelta(seconds=45):
        return "stopped"
    return status.state


def tooltip(status: LocalStatus) -> str:
    connection = status.last_connection_at or "jamais"
    job = status.current_job_type or "aucun"
    state = effective_state(status)
    if state == "stopped" and status.state in {"connected", "running", "error"}:
        state_label = "état périmé"
    else:
        state_label = {
            "connected": "connecté",
            "running": "job en cours",
            "error": "Agent inaccessible",
            "stopped": "arrêté",
        }[state]
    if status.update_state == "available" and status.latest_version:
        update = f"mise à jour {status.latest_version} disponible"
    elif status.update_state == "current":
        update = "à jour"
    elif status.update_state == "unavailable":
        update = "mise à jour non vérifiée"
    else:
        update = "version non vérifiée"
    return (
        f"Katsuyu {status.version} · {update} · {state_label} · "
        f"connexion {connection} · job {job}"
    )[:127]


def open_update(status: LocalStatus) -> None:
    if status.update_state == "available" and status.update_url:
        webbrowser.open(status.update_url)


def open_logs(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer.exe", str(path.parent)], close_fds=True)  # noqa: S603,S607


def run_tray(status_file: Path, log_file: Path) -> None:
    store = StatusStore(status_file)
    variants = icon_variants()
    stopped = Event()
    icon = pystray.Icon(
        "Ohana-Katsuyu",
        variants["stopped"][0],
        "Katsuyu arrêté",
        menu=pystray.Menu(
            pystray.MenuItem(
                "Afficher l’état",
                lambda _icon, _item: _icon.notify(
                    tooltip(store.read()), "Ohana Katsuyu"
                ),
            ),
            pystray.MenuItem(
                "Ouvrir les logs", lambda _icon, _item: open_logs(log_file)
            ),
            pystray.MenuItem(
                "Ouvrir la mise à jour",
                lambda _icon, _item: open_update(store.read()),
                visible=lambda _item: store.read().update_state == "available",
            ),
        ),
    )

    def refresh() -> None:
        frame = 0
        while not stopped.wait(0.4):
            status = store.read()
            state = effective_state(status)
            images = variants[state]
            icon.icon = images[frame % len(images)]
            icon.title = tooltip(status)
            frame += 1

    thread = Thread(target=refresh, name="katsuyu-tray-refresh", daemon=True)
    thread.start()
    try:
        icon.run()
    finally:
        stopped.set()
        thread.join(timeout=2)


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Katsuyu Tray est uniquement disponible sous Windows.")
    root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Ohana" / "Katsuyu"
    parser = argparse.ArgumentParser(description="Ohana Katsuyu notification area")
    parser.add_argument("--status-file", type=Path, default=root / "status.json")
    parser.add_argument("--log-file", type=Path, default=root / "logs" / "katsuyu.log")
    arguments = parser.parse_args()
    run_tray(arguments.status_file, arguments.log_file)


if __name__ == "__main__":
    main()

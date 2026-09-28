"""Automatic update of Katsuyu by its own worker, only when it is idle.

The worker runs as SYSTEM (Task Scheduler), so it can install without a UAC
prompt. It downloads KatsuyuSetup.exe from the official release, checks it
against the release's SHA256SUMS and starts it in a separate one-shot task:
the setup stops the worker, so it must not be one of its child processes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ohana_katsuyu import __version__
from ohana_katsuyu.status import LocalStatus, StatusStore
from ohana_katsuyu.updates import refresh_update_status, version_key

LOGGER = logging.getLogger(__name__)

REPOSITORY = "cedric-HAOS/Ohana-Katsuyu"
RELEASE_BY_TAG_API = (
    f"https://api.github.com/repos/{REPOSITORY}/releases/tags/v{{version}}"
)
DOWNLOAD_PREFIX = f"https://github.com/{REPOSITORY}/releases/download/v{{version}}/"
SETUP_ASSET = "KatsuyuSetup.exe"
SUMS_ASSET = "SHA256SUMS"
UPDATE_TASK_NAME = "Ohana-Katsuyu-Update"
MAX_METADATA_BYTES = 512 * 1024
MAX_SETUP_BYTES = 512 * 1024 * 1024
RETRY_AFTER = timedelta(hours=24)
_SUM_LINE = re.compile(r"^([0-9a-f]{64}) [ *](\S+)$")


class UpdateError(RuntimeError):
    """The update could not be downloaded, verified or started."""


def _read(url: str, limit: int, *, timeout: float = 30.0) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Ohana-Katsuyu/{__version__}",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(limit + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"téléchargement impossible : {error}") from error
    if len(body) > limit:
        raise UpdateError("réponse plus volumineuse que prévu")
    return body


def release_assets(
    version: str, *, read: Callable[[str, int], bytes] = _read
) -> dict[str, str]:
    """Return the download URL of each asset of the official release tag."""
    version_key(version)
    try:
        payload: Any = json.loads(
            read(RELEASE_BY_TAG_API.format(version=version), MAX_METADATA_BYTES)
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("description de release illisible") from error
    if (
        not isinstance(payload, dict)
        or payload.get("draft")
        or payload.get("prerelease")
    ):
        raise UpdateError("la release n'est pas une version stable publiée")
    prefix = DOWNLOAD_PREFIX.format(version=version)
    assets: dict[str, str] = {}
    for asset in payload.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name, url = asset.get("name"), asset.get("browser_download_url")
        if isinstance(name, str) and url == f"{prefix}{name}":
            assets[name] = url
    missing = {SETUP_ASSET, SUMS_ASSET} - set(assets)
    if missing:
        raise UpdateError(
            "assets absents de la release : " + ", ".join(sorted(missing))
        )
    return assets


def parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = _SUM_LINE.match(line.strip())
        if match:
            sums[match.group(2).lstrip("*")] = match.group(1)
    return sums


def stage_update(
    version: str,
    directory: Path,
    *,
    read: Callable[[str, int], bytes] = _read,
) -> Path:
    """Download the setup of ``version`` and keep it only if its SHA-256 matches."""
    assets = release_assets(version, read=read)
    expected = parse_sums(
        read(assets[SUMS_ASSET], MAX_METADATA_BYTES).decode("utf-8", "replace")
    ).get(SETUP_ASSET)
    if expected is None:
        raise UpdateError("SHA256SUMS ne mentionne pas KatsuyuSetup.exe")
    body = read(assets[SETUP_ASSET], MAX_SETUP_BYTES)
    if hashlib.sha256(body).hexdigest() != expected:
        raise UpdateError("empreinte SHA-256 de KatsuyuSetup.exe invalide")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"KatsuyuSetup-{version}.exe"
    partial = destination.with_suffix(".part")
    partial.write_bytes(body)
    partial.replace(destination)
    return destination


def _schtasks(arguments: list[str]) -> None:
    completed = subprocess.run(  # noqa: S603
        ["schtasks.exe", *arguments],
        check=False,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise UpdateError(f"Planificateur de tâches : {detail[:300]}")


def launch_update(
    setup: Path, *, schtasks: Callable[[list[str]], None] = _schtasks
) -> None:
    """Start the setup in its own SYSTEM task so it survives the worker's stop."""
    command = subprocess.list2cmdline([str(setup), "--update-existing", "--background"])
    # A past start time only warns; the task is started explicitly below.
    schtasks(
        [
            "/Create",
            "/F",
            "/SC",
            "ONCE",
            "/ST",
            "00:00",
            "/RU",
            "SYSTEM",
            "/RL",
            "HIGHEST",
            "/TN",
            UPDATE_TASK_NAME,
            "/TR",
            command,
        ]
    )
    schtasks(["/Run", "/TN", UPDATE_TASK_NAME])


def remove_update_leftovers(
    directory: Path,
    binary_root: Path | None,
    *,
    schtasks: Callable[[list[str]], None] = _schtasks,
) -> None:
    """After a restart: drop the one-shot task, staged setups and retired files."""
    try:
        schtasks(["/Delete", "/F", "/TN", UPDATE_TASK_NAME])
    except UpdateError:
        pass
    patterns = [(directory, "KatsuyuSetup-*")]
    if binary_root is not None:
        patterns.append((binary_root, "*.old-*"))
    for root, pattern in patterns:
        if not root.is_dir():
            continue
        for leftover in root.glob(pattern):
            try:
                leftover.unlink()
            except OSError:
                pass  # Still running (the previous tray): next start removes it.


@dataclass(slots=True)
class AutoUpdater:
    store: StatusStore
    directory: Path
    enabled: bool = True
    check: Callable[..., LocalStatus] = refresh_update_status
    stage: Callable[[str, Path], Path] = stage_update
    launch: Callable[[Path], None] = launch_update
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def settle_previous_attempt(self) -> None:
        """A setup that ends with this same version running did not update."""
        status = self.store.read()
        if (
            status.update_state == "installing"
            and status.update_attempted_version
            and status.update_attempted_version != __version__
        ):
            self.store.write(
                state=status.state,
                update_state="failed",
                update_error="installation interrompue ou annulée "
                "(voir logs\\katsuyu-update.log)",
            )

    def maybe_update(self) -> bool:
        """Install a newer release now if allowed; True when the setup started."""
        status = self.check(self.store)
        if not self.enabled or status.update_state != "available":
            return False
        version = status.latest_version
        if not version or version_key(version) <= version_key(__version__):
            return False
        now = self.clock()
        if status.update_attempted_version == version and status.update_attempted_at:
            try:
                attempted = datetime.fromisoformat(status.update_attempted_at)
            except ValueError:
                attempted = None
            if attempted is not None and now - attempted < RETRY_AFTER:
                return False
        self.store.write(
            state=status.state,
            update_state="installing",
            update_error=None,
            update_attempted_version=version,
            update_attempted_at=now.isoformat(),
        )
        LOGGER.info("Katsuyu %s: installing %s automatically", __version__, version)
        try:
            self.launch(self.stage(version, self.directory))
        except (UpdateError, OSError, ValueError) as error:
            LOGGER.warning("Katsuyu automatic update failed: %s", error)
            self.store.write(
                state=self.store.read().state,
                update_state="failed",
                update_error=str(error)[:300],
            )
            return False
        return True

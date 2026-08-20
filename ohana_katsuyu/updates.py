"""Bounded, informational discovery of stable Ohana-Katsuyu releases."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ohana_katsuyu import __version__
from ohana_katsuyu.status import LocalStatus, StatusStore

LOGGER = logging.getLogger(__name__)

LATEST_RELEASE_API = (
    "https://api.github.com/repos/cedric-HAOS/Ohana-Katsuyu/releases/latest"
)
RELEASE_PAGE_PREFIX = "https://github.com/cedric-HAOS/Ohana-Katsuyu/releases/tag/"
CHECK_INTERVAL = timedelta(hours=24)
MAX_RESPONSE_BYTES = 64 * 1024
VERSION_PATTERN = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class UpdateCheckError(RuntimeError):
    """The update source could not be read or did not respect its contract."""


@dataclass(frozen=True, slots=True)
class StableRelease:
    version: str
    url: str


def version_key(value: str) -> tuple[int, int, int]:
    """Parse the stable semantic-version subset used by Katsuyu releases."""
    match = VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"Unsupported Katsuyu version: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def read_latest_release(*, timeout_seconds: float = 5.0) -> StableRelease:
    """Read one small stable-release document without downloading an executable."""
    request = Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Ohana-Katsuyu/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise UpdateCheckError(f"release source unavailable: {error}") from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise UpdateCheckError("release response exceeds the bounded size")
    try:
        payload: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateCheckError("release response is not valid JSON") from error
    if not isinstance(payload, dict):
        raise UpdateCheckError("release response is not an object")
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise UpdateCheckError("release response is not a published stable release")
    tag = payload.get("tag_name")
    url = payload.get("html_url")
    if not isinstance(tag, str) or not isinstance(url, str):
        raise UpdateCheckError("release response omits tag_name or html_url")
    try:
        version_key(tag)
    except ValueError as error:
        raise UpdateCheckError(str(error)) from error
    if url != f"{RELEASE_PAGE_PREFIX}{tag}":
        raise UpdateCheckError("release URL does not match the official Katsuyu tag")
    return StableRelease(version=tag.removeprefix("v"), url=url)


def check_is_due(
    status: LocalStatus,
    *,
    now: datetime,
    interval: timedelta = CHECK_INTERVAL,
) -> bool:
    if status.update_checked_at is None:
        return True
    try:
        checked_at = datetime.fromisoformat(status.update_checked_at)
    except ValueError:
        return True
    if checked_at.tzinfo is None:
        return True
    return now - checked_at.astimezone(UTC) >= interval


def refresh_update_status(
    store: StatusStore,
    *,
    now: datetime | None = None,
    timeout_seconds: float = 5.0,
) -> LocalStatus:
    """Refresh the cached update status at most once per interval."""
    checked_at = now or datetime.now(UTC)
    previous = store.read()
    if not check_is_due(previous, now=checked_at):
        return previous
    try:
        release = read_latest_release(timeout_seconds=timeout_seconds)
        update_state = (
            "available"
            if version_key(release.version) > version_key(__version__)
            else "current"
        )
        return store.write(
            state=previous.state,
            update_state=update_state,
            latest_version=release.version,
            update_checked_at=checked_at.isoformat(),
            update_url=release.url,
        )
    except (UpdateCheckError, ValueError) as error:
        LOGGER.warning("Katsuyu update check failed: %s", error)
        return store.write(
            state=previous.state,
            update_state="unavailable",
            update_checked_at=checked_at.isoformat(),
        )

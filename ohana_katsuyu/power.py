"""Bounded local power operations for Katsuyu."""

from __future__ import annotations

import ctypes
import os
import subprocess
from dataclasses import dataclass
from typing import Any

# WTS_CONNECTSTATE_CLASS values of a session someone is (or was) working in.
_SESSION_STATES_IN_USE = {
    0: "active",
    1: "connected",
    4: "disconnected",
}
_WTS_USER_NAME = 5


@dataclass(frozen=True, slots=True)
class ShutdownVeto:
    """Why Katsuyu keeps the PC on although Agent granted the shutdown."""

    reason: str
    sessions: int | None = None


def request_system_shutdown() -> None:
    """Ask Windows to shut down after Agent marked the final job complete."""
    if os.name != "nt":
        raise RuntimeError("automatic shutdown is only supported on Windows")
    subprocess.run(  # noqa: S603
        ["shutdown.exe", "/s", "/t", "0"],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
    )


def signed_in_sessions() -> int:
    """Count the Windows sessions a user is signed in to (console or remote).

    The worker runs as SYSTEM in session 0, so the services session is skipped.
    A locked screen still counts: the person may have unsaved work. The
    sign-in screen (no user name) does not.
    """
    if os.name != "nt":
        raise RuntimeError("session detection is only supported on Windows")
    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)  # type: ignore[attr-defined]

    class SessionInfo(ctypes.Structure):
        _fields_ = [  # noqa: RUF012
            ("SessionId", ctypes.c_ulong),
            ("WinStationName", ctypes.c_wchar_p),
            ("State", ctypes.c_int),
        ]

    wtsapi32.WTSEnumerateSessionsW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.POINTER(SessionInfo)),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    wtsapi32.WTSQuerySessionInformationW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]

    sessions = ctypes.POINTER(SessionInfo)()
    count = ctypes.c_ulong(0)
    if not wtsapi32.WTSEnumerateSessionsW(
        None, 0, 1, ctypes.byref(sessions), ctypes.byref(count)
    ):
        raise OSError(ctypes.get_last_error(), "WTSEnumerateSessions failed")
    signed_in = 0
    try:
        for index in range(count.value):
            info = sessions[index]
            if info.SessionId == 0 or info.State not in _SESSION_STATES_IN_USE:
                continue
            if _session_user_name(wtsapi32, info.SessionId):
                signed_in += 1
    finally:
        wtsapi32.WTSFreeMemory(sessions)
    return signed_in


def _session_user_name(wtsapi32: Any, session_id: int) -> str:
    buffer = ctypes.c_void_p()
    size = ctypes.c_ulong(0)
    if not wtsapi32.WTSQuerySessionInformationW(
        None, session_id, _WTS_USER_NAME, ctypes.byref(buffer), ctypes.byref(size)
    ):
        raise OSError(ctypes.get_last_error(), "WTSQuerySessionInformation failed")
    try:
        return ctypes.wstring_at(buffer) if buffer.value else ""
    finally:
        wtsapi32.WTSFreeMemory(buffer)


def session_shutdown_veto() -> ShutdownVeto | None:
    """Refuse to power off a PC someone is signed in to.

    When the sessions cannot be read the PC stays on: an idle PC costs a little
    power, an unexpected shutdown costs someone's work.
    """
    try:
        sessions = signed_in_sessions()
    except (OSError, RuntimeError):
        return ShutdownVeto("session_check_failed")
    if sessions > 0:
        return ShutdownVeto("interactive_session", sessions)
    return None

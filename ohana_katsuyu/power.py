"""Bounded local power operations for Katsuyu."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Any

LOGGER = logging.getLogger(__name__)

# WTS_CONNECTSTATE_CLASS values of a session someone is (or was) working in.
_SESSION_STATES_IN_USE = {
    0: "active",
    1: "connected",
    4: "disconnected",
}
# States of a session still attached to the console or a live connection.
_ATTACHED_STATES = {0, 1}
_WTS_SESSION_INFO_EX = 25
_WTS_SESSIONSTATE_LOCK = 0
_WTS_SESSIONSTATE_UNLOCK = 1
_FILETIME_TICKS_PER_SECOND = 10_000_000

# A locked screen right after the sign-in is how Windows reopens a session on
# its own; nobody has used it. Anything unlocked for longer than this proves a
# person was there. Windows locks an automatic sign-in within a couple of seconds.
UNLOCKED_PROOF_SECONDS = 10.0
# A session first seen locked this soon after its sign-in was never seen
# unlocked by this worker: it started with the machine. Older ones are unknown.
FIRST_SIGHT_WINDOW_SECONDS = 120.0
SAMPLE_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ShutdownVeto:
    """Why Katsuyu keeps the PC on although Agent granted the shutdown."""

    reason: str
    sessions: int | None = None


@dataclass(frozen=True, slots=True)
class WindowsSession:
    """One signed-in Windows session, as Windows reports it."""

    session_id: int
    state: int
    user: str
    logon_time: int
    current_time: int
    locked: bool | None

    @property
    def age_seconds(self) -> float:
        return (self.current_time - self.logon_time) / _FILETIME_TICKS_PER_SECOND


def request_system_shutdown() -> None:
    """Ask Windows to shut down after Agent marked the final job complete."""
    if os.name != "nt":
        raise RuntimeError("automatic shutdown is only supported on Windows")
    subprocess.run(  # noqa: S603
        ["shutdown.exe", "/s", "/t", "0"],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
    )


class _SessionInfoLevel1(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ("SessionId", ctypes.c_ulong),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", ctypes.c_long),
        ("WinStationName", ctypes.c_wchar * 33),
        ("UserName", ctypes.c_wchar * 21),
        ("DomainName", ctypes.c_wchar * 18),
        ("LogonTime", ctypes.c_int64),
        ("ConnectTime", ctypes.c_int64),
        ("DisconnectTime", ctypes.c_int64),
        ("LastInputTime", ctypes.c_int64),
        ("CurrentTime", ctypes.c_int64),
        ("IncomingBytes", ctypes.c_ulong),
        ("OutgoingBytes", ctypes.c_ulong),
        ("IncomingFrames", ctypes.c_ulong),
        ("OutgoingFrames", ctypes.c_ulong),
        ("IncomingCompressedBytes", ctypes.c_ulong),
        ("OutgoingCompressedBytes", ctypes.c_ulong),
    ]


class _SessionInfoEx(ctypes.Structure):
    _fields_ = [("Level", ctypes.c_ulong), ("Data", _SessionInfoLevel1)]  # noqa: RUF012


def read_sessions() -> list[WindowsSession]:
    """List the Windows sessions a user is signed in to (console or remote).

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
    found: list[WindowsSession] = []
    try:
        for index in range(count.value):
            info = sessions[index]
            if info.SessionId == 0 or info.State not in _SESSION_STATES_IN_USE:
                continue
            session = _query_session(wtsapi32, info.SessionId, info.State)
            if session.user:
                found.append(session)
    finally:
        wtsapi32.WTSFreeMemory(sessions)
    return found


def _query_session(wtsapi32: Any, session_id: int, state: int) -> WindowsSession:
    buffer = ctypes.c_void_p()
    size = ctypes.c_ulong(0)
    if not wtsapi32.WTSQuerySessionInformationW(
        None, session_id, _WTS_SESSION_INFO_EX, ctypes.byref(buffer), ctypes.byref(size)
    ):
        raise OSError(ctypes.get_last_error(), "WTSQuerySessionInformation failed")
    try:
        if size.value < ctypes.sizeof(_SessionInfoEx):
            raise OSError(0, "unexpected WTSSessionInfoEx size")
        data = _SessionInfoEx.from_address(buffer.value).Data
        if data.SessionFlags == _WTS_SESSIONSTATE_LOCK:
            locked: bool | None = True
        elif data.SessionFlags == _WTS_SESSIONSTATE_UNLOCK:
            locked = False
        else:
            locked = None
        return WindowsSession(
            session_id=session_id,
            state=state,
            user=data.UserName,
            logon_time=data.LogonTime,
            current_time=data.CurrentTime,
            locked=locked,
        )
    finally:
        wtsapi32.WTSFreeMemory(buffer)


class SessionWatch:
    """Tell a session someone used from one Windows reopened by itself.

    Windows can sign the user in again at boot and lock the screen at once. That
    session holds no work, yet it must not keep an idle worker PC on forever. A
    session counts as untouched only while it has always been seen locked, from
    its first sight shortly after it opened. Once seen unlocked (beyond the
    couple of seconds Windows needs to lock an automatic sign-in), or when its
    history is unknown, it counts as used: an unexpected shutdown costs someone's
    work, an idle PC costs a little power.
    """

    def __init__(self) -> None:
        self._touched: dict[tuple[int, int], bool] = {}
        self._lock = Lock()

    def observe(self, sessions: list[WindowsSession]) -> None:
        with self._lock:
            live = {(item.session_id, item.logon_time) for item in sessions}
            for key in set(self._touched) - live:
                del self._touched[key]
            for session in sessions:
                key = (session.session_id, session.logon_time)
                known = self._touched.get(key)
                if known is True:
                    continue
                if known is None:
                    self._touched[key] = self._first_sight_touched(session)
                elif self._proves_use(session):
                    self._touched[key] = True

    @staticmethod
    def _first_sight_touched(session: WindowsSession) -> bool:
        if session.state not in _ATTACHED_STATES:
            # A disconnected session is one somebody left with work in it.
            return True
        if session.locked is True:
            # Locked and freshly opened: never seen unlocked. An older one has
            # a history this worker did not witness.
            return session.age_seconds > FIRST_SIGHT_WINDOW_SECONDS
        # Unlocked or unreadable: proof of use only once Windows had time to
        # lock an automatic sign-in.
        return session.age_seconds >= UNLOCKED_PROOF_SECONDS

    @staticmethod
    def _proves_use(session: WindowsSession) -> bool:
        if session.state not in _ATTACHED_STATES:
            return True
        return session.locked is not True and (
            session.age_seconds >= UNLOCKED_PROOF_SECONDS
        )

    def untouched(self, session: WindowsSession) -> bool:
        with self._lock:
            return (
                self._touched.get((session.session_id, session.logon_time), True)
                is False
            )


SESSION_WATCH = SessionWatch()


def used_sessions() -> tuple[int, int]:
    """Return ``(sessions in use, untouched locked sessions ignored)``."""
    sessions = read_sessions()
    SESSION_WATCH.observe(sessions)
    ignored = sum(1 for item in sessions if SESSION_WATCH.untouched(item))
    return len(sessions) - ignored, ignored


def signed_in_sessions() -> int:
    """Count the Windows sessions someone may be working in."""
    return used_sessions()[0]


def start_session_watch(stop: Event | None = None) -> Thread:
    """Sample the sessions in the background so their history is known."""
    stop = stop or Event()

    def sample() -> None:
        while not stop.wait(SAMPLE_SECONDS):
            try:
                SESSION_WATCH.observe(read_sessions())
            except (OSError, RuntimeError):
                LOGGER.debug("Session sampling failed", exc_info=True)

    thread = Thread(target=sample, name="katsuyu-session-watch", daemon=True)
    thread.start()
    return thread


def session_shutdown_veto() -> ShutdownVeto | None:
    """Refuse to power off a PC someone is signed in to.

    A locked session Windows reopened by itself and nobody touched does not
    count. When the sessions cannot be read the PC stays on: an idle PC costs a
    little power, an unexpected shutdown costs someone's work.
    """
    try:
        sessions, ignored = used_sessions()
    except (OSError, RuntimeError):
        return ShutdownVeto("session_check_failed")
    if ignored:
        LOGGER.info("Ignoring %d locked session(s) nobody has used", ignored)
    if sessions > 0:
        return ShutdownVeto("interactive_session", sessions)
    return None

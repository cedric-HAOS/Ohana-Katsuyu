"""Shutdown veto: Katsuyu never powers off a PC someone is signed in to."""

from __future__ import annotations

import os

import pytest

from ohana_katsuyu import power
from ohana_katsuyu.power import SessionWatch, WindowsSession

SECOND = 10_000_000
BOOT = 1_000_000_000 * SECOND


def session(
    *,
    age: float,
    locked: bool | None,
    state: int = 0,
    session_id: int = 1,
    logon: int = BOOT,
) -> WindowsSession:
    return WindowsSession(
        session_id=session_id,
        state=state,
        user="cedri",
        logon_time=logon,
        current_time=logon + int(age * SECOND),
        locked=locked,
    )


def test_signed_in_session_vetoes_the_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(power, "used_sessions", lambda: (2, 0))

    assert power.session_shutdown_veto() == power.ShutdownVeto("interactive_session", 2)


def test_no_signed_in_session_allows_the_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(power, "used_sessions", lambda: (0, 0))

    assert power.session_shutdown_veto() is None


def test_only_an_untouched_locked_session_allows_the_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(power, "used_sessions", lambda: (0, 1))

    assert power.session_shutdown_veto() is None


@pytest.mark.parametrize("error", [OSError(5, "denied"), RuntimeError("no wts")])
def test_unreadable_sessions_keep_the_pc_on(monkeypatch, error) -> None:
    def broken() -> tuple[int, int]:
        raise error

    monkeypatch.setattr(power, "used_sessions", broken)

    assert power.session_shutdown_veto() == power.ShutdownVeto("session_check_failed")


def test_a_session_windows_reopened_and_locked_by_itself_is_ignored() -> None:
    watch = SessionWatch()
    # Windows signs the user in at boot and locks the screen a second later.
    watch.observe([session(age=1, locked=False)])
    watch.observe([session(age=4, locked=True)])
    watch.observe([session(age=300, locked=True)])

    assert watch.untouched(session(age=300, locked=True))


def test_a_session_seen_unlocked_after_the_first_seconds_counts_forever() -> None:
    watch = SessionWatch()
    watch.observe([session(age=4, locked=True)])
    assert watch.untouched(session(age=4, locked=True))

    watch.observe([session(age=200, locked=False)])
    # The person locked the screen again and left: there may be unsaved work.
    watch.observe([session(age=400, locked=True)])

    assert not watch.untouched(session(age=400, locked=True))


def test_a_session_of_unknown_history_counts_as_used() -> None:
    watch = SessionWatch()
    # Worker restarted while the session was already old and locked.
    watch.observe([session(age=3600, locked=True)])

    assert not watch.untouched(session(age=3600, locked=True))


def test_an_unlocked_or_unreadable_session_counts_as_used() -> None:
    watch = SessionWatch()
    watch.observe([session(age=600, locked=False, session_id=1)])
    watch.observe([session(age=600, locked=None, session_id=2)])

    assert not watch.untouched(session(age=600, locked=False, session_id=1))
    assert not watch.untouched(session(age=600, locked=None, session_id=2))


def test_a_disconnected_session_always_counts() -> None:
    watch = SessionWatch()
    watch.observe([session(age=5, locked=True, state=4)])

    assert not watch.untouched(session(age=5, locked=True, state=4))


def test_a_new_sign_in_is_not_confused_with_the_previous_one() -> None:
    watch = SessionWatch()
    watch.observe([session(age=200, locked=False)])
    later = BOOT + 3600 * SECOND
    watch.observe([session(age=3, locked=True, logon=later)])

    assert watch.untouched(session(age=3, locked=True, logon=later))
    assert not watch.untouched(session(age=200, locked=False))


def test_a_signed_out_session_is_forgotten() -> None:
    watch = SessionWatch()
    watch.observe([session(age=4, locked=True)])
    watch.observe([])

    assert not watch.untouched(session(age=4, locked=True))


@pytest.mark.skipif(os.name != "nt", reason="Windows terminal services only")
def test_the_real_session_query_answers_with_a_count() -> None:
    assert power.signed_in_sessions() >= 0
    assert all(item.user for item in power.read_sessions())

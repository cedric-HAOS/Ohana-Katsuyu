"""Shutdown veto: Katsuyu never powers off a PC someone is signed in to."""

from __future__ import annotations

import os

import pytest

from ohana_katsuyu import power


def test_signed_in_session_vetoes_the_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(power, "signed_in_sessions", lambda: 2)

    assert power.session_shutdown_veto() == power.ShutdownVeto("interactive_session", 2)


def test_no_signed_in_session_allows_the_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(power, "signed_in_sessions", lambda: 0)

    assert power.session_shutdown_veto() is None


@pytest.mark.parametrize("error", [OSError(5, "denied"), RuntimeError("no wts")])
def test_unreadable_sessions_keep_the_pc_on(monkeypatch, error) -> None:
    def broken() -> int:
        raise error

    monkeypatch.setattr(power, "signed_in_sessions", broken)

    assert power.session_shutdown_veto() == power.ShutdownVeto("session_check_failed")


@pytest.mark.skipif(os.name != "nt", reason="Windows terminal services only")
def test_the_real_session_query_answers_with_a_count() -> None:
    assert power.signed_in_sessions() >= 0

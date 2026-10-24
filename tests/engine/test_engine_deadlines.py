from __future__ import annotations

from datetime import date

from charitycare.engine.deadlines import (
    compute_deadlines,
    federal_deadline,
    policy_window_deadline,
)
from charitycare.schemas import ApplicationWindow


def test_federal_240_days() -> None:
    assert federal_deadline(date(2026, 10, 10)) == date(2027, 6, 7)
    assert federal_deadline(date(2027, 7, 1)) == date(2028, 2, 26)  # crosses 29 Feb 2028
    assert federal_deadline(None) is None


def test_policy_window_anchors() -> None:
    w = ApplicationWindow(days=365, anchor="date_of_service")
    assert policy_window_deadline(w, first_statement_date=None, service_date=date(2026, 1, 1)) == date(2027, 1, 1)
    assert policy_window_deadline(w, first_statement_date=date(2026, 1, 1), service_date=None) is None
    assert policy_window_deadline(ApplicationWindow(days=None), first_statement_date=date(2026, 1, 1), service_date=None) is None


def test_shorter_policy_window_never_shortens_federal() -> None:
    dl = compute_deadlines(ApplicationWindow(days=120, anchor="first_post_discharge_statement"),
                           first_statement_date=date(2026, 10, 10), service_date=None, today=date(2026, 10, 24))
    assert dl.federal == date(2027, 6, 7) and dl.policy is None
    assert any("still applies" in n for n in dl.notes)


def test_passed_deadline_note() -> None:
    dl = compute_deadlines(None, first_statement_date=date(2025, 1, 1), service_date=None, today=date(2026, 10, 24))
    assert any("appears to have ended" in n for n in dl.notes)

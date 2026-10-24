"""Application-period arithmetic (26 CFR 1.501(r)-6).

The federal application period ends no earlier than 240 days after the first
post-discharge billing statement (26 CFR 1.501(r)-1(b)(3), -6(c)). A policy
may promise a longer window; it can never shorten the federal one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from charitycare.schemas import ApplicationWindow

FEDERAL_APPLICATION_DAYS = 240
FEDERAL_CITATION = "26 CFR 1.501(r)-6"


def federal_deadline(first_statement_date: date | None) -> date | None:
    """Last day of the federal 240-day application period, or None without a statement date."""
    if first_statement_date is None:
        return None
    return first_statement_date + timedelta(days=FEDERAL_APPLICATION_DAYS)


def policy_window_deadline(
    window: ApplicationWindow | None,
    *,
    first_statement_date: date | None,
    service_date: date | None,
) -> date | None:
    """End of the policy's own window when its anchor date is known, else None."""
    if window is None or window.days is None:
        return None
    anchor = {
        "first_post_discharge_statement": first_statement_date,
        "date_of_service": service_date,
    }.get(window.anchor)
    return anchor + timedelta(days=window.days) if anchor is not None else None


@dataclass(frozen=True)
class Deadlines:
    federal: date | None
    policy: date | None  # only when it is not earlier than the federal date
    notes: tuple[str, ...]


def compute_deadlines(
    window: ApplicationWindow | None,
    *,
    first_statement_date: date | None,
    service_date: date | None,
    today: date,
) -> Deadlines:
    """Federal and policy deadlines plus human-readable notes."""
    fed = federal_deadline(first_statement_date)
    pol = policy_window_deadline(window, first_statement_date=first_statement_date, service_date=service_date)
    notes: list[str] = []
    if fed is not None:
        notes.append(
            f"Federal application period: {FEDERAL_APPLICATION_DAYS} days from the first post-discharge "
            f"statement ({first_statement_date.isoformat()}) ends {fed.isoformat()} ({FEDERAL_CITATION})."  # type: ignore[union-attr]
        )
    if pol is not None and fed is not None and pol < fed:
        notes.append(
            f"The policy window ends {pol.isoformat()}, earlier than the federal minimum; "
            f"the federal date {fed.isoformat()} still applies."
        )
        pol = None
    elif pol is not None and window is not None:
        longer = fed is not None and pol > fed
        notes.append(
            f"Policy window: {window.days} days, ending {pol.isoformat()}"
            + (" (longer than the federal minimum)." if longer else ".")
        )
    elif window is not None and window.days is not None and window.anchor != "first_post_discharge_statement":
        notes.append(f"Policy window of {window.days} days not computed: its anchor date was not entered.")
    last = max(d for d in (fed, pol) if d is not None) if (fed or pol) else None
    if last is not None and today > last:
        notes.append(
            f"The application period appears to have ended on {last.isoformat()}; "
            "ask the hospital whether it will still accept an application."
        )
    return Deadlines(fed, pol, tuple(notes))

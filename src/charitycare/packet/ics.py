"""RFC 5545 ``.ics`` deadline reminders (no third-party dependency).

Events carry only the hospital name and the deadline; no household data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from charitycare.schemas import utcnow


@dataclass(frozen=True)
class DeadlineEvent:
    day: date
    summary: str
    description: str = ""
    alarms_days_before: tuple[int, ...] = (14, 3)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 section 3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts: list[str] = []
    cur = b""
    limit = 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > limit:
            parts.append(cur.decode("utf-8"))
            cur, limit = b"", 74  # continuation lines start with a space
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def build_ics(events: list[DeadlineEvent], *, stamp: datetime | None = None) -> bytes:
    """A VCALENDAR with one all-day VEVENT (plus VALARMs) per deadline."""
    ts = (stamp or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Charity Care Commons//Packet compiler//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for ev in events:
        uid = hashlib.sha256(f"{ev.day.isoformat()}|{ev.summary}".encode()).hexdigest()[:24]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@charitycarecommons.org",
            f"DTSTAMP:{ts}",
            f"DTSTART;VALUE=DATE:{ev.day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(ev.day + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_escape(ev.summary)}",
        ]
        if ev.description:
            lines.append(f"DESCRIPTION:{_escape(ev.description)}")
        lines.append("TRANSP:TRANSPARENT")
        for n in ev.alarms_days_before:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(ev.summary)}", f"TRIGGER:-P{n}D", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode("utf-8")


def deadline_events(hospital_name: str, federal: date | None, policy: date | None) -> list[DeadlineEvent]:
    """Events for the federal 240-day minimum and, when different, the policy deadline."""
    out: list[DeadlineEvent] = []
    if federal:
        out.append(
            DeadlineEvent(
                federal,
                f"Financial assistance application: federal minimum deadline ({hospital_name})",
                "At least 240 days after the first post-discharge billing statement (26 CFR 1.501(r)-1(b)(3)). "
                "Send the application before this date. The hospital decides; this is not legal advice.",
            )
        )
    if policy and policy != federal:
        out.append(
            DeadlineEvent(policy, f"Financial assistance application: policy deadline ({hospital_name})", "Deadline computed from the hospital's published policy.")
        )
    return out

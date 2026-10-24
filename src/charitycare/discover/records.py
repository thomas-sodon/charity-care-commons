"""Records-request queue: the last-resort fallback for unreachable policies (section 11).

Order of fallbacks for a facility whose policy cannot be fetched: Wayback copy
(provisional) -> hospital email -> one *batched* Texas Public Information Act
request covering every outstanding facility. The queue is a small JSON file
(``data/discover/records_requests.json``) because the registry has no table
for it; it holds facility ids and reasons only, never personal data.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from charitycare.config import get_settings
from charitycare.schemas import utcnow
from charitycare.wording import assert_safe_wording

State = Literal["queued", "emailed_hospital", "requested", "received", "closed"]
Reason = Literal["blocked", "dead_link", "not_found", "archived_only"]


@dataclass
class RecordsRequest:
    facility_id: str
    facility_name: str
    ein: str | None
    reason: Reason
    detail: str = ""
    state: State = "queued"
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())


class RecordsRequestQueue:
    """Persistent, idempotent queue keyed by facility id."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else get_settings().data_dir / "discover" / "records_requests.json"
        self._items: dict[str, RecordsRequest] = {}
        if self.path.exists():
            for d in json.loads(self.path.read_text(encoding="utf-8")):
                self._items[d["facility_id"]] = RecordsRequest(**d)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        items = [asdict(r) for r in sorted(self._items.values(), key=lambda r: r.facility_id)]
        self.path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")

    def enqueue(self, facility_id: str, facility_name: str, ein: str | None, reason: Reason, detail: str = "") -> RecordsRequest:
        """Add (or refresh the reason of) an open request; closed/received requests are left alone."""
        cur = self._items.get(facility_id)
        if cur is not None and cur.state in ("received", "closed"):
            return cur
        if cur is None:
            cur = RecordsRequest(facility_id, facility_name, ein, reason, detail)
        else:
            cur.reason, cur.detail, cur.updated_at = reason, detail, utcnow().isoformat()
        self._items[facility_id] = cur
        self.save()
        return cur

    def mark(self, facility_id: str, state: State) -> RecordsRequest:
        r = self._items[facility_id]
        r.state, r.updated_at = state, utcnow().isoformat()
        self.save()
        return r

    def resolve(self, facility_id: str) -> None:
        """A live policy was found: close any open request."""
        if facility_id in self._items and self._items[facility_id].state not in ("received", "closed"):
            self.mark(facility_id, "closed")

    def pending(self) -> list[RecordsRequest]:
        return [r for r in sorted(self._items.values(), key=lambda r: r.facility_id) if r.state in ("queued", "emailed_hospital")]

    def all(self) -> list[RecordsRequest]:
        return sorted(self._items.values(), key=lambda r: r.facility_id)


def render_batch_request(items: list[RecordsRequest], *, requester: str = "Charity Care Commons", today: date | None = None) -> str:
    """Text of one batched Public Information Act request covering all pending facilities."""
    today = today or date.today()
    lines = [
        f"Date: {today.isoformat()}",
        "Re: Request for public information under the Texas Public Information Act (Gov't Code ch. 552)",
        "",
        "Please provide copies of the most recent annual financial assistance (charity care) policy submissions,",
        "including any application form and plain language summary, filed by the following nonprofit hospitals:",
        "",
    ]
    for i, r in enumerate(items, start=1):
        lines.append(f"{i}. {r.facility_name} (registry id {r.facility_id}{', EIN ' + r.ein if r.ein else ''})")
    lines += [
        "",
        "Electronic copies are preferred. We are a public-interest research project; the documents will be used to",
        "publish cited, machine-readable summaries of each hospital's policy.",
        "",
        f"Sincerely,\n{requester}",
    ]
    text = "\n".join(lines)
    assert_safe_wording(text)
    return text

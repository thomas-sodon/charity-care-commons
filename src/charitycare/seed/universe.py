"""Facility universe: CMS ∪ Schedule H (∪ an optional external list) with stable facility IDs.

Each facility gets one stable id:

* ``tx-{ccn}`` when it matches a CMS row (CCN is the anchor),
* ``tx-irs-{ein}-{facility_num}`` for a Schedule H hospital with no CMS match,
* ``tx-ext-{slug}`` for an external-list row with no match.

Matching is deterministic and explainable. Every match records its method and
a confidence in [0, 1]:

==================  ==========  ===================================================
method              confidence  rule
==================  ==========  ===================================================
``ccn``             1.00        the external row carries the CCN
``exact_name_city`` 0.98        normalized names and cities are equal
``address_zip``     0.95        normalized street address and ZIP are equal
``fuzzy_name_city`` 0.70-0.94   same city, name similarity >= 0.80
``fuzzy_name``      0.50-0.69   different/missing city, name similarity >= 0.90
==================  ==========  ===================================================

Matches below :data:`REVIEW_THRESHOLD` are reported for human review
(``ReviewTask(kind="facility_mapping")``) rather than trusted silently.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from charitycare.schemas import Facility, ScheduleHFacility
from charitycare.seed.schedule_h import ParsedReturn, ScheduleHHospital

MatchMethod = Literal["ccn", "exact_name_city", "address_zip", "fuzzy_name_city", "fuzzy_name", "unmatched"]
REVIEW_THRESHOLD = 0.85

_ABBREV = {
    "st": "saint", "ste": "sainte", "mt": "mount", "ft": "fort",
    "hosp": "hospital", "hsp": "hospital", "med": "medical", "ctr": "center", "cntr": "center", "centre": "center",
    "reg": "regional", "rgnl": "regional", "univ": "university", "u": "university", "comm": "community",
    "mem": "memorial", "meml": "memorial", "hlth": "health", "sys": "system", "childrens": "children",
    "chldrns": "children", "intl": "international", "n": "north", "s": "south", "e": "east", "w": "west",
}
_STOP = {"the", "of", "inc", "llc", "dba", "a", "at", "and", "tx", "texas"}
_STREET = {
    "street": "st", "avenue": "ave", "av": "ave", "drive": "dr", "road": "rd", "boulevard": "blvd", "parkway": "pkwy",
    "lane": "ln", "highway": "hwy", "freeway": "fwy", "suite": "ste", "north": "n", "south": "s", "east": "e", "west": "w",
}


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, expand common abbreviations, drop stop words."""
    t = name.lower().replace("&", " and ").replace("'s", "s").replace("’s", "s")
    toks = re.sub(r"[^a-z0-9 ]+", " ", t).split()
    return " ".join(_ABBREV.get(w, w) for w in toks if _ABBREV.get(w, w) not in _STOP)


def normalize_city(city: str | None) -> str:
    return re.sub(r"[^a-z]+", " ", (city or "").lower()).strip()


def normalize_address(addr: str | None) -> str:
    toks = re.sub(r"[^a-z0-9 ]+", " ", (addr or "").lower()).split()
    return " ".join(_STREET.get(w, w) for w in toks)


def name_similarity(a: str, b: str) -> float:
    """Blend of character similarity and token-set overlap on normalized names (0-1)."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb)
    contain = len(ta & tb) / min(len(ta), len(tb))
    return round(max(seq, 0.5 * jacc + 0.5 * contain * seq), 4)


@dataclass(frozen=True)
class Candidate:
    """A facility as described by one source, before reconciliation."""

    source: Literal["cms", "schedule_h", "external"]
    ref: str  # CCN, "{object_id}:{facility_num}", or external row number
    name: str
    city: str | None = None
    address: str | None = None
    zip: str | None = None
    ccn: str | None = None
    ein: str | None = None
    group_letter: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Match:
    source: str
    ref: str
    facility_id: str
    method: MatchMethod
    confidence: float
    score: float = 0.0
    matched_name: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.method != "unmatched" and self.confidence < REVIEW_THRESHOLD


@dataclass
class Universe:
    facilities: dict[str, Facility] = field(default_factory=dict)
    matches: list[Match] = field(default_factory=list)
    sections: list[ScheduleHFacility] = field(default_factory=list)
    external_urls: dict[str, list[str]] = field(default_factory=dict)  # facility_id -> hint URLs

    def coverage(self) -> dict[str, object]:
        """Coverage report: how much of the in-scope CMS universe each other source reaches."""
        cms_ids = {f.id for f in self.facilities.values() if f.ccn}
        sh = [m for m in self.matches if m.source == "schedule_h"]
        ext = [m for m in self.matches if m.source == "external"]
        with_sh = {m.facility_id for m in sh if m.method != "unmatched"} & cms_ids
        by_method: dict[str, int] = {}
        for m in self.matches:
            by_method[m.method] = by_method.get(m.method, 0) + 1
        return {
            "facilities_total": len(self.facilities),
            "cms_in_scope": len(cms_ids),
            "cms_with_schedule_h": len(with_sh),
            "cms_without_schedule_h": sorted(cms_ids - with_sh),
            "cms_schedule_h_coverage": round(len(with_sh) / len(cms_ids), 4) if cms_ids else 0.0,
            "schedule_h_hospitals": len(sh),
            "schedule_h_unmatched": sorted(m.ref for m in sh if m.method == "unmatched"),
            "external_rows": len(ext),
            "external_unmatched": sorted(m.ref for m in ext if m.method == "unmatched"),
            "matches_by_method": dict(sorted(by_method.items())),
            "needs_review": sorted(f"{m.source}:{m.ref}->{m.facility_id}" for m in self.matches if m.needs_review),
        }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_name(text)).strip("-")[:48] or "unnamed"


def best_match(c: Candidate, pool: Iterable[Facility]) -> tuple[Facility | None, MatchMethod, float, float]:
    """Best facility in ``pool`` for candidate ``c``: (facility, method, confidence, raw score)."""
    pool = list(pool)
    if c.ccn:
        for f in pool:
            if f.ccn == c.ccn.zfill(6):
                return f, "ccn", 1.0, 1.0
    nn, nc = normalize_name(c.name), normalize_city(c.city)
    for f in pool:
        if normalize_name(f.name) == nn and (not nc or normalize_city(f.city) == nc):
            return f, "exact_name_city", 0.98, 1.0
    na = normalize_address(c.address)
    if na and c.zip:
        for f in pool:
            if f.zip == c.zip[:5] and normalize_address(f.address) == na:
                return f, "address_zip", 0.95, 1.0
    scored = sorted(((name_similarity(c.name, f.name), f) for f in pool), key=lambda t: (-t[0], t[1].id))
    for score, f in scored:
        same_city = bool(nc) and normalize_city(f.city) == nc
        if same_city and score >= 0.80:
            return f, "fuzzy_name_city", round(0.70 + (score - 0.80) * 1.2, 3), score
        if not same_city and score >= 0.90:
            return f, "fuzzy_name", round(0.50 + (score - 0.90) * 1.9, 3), score
        if score < 0.80:
            break
    return None, "unmatched", 0.0, scored[0][0] if scored else 0.0


def sh_candidates(ret: ParsedReturn) -> list[tuple[Candidate, ScheduleHHospital]]:
    """Texas Section A hospitals of one return as match candidates."""
    return [
        (
            Candidate(
                source="schedule_h", ref=f"{ret.object_id}:{h.facility_num}", name=h.name, city=h.city,
                address=h.address, zip=h.zip, ein=ret.organization.ein, group_letter=h.group_letter,
            ),
            h,
        )
        for h in ret.hospitals
        if (h.state or ret.state or "TX") == "TX"
    ]


def read_external_list(path: str | Path) -> list[Candidate]:
    """External facility list CSV (e.g. a newsroom's or partner's list).

    Columns (case-insensitive): ``name`` (required), ``city``, ``ccn``, ``address``, ``zip``, ``url``.
    """
    out: list[Candidate] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for i, raw in enumerate(csv.DictReader(fh), start=1):
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            if not row.get("name"):
                continue
            out.append(
                Candidate(
                    source="external", ref=f"row{i}", name=row["name"], city=row.get("city") or None,
                    ccn=row.get("ccn") or None, address=row.get("address") or None, zip=row.get("zip") or None,
                    url=row.get("url") or None,
                )
            )
    return out


def reconcile(
    cms: Iterable[Facility],
    returns: Iterable[ParsedReturn] = (),
    external: Iterable[Candidate] = (),
) -> Universe:
    """Merge the sources into one universe with stable ids, match records and Schedule H sections."""
    u = Universe()
    for f in sorted(cms, key=lambda f: f.id):
        u.facilities[f.id] = f.model_copy()
    cms_pool = list(u.facilities.values())

    for ret in sorted(returns, key=lambda r: r.object_id):
        u.sections.extend(ret.sections)
        for c, h in sh_candidates(ret):
            f, method, conf, score = best_match(c, cms_pool)
            if f is None:
                fid = f"tx-irs-{ret.organization.ein}-{h.facility_num}"
                f = u.facilities.get(fid) or Facility(
                    id=fid, name=h.name.title(), address=h.address, city=(h.city or "").title() or None, zip=h.zip,
                    state="TX", tx_license_no=h.license_no,
                )
            updated = f.model_copy(
                update={
                    "ein": ret.organization.ein,
                    "schedule_h_group_letter": h.group_letter or f.schedule_h_group_letter,
                    "tx_license_no": f.tx_license_no or h.license_no,
                }
            )
            u.facilities[updated.id] = updated
            cms_pool = [updated if p.id == updated.id else p for p in cms_pool]
            u.matches.append(Match("schedule_h", c.ref, updated.id, method, conf, score, f.name if method != "unmatched" else None))

    for c in external:
        pool = list(u.facilities.values())
        f, method, conf, score = best_match(c, pool)
        if f is None:
            fid = f"tx-ext-{_slug(c.name)}"
            f = u.facilities.get(fid) or Facility(id=fid, name=c.name, city=c.city, address=c.address, zip=c.zip, state="TX")
            u.facilities[fid] = f
        if c.url:
            u.external_urls.setdefault(f.id, []).append(c.url)
        u.matches.append(Match("external", c.ref, f.id, method, conf, score, f.name if method != "unmatched" else None))
    return u

"""Facility -> policy resolution (design section 5 mechanism 4).

Maps each licensed/CMS facility to the policy version that governs it, storing
the method and a confidence:

=================  ==========  ==============================================================
method             confidence  basis
=================  ==========  ==============================================================
manual             1.00        a reviewer's decision (never overwritten by code)
named_in_policy    0.95/0.80   the facility is named in ``facilities_covered`` (0.95 when that
                               field's quote verified; 0.80 when the name matches only fuzzily)
schedule_h_group   0.85        the facility and the policy's named facilities share a Schedule H
                               reporting group of the same EIN
system_default     0.60        the organisation has exactly one current policy
=================  ==========  ==============================================================

Mappings below :data:`REVIEW_THRESHOLD` get a ``facility_mapping`` review task.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from charitycare.schemas import (
    Evidence,
    Facility,
    Field,
    PolicyFacilityMap,
    PolicyVersion,
    ScheduleHFacility,
)

REVIEW_THRESHOLD = 0.8
_STOP = {"the", "of", "and", "inc", "hospital", "medical", "center", "health", "system", "regional"}


def norm_facility_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", name.casefold().replace("'", "").replace("’", ""))
    return " ".join(s.split())


def name_similarity(a: str, b: str) -> float:
    """Similarity of two facility names in [0, 1] (exact normalized match is 1.0).

    Character similarity alone is not enough: "... Center Dallas" and "... Center
    Plano" share most characters. A distinctive token (not a stop word) present in
    only one name must have a near match (prefix or abbreviation) in the other,
    otherwise the score falls to the token Jaccard overlap.
    """
    na, nb = norm_facility_name(a), norm_facility_name(b)
    if na == nb:
        return 1.0
    ta, tb = set(na.split()) - _STOP, set(nb.split()) - _STOP
    if ta and tb and ta == tb:
        return 0.95
    seq = SequenceMatcher(None, na, nb).ratio()
    all_a, all_b = set(na.split()), set(nb.split())

    def near(tok: str, others: set[str]) -> bool:
        return any(o.startswith(tok) or tok.startswith(o) or SequenceMatcher(None, tok, o).ratio() >= 0.8 for o in others)

    unmatched = [t for t in ta - tb if not near(t, all_b)] + [t for t in tb - ta if not near(t, all_a)]
    if unmatched:
        union = ta | tb
        return min(seq, len(ta & tb) / len(union)) if union else seq
    return seq


@dataclass(frozen=True)
class PolicyCandidate:
    """A policy version plus its (quote-gated) list of covered facility names."""

    version: PolicyVersion
    facilities_covered: Field[list[str]] | None = None
    facilities_verified: bool = False


def resolve_facility(
    facility: Facility,
    candidates: Iterable[PolicyCandidate],
    schedule_h: Iterable[ScheduleHFacility] = (),
    existing: Iterable[PolicyFacilityMap] = (),
) -> PolicyFacilityMap | None:
    """Best mapping of ``facility`` to one of the candidate policy versions."""
    cands = [c for c in candidates if c.version.status == "current"] or list(candidates)
    for m in existing:
        if m.method == "manual" and m.facility_id == facility.id:
            return m
    best: PolicyFacilityMap | None = None

    def offer(m: PolicyFacilityMap) -> None:
        nonlocal best
        if best is None or m.confidence > best.confidence:
            best = m

    for c in cands:
        names = (c.facilities_covered.value or []) if c.facilities_covered and not c.facilities_covered.is_abstained else []
        if not names:
            continue
        sim, matched = max((name_similarity(facility.name, n), n) for n in names)
        evidence: list[Evidence] = list(c.facilities_covered.evidence) if c.facilities_verified and c.facilities_covered else []
        if sim >= 0.999 and c.facilities_verified:
            offer(PolicyFacilityMap(policy_version_id=c.version.id, facility_id=facility.id, method="named_in_policy",
                                    confidence=0.95, evidence=evidence, note=f"named in policy as {matched!r}"))
        elif sim >= 0.9:
            offer(PolicyFacilityMap(policy_version_id=c.version.id, facility_id=facility.id, method="named_in_policy",
                                    confidence=0.8 if c.facilities_verified else 0.7, evidence=evidence,
                                    note=f"name matches {matched!r} at similarity {sim:.2f}"))
    if best is not None and best.confidence >= 0.95:
        return best

    secs = [s for s in schedule_h if facility.ein is None or s.ein == facility.ein]
    if facility.schedule_h_group_letter:
        group = [s for s in secs if s.group_letter == facility.schedule_h_group_letter]
        for c in cands:
            if c.version.organization_ein != facility.ein or not c.facilities_covered or not c.facilities_covered.value:
                continue
            for s in group:
                if any(name_similarity(n, g) >= 0.9 for n in c.facilities_covered.value for g in s.facility_names):
                    offer(PolicyFacilityMap(policy_version_id=c.version.id, facility_id=facility.id, method="schedule_h_group",
                                            confidence=0.85, note=f"Schedule H group {s.group_letter} ({s.object_id})"))
    if best is not None:
        return best

    same_org = [c for c in cands if c.version.organization_ein == facility.ein]
    if len(same_org) == 1:
        return PolicyFacilityMap(policy_version_id=same_org[0].version.id, facility_id=facility.id, method="system_default",
                                 confidence=0.6, note="only current policy of the organization")
    return None


def needs_review(m: PolicyFacilityMap | None) -> bool:
    return m is None or (m.method != "manual" and m.confidence < REVIEW_THRESHOLD)

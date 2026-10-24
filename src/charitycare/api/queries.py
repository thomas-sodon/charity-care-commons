"""Read-side queries over the registry, shared by the API, MCP server, site and exports.

Every function returns plain JSON-able dicts (``model_dump(mode="json")`` plus
display strings from :mod:`charitycare.api.present`), so the surfaces cannot
drift apart. Nothing here writes to the registry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from charitycare.api import present
from charitycare.schemas import (
    DETERMINATION_FIELDS,
    ChangeEvent,
    CrossCheck,
    CrossCheckResult,
    DocumentVersion,
    Facility,
    FieldRecord,
    FieldStatus,
    HospitalResponse,
    PolicyFacilityMap,
    PolicyVersion,
    ScheduleHFacility,
    Source,
    utcnow,
)
from charitycare.store import Registry

Coverage = Literal["verified", "partial", "not_found"]


# --------------------------------------------------------------------------
# Freshness and the sunset policy (design section 17)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Freshness:
    """When the registry was last verified, and whether it is past its sunset window."""

    last_verified: date | None
    stale: bool
    stale_after_days: int

    def as_meta(self) -> dict[str, Any]:
        return {
            "last_verified": self.last_verified.isoformat() if self.last_verified else None,
            "stale": self.stale,
            "stale_after_days": self.stale_after_days,
        }


def last_verified(reg: Registry) -> date | None:
    """Latest reviewer decision or policy ``as_of`` date in the registry."""
    row = reg.conn.execute("SELECT MAX(reviewed_at) FROM field WHERE status = 'verified'").fetchone()
    reviewed = datetime.fromisoformat(row[0]).date() if row and row[0] else None
    row = reg.conn.execute("SELECT MAX(as_of) FROM policy_version").fetchone()
    as_of = date.fromisoformat(row[0]) if row and row[0] else None
    dates = [d for d in (reviewed, as_of) if d is not None]
    return max(dates) if dates else None


def freshness(reg: Registry, today: date, stale_after_days: int, force_stale: bool = False) -> Freshness:
    lv = last_verified(reg)
    stale = force_stale or lv is None or (today - lv) > timedelta(days=stale_after_days)
    return Freshness(lv, stale, stale_after_days)


# --------------------------------------------------------------------------
# Fields and cross-checks
# --------------------------------------------------------------------------


def field_json(rec: FieldRecord, reg: Registry | None = None) -> dict[str, Any]:
    """A stored field with its display text, badge and rendered cross-checks.

    With ``reg``, an unexplained cross-check is shown as a discrepancy only once it is
    publishable (:meth:`Registry.discrepancy_state`); without it, it is held back.
    """
    f = present.typed_field(rec.key, rec.field)
    d = rec.model_dump(mode="json")
    d["field"] = f.model_dump(mode="json")
    d["label"] = present.label(rec.key)
    d["display"] = present.field_display(rec)
    d["badge"] = present.status_badge(rec.status)
    d["usable_for_screening"] = rec.status == FieldStatus.verified and not f.is_abstained
    for cc in d["crosschecks"]:
        model = CrossCheck.model_validate({k: cc[k] for k in CrossCheck.model_fields if k in cc})
        cc.update(present.crosscheck_display(model, _pub_state(reg, model)))
    return d


def _pub_state(reg: Registry | None, cc: CrossCheck) -> str | None:
    if cc.result is not CrossCheckResult.disagree_unexplained:
        return None
    return reg.discrepancy_state(cc) if reg is not None else "unreviewed"


def fields_for_policy(reg: Registry, version_id: str) -> list[FieldRecord]:
    recs = {r.key: r for r in reg.fields_for_policy(version_id)}
    order = [k for k in present.DISPLAY_ORDER if k in recs] + sorted(k for k in recs if k not in present.DISPLAY_ORDER)
    return [recs[k] for k in order]


def crosschecks_for_policy(reg: Registry, version_id: str) -> list[dict[str, Any]]:
    """Cross-checks of every field of a policy, with public labels and any hospital response."""
    pv = reg.get_policy_version(version_id)
    facility_ids = facilities_for_policy(reg, version_id)
    responses = [r for fid in facility_ids for r in reg.responses_for_facility(fid) if r.published]
    out = []
    for rec in fields_for_policy(reg, version_id):
        for cc in rec.crosschecks:
            d = cc.model_dump(mode="json")
            d.update(present.crosscheck_display(cc, _pub_state(reg, cc)))
            d["field_label"] = present.label(cc.field_key)
            d["policy_effective_date"] = pv.effective_date.isoformat() if pv and pv.effective_date else None
            d["hospital_responses"] = [
                r.model_dump(mode="json") for r in responses if r.field_key in (None, cc.field_key)
            ] if d["is_discrepancy"] else []
            out.append(d)
    return out


# --------------------------------------------------------------------------
# Facilities and policies
# --------------------------------------------------------------------------


def facilities_for_policy(reg: Registry, version_id: str) -> list[str]:
    return sorted(m.facility_id for m in reg.select(PolicyFacilityMap, policy_version_id=version_id))


def mapping_for(reg: Registry, version_id: str, facility_id: str) -> PolicyFacilityMap | None:
    return reg.get(PolicyFacilityMap, policy_version_id=version_id, facility_id=facility_id)


def coverage(reg: Registry, facility_id: str) -> tuple[Coverage, PolicyVersion | None, list[FieldRecord]]:
    """``verified`` when every determination field is reviewed (verified or a confirmed abstention),
    ``partial`` when a policy exists but some are provisional or missing, ``not_found`` otherwise."""
    pv = reg.policy_for_facility(facility_id)
    if pv is None:
        return "not_found", None, []
    recs = reg.fields_for_policy(pv.id)
    by_key = {r.key: r for r in recs}
    reviewed = all(
        k in by_key and by_key[k].status in (FieldStatus.verified, FieldStatus.abstained) for k in DETERMINATION_FIELDS
    )
    return ("verified" if reviewed and recs else "partial"), pv, recs


def _value(recs: Iterable[FieldRecord], key: str) -> Any:
    for r in recs:
        if r.key == key:
            f = present.typed_field(key, r.field)
            return None if f.is_abstained else f.value
    return None


def headline(recs: list[FieldRecord]) -> dict[str, Any]:
    """Dashboard columns: free and discount ceilings, asset test, residency, documents, screening."""
    tiers = _value(recs, "fpg_tiers") or []
    free = [t.max_pct_inclusive for t in tiers if t.benefit == "free"]
    disc = [t.max_pct_inclusive for t in tiers if t.benefit != "free"]
    asset = _value(recs, "asset_test")
    by_key = {r.key: r for r in recs}

    def disp(k: str) -> str:
        return present.field_display(by_key[k]) if k in by_key else "Not found"

    return {
        "free_max_pct": max(free) if free else None,
        "discount_max_pct": max(disc) if disc else None,
        "asset_test": None if asset is None else bool(asset.applies),
        "asset_threshold_usd": asset.threshold_usd if asset is not None and asset.applies else None,
        "residency": disp("residency"),
        "documents": disp("required_documents"),
        "screening_timing": _value(recs, "screening_timing"),
        "screening": disp("screening_timing"),
    }


def irs_check(recs: list[FieldRecord], reg: Registry | None = None) -> str:
    """Worst cross-check outcome across a policy's fields: ``discrepancy``, ``context``, ``agree`` or ``none``.

    An unexplained difference counts as ``discrepancy`` only when publishable (needs ``reg``);
    otherwise it is ``context`` (under review).
    """
    checks = [c for r in recs for c in r.crosschecks]
    results = {c.result for c in checks}
    unexplained = [c for c in checks if c.result is CrossCheckResult.disagree_unexplained]
    if any(_pub_state(reg, c) == "public" for c in unexplained):
        return "discrepancy"
    if unexplained:
        return "context"
    if results & {CrossCheckResult.disagree_policy_newer_than_filing, CrossCheckResult.disagree_filing_lag_possible}:
        return "context"
    if CrossCheckResult.agree in results:
        return "agree"
    return "none"


def facility_summary(reg: Registry, fac: Facility, *, since: datetime | None = None) -> dict[str, Any]:
    cov, pv, recs = coverage(reg, fac.id)
    n_ver = sum(r.status == FieldStatus.verified for r in recs)
    changes = []
    if pv is not None:
        changes = [c for c in reg.select(ChangeEvent, policy_version_id=pv.id) if c.published_at is not None]
        if since is not None:
            changes = [c for c in changes if c.published_at and c.published_at > since]
    return {
        **fac.model_dump(mode="json"),
        "coverage": cov,
        "policy_version_id": pv.id if pv else None,
        "policy_title": pv.title if pv else None,
        "policy_as_of": pv.as_of.isoformat() if pv else None,
        "fields_total": len(recs),
        "fields_verified": n_ver,
        "irs_check": irs_check(recs, reg),
        "recent_changes": len(changes),
        **headline(recs),
    }


def list_facilities(reg: Registry, state: str | None = "TX", *, since: datetime | None = None) -> list[dict[str, Any]]:
    return [facility_summary(reg, f, since=since) for f in reg.list_facilities(state=state, in_scope=True)]


def search_facilities(reg: Registry, query: str = "", *, city: str | None = None, county: str | None = None,
                      ccn: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Case-insensitive token match over name, city, county, CCN and id."""
    toks = [t for t in query.casefold().split() if t]
    out = []
    for f in reg.list_facilities(state=None):
        if ccn and f.ccn != ccn:
            continue
        if city and (f.city or "").casefold() != city.casefold():
            continue
        if county and (f.county or "").casefold().removesuffix(" county") != county.casefold().removesuffix(" county"):
            continue
        hay = " ".join(x or "" for x in (f.name, f.city, f.county, f.ccn, f.id, f.address)).casefold()
        if all(t in hay for t in toks):
            out.append(facility_summary(reg, f))
    return out[: max(1, limit)]


def documents_for_policy(reg: Registry, pv: PolicyVersion) -> list[dict[str, Any]]:
    out = []
    for name, sha in sorted(pv.doc_shas.items()):
        doc = reg.get_document_version(sha)
        src = reg.get(Source, id=doc.source_id) if doc and doc.source_id else None
        out.append({
            "name": name,
            "sha256": sha,
            "kind": doc.kind if doc else None,
            "language": doc.language if doc else None,
            "pages": doc.pages if doc else None,
            "fetched_at": doc.fetched_at.isoformat() if doc else None,
            "url": src.url if src else None,
            "wayback_uri": doc.wayback_uri if doc else None,
            "ocr_used": doc.ocr_used if doc else None,
        })
    return out


def policy_bundle(reg: Registry, pv: PolicyVersion, facility_id: str | None = None) -> dict[str, Any]:
    """A policy version with its documents, facility mapping and fields."""
    mapping = mapping_for(reg, pv.id, facility_id) if facility_id else None
    return {
        "policy": pv.model_dump(mode="json"),
        "documents": documents_for_policy(reg, pv),
        "facility_ids": facilities_for_policy(reg, pv.id),
        "mapping": mapping.model_dump(mode="json") if mapping else None,
        "fields": [field_json(r, reg) for r in fields_for_policy(reg, pv.id)],
    }


def version_history(reg: Registry, facility_id: str) -> list[dict[str, Any]]:
    """Every policy version mapped to the facility (newest first) with its change events."""
    ids = [m.policy_version_id for m in reg.select(PolicyFacilityMap, facility_id=facility_id)]
    versions = [pv for pv in (reg.get_policy_version(i) for i in ids) if pv is not None]
    versions.sort(key=lambda p: p.as_of, reverse=True)
    out = []
    for pv in versions:
        evs = sorted(reg.select(ChangeEvent, policy_version_id=pv.id),
                     key=lambda e: e.published_at or datetime.min.replace(tzinfo=UTC))
        out.append({
            "id": pv.id, "title": pv.title, "status": pv.status, "as_of": pv.as_of.isoformat(),
            "effective_date": pv.effective_date.isoformat() if pv.effective_date else None,
            "supersedes": pv.supersedes,
            "changes": [e.model_dump(mode="json") for e in evs if e.published_at is not None],
        })
    return out


def schedule_h_sections(reg: Registry, fac: Facility) -> list[dict[str, Any]]:
    if not fac.ein:
        return []
    keep = ("id", "object_id", "tax_year", "tax_period_end", "group_letter", "facility_names", "line_13a_free_pct",
            "line_13a_discount_pct", "line_13c_asset_level", "line_13g_residency", "line_16a_fap_url", "line_22_agb_method")
    out = []
    for s in reg.schedule_h_for_ein(fac.ein):
        if s.group_letter and fac.schedule_h_group_letter and s.group_letter != fac.schedule_h_group_letter:
            continue
        d = s.model_dump(mode="json")
        out.append({k: d[k] for k in keep})
    return out


def facility_detail(reg: Registry, facility_id: str) -> dict[str, Any] | None:
    fac = reg.get_facility(facility_id)
    if fac is None:
        return None
    summary = facility_summary(reg, fac)
    return {
        **summary,
        "schedule_h": schedule_h_sections(reg, fac),
        "hospital_responses": [r.model_dump(mode="json") for r in reg.responses_for_facility(facility_id) if r.published],
        "versions": version_history(reg, facility_id),
    }


# --------------------------------------------------------------------------
# Changes and link health
# --------------------------------------------------------------------------


def changes(reg: Registry, since: datetime | None = None) -> list[dict[str, Any]]:
    out = []
    for ev in reg.changes_since(since):
        d = ev.model_dump(mode="json")
        d["facility_ids"] = facilities_for_policy(reg, ev.policy_version_id)
        out.append(d)
    out.sort(key=lambda d: d["published_at"], reverse=True)
    return out


LinkState = Literal["ok", "broken", "blocked", "unchecked"]


def link_state(src: Source) -> LinkState:
    if src.blocked:
        return "blocked"
    if src.http_status is None:
        return "unchecked"
    return "ok" if 200 <= src.http_status < 400 else "broken"


def link_health(reg: Registry) -> dict[str, Any]:
    """Per-source reachability (for Dollar For / Lown and the public link-health page)."""
    rows = []
    for src in sorted(reg.select(Source), key=lambda s: s.url):
        rows.append({
            "source_id": src.id, "url": src.url, "kind": src.kind, "state": link_state(src),
            "http_status": src.http_status, "blocked": src.blocked,
            "last_checked": src.last_checked.isoformat() if src.last_checked else None,
            "facility_ids": src.facility_ids,
            "has_snapshot": bool(reg.select(DocumentVersion, source_id=src.id)),
        })
    counts = {s: sum(r["state"] == s for r in rows) for s in ("ok", "broken", "blocked", "unchecked")}
    return {"summary": {"total": len(rows), **counts}, "sources": rows}


def pending_discrepancies(reg: Registry, today: date | None = None) -> int:
    """How many unexplained cross-checks are held back (unreviewed, or the hospital's reply window is open)."""
    return sum(reg.discrepancy_state(cc, today) in ("unreviewed", "awaiting_reply") for cc in reg.discrepancies())


def discrepancies(reg: Registry, today: date | None = None) -> list[dict[str, Any]]:
    """Public "discrepancies for review" list: reviewed, current, past the right-of-reply window."""
    out = []
    for cc in reg.public_discrepancies(today):
        rec = reg.get(FieldRecord, id=cc.field_id) if cc.field_id else None
        if rec is None:
            continue
        fids = facilities_for_policy(reg, rec.policy_version_id)
        responses = [r for fid in fids for r in reg.responses_for_facility(fid)
                     if r.published and r.field_key in (None, cc.field_key)]
        d = cc.model_dump(mode="json")
        d.update(present.crosscheck_display(cc, "public"))
        d.update({"policy_version_id": rec.policy_version_id, "facility_ids": fids, "field_label": present.label(cc.field_key),
                  "hospital_responses": [r.model_dump(mode="json") for r in responses]})
        out.append(d)
    return out


def published_responses(reg: Registry, facility_id: str) -> list[HospitalResponse]:
    return [r for r in reg.responses_for_facility(facility_id) if r.published]


def stats(reg: Registry, state: str | None = "TX", since_days: int = 30, today: date | None = None) -> dict[str, Any]:
    """Dashboard headline numbers (Screen 4)."""
    today = today or utcnow().date()
    facs = list_facilities(reg, state, since=datetime.combine(today - timedelta(days=since_days), datetime.min.time(), UTC))
    n = len(facs)
    covered = sum(f["coverage"] != "not_found" for f in facs)
    total = sum(f["fields_total"] for f in facs)
    ver = sum(f["fields_verified"] for f in facs)
    return {
        "facilities": n,
        "coverage_pct": round(100 * covered / n, 1) if n else 0.0,
        "fields_verified_pct": round(100 * ver / total, 1) if total else 0.0,
        "discrepancies_for_review": len(discrepancies(reg, today)),
        "differences_under_review": pending_discrepancies(reg, today),
        "changes_recent": sum(f["recent_changes"] for f in facs),
        "since_days": since_days,
        "by_coverage": {c: sum(f["coverage"] == c for f in facs) for c in ("verified", "partial", "not_found")},
    }


def schedule_h_section(reg: Registry, section_id: str) -> ScheduleHFacility | None:
    return reg.get(ScheduleHFacility, id=section_id)

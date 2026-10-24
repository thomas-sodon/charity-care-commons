"""Turn a ``DiscoveryResult`` into registry rows: Source, PolicyVersion and PolicyFacilityMap (section 5.4).

Facility -> policy resolution, strongest evidence first:

=====================  ==========  ===============================================================
method                 confidence  evidence
=====================  ==========  ===============================================================
``named_in_policy``    0.95        the facility's name appears verbatim in the FAP text (quote + page)
``schedule_h_group``   0.85        the facility's Schedule H section reports this FAP URL (line 16a)
``system_default``     0.60        same organization (EIN), no facility-level evidence
=====================  ==========  ===============================================================

Archived (provisional) policies are capped at 0.7. Mappings below 0.8 open a
``facility_mapping`` review task. Effective dates parsed here are metadata
for versioning only; the compiled, quote-verified date comes from ``compile/``.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from charitycare.discover.docstore import DocumentStore
from charitycare.discover.models import DiscoveredDoc, DiscoveryResult
from charitycare.pdftext import normalize_text
from charitycare.schemas import (
    Evidence,
    Facility,
    Organization,
    PolicyFacilityMap,
    PolicyVersion,
    ReviewTask,
    ScheduleHFacility,
    Source,
    utcnow,
)
from charitycare.seed.pipeline import source_id_for
from charitycare.store import Registry

REVIEW_BELOW = 0.8
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], start=1)}
_DATE_RE = re.compile(
    r"(effective|revision|revised|approved)(?:\s+date)?\s*:?\s*"
    r"(?:(?P<mon>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})|(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}))",
    re.IGNORECASE,
)


def parse_effective_date(text: str) -> date | None:
    """First "Effective date: July 1, 2025" / "Revision date: 03/15/2026" style date in the text."""
    for m in _DATE_RE.finditer(text):
        try:
            if m.group("mon"):
                mon = _MONTHS.get(m.group("mon").lower())
                if mon:
                    return date(int(m.group("year")), mon, int(m.group("day")))
            else:
                return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        except ValueError:
            continue
    return None


def guess_title(first_page: str, org_names: list[str]) -> str | None:
    """First short line on page 1 that is not the organization's name."""
    names = {normalize_text(n) for n in org_names}
    for line in first_page.splitlines():
        s = line.strip()
        if 4 <= len(s) <= 120 and normalize_text(s) not in names and not _DATE_RE.search(s):
            return s
    return None


def find_name_quote(pages: list[tuple[int, str]], name: str) -> tuple[int, str] | None:
    """(page, verbatim quote) where ``name`` occurs, tolerant to case and line wraps."""
    words = [re.escape(w) for w in re.split(r"\s+", name.strip()) if w]
    if not words:
        return None
    pat = re.compile(r"\s+".join(words).replace("'", "['’]"), re.IGNORECASE)
    for page, text in pages:
        m = pat.search(text)
        if m:
            return page, re.sub(r"\s+", " ", m.group(0))
    return None


def _source_for(doc: DiscoveredDoc, result: DiscoveryResult, now: datetime) -> Source:
    return Source(
        id=source_id_for(doc.url),
        url=doc.url,
        kind=doc.kind,
        discovered_via=doc.discovered_via,
        first_seen=now,
        last_checked=now,
        http_status=doc.http_status,
        blocked=doc.blocked,
        facility_ids=sorted(result.facility_ids),
    )


def ingest(result: DiscoveryResult, registry: Registry, store: DocumentStore, *, as_of: date | None = None) -> PolicyVersion | None:
    """Persist sources, document versions, the policy version and facility mappings for one result."""
    now = utcnow()
    as_of = as_of or now.date()
    with registry.transaction():
        for doc in result.candidates:
            src = _source_for(doc, result, now)
            prev = registry.get(Source, id=src.id)
            if prev is not None:
                src = src.model_copy(update={
                    "first_seen": prev.first_seen or now,
                    "facility_ids": sorted(set(prev.facility_ids) | set(src.facility_ids)),
                    "discovered_via": prev.discovered_via or src.discovered_via,
                })
            registry.upsert_source(src)
            if doc.sha256:
                dv = registry.get_document_version(doc.sha256)
                if dv is not None:
                    registry.add_document_version(dv.model_copy(update={
                        "source_id": dv.source_id or src.id, "kind": doc.kind, "language": doc.language or dv.language,
                        "wayback_uri": doc.archived_url or dv.wayback_uri,
                    }))
                else:
                    raw = store.get_bytes(doc.sha256)
                    if raw is not None:
                        store.put(raw, source_id=src.id, kind=doc.kind, language=doc.language, wayback_uri=doc.archived_url)

        fap = result.best("fap")
        if fap is None or not result.ein:
            return None
        doc_shas: dict[str, str] = {"fap": fap.sha256}  # type: ignore[dict-item]
        for kind in ("application", "pls", "billing_policy"):
            docs = sorted((c for c in result.candidates if c.kind == kind and c.sha256), key=lambda c: (c.provisional, c.language != "en", c.url))
            for c in docs:
                key = kind if (c.language in (None, "en") and kind not in doc_shas) else f"{kind}_{c.language or 'xx'}"
                doc_shas.setdefault(key, c.sha256)  # type: ignore[arg-type]

        pages = store.page_texts(fap.sha256) or []  # type: ignore[arg-type]
        page_list = [(p.page, p.text) for p in pages]
        full = "\n".join(t for _, t in page_list)
        facilities = [f for fid in result.facility_ids if (f := registry.get_facility(fid)) is not None]
        org = registry.get(Organization, ein=result.ein)
        org_names = [f.name for f in facilities] + ([org.name] if org else [])
        pv_id = f"pv-{result.ein}-{fap.sha256[:12]}"  # type: ignore[index]
        current = [p for p in registry.select(PolicyVersion, organization_ein=result.ein, status="current") if p.id != pv_id]
        existing = registry.get_policy_version(pv_id)
        pv = PolicyVersion(
            id=pv_id,
            organization_ein=result.ein,
            title=guess_title(page_list[0][1], org_names) if page_list else None,
            effective_date=parse_effective_date(full[:3000]),
            doc_shas=doc_shas,
            supersedes=existing.supersedes if existing else (max(current, key=lambda p: p.as_of).id if current else None),
            status="current",
            as_of=existing.as_of if existing else as_of,
        )
        registry.add_policy_version(pv)
        if existing is None and pv.supersedes:
            _record_change(registry, store, pv)

        sections = registry.schedule_h_for_ein(result.ein)
        for f in facilities:
            m = map_facility(f, pv, fap, page_list, sections)
            registry.map_policy_facility(m)
            if m.confidence < REVIEW_BELOW:
                registry.add_review_task(ReviewTask(
                    id=f"fmap-{pv.id}-{f.id}", kind="facility_mapping", facility_id=f.id, priority="medium",
                    notes=f"{f.name} mapped to {pv.id} by {m.method} (confidence {m.confidence:.2f}); confirm the governing policy.",
                ))
    return pv


def _record_change(registry: Registry, store: DocumentStore, pv: PolicyVersion) -> None:
    """Diff the new FAP against the version it supersedes and store the triaged :class:`ChangeEvent`.

    Discovery runs without a model, so the deterministic triage labels the change
    (numbers, negations and modal words changing always make it substantive).
    """
    from charitycare.compile.changes import detect_change
    from charitycare.compile.textlayer import DocText

    prev = registry.get_policy_version(pv.supersedes or "")
    old_sha, new_sha = (prev.doc_shas.get("fap") if prev else None), pv.doc_shas.get("fap")
    if not old_sha or not new_sha or old_sha == new_sha:
        return
    old_pages, new_pages = store.page_texts(old_sha), store.page_texts(new_sha)
    if old_pages is None or new_pages is None:
        return
    res = detect_change(DocText(old_sha, tuple(old_pages)), DocText(new_sha, tuple(new_pages)),
                        policy_version_id=pv.id, prev_version_id=prev.id if prev else None, backend=None)
    if res.event is not None:
        registry.add_change_event(res.event)


def map_facility(
    f: Facility, pv: PolicyVersion, fap: DiscoveredDoc, pages: list[tuple[int, str]], sections: list[ScheduleHFacility]
) -> PolicyFacilityMap:
    """Resolve one facility to the policy version, with method, confidence and evidence."""
    cap = 0.7 if fap.provisional else 1.0
    hit = find_name_quote(pages, f.name)
    if hit:
        page, quote = hit
        return PolicyFacilityMap(
            policy_version_id=pv.id, facility_id=f.id, method="named_in_policy", confidence=min(0.95, cap),
            evidence=[Evidence(doc_sha256=fap.sha256 or "", page=page, quote=quote[:400])],
            note="facility named in the policy text" + ("; archived copy" if fap.provisional else ""),
        )
    reported = {
        u for s in sections
        if (f.schedule_h_group_letter and s.group_letter == f.schedule_h_group_letter) or normalize_text(f.name) in {normalize_text(n) for n in s.facility_names}
        for u in ([s.line_16a_fap_url] if s.line_16a_fap_url else []) + s.narrative_urls_recovered
    }
    if fap.url in reported:
        return PolicyFacilityMap(
            policy_version_id=pv.id, facility_id=f.id, method="schedule_h_group", confidence=min(0.85, cap),
            note=f"Schedule H Part V reports this FAP URL for the facility{' (group ' + f.schedule_h_group_letter + ')' if f.schedule_h_group_letter else ''}",
        )
    return PolicyFacilityMap(
        policy_version_id=pv.id, facility_id=f.id, method="system_default", confidence=min(0.6, cap),
        note="same organization (EIN); no facility-level evidence",
    )

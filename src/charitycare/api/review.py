"""Maintainer review decisions (human-in-the-loop H1/H2/H6, design section 7 Screen 2).

A reviewer acts on one open :class:`~charitycare.schemas.ReviewTask`:

* ``approve``      the extracted value (or abstention) is right: field -> verified/abstained;
* ``edit``         replace the value (validated against the ``CompiledPolicy`` type) and
                   optionally the quote, which must be found on the cited page (no quote, no publish);
* ``abstain``      publish no value, with an abstain reason (quotes kept, e.g. a "may" clause);
* ``ask_hospital`` open a hospital query; nothing is published yet (right of reply).

Every decision is written to the changelog as a :class:`ChangeEvent` (published
for approve/edit/abstain, unpublished for hospital queries) whose summary ends
with the machine-readable decision JSON after a ``---`` line.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from charitycare import pdftext
from charitycare.api import present
from charitycare.compile.verify import ungrounded_numbers
from charitycare.schemas import (
    ChangeEvent,
    CompiledPolicy,
    CrossCheckResult,
    DocumentVersion,
    Evidence,
    Field,
    FieldRecord,
    FieldStatus,
    HospitalNotice,
    HospitalResponse,
    PolicyFacilityMap,
    ReviewAction,
    ReviewDecision,
    ReviewTask,
)
from charitycare.store import Registry

Action = ReviewAction  # re-exported; ReviewDecision lives in charitycare.schemas
TASK_STATE: dict[str, str] = {"approve": "approved", "edit": "corrected", "abstain": "abstained", "ask_hospital": "hospital_query"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
PRIORITY_IMPACT = {"high": 60, "medium": 30, "low": 10}
_IMPACT = re.compile(r"^\[impact (\d+)\]\s*")


class ReviewError(ValueError):
    """The decision cannot be applied (unknown task, invalid value, quote not on page ...)."""


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------


def task_impact(task: ReviewTask) -> int:
    """Impact score: the task's ``impact`` column, the legacy ``[impact N]`` notes prefix, else by priority."""
    if task.impact is not None:
        return task.impact
    m = _IMPACT.match(task.notes or "")
    return int(m.group(1)) if m else PRIORITY_IMPACT[task.priority]


def task_note(task: ReviewTask) -> str:
    return _IMPACT.sub("", task.notes or "")


def adjudicator_note(task: ReviewTask) -> str | None:
    """The adjudicator's draft, when the pipeline appended one (``| adjudicator: ...``)."""
    for part in task_note(task).split(" | "):
        if part.lower().startswith("adjudicat"):
            return part.split(":", 1)[1].strip() if ":" in part else part
    return None


def queue(reg: Registry) -> list[ReviewTask]:
    """Open tasks sorted by impact (highest first), then priority, then age."""
    return sorted(reg.select(ReviewTask, state="open"),
                  key=lambda t: (-task_impact(t), PRIORITY_ORDER[t.priority], t.created_at))


# --------------------------------------------------------------------------
# Source context (side-by-side page text)
# --------------------------------------------------------------------------


@lru_cache(maxsize=32)
def _page_texts(pdf_path: str, mtime: float) -> tuple[pdftext.PageText, ...]:
    return tuple(pdftext.page_texts(Path(pdf_path).read_bytes()))


def page_text(reg: Registry, doc_sha256: str, page: int) -> pdftext.PageText | None:
    """Independent text layer of one page of a stored document (None if the PDF is not on disk)."""
    doc = reg.get(DocumentVersion, sha256=doc_sha256)
    if doc is None or not doc.pdf_uri:
        return None
    path = pdftext.uri_to_path(doc.pdf_uri)
    if path is None or not path.is_file():
        return None
    pages = _page_texts(str(path), path.stat().st_mtime)
    return pages[page - 1] if 1 <= page <= len(pages) else None


def highlight(text: str, quote: str) -> tuple[str, str, str] | None:
    """Split ``text`` into (before, match, after) around ``quote``, tolerant of whitespace and case."""
    words = [re.escape(w) for w in pdftext.normalize_text(quote).split()]
    if not words:
        return None
    rx = re.compile(r"\s+".join(words), re.IGNORECASE)
    m = rx.search(text.translate(pdftext._QUOTES))
    if not m:
        return None
    return text[: m.start()], text[m.start(): m.end()], text[m.end():]


def source_context(reg: Registry, ev: Evidence) -> dict[str, Any]:
    """What the reviewer sees next to the field: the page text with the quote marked."""
    pt = page_text(reg, ev.doc_sha256, ev.page)
    if pt is None:
        return {"available": False, "reason": "document not on file", "page": ev.page, "doc_sha256": ev.doc_sha256}
    if pt.ocr_unavailable:
        return {"available": False, "reason": "page has no text layer and OCR is not installed", "page": ev.page,
                "doc_sha256": ev.doc_sha256}
    span = pdftext.find_quote(pt.text, ev.quote)
    parts = highlight(pt.text, ev.quote)
    return {
        "available": True, "page": ev.page, "doc_sha256": ev.doc_sha256, "text": pt.text,
        "match": "exact" if span else "not found", "span": span,
        "before": parts[0] if parts else pt.text, "marked": parts[1] if parts else "", "after": parts[2] if parts else "",
        "ocr_used": pt.ocr_used,
    }


# --------------------------------------------------------------------------
# Applying decisions
# --------------------------------------------------------------------------


def _typed(key: str, data: dict[str, Any]) -> Field[Any]:
    info = CompiledPolicy.model_fields.get(key)
    if info is None or info.annotation is None:
        raise ReviewError(f"unknown field key {key!r}")
    try:
        return info.annotation.model_validate(data)
    except ValueError as exc:
        raise ReviewError(f"value does not fit {present.label(key)}: {exc}") from exc


def _edited_field(reg: Registry, rec: FieldRecord, d: ReviewDecision) -> Field[Any]:
    if d.value is None:
        raise ReviewError("edit needs a value; use abstain to publish no value")
    evidence = [e.model_dump(mode="json") for e in rec.field.evidence]
    if d.quote:
        base = rec.field.evidence[0] if rec.field.evidence else None
        if base is None:
            raise ReviewError("no source document on this field to cite")
        page = d.page or base.page
        pt = page_text(reg, base.doc_sha256, page)
        span = pdftext.find_quote(pt.text, d.quote) if pt is not None else None
        if span is None:
            raise ReviewError(f"quote not found on page {page} of {base.doc_sha256[:12]}: no quote, no publish")
        new = Evidence(doc_sha256=base.doc_sha256, page=page, quote=d.quote, section_label=None,
                       char_start=span[0], char_end=span[1])
        evidence = [new.model_dump(mode="json")] + evidence[1:]
    if not evidence:
        raise ReviewError("an edited value needs a verified quote")
    typed = _typed(rec.key, {"value": d.value, "evidence": evidence, "confidence": 1.0})
    old = rec.field.model_dump(mode="json")["value"]
    if not d.quote and typed.model_dump(mode="json")["value"] != old:
        raise ReviewError("a changed value needs a new quote from the source that supports it")
    missing = ungrounded_numbers(rec.key, typed)
    if missing:
        raise ReviewError("number(s) in the value are printed in no quote: " + "; ".join(missing))
    return Field[Any].model_validate(typed.model_dump(mode="json"))


def _summary(rec: FieldRecord | None, task: ReviewTask, d: ReviewDecision) -> str:
    what = present.label(rec.key) if rec else task.kind.replace("_", " ")
    verb = {"approve": "approved", "edit": "corrected", "abstain": "set to no published value",
            "ask_hospital": "sent to the hospital for comment"}[d.action]
    text = f"Review decision: {what} {verb} by {d.reviewer}."
    if rec is not None and d.action != "ask_hospital":
        text += f" Published: {present.field_display(rec)}."
    if d.note:
        text += f" Note: {d.note}"
    return text + "\n---\n" + d.model_dump_json(exclude_none=True)


def apply_decision(reg: Registry, d: ReviewDecision) -> tuple[ReviewTask, FieldRecord | None, ChangeEvent]:
    """Apply one decision atomically; returns the updated task, field (if any) and changelog entry."""
    task = reg.get(ReviewTask, id=d.task_id)
    if task is None:
        raise ReviewError(f"unknown review task {d.task_id!r}")
    if task.state != "open":
        raise ReviewError(f"task {d.task_id!r} is already {task.state}")
    rec = reg.get(FieldRecord, id=task.field_id) if task.field_id else None
    if task.field_id and rec is None:
        raise ReviewError(f"task field {task.field_id!r} not found")
    if rec is not None and d.action in ("approve", "edit") and not (d.action == "approve" and rec.field.is_abstained):
        blocking = [t.id for t in reg.select(ReviewTask, state="open", field_id=rec.id)
                    if t.id != task.id and t.priority == "high"]
        if blocking:
            raise ReviewError(f"{present.label(rec.key)} still has open high-priority review task(s) "
                              f"{', '.join(sorted(blocking))}; resolve those before verifying the value")
    with reg.transaction():
        if rec is not None:
            rec = _apply_to_field(reg, rec, d)
        elif d.action == "approve" and task.kind == "hospital_correction":
            _publish_response(reg, task)
        new_task = reg.update_review_task(task.id, TASK_STATE[d.action], notes=task.notes)
        pv_id = rec.policy_version_id if rec else _policy_for_task(reg, task)
        ev = ChangeEvent(
            id=f"rd:{task.id}:{d.decided_at.strftime('%Y%m%dT%H%M%S%f')}",
            policy_version_id=pv_id or "unmapped",
            prev_version_id=None,
            triage="cosmetic" if d.action == "approve" else "substantive",
            diff_summary=_summary(rec, task, d.model_copy(update={"field_id": rec.id if rec else None})),
            published_at=None if d.action == "ask_hospital" else d.decided_at,
        )
        reg.add_change_event(ev)
        reg.put(d.model_copy(update={"field_id": rec.id if rec else d.field_id}))
        if rec is not None and d.action in ("approve", "edit", "ask_hospital"):
            record_notice(reg, rec, d.decided_at)
    return new_task, rec, ev


REPLY_BUSINESS_DAYS = 10


def add_business_days(start: date, n: int) -> date:
    """``start`` plus ``n`` weekdays (federal holidays are not modelled)."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def record_notice(reg: Registry, rec: FieldRecord, at: datetime) -> HospitalNotice | None:
    """Start the hospital's right-of-reply window for a reviewed field's unexplained cross-check.

    Returns the notice (existing or new), or None when the field has no
    ``disagree_unexplained`` cross-check. The item stays internal until ``reply_due``.
    """
    rec = reg.get(FieldRecord, id=rec.id) or rec
    if not any(c.result is CrossCheckResult.disagree_unexplained for c in rec.crosschecks):
        return None
    nid = f"hn:{rec.id}"
    existing = reg.get(HospitalNotice, id=nid)
    if existing is not None:
        return existing
    facs = sorted(m.facility_id for m in reg.select(PolicyFacilityMap, policy_version_id=rec.policy_version_id))
    notice = HospitalNotice(id=nid, field_id=rec.id, field_key=rec.key, facility_ids=facs, notified_at=at,
                            reply_due=add_business_days(at.date(), REPLY_BUSINESS_DAYS), channel="queued")
    reg.add_hospital_notice(notice)
    return notice


def _apply_to_field(reg: Registry, rec: FieldRecord, d: ReviewDecision) -> FieldRecord:
    upd: dict[str, Any] = {"reviewer": d.reviewer, "reviewed_at": d.decided_at}
    if d.action == "approve":
        upd["status"] = FieldStatus.abstained if rec.field.is_abstained else FieldStatus.verified
    elif d.action == "edit":
        upd["field"] = _edited_field(reg, rec, d)
        upd["status"] = FieldStatus.verified
    elif d.action == "abstain":
        reason = d.abstain_reason or "not_addressed"
        upd["field"] = Field[Any](value=None, evidence=rec.field.evidence, confidence=rec.field.confidence, abstain_reason=reason)
        upd["status"] = FieldStatus.abstained
    else:  # ask_hospital: field stays as is (not published as verified)
        return rec
    new = rec.model_copy(update=upd)
    reg.put_field(FieldRecord.model_validate(new.model_dump()))
    return reg.get(FieldRecord, id=rec.id) or new


def _publish_response(reg: Registry, task: ReviewTask) -> None:
    rid = task.id.removeprefix("rt:hospital_correction:")
    resp = reg.get(HospitalResponse, id=rid)
    if resp is not None:
        reg.add_hospital_response(resp.model_copy(update={"published": True}))


def _policy_for_task(reg: Registry, task: ReviewTask) -> str | None:
    if task.facility_id:
        pv = reg.policy_for_facility(task.facility_id)
        return pv.id if pv else None
    return None


def parse_value(raw: str | None) -> Any:
    """Parse the console's value box: JSON when it parses, else the raw string."""
    if raw is None or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()

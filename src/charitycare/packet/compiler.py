"""Packet compiler (design section 5 mechanism 5, Screen 3).

``compile_packet`` turns a code-produced :class:`~charitycare.schemas.Determination`,
the household's facts and the hospital's own application form into one PDF:
cover letter, the filled and read-back form (or, if the form changed, the
blank official form plus a filled answer sheet), a cited document checklist
and an optional hardship-letter draft, plus an ``.ics`` deadline file.

Privacy (section 11): all intermediate files live in a private temp directory
that is deleted before the function returns; only a :class:`PacketMetric`
(template id, counts, timings, cost) may be persisted, and logs carry only
those allowlisted values.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path

import pymupdf

from charitycare import pdftext
from charitycare.llm import LLMBackend
from charitycare.packet.answer_sheet import build_answer_sheet
from charitycare.packet.fill import FillPlan, canonical_values, is_hand
from charitycare.packet.ics import build_ics, deadline_events
from charitycare.packet.letters import (
    checklist_items,
    checklist_text,
    cover_letter,
    federal_deadline,
    hardship_letter,
    text_to_pdf,
)
from charitycare.packet.models import ChecklistItem, PacketRequest, PacketResult
from charitycare.packet.readback import fill_and_verify
from charitycare.schemas import PacketMetric, ReviewTask
from charitycare.store import Registry

log = logging.getLogger("charitycare.packet")

TMP_PREFIX = "ccc-packet-"
STALE_AFTER_S = 30 * 60


def purge_stale(tmp_root: Path | None = None, max_age_s: int = STALE_AFTER_S) -> int:
    """Delete abandoned packet temp dirs older than ``max_age_s`` (design: 30 minutes at most)."""
    root = Path(tmp_root or tempfile.gettempdir())
    n = 0
    now = time.time()
    for d in root.glob(TMP_PREFIX + "*"):
        if d.is_dir() and now - d.stat().st_mtime > max_age_s:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def _concat(parts: list[bytes]) -> tuple[bytes, int]:
    out = pymupdf.open()
    for p in parts:
        with pymupdf.open(stream=p, filetype="pdf") as d:
            out.insert_pdf(d)
    out.set_metadata({"title": "Financial assistance application packet", "producer": "Charity Care Commons", "creator": "Charity Care Commons"})
    n = out.page_count
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data, n


def compile_packet(
    req: PacketRequest,
    *,
    today: date | None = None,
    vision_readback: bool = False,
    hardship_use_llm: bool = False,
    backend: LLMBackend | None = None,
    registry: Registry | None = None,
    tmp_root: Path | None = None,
    extra_cost_usd: float = 0.0,
) -> PacketResult:
    """Compile one packet in memory; see the module docstring."""
    t0 = time.monotonic()
    submitted = req.household.application_date or today or date.today()
    tmp = Path(tempfile.mkdtemp(prefix=TMP_PREFIX, dir=tmp_root))
    try:
        return _compile(req, submitted, tmp, t0, vision_readback, hardship_use_llm, backend, registry, extra_cost_usd)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _compile(
    req: PacketRequest,
    submitted: date,
    tmp: Path,
    t0: float,
    vision: bool,
    hardship_llm: bool,
    backend: LLMBackend | None,
    registry: Registry | None,
    extra_cost: float,
) -> PacketResult:
    notes: list[str] = []
    review_tasks: list[ReviewTask] = []
    form_sha = pdftext.sha256_bytes(req.form_pdf)
    tpl = req.template
    template_ok = tpl is not None and tpl.doc_sha256 == form_sha and bool(tpl.field_map)
    plan = FillPlan(canonical_values(req.applicant, req.household))

    if template_ok:
        assert tpl is not None
        outcome = fill_and_verify(req.form_pdf, tpl, plan, template_id=tpl.doc_sha256, vision=vision, backend=backend)
        form_parts = [outcome.pdf]
        specs = tpl.field_map
        form_mode = "filled"
        template_verified = tpl.verified_by is not None
        if not template_verified:
            notes.append("The form template has not been verified by a reviewer yet, so this packet is not labelled verified.")
    else:
        reason = "no template for this form" if tpl is None or not tpl.field_map else "the form changed since its template was made (hash mismatch)"
        notes.append(f"Answer sheet used: {reason}.")
        sheet_pdf, sheet_tpl = build_answer_sheet(req.hospital_name, tpl)
        outcome = fill_and_verify(sheet_pdf, sheet_tpl, plan, template_id=f"answer-sheet:{form_sha}", vision=vision, backend=backend)
        form_parts = [req.form_pdf, outcome.pdf]
        specs = sheet_tpl.field_map
        form_mode = "answer_sheet"
        template_verified = False
        review_tasks.append(
            ReviewTask(
                id=f"form_template:{form_sha[:16]}",
                kind="form_template",
                template_id=form_sha,
                facility_id=req.determination.facility_id,
                priority="high",
                notes=f"Application form needs a (new) template: {reason}. Previous template: {tpl.doc_sha256 if tpl else 'none'}.",
            )
        )
    (tmp / "form.pdf").write_bytes(outcome.pdf)

    hand = [s for s in specs if is_hand(s)]
    withheld = [(s, plan.values.get(s.key, "")) for s in specs if s.key in outcome.withheld]
    if outcome.withheld:
        notes.append(f"{len(outcome.withheld)} field(s) did not read back exactly after one retry and are marked 'write by hand'.")

    letter = cover_letter(req, submitted=submitted)
    sections = checklist_items(req, hand, withheld)
    checklist = checklist_text(req, sections, submitted=submitted)
    parts = [text_to_pdf(letter, title="Cover letter", small_last=True), *form_parts, text_to_pdf(checklist, title="Checklist", heading_first=True, small_last=True)]
    hardship: str | None = None
    hardship_cost = 0.0
    if req.hardship_circumstances is not None:
        hardship, hardship_cost = hardship_letter(req, submitted=submitted, use_llm=hardship_llm, backend=backend)
        parts.append(text_to_pdf(hardship, title="Hardship letter draft", heading_first=True))
    for i, part in enumerate(parts):
        (tmp / f"part{i}.pdf").write_bytes(part)
    pdf, n_pages = _concat([(tmp / f"part{i}.pdf").read_bytes() for i in range(len(parts))])

    fed = federal_deadline(req.determination, req.household.first_statement_date)
    ics = build_ics(deadline_events(req.hospital_name, fed, req.determination.policy_deadline))

    verified = template_ok and template_verified and outcome.all_filled_match
    latency_ms = int((time.monotonic() - t0) * 1000)
    cost = round(extra_cost + outcome.cost_usd + hardship_cost, 6)
    metric = PacketMetric(
        id=uuid.uuid4().hex,
        template_id=outcome.result.template_id,
        n_fields=len(outcome.result.fields),
        all_match=verified,
        retries=outcome.result.retries,
        latency_ms=latency_ms,
        cost_usd=cost,
    )
    if registry is not None:
        registry.add_packet_metric(metric)
        for task in review_tasks:
            if registry.get(ReviewTask, id=task.id) is None:
                registry.add_review_task(task)
    log.info("packet template=%s fields=%d verified=%s retries=%d latency_ms=%d cost_usd=%.6f", metric.template_id, metric.n_fields, verified, metric.retries, latency_ms, cost)

    flat_items: list[ChecklistItem] = [i for k in ("actions", "documents", "conditions", "channels") for i in sections[k]]
    return PacketResult(
        pdf=pdf,
        ics=ics,
        readback=outcome.result,
        verified=verified,
        template_verified=template_verified,
        form_mode=form_mode,
        write_by_hand=[s.key for s in hand] + list(outcome.withheld),
        checklist=flat_items,
        cover_letter=letter,
        hardship_letter=hardship,
        review_tasks=review_tasks,
        notes=notes,
        page_count=n_pages,
        metric=metric,
    )

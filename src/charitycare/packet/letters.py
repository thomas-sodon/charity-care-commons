"""Cover letter, document checklist and hardship-letter draft (design section 8 S13, section 11).

All text comes from Jinja templates in ``packet/templates`` and is checked with
:func:`charitycare.wording.assert_safe_wording` (verbatim hospital quotes, in
curly quotes, are exempt). The cover letter *requests* ECA suspension and a
written notice of missing information under 26 CFR 1.501(r)-6(c)(5)-(6); it
never asserts an outcome. Letters render to PDF with ``pymupdf.Story``.
"""

from __future__ import annotations

import html
import io
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

import jinja2
import pymupdf
from pydantic import BaseModel, ConfigDict

from charitycare import pdftext
from charitycare.config import DISCLAIMER_TEXT, PROMPT_VERSIONS
from charitycare.llm import LLMBackend, LLMError, LLMRequest, get_backend
from charitycare.packet.models import ChecklistItem, PacketRequest
from charitycare.packet.pii import mask_pii
from charitycare.schemas import CompiledPolicy, Determination, Evidence, FormFieldSpec, RequiredDoc
from charitycare.wording import OUTCOME_LABELS, assert_safe_wording, find_banned_wording

FEDERAL_WINDOW_DAYS = 240
HARDSHIP_PROMPT_VERSION = PROMPT_VERSIONS["hardship_letter"]

DOC_LABELS: dict[str, str] = {
    "pay_stub": "Pay stubs",
    "tax_return": "Federal income tax return",
    "w2": "W-2 forms",
    "bank_statement": "Bank statements",
    "photo_id": "Photo identification",
    "proof_of_residency": "Proof of residency",
    "medicaid_denial": "Medicaid decision or denial letter",
    "unemployment_benefits": "Unemployment benefit statement",
    "social_security_statement": "Social Security benefit statement",
    "letter_of_support": "Letter of support",
    "other": "Other document",
}


@lru_cache(maxsize=1)
def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.PackageLoader("charitycare.packet", "templates"),
        autoescape=False,  # plain-text templates; HTML escaping happens in text_to_pdf
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
    )


def render(template: str, **ctx: Any) -> str:
    """Render a text template and enforce the wording rules."""
    text = _env().get_template(template).render(**ctx)
    assert_safe_wording(text, ignore_quoted=True)
    return text


def cite(ev: Evidence | None) -> str | None:
    """'§6.1, p. 5' style citation for one evidence quote."""
    if ev is None:
        return None
    sec = f"§{ev.section_label}, " if ev.section_label else ""
    return f"{sec}p. {ev.page}"


def cite_with_quote(ev: Evidence | None, limit: int = 160) -> str | None:
    if ev is None:
        return None
    q = ev.quote if len(ev.quote) <= limit else ev.quote[: limit - 1].rstrip() + "…"
    return f"{cite(ev)}: “{q}”"


def federal_deadline(det: Determination, first_statement: date | None) -> date | None:
    """The engine's federal deadline, or 240 days after the first post-discharge statement."""
    if det.federal_deadline:
        return det.federal_deadline
    return first_statement + timedelta(days=FEDERAL_WINDOW_DAYS) if first_statement else None


def _channel(policy: CompiledPolicy | None, kind: str) -> str | None:
    if policy is None or not policy.submission_channels.value:
        return None
    return next((c.detail for c in policy.submission_channels.value if c.kind == kind and c.detail), None)


def _mail_address(policy: CompiledPolicy | None) -> str | None:
    detail = _channel(policy, "mail")
    if not detail:
        return None
    # "Financial Assistance Office, 7700 Medical Example Drive, San Antonio, TX 78229" -> address lines
    parts = [p.strip() for p in detail.split(",") if p.strip()]
    if parts and "office" in parts[0].lower():
        parts = parts[1:]
    if len(parts) >= 3:
        return f"{', '.join(parts[:-2])}\n{parts[-2]}, {parts[-1]}"
    return ", ".join(parts)


def cover_letter(req: PacketRequest, *, submitted: date) -> str:
    """The ECA-suspension request letter. Never states an outcome."""
    pol = req.policy
    win = pol.application_window if pol else None
    win_ev = win.evidence[0] if win and win.evidence and not win.is_abstained else None
    title = pol.policy_title.value if pol and pol.policy_title.value else "your Financial Assistance Policy"
    first = req.household.first_statement_date
    return render(
        "cover_letter.txt.j2",
        submitted=submitted.isoformat(),
        hospital_name=req.hospital_name,
        facility_name=req.facility_name,
        mail_address=_mail_address(pol),
        fax=_channel(pol, "fax"),
        patient_name=req.applicant.patient_name,
        policy_title=title,
        policy_as_of=req.policy_as_of.isoformat() if req.policy_as_of else None,
        first_statement=first.isoformat() if first else None,
        federal_deadline=(d.isoformat() if (d := federal_deadline(req.determination, first)) else None),
        window_quote=win_ev.quote if win_ev else None,
        window_cite=cite(win_ev),
        has_phone=bool(req.applicant.phone),
    )


def _evidence_for(doc: RequiredDoc, evidence: list[Evidence]) -> Evidence | None:
    want = pdftext.normalize_text(doc.description)
    for ev in evidence:
        q = pdftext.normalize_text(ev.quote)
        if want and (want in q or q in want):
            return ev
    return None


def checklist_items(
    req: PacketRequest, hand_specs: list[FormFieldSpec], withheld: list[tuple[FormFieldSpec, str]]
) -> dict[str, list[ChecklistItem]]:
    """Checklist sections: actions, documents, conditions, channels (each item cited where possible)."""
    pol = req.policy
    actions: list[ChecklistItem] = []
    sig = [s for s in hand_specs if s.key in {"signature", "signature_date"}]
    if sig:
        pages = sorted({s.page for s in sig})
        actions.append(ChecklistItem(text=f"Patient or guarantor signs and dates the application (page {', '.join(map(str, pages))}).", kind="action"))
    other = [s for s in hand_specs if s.key not in {"signature", "signature_date"}]
    if other:
        actions.append(
            ChecklistItem(text="Write these by hand (left blank on purpose): " + "; ".join(s.label for s in other) + ".", kind="action")
        )
    if withheld:
        actions.append(
            ChecklistItem(
                text="Write these by hand (could not be printed and verified): "
                + "; ".join(f"{s.label}: {v}" for s, v in withheld)
                + ".",
                kind="action",
            )
        )
    actions.append(ChecklistItem(text="Review every answer on the form and correct anything that is wrong.", kind="action"))
    actions.append(ChecklistItem(text="Keep a copy of everything you send and note the date you sent it.", kind="action"))

    documents: list[ChecklistItem] = []
    rd = pol.required_documents if pol else None
    if rd is not None and not rd.is_abstained:
        for doc in rd.value or []:
            label = DOC_LABELS.get(doc.doc_type, "Document")
            text = f"{label}: {doc.description}"
            ev = _evidence_for(doc, rd.evidence)
            same = ev is not None and pdftext.normalize_text(ev.quote) == pdftext.normalize_text(doc.description)
            documents.append(ChecklistItem(text=text, citation=cite(ev) if same else cite_with_quote(ev), kind="document"))
    reason = rd.abstain_reason if rd is not None else None
    no_docs = (
        f"The policy does not list required documents ({reason or 'not compiled'}). Ask the hospital which documents to attach."
    )

    conditions = [ChecklistItem(text=c, kind="condition") for c in req.determination.unevaluated_conditions]

    channels: list[ChecklistItem] = []
    sc = pol.submission_channels if pol else None
    if sc is not None and sc.value:
        for i, ch in enumerate(sc.value):
            ev = sc.evidence[i] if i < len(sc.evidence) else None
            channels.append(ChecklistItem(text=f"{ch.kind.replace('_', ' ').capitalize()}: {ch.detail or 'see policy'}", citation=cite(ev), kind="channel"))
    else:
        channels.append(ChecklistItem(text="Send it to the hospital's financial assistance office (address on the application).", kind="channel"))

    return {"actions": actions, "documents": documents, "conditions": conditions, "channels": channels, "_no_docs": [ChecklistItem(text=no_docs)]}


def checklist_text(req: PacketRequest, sections: dict[str, list[ChecklistItem]], *, submitted: date) -> str:
    first = req.household.first_statement_date
    fed = federal_deadline(req.determination, first)
    deadline = None
    if fed:
        deadline = f"Federal minimum application period: at least until {fed.isoformat()} (240 days after the first post-discharge statement)."
        pd = req.determination.policy_deadline
        if pd and pd != fed:
            deadline += f" The hospital's policy deadline by our count: {pd.isoformat()}."
    as_of = (req.policy_as_of or submitted).isoformat()
    return render(
        "checklist.txt.j2",
        hospital_name=req.hospital_name,
        actions=sections["actions"],
        documents=sections["documents"],
        no_documents_note=sections["_no_docs"][0].text,
        conditions=sections["conditions"],
        channels=sections["channels"],
        deadline_text=deadline,
        disclaimer=DISCLAIMER_TEXT.format(as_of=as_of),
    )


class HardshipDraft(BaseModel):
    """LLM output for the optional hardship-letter body."""

    model_config = ConfigDict(extra="forbid")
    body: str


HARDSHIP_SYSTEM = (
    "You help a patient draft a short, factual financial-hardship statement to a hospital's financial assistance "
    "office. Write 2-4 plain sentences in the first person from the notes provided. Do not add facts, numbers or "
    "diagnoses that are not in the notes. Do not claim the patient qualifies for anything or predict a decision. "
    "Never include Social Security numbers, account numbers or dates of birth."
)


def hardship_letter(req: PacketRequest, *, submitted: date, use_llm: bool = False, backend: LLMBackend | None = None) -> tuple[str, float]:
    """A hardship-letter draft for the patient to edit. Returns (text, LLM cost)."""
    notes = mask_pii(req.hardship_circumstances or "").strip()
    body, cost = notes, 0.0
    if use_llm and notes:
        llm_req = LLMRequest(
            prompt_version=HARDSHIP_PROMPT_VERSION,
            model_tier="sonnet",
            doc_sha256=pdftext.sha256_bytes(notes.encode("utf-8")),
            system=HARDSHIP_SYSTEM,
            user=f"Notes from the navigator:\n{notes}",
            output_schema=HardshipDraft,
            max_tokens=800,
            effort="low",
        )
        try:
            res = (backend or get_backend()).complete(llm_req)
            cost = res.cost_usd
            draft = mask_pii(res.output.body.strip())
            if draft and not find_banned_wording(draft):
                body = draft
        except LLMError:
            pass
    text = render(
        "hardship_letter.txt.j2",
        submitted=submitted.isoformat(),
        hospital_name=req.hospital_name,
        patient_name=req.applicant.patient_name,
        circumstances=body,
        household_size=req.household.household_size,
    )
    return text, cost


def navigator_summary(det: Determination, as_of: date) -> str:
    """Screen 3 step 2 text: outcome label, arithmetic, conditions, deadline, disclaimer."""
    lines = [OUTCOME_LABELS[det.outcome]]
    if det.arithmetic:
        lines.append(det.arithmetic)
    lines += [f"Not evaluated: {c}" for c in det.unevaluated_conditions]
    if det.provisional_fields_used:
        lines.append("Based on fields not yet verified by a reviewer: " + ", ".join(det.provisional_fields_used))
    if det.federal_deadline:
        lines.append(f"Federal minimum application deadline: {det.federal_deadline.isoformat()} (240 days)")
    lines.append("The hospital makes the final decision. Not legal advice.")
    lines.append(DISCLAIMER_TEXT.format(as_of=as_of.isoformat()))
    text = "\n".join(lines)
    assert_safe_wording(text)
    return text


# --------------------------------------------------------------------------
# PDF rendering
# --------------------------------------------------------------------------

_CSS = """
body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.35; }
h1 { font-size: 13pt; margin-bottom: 6pt; }
p { margin: 0 0 7pt 0; }
p.small { font-size: 8.5pt; color: #444444; }
"""


def text_to_pdf(text: str, *, title: str, heading_first: bool = False, small_last: bool = False) -> bytes:
    """Lay plain text out as Letter-size pages (one paragraph per blank-line block).

    ``heading_first`` renders the first line as a heading; ``small_last`` sets the
    last paragraph (the disclaimer) in small type.
    """
    body = []
    if heading_first:
        first, _, text = text.partition("\n")
        body.append(f"<h1>{html.escape(first)}</h1>")
    paras = text.split("\n\n")
    for i, p in enumerate(paras):
        if not p.strip():
            continue
        cls = ' class="small"' if small_last and i == len(paras) - 1 else ""
        body.append(f"<p{cls}>" + "<br/>".join(html.escape(line) for line in p.strip("\n").split("\n")) + "</p>")
    story = pymupdf.Story(html="<body>" + "".join(body) + "</body>", user_css=_CSS)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    mediabox = pymupdf.paper_rect("letter")
    where = mediabox + (60, 60, -60, -60)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    with pymupdf.open(stream=buf.getvalue(), filetype="pdf") as doc:
        doc.set_metadata({"title": title, "producer": "Charity Care Commons", "creator": "Charity Care Commons"})
        return doc.tobytes(garbage=3, deflate=True)

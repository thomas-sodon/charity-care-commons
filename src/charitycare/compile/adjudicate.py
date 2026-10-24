"""Opus-class adjudicator (step S7): an advisory draft for the human reviewer.

Invoked on a Schedule H disagreement, a low-confidence field, or a
multi-facility policy. The draft must quote both sources: a policy quote that
passes the same quote verifier as compiled fields, and a Schedule H line copied
exactly from the context block we supply. A draft that fails either check is
discarded. Without a key (replay mode) a recorded fixture is used; if none
exists, a deterministic code draft is produced instead, labelled as such.
Nothing here changes a cross-check result or a field status.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PField

from charitycare.compile.crosscheck import worst
from charitycare.compile.prompts import ADJUDICATE_VERSION, adjudicate_system
from charitycare.compile.textlayer import DocText, page_text_xml
from charitycare.compile.verify import normalize_variants, verify_evidence
from charitycare.llm import DocumentPart, LLMBackend, LLMError, LLMRequest
from charitycare.schemas import (
    CrossCheck,
    CrossCheckResult,
    Evidence,
    Field,
    ScheduleHFacility,
)
from charitycare.wording import find_banned_wording

LOW_CONFIDENCE = 0.85
MAPPING_SUBJECT = "facility_mapping"


class Adjudication(BaseModel):
    """LLM output schema for one adjudication (advisory only)."""

    model_config = ConfigDict(extra="forbid")

    subject: str = PField(description="Field key under review, or facility_mapping")
    recommended_result: CrossCheckResult
    policy_evidence: Evidence = PField(description="Verbatim policy quote with its page")
    schedule_h_quote: str = PField(description="Schedule H line copied exactly from the context block")
    rationale: str
    confidence: float = PField(ge=0.0, le=1.0)


@dataclass
class AdjudicationRecord:
    subject: str
    adjudication: Adjudication | None
    source: Literal["live", "replay", "record", "heuristic"]
    valid: bool
    problems: list[str] = field(default_factory=list)
    cost_usd: float = 0.0

    def summary(self) -> str:
        if not self.adjudication:
            return "adjudicator: no draft"
        a = self.adjudication
        return f"adjudicator ({self.source}): {a.recommended_result.value} - {a.rationale}"


def _line_label(attr: str) -> tuple[str, str]:
    _, line, *rest = attr.split("_")
    return line, "_".join(rest)


def schedule_h_context(sec: ScheduleHFacility) -> list[str]:
    """Exact Schedule H lines the adjudicator may quote, one per answered line."""
    lines = [
        f"return {sec.object_id}, tax period {sec.tax_period_begin} to {sec.tax_period_end}, "
        + (f"reporting group {sec.group_letter}" if sec.group_letter else "single facility"),
        f"section A facility_names = {'; '.join(sec.facility_names)}",
    ]
    for attr in type(sec).model_fields:
        if attr.startswith("line_"):
            v = getattr(sec, attr)
            if v is None and attr.startswith("line_13") and sec.line_13_criteria_explained and attr != "line_13_criteria_explained":
                v = False  # unchecked box omitted from the XML
            if v is not None and v != []:
                line, name = _line_label(attr)
                lines.append(f"line {line} {name} = {v:g}" if isinstance(v, float) else f"line {line} {name} = {v}")
    return lines


def validate(adj: Adjudication, docs: dict[str, DocText], context: list[str]) -> list[str]:
    """Problems with a draft (empty means both quotes check out and the wording is neutral)."""
    problems = []
    qc = verify_evidence(adj.policy_evidence, docs)
    if not qc.ok:
        problems.append("policy quote: " + qc.describe())
    needle = normalize_variants(adj.schedule_h_quote)["plain"]
    hay = normalize_variants("\n".join(context))["plain"]
    if len(needle) < 8 or needle not in hay:
        problems.append(f"Schedule H quote {adj.schedule_h_quote!r} is not a line of the supplied context")
    if find_banned_wording(adj.rationale):
        problems.append("rationale uses banned wording")
    return problems


def heuristic_draft(subject: str, f: Field[Any] | None, checks: list[CrossCheck], context: list[str], doc_sha: str) -> Adjudication | None:
    """Deterministic draft used when no model output is available."""
    ev = f.evidence[0] if f and f.evidence else None
    if ev is None:
        return None
    result = worst(checks) or CrossCheckResult.not_reported
    line = next((c.external_ref.split("|")[1].removeprefix("Part V ") for c in checks if c.result == result), None)
    default = context[1] if subject in (MAPPING_SUBJECT, "facilities_covered") else context[0]
    sh = next((ln for ln in context if line and ln.startswith(line + " ")), default)
    if result is CrossCheckResult.disagree_policy_newer_than_filing:
        why = "The policy was adopted after the filing period ended, so the filing describes an earlier policy."
    elif result is CrossCheckResult.disagree_filing_lag_possible:
        why = "The policy date is unknown, so the filing may describe an earlier policy; reviewer to decide."
    elif result is CrossCheckResult.disagree_unexplained:
        why = "The filing period overlaps the policy's effective date and neither source explains the difference; discrepancy for review."
    elif result is CrossCheckResult.agree:
        why = "The policy text and the Schedule H answer are consistent."
    else:
        why = "Schedule H does not answer this line; verify the field against the quote."
    return Adjudication(subject=subject, recommended_result=result, policy_evidence=ev, schedule_h_quote=sh,
                        rationale=f"Code draft (no model output available). {why}", confidence=0.5)


def adjudicate(
    subject: str,
    *,
    field_value: Field[Any] | None,
    checks: list[CrossCheck],
    section: ScheduleHFacility | None,
    doc: DocText,
    docs: dict[str, DocText],
    backend: LLMBackend,
) -> AdjudicationRecord:
    """Draft an advisory adjudication for one subject; falls back to a code draft."""
    context = schedule_h_context(section) if section else ["no Schedule H filing matched", "section A facility_names = "]
    payload = {
        "subject": subject,
        "compiled": field_value.model_dump(mode="json") if field_value else None,
        "crosschecks": [c.model_dump(mode="json") for c in checks],
    }
    req: LLMRequest[Adjudication] = LLMRequest(
        prompt_version=ADJUDICATE_VERSION,
        model_tier="opus",
        doc_sha256=doc.sha256,
        system=adjudicate_system(),
        documents=(
            DocumentPart("text", page_text_xml(doc, tuple(p.page for p in doc.pages)), title="policy"),
            DocumentPart("text", "\n".join(context), title="schedule_h_context"),
        ),
        user="Adjudicate this item. Use doc_sha256 \"" + doc.sha256 + "\".\n" + json.dumps(payload, indent=2, default=str),
        output_schema=Adjudication,
        variant=subject,
        max_tokens=4000,
        effort="high",
    )
    problems: list[str] = []
    try:
        res = backend.complete(req)
        problems = validate(res.output, docs, context)
        if not problems:
            return AdjudicationRecord(subject, res.output, res.backend, True, cost_usd=res.cost_usd)
        problems = ["model draft discarded: " + p for p in problems]
    except LLMError as exc:
        problems = [f"model unavailable ({type(exc).__name__})"]
    draft = heuristic_draft(subject, field_value, checks, context, doc.sha256)
    if draft is None:
        return AdjudicationRecord(subject, None, "heuristic", False, problems + ["no verified policy quote to cite"])
    more = validate(draft, docs, context)
    return AdjudicationRecord(subject, draft if not more else None, "heuristic", not more, problems + more)


def subjects_for(
    fields: dict[str, Field[Any]], checks: dict[str, list[CrossCheck]], *, multi_facility: bool
) -> list[str]:
    """Which subjects need an adjudication: disagreements, low confidence, multi-facility."""
    out = []
    for key, f in fields.items():
        disagree = any(c.result in (CrossCheckResult.disagree_unexplained, CrossCheckResult.disagree_policy_newer_than_filing,
                                    CrossCheckResult.disagree_filing_lag_possible)
                       for c in checks.get(key, []))
        low = not f.is_abstained and f.confidence < LOW_CONFIDENCE
        if disagree or low:
            out.append(key)
    if multi_facility and "facilities_covered" not in out:
        out.append("facilities_covered")
    return out

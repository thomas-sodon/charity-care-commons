"""Plain-text rendering of a determination, with the versioned disclaimer (section 11)."""

from __future__ import annotations

from datetime import date

from charitycare.config import DISCLAIMER_TEXT, DISCLAIMER_VERSION
from charitycare.engine.core import NOT_MET_PREFIX
from charitycare.schemas import Determination
from charitycare.wording import OUTCOME_LABELS, assert_safe_wording


def disclaimer(as_of: date | str | None) -> str:
    """The section 11 disclaimer for a policy captured on ``as_of``."""
    when = as_of.isoformat() if isinstance(as_of, date) else (as_of or "the date shown")
    return DISCLAIMER_TEXT.format(as_of=when)


def render_text(det: Determination, *, policy_as_of: date | str | None = None) -> str:
    """Navigator-facing summary. Raises if generated text uses banned wording."""
    lines = [OUTCOME_LABELS[det.outcome].upper()]
    not_met = sum(1 for n in det.notes if n.startswith(NOT_MET_PREFIX))
    if not_met:
        lines.append(f"CAUTION: {not_met} other policy condition(s) likely not met; see below. The income result alone does not settle it.")
    lines.append(det.arithmetic)
    if det.matched_tier is not None and det.citations:
        c = det.citations[0]
        lines.append(f"  Policy text: “{c.quote}” (p.{c.page})")
    for n in det.notes:
        lines.append(f"- {n}")
    for u in det.unevaluated_conditions:
        lines.append(f"- NOT EVALUATED: {u}")
    if det.federal_deadline:
        lines.append(f"Apply by: {det.federal_deadline.isoformat()} (federal minimum, 240 days)"
                     + (f"; policy window ends {det.policy_deadline.isoformat()}" if det.policy_deadline and det.policy_deadline != det.federal_deadline else ""))
    lines.append("Hospital decides. Not legal advice.")
    lines.append(f"{disclaimer(policy_as_of)} [{det.disclaimer_version or DISCLAIMER_VERSION}]")
    text = "\n".join(lines)
    assert_safe_wording(text)
    return text

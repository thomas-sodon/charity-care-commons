"""Demo-only stand-in for the human reviewer (H1-H3), driven by the synthetic gold labels.

In production every ``verified`` status comes from a person in the review console.
For the offline demo, :func:`auto_review` plays that person *explicitly*: every
decision it writes carries the reviewer id ``demo:gold-reviewer`` (and a note
saying so), goes through the same :func:`charitycare.api.review.apply_decision`
path as the console, and lands in the changelog like any other decision.

Rules (deliberately conservative, so the interesting cases stay in the queue):

* ``field_verify`` tasks are approved only when the compiled value (or abstention)
  equals the gold label for that hospital;
* ``crosscheck_disagreement`` tasks are approved only when the value matches the
  policy text per gold: the field becomes verified, while the Schedule H
  cross-check row stays a public "discrepancy for review" awaiting the hospital's reply;
* ``quote_unverified`` and ``round_trip_mismatch`` tasks (the corpus's deliberate
  errors and the stale printed income table) are never touched: they stay open;
* spot-check batches are promoted once their sampled task is approved;
* form templates are marked verified only when they match the gold template exactly
  (F1 = 1.0); otherwise a ``form_template`` task stays open.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from charitycare.schemas import FieldRecord, FormTemplate, ReviewDecision, ReviewTask, utcnow
from charitycare.store import Registry

DEMO_REVIEWER = "demo:gold-reviewer"
DEMO_NOTE = "Automated demo review against synthetic gold labels (not a human decision)."
AUTO_KINDS = ("field_verify", "crosscheck_disagreement")


@dataclass
class ReviewSummary:
    """What the demo reviewer did, for the CLI summary."""

    approved: Counter[str] = field(default_factory=Counter)
    left_open: Counter[str] = field(default_factory=Counter)
    promoted: int = 0
    templates_verified: int = 0
    templates_open: int = 0
    mismatches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"approved": dict(self.approved), "left_open": dict(self.left_open), "spot_check_promoted": self.promoted,
                "templates_verified": self.templates_verified, "templates_open": self.templates_open,
                "gold_mismatches": self.mismatches}


def load_gold(demo_dir: Path) -> dict[str, dict[str, Any]]:
    """Gold label files keyed by facility id."""
    out: dict[str, dict[str, Any]] = {}
    for p in sorted((demo_dir / "gold").glob("*.json")):
        g = json.loads(p.read_text(encoding="utf-8"))
        for fid in g["facility_ids"]:
            out[fid] = g
    return out


def matches_gold(rec: FieldRecord, gold_field: dict[str, Any] | None) -> bool:
    """Value equality with the gold label; an abstention matches only a gold abstention."""
    if gold_field is None:
        return False
    if rec.field.is_abstained:
        return gold_field.get("abstain_reason") is not None
    if gold_field.get("abstain_reason") is not None:
        return False
    return rec.field.model_dump(mode="json")["value"] == gold_field.get("value")


def _gold_for_policy(reg: Registry, gold: dict[str, dict[str, Any]], policy_version_id: str) -> dict[str, Any] | None:
    from charitycare.schemas import PolicyFacilityMap

    for m in reg.select(PolicyFacilityMap, policy_version_id=policy_version_id):
        if m.facility_id in gold:
            return gold[m.facility_id]
    return None


def auto_review(reg: Registry, demo_dir: Path, *, demo: bool = False) -> ReviewSummary:
    """Approve gold-matching review tasks as the demo reviewer. Requires ``demo=True``."""
    if not demo:
        raise PermissionError("auto_review only runs with an explicit demo flag; real reviews go through the console")
    from charitycare.api.review import ReviewError, apply_decision
    from charitycare.compile.review import promote_spot_checked

    gold = load_gold(demo_dir)
    summary = ReviewSummary()
    policies: set[str] = set()
    for task in sorted(reg.select(ReviewTask, state="open"), key=lambda t: t.id):
        rec = reg.get(FieldRecord, id=task.field_id) if task.field_id else None
        if task.kind not in AUTO_KINDS or rec is None:
            summary.left_open[task.kind] += 1
            continue
        g = _gold_for_policy(reg, gold, rec.policy_version_id)
        if not matches_gold(rec, (g or {}).get("policy", {}).get(rec.key)):
            summary.left_open[task.kind] += 1
            summary.mismatches.append(rec.id)
            continue
        note = DEMO_NOTE
        if task.kind == "crosscheck_disagreement":
            note += " Value matches the policy text; the IRS-filing discrepancy stays published for the hospital's reply."
        try:
            apply_decision(reg, ReviewDecision(task_id=task.id, field_id=rec.id, action="approve", reviewer=DEMO_REVIEWER,
                                               note=note, decided_at=utcnow()))
        except ReviewError:  # e.g. a round-trip mismatch on the same field is still open for a human
            summary.left_open[task.kind] += 1
            continue
        summary.approved[task.kind] += 1
        policies.add(rec.policy_version_id)
    for pv in sorted(policies):
        summary.promoted += len(promote_spot_checked(reg, pv, DEMO_REVIEWER))
    return summary


def verify_templates(reg: Registry, demo_dir: Path, *, demo: bool = False) -> tuple[int, int]:
    """Build a form template for every stored application form; mark it verified only on an exact gold match.

    Returns ``(verified, left_for_review)``. Requires ``demo=True``.
    """
    if not demo:
        raise PermissionError("verify_templates only runs with an explicit demo flag")
    from charitycare.compile.pipeline import default_pdf_loader
    from charitycare.compile.review import impact, make_task
    from charitycare.packet.template import build_template, score_template
    from charitycare.schemas import PolicyVersion

    gold_by_sha: dict[str, dict[str, Any]] = {}
    for g in load_gold(demo_dir).values():
        for doc in g["documents"].values():
            if doc.get("form_template"):
                gold_by_sha[doc["sha256"]] = doc["form_template"]
    verified = left = 0
    seen: set[str] = set()
    for pv in reg.select(PolicyVersion):
        for name, sha in sorted(pv.doc_shas.items()):
            if not name.startswith("application") or sha in seen:
                continue
            seen.add(sha)
            doc = reg.get_document_version(sha)
            if doc is None:
                continue
            build = build_template(default_pdf_loader(doc), language=doc.language or "en")
            tpl = build.template
            gold = gold_by_sha.get(sha)
            if gold is not None and score_template(tpl, FormTemplate.model_validate(gold))["f1"] == 1.0:
                tpl = tpl.model_copy(update={"verified_by": DEMO_REVIEWER, "verified_at": utcnow()})
                verified += 1
            else:
                left += 1
                reg.add_review_task(make_task("form_template", sha, "high", f"form template for {name} ({build.method}) needs review",
                                              impact_score=impact("form_template", None)).model_copy(update={"template_id": sha}))
            reg.upsert_form_template(tpl)
    return verified, left


__all__ = ["DEMO_REVIEWER", "ReviewSummary", "auto_review", "load_gold", "matches_gold", "verify_templates"]

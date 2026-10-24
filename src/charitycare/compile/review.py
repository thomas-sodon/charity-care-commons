"""Provisional/verified gating, review tasks and spot-check sampling (step S8).

Code never marks a field ``verified``; a reviewer does (human-in-the-loop H1).
This module decides each field's initial status and which review tasks to open:

* abstained fields: a low-priority task to confirm the abstention (a
  ``quote_unverified`` task, high priority for determination fields, when the
  abstention was forced by the quote gate);
* ``disagree_unexplained`` cross-checks and round-trip mismatches: high;
* fields that agree with Schedule H at confidence >= 0.9 form a *spot-check
  batch*: a deterministic 10% sample (at least one, chosen by sha256 of the field
  id) gets a task; when every sampled task is approved, the rest of the batch is
  promoted to verified by :func:`promote_spot_checked` on the reviewer's behalf;
* every other provisional field: one ``field_verify`` task.

Tasks carry an impact score (``[impact N]`` prefix in ``notes``) and
:func:`review_queue` sorts by priority, then impact.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from charitycare.schemas import (
    DETERMINATION_FIELDS,
    CrossCheck,
    CrossCheckResult,
    FieldRecord,
    FieldStatus,
    ReviewKind,
    ReviewTask,
    utcnow,
)
from charitycare.store import Registry

SPOT_CHECK_RATE = 0.10
SPOT_CHECK_MIN_CONFIDENCE = 0.9
SPOT_TAG = "spot-check sample"
_IMPACT = re.compile(r"^\[impact (\d+)\]")

PACKET_FIELDS = ("required_documents", "submission_channels", "application_window")
_KIND_WEIGHT: dict[str, int] = {
    "crosscheck_disagreement": 50,
    "round_trip_mismatch": 45,
    "quote_unverified": 40,
    "facility_mapping": 35,
    "change_review": 30,
    "hospital_correction": 30,
    "field_verify": 10,
    "form_template": 20,
}


def impact(kind: str, key: str | None, n_facilities: int = 1, confidence: float | None = None) -> int:
    """Higher means more patients or decisions are affected if the item is wrong."""
    score = _KIND_WEIGHT.get(kind, 10)
    if key in DETERMINATION_FIELDS:
        score += 20
    elif key in PACKET_FIELDS:
        score += 10
    score += 5 * max(1, n_facilities)
    if confidence is not None:
        score += round((1 - confidence) * 10)
    return score


def task_impact(task: ReviewTask) -> int:
    """The task's impact score: the ``impact`` column, else the legacy ``[impact N]`` notes prefix."""
    if task.impact is not None:
        return task.impact
    m = _IMPACT.match(task.notes or "")
    return int(m.group(1)) if m else 0


def task_id(kind: str, subject: str) -> str:
    return f"rt:{kind}:{subject}"


def make_task(kind: ReviewKind, subject: str, priority: str, notes: str, *, impact_score: int,
              field_id: str | None = None, facility_id: str | None = None, at: datetime | None = None,
              batch: Iterable[str] = ()) -> ReviewTask:
    # The "[impact N]" notes prefix is kept for readers of older registries; ``impact`` is authoritative.
    return ReviewTask(id=task_id(kind, subject), kind=kind, field_id=field_id, facility_id=facility_id,
                      priority=priority, notes=f"[impact {impact_score}] {notes}", impact=impact_score,  # type: ignore[arg-type]
                      batch=sorted(batch), created_at=at or utcnow())


def is_spot_checkable(rec: FieldRecord) -> bool:
    """Agrees with Schedule H (and nothing disagrees) at high confidence."""
    results = {c.result for c in rec.crosschecks}
    return (
        rec.status is FieldStatus.provisional
        and CrossCheckResult.agree in results
        and not results & {CrossCheckResult.disagree_unexplained, CrossCheckResult.disagree_policy_newer_than_filing,
                           CrossCheckResult.disagree_filing_lag_possible}
        and rec.field.confidence >= SPOT_CHECK_MIN_CONFIDENCE
    )


def spot_check_sample(field_ids: Iterable[str], rate: float = SPOT_CHECK_RATE) -> list[str]:
    """Deterministic sample: the ``ceil(rate * n)`` ids with the smallest sha256 digest."""
    ids = sorted(set(field_ids))
    if not ids:
        return []
    k = max(1, math.ceil(rate * len(ids)))
    return sorted(ids, key=lambda i: hashlib.sha256(i.encode()).hexdigest())[:k]


@dataclass
class FieldContext:
    """What the gate knows about one field when it plans review work."""

    record: FieldRecord
    forced_abstention: bool = False  # the quote gate nulled the value
    problems: tuple[str, ...] = ()
    round_trip_mismatch: str | None = None
    adjudication: str | None = None


def _summary(rec: FieldRecord, checks: list[CrossCheck]) -> str:
    if rec.field.is_abstained:
        val = "abstained" + (f" ({rec.field.abstain_reason})" if rec.field.abstain_reason else "")
    else:
        val = json.dumps(rec.field.model_dump(mode="json")["value"], ensure_ascii=False)
        val = val if len(val) <= 160 else val[:157] + "..."
    parts = [f"{rec.key}: extracted {val}", f"conf {rec.field.confidence:.2f}"]
    for c in checks:
        parts.append(f"Sched H {c.external_ref.split('|')[1]} {c.external_value} -> {c.result.value}")
    return " | ".join(parts)


def plan_tasks(contexts: list[FieldContext], *, n_facilities: int = 1, at: datetime | None = None) -> list[ReviewTask]:
    """Review tasks for one compiled policy version, including the spot-check sample."""
    tasks: list[ReviewTask] = []
    batch = [c.record for c in contexts if is_spot_checkable(c.record) and not c.round_trip_mismatch]
    sample = set(spot_check_sample([r.id for r in batch]))
    batch_ids = {r.id for r in batch}
    for ctx in contexts:
        rec, key = ctx.record, ctx.record.key
        base = _summary(rec, rec.crosschecks)
        if ctx.adjudication:
            base += f" | {ctx.adjudication}"
        conf = rec.field.confidence
        if ctx.forced_abstention:
            pr = "high" if key in DETERMINATION_FIELDS else "medium"
            tasks.append(make_task("quote_unverified", rec.id, pr, base + " | " + "; ".join(ctx.problems),
                                   impact_score=impact("quote_unverified", key, n_facilities, conf), field_id=rec.id, at=at))
        unexplained = [c for c in rec.crosschecks if c.result is CrossCheckResult.disagree_unexplained]
        newer = [c for c in rec.crosschecks if c.result in (CrossCheckResult.disagree_policy_newer_than_filing,
                                                            CrossCheckResult.disagree_filing_lag_possible)]
        if unexplained or newer:
            pr = "high" if unexplained else "medium"
            tasks.append(make_task("crosscheck_disagreement", rec.id, pr, base,
                                   impact_score=impact("crosscheck_disagreement", key, n_facilities, conf), field_id=rec.id, at=at))
        if ctx.round_trip_mismatch:
            tasks.append(make_task("round_trip_mismatch", rec.id, "high", base + " | round trip: " + ctx.round_trip_mismatch,
                                   impact_score=impact("round_trip_mismatch", key, n_facilities, conf), field_id=rec.id, at=at))
        if rec.id in batch_ids:
            if rec.id in sample:
                tasks.append(make_task("field_verify", rec.id, "low",
                                       f"{SPOT_TAG} (10% of {len(batch)}; batch: {','.join(sorted(r.key for r in batch))}) | {base}",
                                       impact_score=impact("field_verify", key, n_facilities, conf), field_id=rec.id, at=at,
                                       batch=batch_ids))
            continue
        if ctx.forced_abstention or unexplained or ctx.round_trip_mismatch:
            continue
        if rec.field.is_abstained:
            tasks.append(make_task("field_verify", rec.id, "low", "confirm abstention | " + base,
                                   impact_score=impact("field_verify", key, n_facilities, None), field_id=rec.id, at=at))
            continue
        pr = "high" if key in DETERMINATION_FIELDS else ("medium" if conf < 0.85 or key in PACKET_FIELDS else "low")
        tasks.append(make_task("field_verify", rec.id, pr, base, impact_score=impact("field_verify", key, n_facilities, conf),
                               field_id=rec.id, at=at))
    return tasks


def review_queue(registry: Registry) -> list[ReviewTask]:
    """Open review tasks sorted by priority, then impact (highest first), then age."""
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(registry.select(ReviewTask, state="open"), key=lambda t: (order[t.priority], -task_impact(t), t.created_at))


def promote_spot_checked(registry: Registry, policy_version_id: str, reviewer: str) -> list[str]:
    """Verify the unsampled rest of a spot-check batch once every sampled task is approved.

    Only ids stored in the sampled tasks' ``batch`` are promoted, and never a field that
    still has an open task of another kind (round trip, cross-check, quote).
    Returns the promoted field ids (empty while any sampled task is still open or was corrected).
    """
    prefix = f"{policy_version_id}:"
    spot = [t for t in registry.select(ReviewTask, kind="field_verify")
            if t.field_id and t.field_id.startswith(prefix) and SPOT_TAG in (t.notes or "")]
    if not spot or any(t.state != "approved" for t in spot):
        return []
    batch_ids = {i for t in spot for i in t.batch}
    blocked = {t.field_id for t in registry.select(ReviewTask, state="open")
               if t.field_id and t.kind != "field_verify"}
    promoted = []
    for rec in registry.fields_for_policy(policy_version_id):
        if rec.id in batch_ids and rec.id not in blocked and is_spot_checkable(rec):
            registry.set_field_status(rec.id, FieldStatus.verified, reviewer=f"spot-check:{reviewer}")
            promoted.append(rec.id)
    return promoted

"""Regressions for review findings: review blocking, edit grounding, discrepancy publication gate, input bounds."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from api_helpers import EM, EM_PV, make_client

from charitycare.api import queries, review
from charitycare.schemas import (
    FieldStatus,
    HospitalNotice,
    HospitalResponse,
    PolicyVersion,
    ReviewDecision,
    ReviewTask,
    utcnow,
)
from charitycare.store import Registry

ASSET = f"{EM_PV}:asset_test"


def _decide(reg: Registry, task_id: str, action: str = "approve", **kw):
    return review.apply_decision(reg, ReviewDecision(task_id=task_id, action=action, reviewer="t", decided_at=utcnow(), **kw))


def test_verify_refused_while_another_high_task_is_open(seeded: Registry):
    seeded.add_review_task(ReviewTask(id="rt:field_verify:x", kind="field_verify", field_id=ASSET, priority="low"))
    with pytest.raises(review.ReviewError, match="open high-priority"):
        _decide(seeded, "rt:field_verify:x")
    _decide(seeded, f"rt:crosscheck_disagreement:{ASSET}")
    _decide(seeded, "rt:field_verify:x")  # allowed once the high task is resolved


def test_edit_needs_new_quote_and_grounded_numbers(seeded: Registry):
    tid = f"rt:crosscheck_disagreement:{ASSET}"
    value = {"applies": True, "threshold_usd": 25000, "counted_assets": [], "exclusions": [], "applies_to_tiers": "all"}
    with pytest.raises(review.ReviewError, match="needs a new quote"):
        _decide(seeded, tid, "edit", value=value)
    quote = "Patients whose household liquid assets exceed $10,000 are not eligible for free or discounted care."
    with pytest.raises(review.ReviewError, match="printed in no quote"):
        _decide(seeded, tid, "edit", value=value, quote=quote, page=4)
    assert seeded.get(ReviewTask, id=tid).state == "open"


def test_discrepancy_gated_on_review_reply_window_and_current_version(seeded: Registry):
    assert [d["field_key"] for d in queries.discrepancies(seeded)] == ["asset_test"]  # published hospital reply
    for r in seeded.select(HospitalResponse):
        seeded.add_hospital_response(r.model_copy(update={"published": False}))
    assert queries.discrepancies(seeded) == []  # reviewed but the hospital has not been given a reply window
    _decide(seeded, f"rt:crosscheck_disagreement:{ASSET}")
    [notice] = seeded.select(HospitalNotice)
    assert notice.reply_due > date.today() and queries.discrepancies(seeded) == []
    assert queries.pending_discrepancies(seeded) == 1
    assert queries.discrepancies(seeded, notice.reply_due) != []  # window passed
    rows = queries.crosschecks_for_policy(seeded, EM_PV)
    asset = next(r for r in rows if r["field_key"] == "asset_test")
    assert not asset["is_discrepancy"] and asset["publication"] == "awaiting_reply"
    seeded.set_field_status(ASSET, FieldStatus.provisional)
    assert queries.discrepancies(seeded, notice.reply_due + timedelta(days=1)) == []  # unreviewed value
    seeded.set_field_status(ASSET, FieldStatus.verified)
    pv = seeded.get(PolicyVersion, id=EM_PV)
    seeded.put(pv.model_copy(update={"status": "superseded"}))
    assert queries.discrepancies(seeded, notice.reply_due) == []  # withdrawn with its policy version


@pytest.mark.parametrize("hh", [
    {"household_size": 3, "annual_income": "1e30"},
    {"household_size": 3, "annual_income": "49700", "first_statement_date": "9999-12-31"},
    {"household_size": 100000000, "annual_income": "1"},
    {"household_size": 3, "annual_income": "49700", "liquid_assets": "-5"},
])
def test_screen_rejects_out_of_range_inputs(seeded: Registry, hh):
    r = make_client(seeded).post("/v1/screen", json={"facility_id": EM, "household": hh})
    assert r.status_code == 422


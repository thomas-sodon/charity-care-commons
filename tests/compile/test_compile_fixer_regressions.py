"""Regressions for review findings: spot-check promotion, quote boundaries and fuzzy guard, filing lag."""

from __future__ import annotations

from datetime import date
from typing import Any

from charitycare.compile.crosscheck import resolve
from charitycare.compile.review import FieldContext, plan_tasks, promote_spot_checked
from charitycare.compile.verify import fuzzy_safe, match_on_page
from charitycare.schemas import CrossCheck, CrossCheckResult, Field, FieldRecord, FieldStatus, ReviewTask
from charitycare.store import Registry


def _rec(key: str, value: Any) -> FieldRecord:
    f = Field[Any].model_validate({"value": value, "evidence": [{"doc_sha256": "a" * 64, "page": 1, "quote": "x" * 10}],
                                   "confidence": 0.95})
    cc = CrossCheck(field_key=key, source="schedule_h", external_ref="irs:1|Part V line 13a|group A", external_value=1,
                    filing_period_end=date(2025, 12, 31), result=CrossCheckResult.agree)
    return FieldRecord(id=FieldRecord.make_id("pv", key), policy_version_id="pv", key=key, field=f,
                       status=FieldStatus.provisional, crosschecks=[cc], as_of=date(2026, 10, 1))


def test_round_trip_mismatch_field_is_never_promoted_or_field_verified() -> None:
    reg = Registry(":memory:")
    recs = [_rec("fpg_tiers", [{"max_pct_inclusive": 200, "benefit": "free"}]), _rec("agb_pct", 30.0),
            _rec("asset_test", {"applies": False})]
    ctx = [FieldContext(recs[0], round_trip_mismatch="p.3 printed $54,640 vs computed"), FieldContext(recs[1]),
           FieldContext(recs[2])]
    tasks = plan_tasks(ctx)
    assert [t.kind for t in tasks if t.field_id == "pv:fpg_tiers"] == ["round_trip_mismatch"]
    for r in recs:
        reg.put_field(r)
    for t in tasks:
        reg.add_review_task(t)
    for t in tasks:
        if t.batch:
            reg.update_review_task(t.id, "approved", notes=t.notes)
    promoted = promote_spot_checked(reg, "pv", "alice")
    assert "pv:fpg_tiers" not in promoted and promoted
    assert reg.get(FieldRecord, id="pv:fpg_tiers").status is FieldStatus.provisional
    # a field outside the stored batch is not promoted even if it later looks spot-checkable
    reg.put_field(_rec("residency", {"required": False}))
    reg.add_review_task(ReviewTask(id="x", kind="field_verify", field_id="pv:zzz", notes="spot-check sample", state="approved",
                                   batch=["pv:agb_pct"]))
    assert "pv:residency" not in promote_spot_checked(reg, "pv", "alice")


def test_exact_quote_must_start_and_end_on_word_boundaries() -> None:
    page = ("Uninsured patients with household income at or below 200% FPG qualify for free care. "
            "Patients are ineligible for discounts if they have assets.")
    assert match_on_page(page, "insured patients with household income at or below 200% FPG qualify for free care.")[0] == "none"
    assert match_on_page(page, "0% FPG qualify")[0] == "none"
    assert match_on_page(page, "eligible for discounts if they have assets")[0] == "none"
    assert match_on_page(page, "200% FPG qualify")[0] == "exact"
    assert match_on_page(page, "Uninsured patients with household income")[0] == "exact"


def test_fuzzy_guard_rejects_meaning_changes() -> None:
    page = ("Assistance is available to residents of Bexar County who apply within the application period and who "
            "provide the required documentation listed below.")
    assert match_on_page(page, page.replace("Bexar", "Bear"))[0] == "none"
    p2 = ("Patients who are uninsured with household income at or below the federal poverty guideline levels qualify "
          "for full charity care under this policy section.")
    assert match_on_page(p2, p2.replace("uninsured", "insured"))[0] == "none"
    p3 = ("The hospital will waive all remaining balances for qualifying patients whose household income is "
          "documented with pay stubs and tax returns.")
    assert match_on_page(p3, p3.replace("will", "may"))[0] == "none"
    assert not fuzzy_safe("patients are ineligible", "patients are eligible")
    assert not fuzzy_safe("free care for all patients", "free care for patients")  # whole-token deletion
    assert fuzzy_safe("disc0unt applies", "discount applies")


def test_unknown_policy_date_is_held_not_unexplained() -> None:
    assert resolve(False, None, date(2025, 12, 31)) is CrossCheckResult.disagree_filing_lag_possible
    assert resolve(False, date(2025, 7, 1), date(2025, 12, 31)) is CrossCheckResult.disagree_unexplained

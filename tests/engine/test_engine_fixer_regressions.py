"""Regressions for review findings: tier gaps, stated guideline year, hardship/Medicaid on unusable fields, pay frequency."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from charitycare.engine.core import evaluate, guideline_year_in
from charitycare.packet.extract import IncomeDoc, XField, infer_frequency
from charitycare.schemas import Field, FieldRecord, FieldStatus, Household


def _rec(key: str, value: Any, quote: str = "x" * 10, abst: str | None = None,
         status: FieldStatus = FieldStatus.verified) -> FieldRecord:
    f = Field[Any].model_validate({"value": value, "evidence": [{"doc_sha256": "a" * 64, "page": 1, "quote": quote}],
                                   "confidence": 0.9, "abstain_reason": abst})
    return FieldRecord(id=FieldRecord.make_id("pv", key), policy_version_id="pv", key=key, field=f, status=status,
                       as_of=date(2026, 10, 1), reviewer="r")


BASE = [_rec("fpg_year_rule", "current_at_application"), _rec("asset_test", {"applies": False}),
        _rec("residency", {"required": False}), _rec("application_window", {"days": 240, "anchor": "first_post_discharge_statement"})]


def _eval(recs: list[FieldRecord], **hh: Any):
    h = Household(application_date=date(2026, 10, 1), **hh)
    return evaluate(h, recs, facility_id="f", policy_version_id="pv", today=date(2026, 10, 1)).determination


def test_scoped_tier_gap_is_not_reported_as_above_all_tiers() -> None:
    tiers = [{"max_pct_inclusive": 200, "benefit": "free", "applies_to": "uninsured"},
             {"min_pct_exclusive": 200, "max_pct_inclusive": 400, "benefit": "percent_discount", "discount_pct": 60}]
    recs = BASE + [_rec("fpg_tiers", tiers, "200% free uninsured; 200% to 400% 60% discount"),
                   _rec("insured_patients", {"insured_eligible": True, "catastrophic_threshold_pct_income": 20})]
    d = _eval(recs, household_size=3, annual_income=Decimal(40000), insured=True, out_of_pocket_medical_12m=Decimal(0))
    assert "> 400%" not in d.arithmetic and "apply only to uninsured patients" in d.arithmetic
    assert any("apply only to uninsured" in n for n in d.notes)


def test_band_gap_is_insufficient_information() -> None:
    tiers = [{"max_pct_inclusive": 150, "benefit": "free"},
             {"min_pct_exclusive": 200, "max_pct_inclusive": 300, "benefit": "percent_discount", "discount_pct": 50}]
    recs = BASE + [_rec("fpg_tiers", tiers, "150% free; 200% to 300% 50% discount"),
                   _rec("insured_patients", {"insured_eligible": True})]
    d = _eval(recs, household_size=3, annual_income=Decimal(47811))  # 175% FPL
    assert d.outcome == "insufficient_information" and "gap" in d.arithmetic


def test_guideline_year_prefers_the_guideline_wording() -> None:
    assert guideline_year_in("Effective January 1, 2026, eligibility is based on the 2025 Federal Poverty Guidelines.") == 2025
    assert guideline_year_in("Revised 2024 and 2025.") is None
    assert guideline_year_in("We use the 2026 FPG.") == 2026


def test_abstained_insured_rule_leaves_hardship_unevaluated() -> None:
    recs = BASE + [_rec("fpg_tiers", [{"max_pct_inclusive": 200, "benefit": "free"}], "200%"),
                   _rec("insured_patients", None, abst="quote_unverified", status=FieldStatus.abstained),
                   _rec("medicaid_application_required_first", None, abst="illegible", status=FieldStatus.abstained)]
    d = _eval(recs, household_size=3, annual_income=Decimal(80000), out_of_pocket_medical_12m=Decimal(30000))
    assert any("Medical-hardship rule not evaluated" in u for u in d.unevaluated_conditions)
    assert not any("no medical-hardship rule" in n for n in d.notes)


def test_biweekly_period_starting_on_the_first_is_biweekly() -> None:
    def stub(s: date, e: date) -> IncomeDoc:
        return IncomeDoc(period_start=XField[date](value=s, confidence=1), period_end=XField[date](value=e, confidence=1))

    assert infer_frequency(stub(date(2026, 6, 1), date(2026, 6, 14))) == "biweekly"
    assert infer_frequency(stub(date(2026, 3, 16), date(2026, 3, 29))) == "biweekly"
    assert infer_frequency(stub(date(2026, 6, 1), date(2026, 6, 15))) == "semimonthly"
    assert infer_frequency(stub(date(2026, 2, 16), date(2026, 2, 28))) == "semimonthly"

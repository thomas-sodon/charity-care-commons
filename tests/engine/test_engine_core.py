from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from engine_helpers import TODAY, hh, make_records

from charitycare.engine.core import (
    display_pct,
    evaluate,
    fmt_pct,
    match_tier,
    no_policy_determination,
    normalize_county,
)
from charitycare.schemas import FieldStatus, FPGTier
from charitycare.wording import find_banned_wording


def run(hospital: str, household, **kw):
    return evaluate(household, make_records(hospital, **kw), facility_id="tx-test", policy_version_id="pv:test", today=TODAY)


def all_text(det) -> str:
    return "\n".join([det.arithmetic, *det.notes, *det.unevaluated_conditions])


def test_design_example_maria() -> None:
    """Screen 3 of the design: $49,700, household of 3, 2026 FPG -> 181.9% FPL, free care, 2027-06-07."""
    r = run("example-methodist", hh(county="Bexar", first_statement_date=date(2026, 10, 10)))
    d = r.determination
    assert d.outcome == "likely_free"
    assert d.fpg_year == 2026 and d.pct_fpl == Decimal("181.92")
    assert d.arithmetic == "$49,700 ÷ $27,320 (2026 FPG, HH of 3) = 181.9% FPL ≤ 200% (free care tier)"
    assert d.matched_tier is not None and d.matched_tier.max_pct_inclusive == 200
    assert d.federal_deadline == date(2027, 6, 7) and d.policy_deadline == date(2027, 6, 7)
    assert d.citations[0].section_label == "4.2" and "at or below 200%" in d.citations[0].quote
    assert any("bank statements" in u and "$10,000" in u and "2 months" in u for u in d.unevaluated_conditions)
    assert r.statuses()["asset_test"] == "unknown" and r.statuses()["residency"] == "met"
    assert d.disclaimer_version.startswith("disclaimer@")
    assert find_banned_wording(all_text(d)) == []


@pytest.mark.parametrize("income,outcome,idx", [
    ("54640.00", "likely_free", 0),       # exactly 200% of $27,320
    ("54640.01", "likely_discount", 1),   # one cent above
    ("54639.00", "likely_free", 0),       # one dollar below
    ("109280.00", "likely_discount", 1),  # exactly 400%
    ("109280.01", "likely_not_eligible_on_income", None),
    ("0", "likely_free", 0),
])
def test_exact_cutoffs(income: str, outcome: str, idx: int | None) -> None:
    r = run("example-methodist", hh(annual_income=income, county="Bexar", liquid_assets="0"))
    assert r.determination.outcome == outcome
    assert r.matched_tier_index == idx


def test_display_never_rounds_onto_a_bound() -> None:
    d = run("example-methodist", hh(annual_income="54640.01", county="Bexar")).determination
    assert "200.00% FPL" not in d.arithmetic and "= 200.0% FPL" not in d.arithmetic
    assert "200.0000" in d.arithmetic or "200.00" in d.arithmetic
    assert display_pct(Decimal("181.918"), [200]) == "181.9"
    assert display_pct(Decimal("200"), [200]) == "200.0"
    assert display_pct(Decimal("199.97"), [200]) == "199.97"
    assert fmt_pct(200.0) == "200" and fmt_pct(137.5) == "137.5"


def test_provisional_fields_never_drive() -> None:
    r = run("example-methodist", hh(county="Bexar"), status={"fpg_tiers": FieldStatus.provisional,
                                                              "residency": FieldStatus.provisional})
    d = r.determination
    assert d.outcome == "preliminary_provisional_fields"
    assert d.matched_tier is None and r.matched_tier_index is None
    assert d.provisional_fields_used == ["fpg_tiers", "residency"]
    assert not any("tier" in c.quote for c in d.citations)
    assert "≤" not in d.arithmetic
    assert find_banned_wording(all_text(d)) == []


def test_provisional_non_driving_field_does_not_block() -> None:
    d = run("example-methodist", hh(county="Bexar"), status={"refund_policy": FieldStatus.provisional}).determination
    assert d.outcome == "likely_free" and d.provisional_fields_used == []


def test_disputed_tiers_are_insufficient() -> None:
    d = run("example-methodist", hh(), status={"fpg_tiers": FieldStatus.disputed}).determination
    assert d.outcome == "insufficient_information" and d.matched_tier is None


def test_abstained_may_clause_shown_as_hospital_discretion() -> None:
    d = run("gulf-coast-baptist", hh()).determination
    disc = [n for n in d.notes if n.startswith("Hospital discretion")]
    assert disc and "“The Hospital may, at its sole discretion" in disc[0]
    assert any("sole discretion" in c.quote for c in d.citations)
    assert find_banned_wording(all_text(d)) == []


def test_discretionary_tiers() -> None:
    def edit(p):
        p["fpg_tiers"] = {"value": None, "abstain_reason": "ambiguous_may_clause",
                          "evidence": p["fpg_tiers"]["evidence"][:1]}
    d = run("example-methodist", hh(), edit=edit).determination
    assert d.outcome == "insufficient_information"
    assert any(n.startswith("Income tiers: hospital discretion") for n in d.notes)


def test_unspecified_year_reports_prior_year_flip() -> None:
    # Hill Country: rule unspecified. 2026 HH3 200% = $54,640; 2025 HH3 200% = $53,300.
    r = run("hill-country-community", hh(annual_income="54000", county="Kerr", liquid_assets="0"))
    d = r.determination
    assert d.fpg_year == 2026 and d.outcome == "likely_free"
    assert r.prior_year_flip and r.prior_year_tier_index == 1
    assert any("does not say which year" in n for n in d.notes)
    assert any(n.startswith("Under the 2025 guidelines") and "50% discount tier" in n for n in d.notes)
    r2 = run("hill-country-community", hh(annual_income="30000", county="Kerr", liquid_assets="0"))
    assert not r2.prior_year_flip


def test_application_before_new_guidelines_uses_prior_year() -> None:
    d = run("example-methodist", hh(application_date=date(2026, 1, 14), county="Bexar")).determination
    assert d.fpg_year == 2025
    d = run("example-methodist", hh(application_date=date(2026, 1, 15), county="Bexar")).determination
    assert d.fpg_year == 2026


def test_current_at_service_and_fallback() -> None:
    assert run("gulf-coast-baptist", hh(service_date=date(2026, 1, 14))).determination.fpg_year == 2025
    r = run("gulf-coast-baptist", hh(service_date=None, application_date=date(2026, 3, 1)))
    assert r.determination.fpg_year == 2026
    assert any("no service date was entered" in n for n in r.determination.notes)


def test_stated_year_from_quote() -> None:
    d = run("panhandle-university", hh(application_date=date(2025, 6, 1))).determination
    assert d.fpg_year == 2026
    assert any("states it uses the 2026" in n for n in d.notes)


def test_asset_edges() -> None:
    st = lambda a: run("example-methodist", hh(county="Bexar", liquid_assets=a)).statuses()["asset_test"]  # noqa: E731
    assert st("10000") == "met" and st("10000.01") == "not_met" and st(None) == "unknown"
    d = run("example-methodist", hh(county="Bexar", liquid_assets="10000.01")).determination
    assert d.outcome == "likely_free"  # income outcome; the asset condition is reported beside it
    assert any(n.startswith("Likely not met: reported liquid assets of $10,000.01") for n in d.notes)


def test_asset_test_tier_scope() -> None:
    def edit(p):
        p["asset_test"]["value"]["applies_to_tiers"] = "free_only"
    r = run("example-methodist", hh(annual_income="70000", county="Bexar", liquid_assets="50000"), edit=edit)
    assert r.determination.outcome == "likely_discount" and r.statuses()["asset_test"] == "not_applicable"


@pytest.mark.parametrize("county,status", [
    ("Bexar", "met"), ("bexar county", "met"), ("  Medina  County ", "met"), ("Wilson", "not_met"), (None, "unknown"),
])
def test_residency_counties(county, status) -> None:
    assert run("example-methodist", hh(county=county)).statuses()["residency"] == status


def test_residency_emergency_exempt_and_state() -> None:
    assert run("example-methodist", hh(county="Harris", emergency_care=True)).statuses()["residency"] == "met"
    assert run("valley-childrens", hh(state="NM")).statuses()["residency"] == "not_met"
    assert run("valley-childrens", hh(state="tx")).statuses()["residency"] == "met"
    assert run("lone-star-health", hh(county="Anywhere")).statuses()["residency"] == "not_applicable"
    assert normalize_county(" Bexar County ") == "bexar"


def test_uninsured_only_policy() -> None:
    st = lambda ins: run("hill-country-community", hh(insured=ins, county="Kerr")).statuses()["insured_patients"]  # noqa: E731
    assert (st(True), st(False), st(None)) == ("not_met", "met", "unknown")


def test_catastrophic_rule() -> None:
    # Gulf Coast: tiers to 300%; OOP must EXCEED 20% of income.
    base = dict(household_size=2, annual_income="70000", insured=True, service_date=date(2026, 9, 1))
    r = run("gulf-coast-baptist", hh(**base, out_of_pocket_medical_12m="14000"))
    assert r.determination.outcome == "likely_not_eligible_on_income" and r.statuses()["medical_hardship"] == "not_met"
    r = run("gulf-coast-baptist", hh(**base, out_of_pocket_medical_12m="14000.01"))
    assert r.determination.outcome == "likely_discount" and r.statuses()["medical_hardship"] == "met"
    assert r.determination.matched_tier is None
    assert any("20% of household income" in c.quote for c in r.determination.citations)
    r = run("gulf-coast-baptist", hh(**base))
    assert r.determination.outcome == "likely_not_eligible_on_income" and r.statuses()["medical_hardship"] == "unknown"
    assert any("Medical-hardship rule not evaluated" in u for u in r.determination.unevaluated_conditions)


def test_medicaid_first_listed() -> None:
    r = run("lone-star-health", hh())
    assert r.statuses()["medicaid_first"] == "unknown"
    assert any(u.startswith("Medicaid first") for u in r.determination.unevaluated_conditions)
    assert run("example-methodist", hh(county="Bexar")).statuses()["medicaid_first"] == "not_applicable"


def test_tier_insurance_scope() -> None:
    tiers = [FPGTier(max_pct_inclusive=200, benefit="free", applies_to="uninsured"),
             FPGTier(min_pct_exclusive=None, max_pct_inclusive=300, benefit="percent_discount", discount_pct=40)]
    assert match_tier(tiers, Decimal(30000), 3, 2026, insured=True)[0] == 1
    assert match_tier(tiers, Decimal(30000), 3, 2026, insured=False)[0] == 0


def test_longer_policy_window() -> None:
    d = run("brazos-valley", hh(county="Brazos", first_statement_date=date(2026, 10, 10))).determination
    assert d.federal_deadline == date(2027, 6, 7) and d.policy_deadline == date(2027, 10, 10)
    assert any("longer than the federal minimum" in n for n in d.notes)


def test_missing_statement_date() -> None:
    d = run("example-methodist", hh(county="Bexar")).determination
    assert d.federal_deadline is None and d.policy_deadline is None
    assert any("first post-discharge billing statement" in u for u in d.unevaluated_conditions)


def test_missing_records_are_insufficient() -> None:
    d = evaluate(hh(), [], facility_id="f", policy_version_id="pv", today=TODAY)
    assert d.determination.outcome == "insufficient_information"
    n = no_policy_determination("f", hh(), today=TODAY)
    assert n.outcome == "insufficient_information" and n.pct_fpl == Decimal("181.92")

"""Property-based tests of the engine (hypothesis)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from engine_helpers import TODAY
from hypothesis import given, settings
from hypothesis import strategies as st

from charitycare import fpg
from charitycare.engine.core import evaluate
from charitycare.engine.policy import demo_policies
from charitycare.schemas import DETERMINATION_FIELDS, FieldStatus, Household
from charitycare.wording import find_banned_wording

POLICIES = sorted(demo_policies().items())
FACILITIES = [fid for fid, _ in POLICIES]
RANK = {"likely_free": 0, "likely_discount": 1, "likely_not_eligible_on_income": 2}

money = st.decimals(min_value=0, max_value=400_000, places=2, allow_nan=False, allow_infinity=False)
sizes = st.integers(min_value=1, max_value=12)
dates = st.dates(min_value=date(2025, 1, 1), max_value=date(2026, 12, 31))


@st.composite
def households(draw: st.DrawFn) -> Household:
    return Household(
        household_size=draw(sizes), annual_income=draw(money),
        liquid_assets=draw(st.none() | money), county=draw(st.none() | st.sampled_from(["Bexar", "Kerr", "Harris", "Brazos"])),
        state=draw(st.sampled_from(["TX", "LA"])), insured=draw(st.none() | st.booleans()),
        out_of_pocket_medical_12m=draw(st.none() | money), emergency_care=draw(st.booleans()),
        service_date=draw(st.none() | dates), first_statement_date=draw(st.none() | dates),
        application_date=draw(st.none() | dates),
    )


def run(fid: str, h: Household, records=None):
    dp = demo_policies()[fid]
    return evaluate(h, records if records is not None else dp.records, facility_id=fid,
                    policy_version_id=dp.policy_version_id, today=TODAY)


def income_rank(fid: str, h: Household) -> int:
    """Rank of the income-only result (hardship rule switched off by omitting OOP)."""
    return RANK[run(fid, h.model_copy(update={"out_of_pocket_medical_12m": None})).determination.outcome]


@settings(max_examples=150, deadline=None)
@given(st.sampled_from(FACILITIES), households(), money, money)
def test_monotone_in_income(fid: str, h: Household, a: Decimal, b: Decimal) -> None:
    lo, hi = sorted((a, b))
    r_lo = income_rank(fid, h.model_copy(update={"annual_income": lo}))
    r_hi = income_rank(fid, h.model_copy(update={"annual_income": hi}))
    assert r_lo <= r_hi


@settings(max_examples=150, deadline=None)
@given(st.sampled_from(FACILITIES), households(), sizes, sizes)
def test_larger_household_never_worse(fid: str, h: Household, a: int, b: int) -> None:
    small, big = sorted((a, b))
    assert income_rank(fid, h.model_copy(update={"household_size": big})) <= income_rank(fid, h.model_copy(update={"household_size": small}))


@settings(max_examples=150, deadline=None)
@given(st.sampled_from(FACILITIES), households())
def test_matches_independent_tier_math(fid: str, h: Household) -> None:
    r = run(fid, h)
    d = r.determination
    assert d.pct_fpl == fpg.pct_fpl(h.annual_income, h.household_size, d.fpg_year)
    if d.matched_tier is not None:
        t = d.matched_tier
        assert h.annual_income * 100 <= Decimal(str(t.max_pct_inclusive)) * fpg.fpg(d.fpg_year, h.household_size)
        if t.min_pct_exclusive is not None:
            assert h.annual_income * 100 > Decimal(str(t.min_pct_exclusive)) * fpg.fpg(d.fpg_year, h.household_size)
        assert (t.benefit == "free") == (d.outcome == "likely_free")
        assert d.citations, "a matched tier must be cited"
    if d.federal_deadline is not None:
        assert d.federal_deadline - h.first_statement_date == timedelta(days=240)  # type: ignore[operator]
    assert find_banned_wording("\n".join([d.arithmetic, *d.notes, *d.unevaluated_conditions])) == []


@settings(max_examples=100, deadline=None)
@given(st.sampled_from(FACILITIES), households(), st.sets(st.sampled_from(DETERMINATION_FIELDS), min_size=1))
def test_any_provisional_driving_field_gives_preliminary(fid: str, h: Household, keys: set[str]) -> None:
    dp = demo_policies()[fid]
    recs = [r.model_copy(update={"status": FieldStatus.provisional}) if r.key in keys else r for r in dp.records]
    d = run(fid, h, recs).determination
    assert d.outcome == "preliminary_provisional_fields"
    assert d.matched_tier is None and d.provisional_fields_used == sorted(keys)


@settings(max_examples=100, deadline=None)
@given(st.sampled_from(FACILITIES), households())
def test_non_income_facts_do_not_change_income_outcome(fid: str, h: Household) -> None:
    """Assets, county and insurance status are reported as conditions; they never move the income tier."""
    base = run(fid, h).matched_tier_index
    other = h.model_copy(update={"liquid_assets": Decimal("999999"), "county": "Nowhere", "state": "LA"})
    assert run(fid, other).matched_tier_index == base


@settings(max_examples=100, deadline=None)
@given(st.sampled_from(FACILITIES), households())
def test_deterministic(fid: str, h: Household) -> None:
    assert run(fid, h).determination == run(fid, h).determination

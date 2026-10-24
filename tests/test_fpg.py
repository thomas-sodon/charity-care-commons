from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from charitycare import fpg

# Published HHS tables, 48 contiguous states + DC, household sizes 1-8.
PUBLISHED = {
    2023: [14_580, 19_720, 24_860, 30_000, 35_140, 40_280, 45_420, 50_560],
    2024: [15_060, 20_440, 25_820, 31_200, 36_580, 41_960, 47_340, 52_720],
    2025: [15_650, 21_150, 26_650, 32_150, 37_650, 43_150, 48_650, 54_150],
    2026: [15_960, 21_640, 27_320, 33_000, 38_680, 44_360, 50_040, 55_720],
}
PER_EXTRA = {2023: 5_140, 2024: 5_380, 2025: 5_500, 2026: 5_680}


@pytest.mark.parametrize("year", sorted(PUBLISHED))
def test_published_tables(year: int) -> None:
    assert [fpg.fpg(year, n) for n in range(1, 9)] == PUBLISHED[year]
    assert fpg.fpg(year, 10) == PUBLISHED[year][7] + 2 * PER_EXTRA[year]


def test_design_doc_example() -> None:
    assert fpg.fpg(2026, 3) == 27_320
    assert fpg.pct_fpl(49_700, 3, 2026) == Decimal("181.92")


def test_boundaries_are_exact() -> None:
    limit = fpg.income_at_pct(200, 3, 2026)
    assert limit == Decimal("54640")
    assert fpg.income_within_pct(limit, 200, 3, 2026)
    assert not fpg.income_within_pct(limit + Decimal("0.01"), 200, 3, 2026)


def test_errors() -> None:
    with pytest.raises(fpg.UnknownGuidelineYear):
        fpg.fpg(2019, 2)
    with pytest.raises(ValueError):
        fpg.fpg(2026, 0)
    with pytest.raises(ValueError):
        fpg.pct_fpl(-1, 2, 2026)


def test_guideline_year_for() -> None:
    assert fpg.guideline_year_for(date(2026, 1, 1)) == 2025
    assert fpg.guideline_year_for(date(2026, 6, 1)) == 2026
    assert fpg.guideline_year_for(date(2020, 6, 1)) == 2023
    assert fpg.latest_year() == 2026


def test_format_usd() -> None:
    assert fpg.format_usd(27320) == "$27,320"
    assert fpg.format_usd(Decimal("49700.5")) == "$49,700.50"


@given(st.integers(min_value=0, max_value=2_000_000), st.integers(min_value=1, max_value=15), st.sampled_from(sorted(PUBLISHED)))
def test_pct_consistent_with_exact_test(income: int, size: int, year: int) -> None:
    pct = fpg.pct_fpl(income, size, year)
    # exact comparison agrees with the rounded percentage away from the rounding boundary
    if pct <= Decimal("199.99"):
        assert fpg.income_within_pct(income, 200, size, year)
    if pct >= Decimal("200.01"):
        assert not fpg.income_within_pct(income, 200, size, year)


@given(st.integers(min_value=1, max_value=20), st.sampled_from(sorted(PUBLISHED)))
def test_monotonic_in_size(size: int, year: int) -> None:
    assert fpg.fpg(year, size + 1) > fpg.fpg(year, size)

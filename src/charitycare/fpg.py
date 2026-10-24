"""HHS poverty guidelines (48 contiguous states and DC) and %FPL arithmetic.

Values are the published HHS/ASPE guidelines, verified on 2026-10-24 against
https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines (2026
table) and the ASPE Poverty Guidelines API
(``/poverty-guidelines/api/{year}/us/{size}``) for 2023-2025. For the 48
states the guideline is exactly ``base + increment * (household_size - 1)``
for every household size, including sizes above 8.

Alaska and Hawaii have separate, higher guidelines and are out of scope for
the Texas edition. The January 2027 update must add a row here (and the test
suite pins every value).

All money math uses ``Decimal``; no floats touch an eligibility decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class Guideline:
    year: int
    base: int  # household of 1
    increment: int  # each additional person
    published: date  # Federal Register publication (approximate where noted)
    source: str


GUIDELINES: dict[int, Guideline] = {
    2023: Guideline(2023, 14_580, 5_140, date(2023, 1, 19), "88 FR 3424; ASPE API 2023/us"),
    2024: Guideline(2024, 15_060, 5_380, date(2024, 1, 17), "89 FR 2961; ASPE API 2024/us"),
    2025: Guideline(2025, 15_650, 5_500, date(2025, 1, 17), "90 FR 5917; ASPE API 2025/us"),
    # 2026 publication date approximate (mid-January); values from the ASPE 2026 table.
    2026: Guideline(2026, 15_960, 5_680, date(2026, 1, 15), "ASPE 2026 poverty guidelines table"),
}


class UnknownGuidelineYear(KeyError):
    """Raised for a year with no published guideline in this table."""


def years() -> list[int]:
    return sorted(GUIDELINES)


def latest_year() -> int:
    return max(GUIDELINES)


def fpg(year: int, household_size: int) -> int:
    """Poverty guideline in whole dollars for ``household_size`` people in ``year``."""
    if household_size < 1:
        raise ValueError("household_size must be >= 1")
    try:
        g = GUIDELINES[year]
    except KeyError as exc:
        raise UnknownGuidelineYear(f"no HHS poverty guideline loaded for {year}") from exc
    return g.base + g.increment * (household_size - 1)


def pct_fpl(annual_income: Decimal | int | float | str, household_size: int, year: int) -> Decimal:
    """Household income as a percent of the guideline, rounded half-up to 0.01.

    Example: ``pct_fpl(49700, 3, 2026) == Decimal("181.92")``.
    Use :func:`income_within_pct` for boundary decisions; it avoids rounding.
    """
    income = Decimal(str(annual_income))
    if income < 0:
        raise ValueError("annual_income must be >= 0")
    raw = income * 100 / Decimal(fpg(year, household_size))
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def income_at_pct(pct: Decimal | int | float | str, household_size: int, year: int) -> Decimal:
    """Dollar income equal to ``pct`` percent of the guideline (unrounded)."""
    return Decimal(str(pct)) * Decimal(fpg(year, household_size)) / 100


def income_within_pct(
    annual_income: Decimal | int | float | str, pct: Decimal | int | float | str, household_size: int, year: int
) -> bool:
    """Exact test ``income <= pct% of FPG`` with no intermediate rounding."""
    return Decimal(str(annual_income)) * 100 <= Decimal(str(pct)) * Decimal(fpg(year, household_size))


def guideline_year_for(on: date) -> int:
    """Guideline year in effect on a date (the latest one published on or before it).

    Dates before the earliest loaded publication fall back to the earliest year,
    dates after the latest use the latest year.
    """
    eligible = [g.year for g in GUIDELINES.values() if g.published <= on]
    return max(eligible) if eligible else min(GUIDELINES)


def format_usd(amount: Decimal | int | float) -> str:
    """``$27,320`` style formatting (whole dollars unless cents are present)."""
    d = Decimal(str(amount))
    if d == d.to_integral_value():
        return f"${int(d):,}"
    return f"${d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"

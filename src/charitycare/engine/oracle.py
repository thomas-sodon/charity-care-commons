"""An independent, deliberately simple reference oracle for the engine.

It shares no code with :mod:`charitycare.engine.core` or :mod:`charitycare.fpg`:
it reads the gold policy as plain JSON, keeps its own copy of the HHS table,
and does exact arithmetic with :class:`fractions.Fraction`. The synthetic
household set (``data/demo/households.json``) stores the oracle's expected
outcomes, and the test suite requires the engine to agree on every one.
"""

from __future__ import annotations

from datetime import date, timedelta
from fractions import Fraction
from typing import Any

# year -> (household of 1, each additional person, publication date). Independent copy of HHS 48-state table.
ORACLE_FPG: dict[int, tuple[int, int, date]] = {
    2023: (14_580, 5_140, date(2023, 1, 19)),
    2024: (15_060, 5_380, date(2024, 1, 17)),
    2025: (15_650, 5_500, date(2025, 1, 17)),
    2026: (15_960, 5_680, date(2026, 1, 15)),
}
DRIVING = ("fpg_tiers", "fpg_year_rule", "asset_test", "residency", "insured_patients", "application_window")


def poverty_line(year: int, size: int) -> int:
    base, inc, _ = ORACLE_FPG[year]
    return base + inc * (size - 1)


def year_on(d: date) -> int:
    best = None
    for y, (_, _, pub) in ORACLE_FPG.items():
        if pub <= d and (best is None or y > best):
            best = y
    return best if best is not None else min(ORACLE_FPG)


def frac(x: Any) -> Fraction:
    return Fraction(str(x))


def pct_2dp(income: Fraction, line: int) -> str:
    """Percent of the line, rounded half up to 2 decimals, as text."""
    hundredths = income * 100 * 100 / line
    n = int(hundredths + Fraction(1, 2)) if hundredths >= 0 else 0
    return f"{n // 100}.{n % 100:02d}"


def tier_index(tiers: list[dict[str, Any]], income: Fraction, line: int) -> int | None:
    ordered = sorted(tiers, key=lambda t: (t["max_pct_inclusive"], t["min_pct_exclusive"] or 0))
    for i, t in enumerate(ordered):
        assert t.get("applies_to", "all") == "all", "oracle only models tiers that apply to all patients"
        lo = t["min_pct_exclusive"]
        if lo is not None and income * 100 <= frac(lo) * line:
            continue
        if income * 100 <= frac(t["max_pct_inclusive"]) * line:
            return i
    return None


def _county(c: str) -> str:
    c = " ".join(c.split()).lower()
    return c[:-7].strip() if c.endswith(" county") else c


def expected(policy: dict[str, Any], hh: dict[str, Any], today: date, scenario: str = "verified") -> dict[str, Any]:
    """Expected engine output for one household (plain JSON-able dict)."""
    kind, _, skey = scenario.partition(":")
    fields = {k: dict(v) for k, v in policy.items()}
    if kind == "abstained":
        fields[skey] = {"value": None, "evidence": [], "abstain_reason": "not_addressed"}
    provisional = sorted([skey]) if kind == "provisional" and skey in DRIVING else []

    def val(k: str) -> Any:
        return fields[k]["value"] if fields[k].get("abstain_reason") is None else None

    d = lambda s: date.fromisoformat(s) if s else None  # noqa: E731
    app = d(hh.get("application_date")) or today
    service, first = d(hh.get("service_date")), d(hh.get("first_statement_date"))
    size, income = hh["household_size"], frac(hh["annual_income"])

    rule = val("fpg_year_rule")
    assumed = False
    if rule == "current_at_application":
        year = year_on(app)
    elif rule == "current_at_service" and service is not None:
        year = year_on(service)
    elif rule == "stated_year":
        quote = " ".join(e["quote"] for e in fields["fpg_year_rule"]["evidence"])
        yrs = [y for y in ORACLE_FPG if str(y) in quote]
        year, assumed = (yrs[0], False) if yrs else (year_on(app), True)
    else:
        year, assumed = year_on(app), True

    fed = first + timedelta(days=240) if first else None
    win = val("application_window") if "application_window" not in provisional else None
    pol = None
    if win and win.get("days") and win.get("anchor") == "first_post_discharge_statement" and first:
        pol = first + timedelta(days=win["days"])
        if fed and pol < fed:
            pol = None
    out: dict[str, Any] = {
        "fpg_year": year, "pct_fpl": pct_2dp(income, poverty_line(year, size)),
        "federal_deadline": fed.isoformat() if fed else None, "policy_deadline": pol.isoformat() if pol else None,
        "provisional_fields_used": provisional, "matched_tier_index": None, "prior_year_flip": False, "conditions": None,
    }
    if provisional:
        out["outcome"] = "preliminary_provisional_fields"
        return out
    tiers = val("fpg_tiers")
    if not tiers:
        out["outcome"] = "insufficient_information"
        return out

    line = poverty_line(year, size)
    idx = tier_index(tiers, income, line)
    ordered = sorted(tiers, key=lambda t: (t["max_pct_inclusive"], t["min_pct_exclusive"] or 0))
    conds: dict[str, str] = {}
    insured_rule = val("insured_patients")
    if idx is not None:
        benefit = ordered[idx]["benefit"]
        outcome = "likely_free" if benefit == "free" else "likely_discount"
        conds["medical_hardship"] = "not_applicable"
    else:
        thr = insured_rule.get("catastrophic_threshold_pct_income") if insured_rule else None
        oop = hh.get("out_of_pocket_medical_12m")
        if thr is None:
            conds["medical_hardship"] = "not_applicable"
        elif oop is None:
            conds["medical_hardship"] = "unknown"
        else:
            conds["medical_hardship"] = "met" if frac(oop) * 100 > frac(thr) * income else "not_met"
        outcome = "likely_discount" if conds["medical_hardship"] == "met" else "likely_not_eligible_on_income"
        benefit = "percent_discount"

    if outcome == "likely_not_eligible_on_income":
        conds.update(asset_test="not_applicable", residency="not_applicable", insured_patients="not_applicable",
                     medicaid_first="not_applicable")
    else:
        at = val("asset_test")
        if at is None:
            conds["asset_test"] = "unknown"
        elif not at["applies"]:
            conds["asset_test"] = "not_applicable"
        elif (at["applies_to_tiers"] == "free_only" and benefit != "free") or (at["applies_to_tiers"] == "discount_only" and benefit == "free"):
            conds["asset_test"] = "not_applicable"
        elif at["threshold_usd"] is None or hh.get("liquid_assets") is None:
            conds["asset_test"] = "unknown"
        else:
            conds["asset_test"] = "met" if frac(hh["liquid_assets"]) <= frac(at["threshold_usd"]) else "not_met"

        r = val("residency")
        if r is None:
            conds["residency"] = "unknown"
        elif not r["required"]:
            conds["residency"] = "not_applicable"
        elif r["emergency_exempt"] and hh.get("emergency_care"):
            conds["residency"] = "met"
        elif r["area_type"] == "counties" and r["counties"]:
            c = hh.get("county")
            conds["residency"] = "unknown" if c is None else ("met" if _county(c) in {_county(x) for x in r["counties"]} else "not_met")
        elif r["area_type"] == "state" and r["states"]:
            conds["residency"] = "met" if hh.get("state", "TX").strip().upper() in r["states"] else "not_met"
        else:
            conds["residency"] = "unknown"

        if insured_rule is None:
            conds["insured_patients"] = "unknown"
        elif insured_rule["insured_eligible"]:
            conds["insured_patients"] = "not_applicable"
        else:
            ins = hh.get("insured")
            conds["insured_patients"] = "unknown" if ins is None else ("not_met" if ins else "met")

        conds["medicaid_first"] = "unknown" if val("medicaid_application_required_first") is True else "not_applicable"

    if assumed and (year - 1) in ORACLE_FPG:
        out["prior_year_flip"] = tier_index(tiers, income, poverty_line(year - 1, size)) != idx
    out.update(outcome=outcome, matched_tier_index=idx, conditions=conds)
    return out

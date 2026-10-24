"""Deterministic eligibility engine (design S13, sections 8 and 11).

``evaluate(household, records, ...)`` turns a household's facts and one
policy's ``FieldRecord`` rows into a cited :class:`~charitycare.schemas.Determination`.
It is pure code: no LLM, no I/O, ``Decimal`` money with explicit rounding.

Guardrails enforced here:

* Provisional fields never drive a result: if any of
  :data:`~charitycare.schemas.DETERMINATION_FIELDS` is provisional the outcome
  is ``preliminary_provisional_fields`` and no tier is matched.
* The outcome is about income (the %FPL tier, or the policy's medical-hardship
  rule when income is above every tier). Asset, residency, insurance and
  Medicaid-first conditions are reported beside it as met, likely not met or
  not evaluated; the hospital makes the final decision.
* Abstained "may" clauses are shown as hospital discretion with their quote.
* An unstated FPG year uses the guidelines current at the application date,
  and the prior-year result is reported when the tier would flip.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from charitycare import fpg
from charitycare.config import DISCLAIMER_VERSION
from charitycare.engine.deadlines import compute_deadlines
from charitycare.engine.policy import FieldView, PolicyView
from charitycare.schemas import (
    DETERMINATION_FIELDS,
    AssetTest,
    Determination,
    DeterminationOutcome,
    Evidence,
    FieldRecord,
    FieldStatus,
    FPGTier,
    Household,
    InsuredRule,
    Residency,
)

ConditionStatus = Literal["met", "not_met", "unknown", "not_applicable", "discretion"]
CONDITION_KEYS: tuple[str, ...] = ("asset_test", "residency", "insured_patients", "medical_hardship", "medicaid_first")

NOT_MET_PREFIX = "Likely not met:"
_YEAR_RX = re.compile(r"\b(20\d{2})\b")
_PCT_RX = re.compile(r"(\d+(?:\.\d+)?)\s*%")


@dataclass(frozen=True)
class Condition:
    """One non-income condition of the policy, as evaluated for a household."""

    key: str
    status: ConditionStatus
    text: str
    citations: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class YearChoice:
    year: int
    assumed: bool  # True when the policy's rule could not be applied (prior-year check runs)
    note: str


@dataclass(frozen=True)
class EngineResult:
    """The public ``Determination`` plus the structured condition report behind it."""

    determination: Determination
    conditions: tuple[Condition, ...] = ()
    matched_tier_index: int | None = None
    prior_year_flip: bool = False
    prior_year_tier_index: int | None = None

    def condition(self, key: str) -> Condition | None:
        return next((c for c in self.conditions if c.key == key), None)

    def statuses(self) -> dict[str, ConditionStatus]:
        return {c.key: c.status for c in self.conditions}


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def fmt_pct(p: float | Decimal) -> str:
    """``200.0 -> "200"``, ``137.5 -> "137.5"``."""
    d = Decimal(str(p)).normalize()
    return f"{d:f}" if d != d.to_integral_value() else str(int(d))


def _exact_pct(income: Decimal, fpg_amount: int) -> Decimal:
    return income * 100 / Decimal(fpg_amount)


def display_pct(exact: Decimal, bounds: Iterable[float]) -> str:
    """%FPL to one decimal (half-up), with more digits when rounding would land on a tier bound."""
    bset = {Decimal(str(b)) for b in bounds}
    for q in ("0.1", "0.01", "0.0001"):
        disp = exact.quantize(Decimal(q), rounding=ROUND_HALF_UP)
        if disp not in bset or disp == exact:
            return f"{disp:f}"
    return f"{exact.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP):f}"


def tier_label(t: FPGTier) -> str:
    if t.benefit == "free":
        return "free care tier"
    if t.benefit == "agb_cap":
        return "amounts-generally-billed cap tier"
    if t.discount_pct is not None:
        kind = "sliding-scale" if t.benefit == "sliding_scale" else "discount"
        return f"{fmt_pct(t.discount_pct)}% {kind} tier"
    return "discount tier"


def _cite(ev: Evidence) -> str:
    sec = f"§{ev.section_label} " if ev.section_label else ""
    return f"{sec}p.{ev.page}"


def _quote(ev: Evidence) -> str:
    return f"“{ev.quote}” ({_cite(ev)})"


# --------------------------------------------------------------------------
# FPG year and tier matching
# --------------------------------------------------------------------------


_GUIDELINE_WORD = re.compile(r"poverty|\bfpg\b|\bfpl\b|guideline", re.IGNORECASE)


def guideline_year_in(text: str, *, table_only: bool = True) -> int | None:
    """The guideline year a sentence names, or None when it is ambiguous.

    With one distinct candidate year (in the FPG table) that year is returned. With
    several (for example "Effective January 1, 2026, ... the 2025 Federal Poverty
    Guidelines"), the year clearly nearest the guideline wording wins; when no year is
    clearly tied to the wording, None is returned so the caller treats the year as assumed.
    """
    cands = [(int(m.group(1)), m.start(), m.end()) for m in _YEAR_RX.finditer(text)
             if not table_only or int(m.group(1)) in fpg.GUIDELINES]
    if not cands:
        return None
    if len({y for y, _, _ in cands}) == 1:
        return cands[0][0]
    words = [(m.start(), m.end()) for m in _GUIDELINE_WORD.finditer(text)]
    if not words:
        return None
    dist: dict[int, int] = {}
    for y, a, b in cands:
        d = min((w0 - b) if w0 >= b else (a - w1) if a >= w1 else 0 for w0, w1 in words)
        dist[y] = min(dist.get(y, d), d)
    ranked = sorted(dist.items(), key=lambda kv: kv[1])
    (best, d0), (_, d1) = ranked[0], ranked[1]
    return best if d0 <= 25 and d1 > d0 + 15 else None


def stated_year(view: FieldView) -> int | None:
    """The guideline year printed in the fpg_year_rule (or tier) quote, if unambiguous and in the table."""
    for ev in view.evidence:
        y = guideline_year_in(ev.quote)
        if y is not None:
            return y
    return None


def choose_year(view: PolicyView, household: Household, application_date: date) -> YearChoice:
    """Apply the policy's FPG year rule; fall back to the application date when it cannot be applied."""
    rule_view = view.get("fpg_year_rule")
    rule = rule_view.value if rule_view.state in ("usable", "provisional") else None
    app_year = fpg.guideline_year_for(application_date)
    if rule == "current_at_application":
        return YearChoice(app_year, False, f"Policy uses the guidelines current at application ({application_date.isoformat()}): {app_year} guidelines.")
    if rule == "current_at_service" and household.service_date is not None:
        y = fpg.guideline_year_for(household.service_date)
        return YearChoice(y, False, f"Policy uses the guidelines in effect on the date of service ({household.service_date.isoformat()}): {y} guidelines.")
    if rule == "stated_year":
        y = stated_year(rule_view) or stated_year(view.get("fpg_tiers"))
        if y is not None:
            return YearChoice(y, False, f"Policy states it uses the {y} guidelines.")
    reason = {
        "current_at_service": "The policy uses the guidelines in effect on the date of service, but no service date was entered",
        "stated_year": "The policy names a guideline year that is not in our table",
    }.get(rule or "", "The policy does not say which year's poverty guidelines apply")
    return YearChoice(app_year, True, f"{reason}; this estimate assumes the {app_year} guidelines current on the application date ({application_date.isoformat()}).")


def _in_tier(t: FPGTier, income: Decimal, size: int, year: int) -> bool:
    above_min = t.min_pct_exclusive is None or not fpg.income_within_pct(income, t.min_pct_exclusive, size, year)
    return above_min and fpg.income_within_pct(income, t.max_pct_inclusive, size, year)


def ordered_tiers(tiers: Sequence[FPGTier]) -> list[FPGTier]:
    return sorted(tiers, key=lambda t: (t.max_pct_inclusive, t.min_pct_exclusive or 0.0))


def match_tier(
    tiers: Sequence[FPGTier], income: Decimal, size: int, year: int, insured: bool | None
) -> tuple[int | None, FPGTier | None]:
    """First tier (lowest band first) whose band contains the income and whose scope fits the insurance status.

    Returns the index into :func:`ordered_tiers` and the tier.
    """
    for i, t in enumerate(ordered_tiers(tiers)):
        if t.applies_to == "insured" and insured is False:
            continue
        if t.applies_to == "uninsured" and insured is True:
            continue
        if _in_tier(t, income, size, year):
            return i, t
    return None, None


def _tier_evidence(t: FPGTier, evidence: Sequence[Evidence]) -> list[Evidence]:
    """The quote(s) that state this tier's band; all tier quotes if none stands out."""
    want = {Decimal(str(t.max_pct_inclusive))}
    if t.min_pct_exclusive is not None:
        want.add(Decimal(str(t.min_pct_exclusive)))
    allowed = want | {Decimal(100)} | ({Decimal(str(t.discount_pct))} if t.discount_pct is not None else set())
    best: list[tuple[int, Evidence]] = []
    for ev in evidence:
        nums = {Decimal(n) for n in _PCT_RX.findall(ev.quote)}
        score = 10 * len(nums & want) - len(nums - allowed) - (0 if t.min_pct_exclusive is not None or "above" not in ev.quote.lower() else 5)
        if nums & want:
            best.append((score, ev))
    if not best:
        return list(evidence)
    top = max(s for s, _ in best)
    return [ev for s, ev in best if s == top][:1]


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------


def _unusable(key: str, v: FieldView, what: str) -> Condition:
    if v.is_discretionary:
        return Condition(key, "discretion", f"{what}: hospital discretion, see quote {_quote(v.evidence[0])}.", tuple(v.evidence))
    why = {"missing": "not compiled for this policy", "abstained": "not stated in the policy as compiled",
           "provisional": "pending reviewer verification"}[v.state]
    return Condition(key, "unknown", f"{what} not evaluated: {why}; confirm with the hospital.", tuple(v.evidence))


def _confirmed_not_addressed(v: FieldView) -> bool:
    """A reviewer confirmed the policy does not address this field (not a failed or disputed read)."""
    return (v.state == "abstained" and v.abstain_reason == "not_addressed" and v.record is not None
            and v.record.status == FieldStatus.abstained)


def _bank_statement_hint(view: PolicyView) -> str:
    docs = view.usable("required_documents") or []
    for d in docs:
        if d.doc_type == "bank_statement":
            if d.period_months:
                return f"attach {d.period_months} months of bank statements"
            if d.count:
                return f"attach {d.count} bank statements"
    return "attach recent bank statements"


def asset_condition(view: PolicyView, household: Household, benefit: str | None) -> Condition:
    v = view.get("asset_test")
    if v.state != "usable":
        return _unusable("asset_test", v, "Asset test")
    at: AssetTest = v.value
    ev = tuple(v.evidence)
    if not at.applies:
        return Condition("asset_test", "not_applicable", "The policy does not consider assets.", ev)
    if (at.applies_to_tiers == "free_only" and benefit != "free") or (at.applies_to_tiers == "discount_only" and benefit == "free"):
        return Condition("asset_test", "not_applicable", "The policy's asset test does not apply to this tier.", ev)
    if at.threshold_usd is None:
        return Condition("asset_test", "unknown", f"Asset test not evaluated: the policy considers assets but states no dollar limit; {_bank_statement_hint(view)}.", ev)
    limit = Decimal(str(at.threshold_usd))
    counted = f" ({', '.join(at.counted_assets)})" if at.counted_assets else ""
    excluded = f"; not counted: {', '.join(at.exclusions)}" if at.exclusions else ""
    if household.liquid_assets is None:
        return Condition("asset_test", "unknown",
                         f"Asset test not evaluated: the policy limits liquid assets{counted} to {fpg.format_usd(limit)}{excluded}; {_bank_statement_hint(view)}.", ev)
    a = Decimal(household.liquid_assets)
    if a <= limit:
        return Condition("asset_test", "met",
                         f"Asset test: reported liquid assets of {fpg.format_usd(a)} are at or below the {fpg.format_usd(limit)} limit; the hospital will check bank statements.", ev)
    return Condition("asset_test", "not_met",
                     f"{NOT_MET_PREFIX} reported liquid assets of {fpg.format_usd(a)} are above the policy's {fpg.format_usd(limit)} limit ({_cite(ev[0]) if ev else 'asset test'}). The hospital decides.", ev)


def normalize_county(name: str) -> str:
    """``" Bexar County " -> "bexar"``."""
    n = " ".join(name.strip().split()).casefold()
    return n[: -len(" county")].strip() if n.endswith(" county") else n


def residency_condition(view: PolicyView, household: Household) -> Condition:
    v = view.get("residency")
    if v.state != "usable":
        return _unusable("residency", v, "Residency")
    r: Residency = v.value
    ev = tuple(v.evidence)
    if not r.required:
        return Condition("residency", "not_applicable", "The policy has no residency requirement.", ev)
    if r.emergency_exempt and household.emergency_care:
        return Condition("residency", "met", "Residency: the policy's residency requirement does not apply to emergency care.", ev)
    if r.area_type == "counties" and r.counties:
        area = ", ".join(r.counties) + " counties"
        if household.county is None:
            return Condition("residency", "unknown", f"Residency not evaluated: the policy requires residence in {area}; enter the patient's county.", ev)
        if normalize_county(household.county) in {normalize_county(c) for c in r.counties}:
            return Condition("residency", "met", f"Residency: {household.county.strip()} is within the policy's area ({area}).", ev)
        return Condition("residency", "not_met",
                         f"{NOT_MET_PREFIX} {household.county.strip()} is outside the policy's residency area ({area})"
                         + ("; emergency care is exempt" if r.emergency_exempt else "") + ". The hospital decides.", ev)
    if r.area_type == "state" and r.states:
        states = ", ".join(r.states)
        if household.state.strip().upper() in {s.upper() for s in r.states}:
            return Condition("residency", "met", f"Residency: the patient lives in {states}, as the policy requires.", ev)
        return Condition("residency", "not_met",
                         f"{NOT_MET_PREFIX} the policy requires residence in {states}"
                         + ("; emergency care is exempt" if r.emergency_exempt else "") + ". The hospital decides.", ev)
    return Condition("residency", "unknown", "Residency not evaluated: the policy limits assistance to its service area; confirm with the hospital.", ev)


def insured_condition(view: PolicyView, household: Household) -> Condition:
    v = view.get("insured_patients")
    if v.state != "usable":
        return _unusable("insured_patients", v, "Insurance status rule")
    ir: InsuredRule = v.value
    ev = tuple(v.evidence)
    if ir.insured_eligible:
        return Condition("insured_patients", "not_applicable", "The policy also covers balances after insurance.", ev)
    if household.insured is None:
        return Condition("insured_patients", "unknown", "Insurance status not evaluated: the policy covers only patients without health insurance; enter insurance status.", ev)
    if household.insured:
        return Condition("insured_patients", "not_met",
                         f"{NOT_MET_PREFIX} the policy covers only patients without health insurance ({_cite(ev[0]) if ev else 'insured rule'}). The hospital decides.", ev)
    return Condition("insured_patients", "met", "Insurance: the patient is uninsured, as the policy requires.", ev)


def hardship_condition(view: PolicyView, household: Household) -> Condition:
    """The medical-indigency rule: out-of-pocket bills above X% of income (strictly greater)."""
    v = view.get("insured_patients")
    if v.state != "usable":
        if _confirmed_not_addressed(v):
            return Condition("medical_hardship", "not_applicable", "The policy as compiled states no medical-hardship rule.")
        return _unusable("medical_hardship", v, "Medical-hardship rule")
    if v.value.catastrophic_threshold_pct_income is None:
        return Condition("medical_hardship", "not_applicable", "The policy has no medical-hardship rule.")
    thr = Decimal(str(v.value.catastrophic_threshold_pct_income))
    ev = tuple(e for e in v.evidence if "%" in e.quote) or tuple(v.evidence)
    if household.out_of_pocket_medical_12m is None:
        return Condition("medical_hardship", "unknown",
                         f"Medical-hardship rule not evaluated: the policy offers a discount when out-of-pocket medical bills for the prior 12 months exceed {fmt_pct(thr)}% of household income; enter those bills.", ev)
    oop, inc = Decimal(household.out_of_pocket_medical_12m), Decimal(household.annual_income)
    if oop * 100 > thr * inc:
        return Condition("medical_hardship", "met",
                         f"Medical hardship: out-of-pocket bills of {fpg.format_usd(oop)} exceed {fmt_pct(thr)}% of income ({fpg.format_usd((thr * inc / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))}), which the policy treats as qualifying for a discount.", ev)
    return Condition("medical_hardship", "not_met",
                     f"Medical hardship: out-of-pocket bills of {fpg.format_usd(oop)} do not exceed {fmt_pct(thr)}% of income.", ev)


def medicaid_condition(view: PolicyView) -> Condition:
    v = view.get("medicaid_application_required_first")
    if v.state == "usable" and v.value is True:
        return Condition("medicaid_first", "unknown",
                         "Medicaid first: the hospital asks patients who appear likely to qualify for Medicaid to apply for Medicaid "
                         "(or provide a denial) before it approves assistance.", tuple(v.evidence))
    if v.state == "usable" or _confirmed_not_addressed(v):
        return Condition("medicaid_first", "not_applicable", "No Medicaid-first requirement found.", tuple(v.evidence))
    return _unusable("medicaid_first", v, "Medicaid-first requirement")


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


def _dedupe(evs: Iterable[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, int, str]] = set()
    out: list[Evidence] = []
    for e in evs:
        k = (e.doc_sha256, e.page, e.quote)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _income_note(view: PolicyView) -> str | None:
    d = view.usable("income_definition")
    return {
        "gross": "The policy counts gross household income (before taxes and deductions).",
        "net": "The policy counts net household income (after taxes); enter net income.",
        "adjusted_gross": "The policy counts adjusted gross income (as on the tax return).",
    }.get(d or "")


def _arithmetic(income: Decimal, size: int, year: int, fpg_amount: int, tiers: Sequence[FPGTier] | None,
                matched: FPGTier | None, no_tier: str | None = None) -> str:
    exact = _exact_pct(income, fpg_amount)
    bounds = [b for t in (tiers or []) for b in (t.max_pct_inclusive, t.min_pct_exclusive) if b is not None]
    s = f"{fpg.format_usd(income)} ÷ {fpg.format_usd(fpg_amount)} ({year} FPG, HH of {size}) = {display_pct(exact, bounds)}% FPL"
    if matched is not None:
        s += f" ≤ {fmt_pct(matched.max_pct_inclusive)}% ({tier_label(matched)})"
    elif no_tier:
        s += f" ({no_tier})"
    elif tiers:
        s += f" > {fmt_pct(max(t.max_pct_inclusive for t in tiers))}% (highest income tier)"
    return s


def tier_gap(tiers: Sequence[FPGTier], income: Decimal, size: int, year: int, insured: bool | None) -> str | None:
    """Why no tier applies when the income is not above every tier (None when it is above the top bound)."""
    top = max(t.max_pct_inclusive for t in tiers)
    if not fpg.income_within_pct(income, top, size, year):
        return None
    scoped = [t for t in tiers if _in_tier(t, income, size, year)]
    if scoped:
        scopes = sorted({t.applies_to for t in scoped})
        who = "insured" if insured else "uninsured"
        return f"no tier applies to {who} patients at this income; the policy's tiers at this income apply only to {' and '.join(scopes)} patients"
    return "no income tier in the policy covers this income (the tier bands leave a gap)"


def evaluate(
    household: Household,
    records: Sequence[FieldRecord],
    *,
    facility_id: str,
    policy_version_id: str,
    today: date | None = None,
) -> EngineResult:
    """Screen one household against one policy version's field records."""
    today = today or date.today()
    view = PolicyView(policy_version_id, list(records))
    app_date = household.application_date or today
    size, income = household.household_size, Decimal(household.annual_income)

    yc = choose_year(view, household, app_date)
    fpg_amount = fpg.fpg(yc.year, size)
    pct = fpg.pct_fpl(income, size, yc.year)

    provisional = view.provisional(DETERMINATION_FIELDS)
    window_view = view.get("application_window")
    dl = compute_deadlines(window_view.value if window_view.state == "usable" else None,
                           first_statement_date=household.first_statement_date,
                           service_date=household.service_date, today=today)
    unevaluated: list[str] = []
    if household.first_statement_date is None:
        unevaluated.append("Application deadline not computed: enter the date of the first post-discharge billing statement.")
    notes: list[str] = []
    citations: list[Evidence] = []
    discretion = view.discretionary()
    gap: str | None = None

    def build(outcome: DeterminationOutcome, *, tiers: Sequence[FPGTier] | None, matched: FPGTier | None,
              conditions: Sequence[Condition] = (), idx: int | None = None, flip: bool = False,
              prior_idx: int | None = None) -> EngineResult:
        for c in conditions:
            if c.status == "unknown":
                unevaluated.append(c.text)
            elif c.status in ("met", "not_met", "discretion"):
                notes.append(c.text)
            if c.status != "not_applicable":
                citations.extend(c.citations)
        for v in discretion:
            if v.key not in {c.key for c in conditions}:
                notes.append(f"Hospital discretion ({v.key.replace('_', ' ')}): see quote {_quote(v.evidence[0])}.")
                citations.extend(v.evidence)
        notes.extend(dl.notes)
        if window_view.state == "usable" and (dl.policy is not None):
            citations.extend(window_view.evidence)
        det = Determination(
            facility_id=facility_id, policy_version_id=policy_version_id, fpg_year=yc.year,
            household_size=size, annual_income=income, pct_fpl=pct, outcome=outcome, matched_tier=matched,
            arithmetic=_arithmetic(income, size, yc.year, fpg_amount, tiers, matched, gap),
            unevaluated_conditions=unevaluated, provisional_fields_used=provisional if outcome == "preliminary_provisional_fields" else [],
            citations=_dedupe(citations), federal_deadline=dl.federal, policy_deadline=dl.policy,
            disclaimer_version=DISCLAIMER_VERSION, notes=notes,
        )
        return EngineResult(det, tuple(conditions), idx, flip, prior_idx)

    # 1. Provisional fields never drive a determination.
    if provisional:
        notes.append("Preliminary: these policy fields are not yet verified by a reviewer, so no tier is shown: "
                     + ", ".join(k.replace("_", " ") for k in provisional) + ".")
        notes.append(yc.note)
        return build("preliminary_provisional_fields", tiers=None, matched=None)

    # 2. Income tiers must be verified and present.
    tv = view.get("fpg_tiers")
    tiers: list[FPGTier] | None = tv.value if tv.state == "usable" else None
    if not tiers:
        if tv.is_discretionary:
            notes.append(f"Income tiers: hospital discretion, see quote {_quote(tv.evidence[0])}.")
            citations.extend(tv.evidence)
            discretion = [d for d in discretion if d.key != "fpg_tiers"]
        else:
            unevaluated.append("Income tiers not available for this policy (not stated or not yet compiled); "
                               "the hospital's financial assistance office can say which tier applies.")
        notes.append(yc.note)
        return build("insufficient_information", tiers=None, matched=None)

    notes.append(yc.note)
    if (inote := _income_note(view)) is not None:
        notes.append(inote)

    idx, matched = match_tier(tiers, income, size, yc.year, household.insured)
    conditions: list[Condition] = []
    if matched is not None:
        outcome: DeterminationOutcome = "likely_free" if matched.benefit == "free" else "likely_discount"
        citations.extend(_tier_evidence(matched, tv.evidence))
        benefit: str | None = matched.benefit
        conditions.append(Condition("medical_hardship", "not_applicable", "Income tier applies; medical-hardship rule not needed."))
        if matched.applies_to != "all" and household.insured is None:
            unevaluated.append(f"This tier applies to {matched.applies_to} patients only; enter insurance status.")
    else:
        gap = tier_gap(tiers, income, size, yc.year, household.insured)
        top = ordered_tiers(tiers)[-1]
        citations.extend(_tier_evidence(top, tv.evidence))
        hc = hardship_condition(view, household)
        conditions.append(hc)
        outcome = "likely_discount" if hc.status == "met" else "likely_not_eligible_on_income"
        benefit = "percent_discount" if hc.status == "met" else None
        if gap is not None:
            notes.append(f"Income tiers: {gap}.")
            if "gap" in gap and hc.status != "met":
                outcome = "insufficient_information"
                unevaluated.append("No income tier covers this income; the hospital's financial assistance office can say which tier applies.")

    if (fy := view.get("fpg_year_rule")).state == "usable":
        citations.extend(fy.evidence)
    if outcome in ("likely_not_eligible_on_income", "insufficient_information") and matched is None:
        why = f"Not reached: {gap}." if gap else "Not reached: income is above the policy's tiers."
        conditions += [Condition(k, "not_applicable", why)
                       for k in ("asset_test", "residency", "insured_patients", "medicaid_first")]
    else:
        conditions += [asset_condition(view, household, benefit), residency_condition(view, household),
                       insured_condition(view, household), medicaid_condition(view)]
    conditions.sort(key=lambda c: CONDITION_KEYS.index(c.key))

    # 3. Unstated FPG year: report the prior-year result when the tier flips.
    flip, prior_idx = False, None
    if yc.assumed and (yc.year - 1) in fpg.GUIDELINES:
        py = yc.year - 1
        prior_idx, prior_tier = match_tier(tiers, income, size, py, household.insured)
        if prior_idx != idx:
            flip = True
            prior_pct = fpg.pct_fpl(income, size, py)
            what = f"the {tier_label(prior_tier)}" if prior_tier is not None else "no income tier (above the highest tier)"
            notes.append(f"Under the {py} guidelines ({fpg.format_usd(fpg.fpg(py, size))} for a household of {size}) "
                         f"this income is {prior_pct}% FPL and would fall in {what}; ask the hospital which year it uses.")
    return build(outcome, tiers=tiers, matched=matched, conditions=conditions, idx=idx, flip=flip, prior_idx=prior_idx)


def no_policy_determination(facility_id: str, household: Household, *, today: date | None = None) -> Determination:
    """Result when no compiled policy is on file for the facility."""
    today = today or date.today()
    year = fpg.guideline_year_for(household.application_date or today)
    size, income = household.household_size, Decimal(household.annual_income)
    dl = compute_deadlines(None, first_statement_date=household.first_statement_date, service_date=None, today=today)
    return Determination(
        facility_id=facility_id, policy_version_id="", fpg_year=year, household_size=size, annual_income=income,
        pct_fpl=fpg.pct_fpl(income, size, year), outcome="insufficient_information",
        arithmetic=_arithmetic(income, size, year, fpg.fpg(year, size), None, None),
        unevaluated_conditions=["No compiled financial assistance policy is on file for this facility yet."],
        federal_deadline=dl.federal, disclaimer_version=DISCLAIMER_VERSION, notes=list(dl.notes),
    )

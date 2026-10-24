"""Filing-period-aware Schedule H cross-check (design section 5 mechanism 3, step S6).

Compiled values are compared, in code, with the hospital's sworn Schedule H
Part V Section B answers. Each comparison resolves to a
:class:`~charitycare.schemas.CrossCheckResult`:

* ``agree``;
* ``disagree_policy_newer_than_filing`` when the values differ but the policy's
  effective/revision date is after the filing's tax period end (the filing
  describes an older policy, so this is context, not a discrepancy);
* ``disagree_filing_lag_possible`` when the values differ but the policy date is
  unknown (the filing may describe the prior policy; a reviewer decides, nothing
  is public);
* ``disagree_unexplained`` otherwise (the only result that can reach the public
  "discrepancies for review" list, after the hospital's right of reply);
* ``not_reported`` when the filing does not answer the line.

Only published (quote-verified, non-abstained) values are compared.
Part V line 13 sub-boxes are checkboxes that are omitted when unchecked, so
when line 13 itself is answered "Yes" a missing box is read as "not a criterion".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any
from urllib.parse import urlparse

from charitycare.compile.mapping import name_similarity
from charitycare.schemas import (
    CompiledPolicy,
    CrossCheck,
    CrossCheckResult,
    Facility,
    ScheduleHFacility,
)
from charitycare.wording import assert_safe_wording

DOCUMENTS_KEY = "_documents"  # pseudo field key for 16a-c / 16i document-level checks


def _norm_name(s: str) -> str:
    return " ".join("".join(c for c in s.casefold() if c.isalnum() or c.isspace()).split())


def select_section(
    sections: Iterable[ScheduleHFacility], facilities: Iterable[Facility] = (), facility_names: Iterable[str] = ()
) -> ScheduleHFacility | None:
    """Pick the Schedule H block that reports on these facilities (latest tax period wins).

    Matching order: the facility's Schedule H group letter, then a facility name listed
    in the block, then the only block of the return.
    """
    secs = sorted(sections, key=lambda s: s.tax_period_end, reverse=True)
    if not secs:
        return None
    facs = list(facilities)
    letters = {f.schedule_h_group_letter for f in facs if f.schedule_h_group_letter}
    names = {_norm_name(n) for n in [*facility_names, *(f.name for f in facs)] if n}
    for s in secs:
        if s.group_letter and s.group_letter in letters:
            return s
    for s in secs:
        if names & {_norm_name(n) for n in s.facility_names}:
            return s
    latest = [s for s in secs if s.object_id == secs[0].object_id]
    return latest[0] if len(latest) == 1 else None


def resolve(agree: bool, policy_date: date | None, period_end: date) -> CrossCheckResult:
    """Map a raw comparison to a result, accounting for filing lag.

    Without a policy date the code cannot tell filing lag from a real difference, so
    the difference is held for review (``disagree_filing_lag_possible``). A policy
    revised inside the tax period stays ``disagree_unexplained``: Schedule H reports
    the policy in effect at the end of the period.
    """
    if agree:
        return CrossCheckResult.agree
    if policy_date is None:
        return CrossCheckResult.disagree_filing_lag_possible
    if policy_date > period_end:
        return CrossCheckResult.disagree_policy_newer_than_filing
    return CrossCheckResult.disagree_unexplained


def _ref(sec: ScheduleHFacility, line: str) -> str:
    who = f"group {sec.group_letter}" if sec.group_letter else (
        f"facility {','.join(map(str, sec.facility_numbers))}" if sec.facility_numbers else "facility"
    )
    label = line if line.startswith("section") else f"line {line}"
    return f"irs:{sec.object_id}|Part V {label}|{who}"


def _box(sec: ScheduleHFacility, attr: str) -> bool | None:
    v = getattr(sec, attr)
    if v is not None:
        return bool(v)
    return False if sec.line_13_criteria_explained else None


def _fmt(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


class _Builder:
    def __init__(self, sec: ScheduleHFacility, policy_date: date | None):
        self.sec, self.policy_date = sec, policy_date
        self.out: dict[str, list[CrossCheck]] = {}

    def add(self, key: str, line: str, external: Any, compiled: Any, agree: bool | None, what: str) -> None:
        label = line if line.startswith("section") else f"line {line}"
        if agree is None:
            result, note = CrossCheckResult.not_reported, f"Schedule H {label} does not report {what}."
        else:
            result = resolve(agree, self.policy_date, self.sec.tax_period_end)
            note = f"{what}: policy {_fmt(compiled)} vs Schedule H {label} {_fmt(external)}"
            if result is CrossCheckResult.disagree_policy_newer_than_filing:
                note += f"; the policy date {self.policy_date} is after the filing period end {self.sec.tax_period_end}"
            elif result is CrossCheckResult.disagree_filing_lag_possible:
                note += "; the policy date is not known, so the filing may describe an earlier policy (held for reviewer, not public)"
            elif result is CrossCheckResult.disagree_unexplained:
                note += f"; discrepancy for review (filing period ends {self.sec.tax_period_end}, policy date {self.policy_date})"
        assert_safe_wording(note)
        self.out.setdefault(key, []).append(
            CrossCheck(
                field_key=key,
                source="schedule_h",
                external_ref=_ref(self.sec, line),
                external_value=external,
                filing_period_end=self.sec.tax_period_end,
                policy_date=self.policy_date,
                result=result,
                note=note,
            )
        )


def published_value(policy: CompiledPolicy, key: str, published: set[str] | None) -> Any:
    f = getattr(policy, key)
    if f.is_abstained or (published is not None and key not in published):
        return None
    return f.value


def cross_check(
    policy: CompiledPolicy, sec: ScheduleHFacility, *, published: set[str] | None = None
) -> dict[str, list[CrossCheck]]:
    """Cross-check the published fields of ``policy`` against one Schedule H block.

    ``published`` restricts the comparison to fields that passed the quote gate
    (``None`` compares every non-abstained field).
    """
    eff = published_value(policy, "effective_or_revision_date", published)
    b = _Builder(sec, eff if isinstance(eff, date) else None)

    covered = published_value(policy, "facilities_covered", published)
    if covered and sec.facility_names:
        missing = [n for n in sec.facility_names if max(name_similarity(n, c) for c in covered) < 0.9]
        b.add("facilities_covered", "section A", sec.facility_names, covered, not missing, "facilities reported")

    tiers = published_value(policy, "fpg_tiers", published)
    if tiers:
        free = max((t.max_pct_inclusive for t in tiers if t.benefit == "free"), default=None)
        top = max(t.max_pct_inclusive for t in tiers)
        ext = {"free_pct": sec.line_13a_free_pct, "discount_pct": sec.line_13a_discount_pct}
        comp = {"free_pct": free, "discount_pct": top}
        if sec.line_13a_fpg_used is False:
            b.add("fpg_tiers", "13a", ext | {"fpg_used": False}, comp, False, "FPG income limits")
        elif sec.line_13a_free_pct is None and sec.line_13a_discount_pct is None:
            b.add("fpg_tiers", "13a", ext, comp, None, "FPG income limits")
        else:
            agree = all(ext[k] is None or comp[k] == ext[k] for k in ext)
            b.add("fpg_tiers", "13a", ext, comp, agree, "free / discount limits (% FPG)")

    asset = published_value(policy, "asset_test", published)
    if asset is not None:
        ext = _box(sec, "line_13c_asset_level")
        b.add("asset_test", "13c", ext, asset.applies, None if ext is None else ext == asset.applies, "asset test used")

    res = published_value(policy, "residency", published)
    if res is not None:
        ext = _box(sec, "line_13g_residency")
        b.add("residency", "13g", ext, res.required, None if ext is None else ext == res.required, "residency required")

    ins = published_value(policy, "insured_patients", published)
    if ins is not None:
        restricts = (not ins.insured_eligible) or any(t.applies_to != "all" for t in (tiers or []))
        ext = _box(sec, "line_13e_insurance_status")
        b.add("insured_patients", "13e", ext, restricts, None if ext is None else (ext or not restricts), "insurance status used")
        catastrophic = ins.catastrophic_threshold_pct_income is not None
        ext = _box(sec, "line_13d_medical_indigency")
        b.add("insured_patients", "13d", ext, catastrophic, None if ext is None else ext == catastrophic, "medical indigency used")

    docs = published_value(policy, "required_documents", published)
    if docs is not None:
        for line, attr, what in (("15a", "line_15a_described_info", "application information described"),
                                 ("15b", "line_15b_described_docs", "supporting documents described")):
            ext = getattr(sec, attr)
            b.add("required_documents", line, ext, bool(docs), None if ext is None else ext == bool(docs), what)

    agb = published_value(policy, "agb_method", published)
    if agb is not None:
        ext = sec.line_22_agb_method
        b.add("agb_method", "22", ext, agb, None if ext is None else ext == agb, "AGB method")
    return b.out


def _norm_url(u: str | None) -> str | None:
    if not u or "://" not in u and not u.lower().startswith("www."):
        return None
    p = urlparse(u if "://" in u else "https://" + u)
    host = (p.hostname or "").lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}".lower()


def document_cross_check(
    sec: ScheduleHFacility,
    urls: dict[str, str],
    languages: Iterable[str] = (),
    policy_date: date | None = None,
) -> list[CrossCheck]:
    """Lines 16a-c (FAP, application, plain-language summary URLs) and 16i (translations).

    ``urls`` maps ``fap`` / ``application`` / ``pls`` to the URL the document was found
    at. A line 16a that says "see Part V, Section C" is resolved through the URLs
    recovered from the narrative. Translations are compared only when we found one.
    """
    b = _Builder(sec, policy_date)
    narrative = {_norm_url(u) for u in sec.narrative_urls_recovered} - {None}
    for kind, line, attr in (("fap", "16a", "line_16a_fap_url"), ("application", "16b", "line_16b_app_url"), ("pls", "16c", "line_16c_pls_url")):
        found = _norm_url(urls.get(kind))
        if found is None:
            continue
        reported = getattr(sec, attr)
        candidates = {_norm_url(reported)} - {None} or (narrative if reported else set())
        b.add(DOCUMENTS_KEY, line, reported, urls[kind], None if not candidates else found in candidates, f"{kind} URL")
    langs = {lang.split("-")[0].lower() for lang in languages}
    if langs - {"en"} and sec.line_16i_translated is not None:
        b.add(DOCUMENTS_KEY, "16i", sec.line_16i_translated, sorted(langs), bool(sec.line_16i_translated), "translations available")
    return b.out.get(DOCUMENTS_KEY, [])


def worst(checks: Iterable[CrossCheck]) -> CrossCheckResult | None:
    """Most severe result among checks (unexplained > newer > not_reported > agree)."""
    order = [CrossCheckResult.disagree_unexplained, CrossCheckResult.disagree_filing_lag_possible,
             CrossCheckResult.disagree_policy_newer_than_filing,
             CrossCheckResult.not_reported, CrossCheckResult.agree]
    results = {c.result for c in checks}
    return next((r for r in order if r in results), None)

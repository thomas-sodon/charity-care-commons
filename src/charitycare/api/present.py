"""Human-readable rendering of compiled fields, statuses and cross-checks.

Shared by the read API (``display`` strings), the review console, the MCP
server and the static site, so every surface words things the same way. All
strings here are our own text and must pass :mod:`charitycare.wording`
(no "eligible" without "likely", "discrepancy for review", never "violation").
"""

from __future__ import annotations

from datetime import date
from typing import Any

from charitycare.schemas import (
    CompiledPolicy,
    CrossCheck,
    CrossCheckResult,
    Field,
    FieldRecord,
    FieldStatus,
    FPGTier,
)
from charitycare.wording import DISCREPANCY_LABEL

FIELD_LABELS: dict[str, str] = {
    "policy_title": "Policy title",
    "effective_or_revision_date": "Effective / revised",
    "facilities_covered": "Facilities covered",
    "fpg_tiers": "Income tiers",
    "fpg_year_rule": "Poverty guideline year",
    "income_definition": "Income counted",
    "household_definition": "Household",
    "asset_test": "Asset test",
    "residency": "Residency",
    "insured_patients": "Insured patients",
    "medicaid_application_required_first": "Medicaid application first",
    "excluded_services": "Excluded services",
    "provider_list_location": "Provider list",
    "agb_method": "AGB method",
    "agb_pct": "AGB percentage",
    "application_window": "Application window",
    "required_documents": "Documents",
    "submission_channels": "Apply by",
    "presumptive_eligibility": "Presumptive eligibility",
    "screening_timing": "Screening",
    "refund_policy": "Refunds",
    "determination_notice": "Written decision",
    "award_period_months": "Award period",
}

# Order of the rows on the hospital page (Screen 1): the rules a navigator needs first.
DISPLAY_ORDER: tuple[str, ...] = (
    "fpg_tiers",
    "asset_test",
    "residency",
    "insured_patients",
    "medicaid_application_required_first",
    "required_documents",
    "submission_channels",
    "application_window",
    "screening_timing",
    "presumptive_eligibility",
    "refund_policy",
    "fpg_year_rule",
    "income_definition",
    "household_definition",
    "excluded_services",
    "agb_method",
    "agb_pct",
    "determination_notice",
    "award_period_months",
    "provider_list_location",
    "facilities_covered",
    "effective_or_revision_date",
    "policy_title",
)

ABSTAIN_TEXT: dict[str, str] = {
    "not_addressed": "Not addressed in policy",
    "ambiguous_may_clause": "Hospital discretion, see quote",
    "conflicting_clauses": "Conflicting clauses, see quotes",
    "illegible": "Page not machine-readable (no text layer); see the PDF",
    "quote_unverified": "Not published: the supporting quote could not be verified on the cited page",
}

STATUS_BADGES: dict[str, tuple[str, str]] = {
    # status -> (symbol, label)
    "verified": ("✓", "verified"),
    "provisional": ("○", "provisional"),
    "disputed": ("!", "disputed"),
    "abstained": ("–", "abstained"),
}

CROSSCHECK_LABELS: dict[str, str] = {
    "agree": "matches IRS filing",
    "disagree_policy_newer_than_filing": "policy is newer than the filing period (context, not a discrepancy)",
    "disagree_unexplained": DISCREPANCY_LABEL,
    "disagree_filing_lag_possible": "filing may predate the policy (context, under review)",
    "not_reported": "not reported on the IRS filing",
}

_ENUM_TEXT: dict[str, str] = {
    "current_at_application": "Guidelines current at the application date",
    "current_at_service": "Guidelines current at the date of service",
    "stated_year": "A stated guideline year",
    "unspecified": "Not specified",
    "gross": "Gross income",
    "net": "Net income",
    "adjusted_gross": "Adjusted gross income",
    "lookback_medicare": "Look-back, Medicare fee-for-service",
    "lookback_medicare_private": "Look-back, Medicare and private insurers",
    "lookback_medicaid": "Look-back, Medicaid",
    "prospective": "Prospective (Medicare/Medicaid rates)",
    "before_billing": "Before billing",
    "after_billing_before_collections": "After billing, before collections",
    "before_writeoff": "Before bad-debt write-off",
    "first_post_discharge_statement": "the first post-discharge statement",
    "date_of_service": "the date of service",
    "discharge": "discharge",
}

_CHANNEL_TEXT = {"mail": "Mail", "fax": "Fax", "email": "E-mail", "portal": "Online portal", "in_person": "In person", "phone": "Phone"}

_DOC_TEXT = {
    "pay_stub": "pay stubs",
    "tax_return": "tax return",
    "w2": "W-2",
    "bank_statement": "bank statements",
    "photo_id": "photo ID",
    "proof_of_residency": "proof of residency",
    "medicaid_denial": "Medicaid denial",
    "unemployment_benefits": "unemployment benefits letter",
    "social_security_statement": "Social Security statement",
    "letter_of_support": "letter of support",
    "other": "other document",
}


def label(key: str) -> str:
    return FIELD_LABELS.get(key, key.replace("_", " ").capitalize())


def typed_field(key: str, raw: Field[Any]) -> Field[Any]:
    """Re-validate a stored ``Field[Any]`` against its ``CompiledPolicy`` annotation."""
    info = CompiledPolicy.model_fields.get(key)
    if info is None or info.annotation is None:
        return raw
    try:
        return info.annotation.model_validate(raw.model_dump(mode="json"))
    except ValueError:
        return raw


def _pct(x: float | None) -> str:
    if x is None:
        return "0%"
    return f"{x:g}%"


def _usd(x: float | None) -> str:
    if x is None:
        return "an unstated amount"
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def tier_text(t: FPGTier) -> str:
    rng = f"up to {_pct(t.max_pct_inclusive)} FPL" if not t.min_pct_exclusive else (
        f"over {_pct(t.min_pct_exclusive)} to {_pct(t.max_pct_inclusive)} FPL"
    )
    benefit = {
        "free": "Free care",
        "percent_discount": f"{t.discount_pct:g}% discount" if t.discount_pct is not None else "Discount",
        "agb_cap": "Charges capped at AGB",
        "sliding_scale": "Sliding-scale discount",
    }[t.benefit]
    who = "" if t.applies_to == "all" else f" ({t.applies_to} patients)"
    return f"{benefit} {rng}{who}"


def _get(v: Any, name: str, default: Any = None) -> Any:
    return getattr(v, name, v.get(name, default) if isinstance(v, dict) else default)


# Free-text fields whose value is the hospital's own wording: shown in curly quotes, as hospital text.
HOSPITAL_TEXT_FIELDS = frozenset({"refund_policy", "household_definition", "provider_list_location"})


def hospital_text(s: str) -> str:
    """Wrap hospital wording in curly quotes (our wording guard exempts quoted hospital text)."""
    s = s.strip().strip("“”\"")
    return f"“{s}”"


def format_value(key: str, value: Any) -> str:
    """One-line display text for a compiled field value (``None`` -> empty string)."""
    if value is None:
        return ""
    if key == "fpg_tiers":
        tiers = sorted(value, key=lambda t: _get(t, "max_pct_inclusive", 0))
        return "; ".join(tier_text(t if isinstance(t, FPGTier) else FPGTier.model_validate(t)) for t in tiers)
    if key == "asset_test":
        if not _get(value, "applies"):
            return "No asset test"
        txt = f"Yes, over {_usd(_get(value, 'threshold_usd'))}"
        counted = _get(value, "counted_assets") or []
        if counted:
            txt += f" in {', '.join(counted)}"
        excl = _get(value, "exclusions") or []
        if excl:
            txt += f"; not counted: {', '.join(excl)}"
        scope = _get(value, "applies_to_tiers", "all")
        if scope != "all":
            txt += f" ({scope.replace('_', ' ')})"
        return txt
    if key == "residency":
        if not _get(value, "required"):
            return "No residency requirement"
        area = _get(value, "area_type")
        if area == "counties":
            cs = _get(value, "counties") or []
            txt = f"{', '.join(cs)} {'county' if len(cs) == 1 else 'counties'}"
        elif area == "state":
            states = _get(value, "states") or ["TX"]
            txt = f"Residents of {', '.join(states)}"
        elif area == "service_area":
            txt = "Hospital service area"
        else:
            txt = "Required"
        if _get(value, "emergency_exempt"):
            txt += "; emergency care exempt"
        return txt
    if key == "insured_patients":
        txt = "Insured patients may apply" if _get(value, "insured_eligible") else "Uninsured patients only"
        cat = _get(value, "catastrophic_threshold_pct_income")
        if cat is not None:
            txt += f"; medical bills over {cat:g}% of income qualify"
        return txt
    if key == "application_window":
        days = _get(value, "days")
        anchor = _ENUM_TEXT.get(_get(value, "anchor", "unspecified"), "an unspecified date")
        if days is None:
            return "Window not stated"
        return f"{days} days" + ("" if anchor == "Not specified" else f" from {anchor}")
    if key == "required_documents":
        parts = []
        for d in value:
            n, months, kind = _get(d, "count"), _get(d, "period_months"), _get(d, "doc_type")
            name = _DOC_TEXT.get(kind, kind)
            if months:
                parts.append(f"{months} months {name}")
            elif n and n > 1:
                parts.append(f"{n} {name}")
            elif kind == "other":
                parts.append(_get(d, "description") or name)
            else:
                parts.append(name)
        return " · ".join(parts)
    if key == "submission_channels":
        return " · ".join(_CHANNEL_TEXT.get(_get(c, "kind"), str(_get(c, "kind"))) for c in value)
    if key == "presumptive_eligibility":
        if not _get(value, "allowed"):
            return "Not offered"
        crit = _get(value, "criteria") or []
        txt = "Yes" + (f": {'; '.join(hospital_text(c) for c in crit)}" if crit else "")
        if _get(value, "uses_credit_data"):
            txt += " (uses credit data)"
        return txt
    if key == "agb_pct":
        return f"{float(value):g}% of gross charges"
    if key == "award_period_months":
        return f"{value} months"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "None listed"
    if isinstance(value, str):
        return hospital_text(value) if key in HOSPITAL_TEXT_FIELDS else _ENUM_TEXT.get(value, value)
    return str(value)


def field_display(rec: FieldRecord) -> str:
    """Display text for a stored field, including abstentions."""
    f = typed_field(rec.key, rec.field)
    if f.abstain_reason is not None:
        return ABSTAIN_TEXT.get(f.abstain_reason, "Abstained")
    if f.value is None:
        return "No value"
    return format_value(rec.key, f.value)


def status_badge(status: FieldStatus | str) -> dict[str, str]:
    s = status.value if isinstance(status, FieldStatus) else str(status)
    sym, text = STATUS_BADGES.get(s, ("?", s))
    return {"status": s, "symbol": sym, "label": text}


def _ext_value_text(cc: CrossCheck) -> str:
    v = cc.external_value
    if v is None:
        return "not reported"
    if isinstance(v, bool):
        return "checked" if v else "not checked"
    if isinstance(v, dict) and {"free_pct", "discount_pct"} <= set(v):
        return f"free up to {format_scalar(v['free_pct'])}%, discount up to {format_scalar(v['discount_pct'])}%"
    if isinstance(v, dict):
        return ", ".join(f"{k.replace('_', ' ')} {format_scalar(x)}" for k, x in v.items())
    return format_scalar(v)


def format_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, str):
        return _ENUM_TEXT.get(v, v)
    return str(v)


HELD_LABEL = "difference under review (not yet published)"


def crosscheck_display(cc: CrossCheck, state: str | None = None) -> dict[str, Any]:
    """Public rendering of one cross-check (``public_label`` never says "violation").

    ``state`` is the registry's publication state for a ``disagree_unexplained`` check
    (:meth:`Registry.discrepancy_state`); anything other than ``public`` is held back
    and rendered as a difference under review, not as a discrepancy.
    """
    fy = f"IRS FY{cc.filing_period_end.year}" if cc.filing_period_end else "IRS filing"
    res = cc.result.value if isinstance(cc.result, CrossCheckResult) else str(cc.result)
    held = res == CrossCheckResult.disagree_unexplained.value and state not in (None, "public")
    symbol = "ⓘ" if held else {"agree": "✓", "disagree_unexplained": "⚠", "disagree_policy_newer_than_filing": "ⓘ",
                                "disagree_filing_lag_possible": "ⓘ"}.get(res, "–")
    return {
        "result": res,
        "symbol": symbol,
        "public_label": HELD_LABEL if held else CROSSCHECK_LABELS.get(res, res),
        "summary": f"{fy}: {_ext_value_text(cc)} {symbol}".strip(),
        "external_text": _ext_value_text(cc),
        "is_discrepancy": res == CrossCheckResult.disagree_unexplained.value and not held,
        "publication": state if res == CrossCheckResult.disagree_unexplained.value else None,
    }

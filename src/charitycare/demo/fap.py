"""Generate each demo hospital's FAP text and its gold labels from one spec.

Every cited sentence is registered with :meth:`FapWriter.cite` at the moment it
is written, so the gold quote, page and section label are exact by
construction. :func:`gold_policy` then turns those citations plus the spec into
a :class:`~charitycare.schemas.CompiledPolicy`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from charitycare import fpg as fpglib
from charitycare.demo.render import Block, PageSpec
from charitycare.demo.spec import HospitalSpec
from charitycare.schemas import (
    ApplicationWindow,
    AssetTest,
    Channel,
    CompiledPolicy,
    Evidence,
    Field,
    FPGTier,
    InsuredRule,
    Presumptive,
    RequiredDoc,
    Residency,
)

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def long_date(d: date) -> str:
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def pct(v: float) -> str:
    return f"{v:g}%"


def join_list(items: list[str] | tuple[str, ...], conj: str = "and") -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} {conj} {items[1]}"
    return ", ".join(items[:-1]) + f", {conj} {items[-1]}"


@dataclass
class Cite:
    page: int
    quote: str
    section: str | None


@dataclass
class FapWriter:
    pages: list[PageSpec] = field(default_factory=list)
    cites: dict[str, list[Cite]] = field(default_factory=dict)
    section: str | None = None

    def new_page(self, image_only: bool = False) -> None:
        self.pages.append(PageSpec(image_only=image_only))

    @property
    def page_no(self) -> int:
        return len(self.pages)

    def block(self, style: str, text: str) -> None:
        self.pages[-1].blocks.append(Block(style, text))  # type: ignore[arg-type]

    def h(self, label: str | None, title: str) -> None:
        self.section = label
        self.block("h", f"{label} {title}" if label else title)

    def cite(self, key: str, quote: str) -> str:
        if len(quote) > 400:
            raise ValueError(f"quote for {key} exceeds 400 chars")
        self.cites.setdefault(key, []).append(Cite(self.page_no, quote, self.section))
        return quote

    def p(self, *parts: str | tuple[str, str]) -> None:
        """Paragraph made of plain strings and ``(key, sentence)`` citations."""
        out = []
        for part in parts:
            if isinstance(part, tuple):
                out.append(self.cite(part[0], part[1]))
            else:
                out.append(part)
        self.block("p", " ".join(out))


@dataclass
class IncomeTable:
    stated_year: int
    computed_from_year: int
    page: int
    pcts: list[float]
    rows: list[dict[str, Any]]


def _money(v: Decimal) -> str:
    return f"${int(v.quantize(Decimal('1'), rounding=ROUND_HALF_UP)):,}"


def build_fap(h: HospitalSpec) -> tuple[FapWriter, IncomeTable | None]:
    """Write the FAP for hospital ``h``; returns the writer (pages + citations) and any income table."""
    w = FapWriter()
    table: IncomeTable | None = None
    fac_names = [f.name for f in h.facilities]

    # ---- page 1: title, purpose, scope -------------------------------------------------
    w.new_page()
    w.block("title", h.system_name or h.name)
    w.block("title", w.cite("policy_title", h.policy_title))
    if h.id == "pineywoods-regional":
        w.block("subtitle", w.cite("effective_or_revision_date", f"Revision date: {long_date(h.effective_date)}") + " (supersedes the policy dated July 1, 2023)")
    else:
        w.block("subtitle", w.cite("effective_or_revision_date", f"Effective date: {long_date(h.effective_date)}"))
    w.h("1.", "Purpose")
    w.p(
        f"{h.system_name or h.name} is committed to providing financial assistance to patients who are uninsured, "
        "underinsured, or otherwise unable to pay for emergency and other medically necessary care, consistent with "
        "Section 501(r) of the Internal Revenue Code and applicable Texas law."
    )
    w.h("2.", "Scope")
    if h.addendum:
        w.p("This Policy applies to emergency and other medically necessary care provided by each hospital facility of the System listed in Addendum A.")
    else:
        w.p(("facilities_covered", f"This Policy applies to emergency and other medically necessary care provided by {join_list(fac_names)}."))
    if h.excluded_services:
        w.p(("excluded_services", f"This Policy does not cover {join_list(h.excluded_services)}."))
    else:
        w.p(("excluded_services", "All medically necessary services provided by the Hospital are covered by this Policy."))
    w.p(
        (
            "provider_list_location",
            "A list of providers who deliver care in the Hospital, showing which providers are and are not covered by "
            f"this Policy, is in {h.provider_list_location}.",
        )
    )

    # ---- page 2: definitions ----------------------------------------------------------
    w.new_page()
    w.h("3.", "Definitions")
    w.section = "3.1"
    w.p(("household_definition", f"3.1 Household. For purposes of this Policy, the household includes {h.household_text}."))
    w.section = "3.2"
    income_sentence = {
        "gross": "3.2 Income. Household income means the household's total gross annual income before taxes and deductions.",
        "net": "3.2 Income. Household income means the household's net income after taxes and payroll deductions.",
        "adjusted_gross": "3.2 Income. Household income means adjusted gross income as reported on the federal tax return.",
        "unspecified": "3.2 Income. Household income includes wages, benefits and other income of the household.",
    }[h.income_definition]
    w.p(("income_definition", income_sentence))
    w.section = "3.3"
    year_sentence = {
        "current_at_application": "Eligibility is determined using the Federal Poverty Guidelines in effect on the date the application is received.",
        "current_at_service": "Eligibility is determined using the Federal Poverty Guidelines in effect on the date of service.",
        "stated_year": f"Eligibility is determined using the {h.income_table[0] if h.income_table else 2026} Federal Poverty Guidelines shown in the table in Section 4.",
        "unspecified": "The Hospital uses the Federal Poverty Guidelines published by the U.S. Department of Health and Human Services.",
    }[h.fpg_year_rule]
    w.p("3.3 Federal Poverty Guidelines (FPG).", ("fpg_year_rule", year_sentence))
    w.section = "3.4"
    w.p(
        "3.4 Medically necessary care. Care that is reasonable and necessary to diagnose or treat an illness or injury, "
        "as determined by the treating provider."
    )

    # ---- page 3: eligibility tiers (+ printed income table) ----------------------------
    w.new_page(image_only=3 in h.image_only_fap_pages)
    w.h("4.", "Eligibility for Financial Assistance")
    top = max(t.hi for t in h.tiers)
    w.section = "4.1"
    w.p(
        f"4.1 Patients may qualify for financial assistance if household income is at or below {pct(top)} of the FPG "
        "and the other conditions of this Policy are met."
    )
    w.section = "4.2"
    for t in h.tiers:
        if t.benefit == "free":
            s = f"Patients with household income at or below {pct(t.hi)} of the Federal Poverty Guidelines qualify for free care, a 100% discount."
        elif t.benefit == "agb_cap":
            s = (
                f"Patients with household income above {pct(t.lo or 0)} and at or below {pct(t.hi)} of the Federal Poverty "
                "Guidelines will not be charged more than amounts generally billed."
            )
        elif t.benefit == "percent_discount":
            s = (
                f"Patients with household income above {pct(t.lo or 0)} and at or below {pct(t.hi)} of the Federal Poverty "
                f"Guidelines receive a {t.discount_pct:g}% discount from gross charges."
            )
        else:
            s = f"Household income above {pct(t.lo or 0)} and up to {pct(t.hi)} of the Federal Poverty Guidelines: {t.discount_pct:g}% discount (sliding scale)."
        w.p(("fpg_tiers", s))
    if h.income_table:
        stated, computed = h.income_table
        pcts = sorted({100.0, *[t.hi for t in h.tiers]})
        w.block("p", f"{stated} Federal Poverty Guidelines income limits (annual household income):")
        header = "Household size " + "".join(f"{pct(p):>12}" for p in pcts)
        lines = [header]
        rows = []
        for size in range(1, 9):
            amounts = [fpglib.income_at_pct(p, size, computed) for p in pcts]
            lines.append(f"{size:>14} " + "".join(f"{_money(a):>12}" for a in amounts))
            rows.extend({"household_size": size, "pct": p, "amount": int(a.quantize(Decimal('1'), rounding=ROUND_HALF_UP))} for p, a in zip(pcts, amounts))
        w.block("mono", "\n".join(lines))
        table = IncomeTable(stated, computed, w.page_no, pcts, rows)

    # ---- page 4: assets, residency, insurance, Medicaid -------------------------------
    w.new_page()
    w.h("5.", "Other Eligibility Conditions")
    w.section = "5.1"
    if h.asset_threshold is not None:
        w.p(
            "5.1 Assets.",
            ("asset_test", f"Patients whose household liquid assets exceed ${h.asset_threshold:,.0f} are not eligible for free or discounted care."),
            ("asset_test", "Liquid assets include checking accounts, savings accounts, and investments."),
            ("asset_test", f"The following assets are not counted: {join_list(h.asset_exclusions)}."),
        )
    else:
        w.p("5.1 Assets.", ("asset_test", "The Hospital does not consider assets when determining eligibility."))
    w.section = "5.2"
    if h.residency_counties:
        parts: list[str | tuple[str, str]] = [
            "5.2 Residency.",
            ("residency", f"To qualify, patients must reside in one of the following Texas counties: {join_list(h.residency_counties, 'or')}."),
        ]
        if h.emergency_exempt:
            parts.append(("residency", "The residency requirement does not apply to emergency care."))
        w.p(*parts)
    elif h.residency_state:
        parts = ["5.2 Residency.", ("residency", "Patients must be residents of the State of Texas.")]
        if h.emergency_exempt:
            parts.append(("residency", "The residency requirement does not apply to emergency care."))
        w.p(*parts)
    else:
        w.p("5.2 Residency.", ("residency", "There is no residency requirement for financial assistance."))
    w.section = "5.3"
    if not h.insured_eligible:
        w.p("5.3 Insurance.", ("insured_patients", "Financial assistance under this Policy is available only to patients who have no health insurance."))
    else:
        parts = [
            "5.3 Insurance.",
            ("insured_patients", "Insured patients are eligible for assistance with deductibles, co-insurance and other balances after insurance."),
        ]
        if h.catastrophic_pct is not None:
            parts.append(
                (
                    "insured_patients",
                    f"Patients whose out-of-pocket medical bills for the prior 12 months exceed {pct(h.catastrophic_pct)} of household "
                    "income also qualify for a discount, regardless of insurance status.",
                )
            )
        w.p(*parts)
    w.section = "5.4"
    if h.medicaid_first:
        w.p("5.4 Other programs.", ("medicaid_application_required_first", "Patients who appear likely to qualify for Medicaid must apply for Medicaid before financial assistance is approved."))
    else:
        w.p("5.4 Other programs.", ("medicaid_application_required_first", "Patients are not required to apply for Medicaid before receiving a financial assistance determination."))

    # ---- page 5: how to apply -----------------------------------------------------------
    w.new_page()
    w.h("6.", "How to Apply")
    w.section = "6.1"
    w.p("6.1 Application period.", ("application_window", f"Patients may apply at any time within {h.window_days} days after the date of the first post-discharge billing statement."))
    w.section = "6.2"
    w.block("p", "6.2 Required documents. Applicants must attach the following documents:")
    for i, d in enumerate(h.required_docs):
        w.block("p", f"({chr(97 + i)}) " + w.cite("required_documents", d.text) + ";")
    w.section = "6.3"
    w.block("p", "6.3 Where to apply. Completed applications may be submitted:")
    verb = {"mail": "By mail to", "fax": "By fax to", "email": "By e-mail to", "portal": "Online at", "in_person": "In person at", "phone": "By telephone at"}
    for kind, detail in h.channels:
        w.block("p", "- " + w.cite("submission_channels", f"{verb[kind]} {detail}."))
    w.section = "6.4"
    if h.determination_notice:
        w.p("6.4 Determination.", ("determination_notice", "The Hospital will notify the patient in writing of its determination and the basis for the determination."))
    else:
        w.p("6.4 Determination. Applications are reviewed by the Business Office.")
    w.section = "6.5"
    w.p("6.5 Award period.", ("award_period_months", f"Approved financial assistance applies to eligible care for {h.award_months} months from the date of determination."))

    # ---- page 6: billing, presumptive, screening, refunds ---------------------------------
    w.new_page()
    w.h("7.", "Billing and Collections")
    w.section = "7.1"
    agb = {
        "lookback_medicare": "The Hospital calculates amounts generally billed using the look-back method based on Medicare fee-for-service claims.",
        "lookback_medicare_private": "The Hospital calculates amounts generally billed using the look-back method based on Medicare fee-for-service and all private health insurers.",
        "lookback_medicaid": "The Hospital calculates amounts generally billed using the look-back method based on Medicaid, together with Medicare fee-for-service and private health insurers.",
        "prospective": "The Hospital calculates amounts generally billed using the prospective Medicare method.",
    }[h.agb_method]
    w.p("7.1 Amounts generally billed (AGB).", ("agb_method", agb), ("agb_pct", f"The current AGB percentage is {h.agb_pct:g}% of gross charges."))
    w.section = "7.2"
    if h.presumptive == "allowed" and h.presumptive_credit_data:
        w.p("7.2 Presumptive eligibility.", ("presumptive_eligibility", "The Hospital uses predictive scoring based on third-party credit data to grant presumptive eligibility for free care without an application."))
    elif h.presumptive == "allowed":
        w.p("7.2 Presumptive eligibility.", ("presumptive_eligibility", f"The Hospital grants presumptive eligibility for free care, without an application, to patients who are {join_list(h.presumptive_criteria, 'or')}."))
    elif h.presumptive == "may":
        w.p("7.2 Presumptive eligibility.", ("presumptive_eligibility", "The Hospital may, at its sole discretion, consider presumptive eligibility for patients experiencing homelessness or patients who are deceased with no estate."))
    else:
        w.p("7.2 Presumptive eligibility.", ("presumptive_eligibility", "The Hospital does not make presumptive eligibility determinations; an application is required."))
    w.section = "7.3"
    timing = {
        "before_billing": "The Hospital screens patients for financial assistance before issuing the first billing statement.",
        "after_billing_before_collections": "The Hospital screens patients for financial assistance after billing and before referring any account to collections.",
        "before_writeoff": "Accounts are screened for financial assistance before any balance is written off as bad debt.",
    }.get(h.screening_timing)
    if timing:
        w.p("7.3 Screening.", ("screening_timing", timing))
    w.section = "7.4"
    if h.refund_text:
        w.p("7.4 Refunds.", ("refund_policy", h.refund_text))
    w.section = "7.5"
    w.p(
        "7.5 Collections. The Hospital will not engage in extraordinary collection actions before making reasonable efforts "
        "to determine whether a patient is eligible for financial assistance. The Billing and Collections Policy is available free of charge."
    )
    w.block("p", f"Questions: call {h.phone} or write to {h.mail_address}.")

    # ---- addendum ---------------------------------------------------------------------------
    if h.addendum:
        w.new_page()
        w.h("Addendum A.", "Hospital Facilities Covered by this Policy")
        w.section = "Addendum A"
        w.p(("facilities_covered", f"The following hospital facilities of the System are covered by this Policy: {'; '.join(fac_names)}."))
        for f in h.facilities:
            w.block("p", f"{f.name}, {f.address}, {f.city}, Texas {f.zip} ({f.county} County). Financial counselors are available in the admitting area.")
        w.block("p", "Each facility follows the eligibility criteria in Sections 4 and 5 without facility-specific changes.")
    return w, table


# ------------------------------------------------------------------------------------------
# Gold labels
# ------------------------------------------------------------------------------------------


def _ev(sha: str, cites: list[Cite]) -> list[Evidence]:
    return [Evidence(doc_sha256=sha, page=c.page, quote=c.quote, section_label=c.section) for c in cites]


def gold_policy(h: HospitalSpec, w: FapWriter, fap_sha: str) -> CompiledPolicy:
    """The ground-truth compiled policy for ``h`` (confidence 1.0, abstentions where the text abstains)."""

    def f(key: str, value: Any) -> Field[Any]:
        cites = w.cites.get(key, [])
        if not cites:
            return Field(value=None, evidence=[], confidence=1.0, abstain_reason="not_addressed")
        return Field(value=value, evidence=_ev(fap_sha, cites), confidence=1.0)

    tiers = [
        FPGTier(
            min_pct_exclusive=t.lo,
            max_pct_inclusive=t.hi,
            benefit=t.benefit,
            discount_pct=t.discount_pct if t.benefit in ("percent_discount", "sliding_scale") else None,
            applies_to="all",
        )
        for t in h.tiers
    ]
    asset = (
        AssetTest(
            applies=True,
            threshold_usd=h.asset_threshold,
            counted_assets=["checking accounts", "savings accounts", "investments"],
            exclusions=list(h.asset_exclusions),
        )
        if h.asset_threshold is not None
        else AssetTest(applies=False)
    )
    if h.residency_counties:
        res = Residency(required=True, area_type="counties", counties=list(h.residency_counties), states=["TX"], emergency_exempt=h.emergency_exempt)
    elif h.residency_state:
        res = Residency(required=True, area_type="state", states=["TX"], emergency_exempt=h.emergency_exempt)
    else:
        res = Residency(required=False, area_type="none")
    if h.presumptive == "may":
        presumptive = Field(
            value=None,
            evidence=_ev(fap_sha, w.cites["presumptive_eligibility"]),
            confidence=1.0,
            abstain_reason="ambiguous_may_clause",
        )
    else:
        presumptive = f(
            "presumptive_eligibility",
            Presumptive(
                allowed=h.presumptive == "allowed",
                criteria=list(h.presumptive_criteria),
                uses_credit_data=h.presumptive_credit_data,
            ),
        )
    return CompiledPolicy(
        policy_title=f("policy_title", h.policy_title),
        effective_or_revision_date=f("effective_or_revision_date", h.effective_date),
        facilities_covered=f("facilities_covered", [x.name for x in h.facilities]),
        fpg_tiers=f("fpg_tiers", tiers),
        fpg_year_rule=f("fpg_year_rule", h.fpg_year_rule),
        income_definition=f("income_definition", h.income_definition),
        household_definition=f("household_definition", h.household_text),
        asset_test=f("asset_test", asset),
        residency=f("residency", res),
        insured_patients=f("insured_patients", InsuredRule(insured_eligible=h.insured_eligible, catastrophic_threshold_pct_income=h.catastrophic_pct)),
        medicaid_application_required_first=f("medicaid_application_required_first", h.medicaid_first),
        excluded_services=f("excluded_services", list(h.excluded_services)),
        provider_list_location=f("provider_list_location", h.provider_list_location),
        agb_method=f("agb_method", h.agb_method),
        agb_pct=f("agb_pct", h.agb_pct),
        application_window=f("application_window", ApplicationWindow(days=h.window_days, anchor="first_post_discharge_statement")),
        required_documents=f(
            "required_documents",
            [RequiredDoc(doc_type=d.kind, count=d.count, period_months=d.period_months, description=d.text) for d in h.required_docs],
        ),
        submission_channels=f("submission_channels", [Channel(kind=k, detail=d) for k, d in h.channels]),  # type: ignore[arg-type]
        presumptive_eligibility=presumptive,
        screening_timing=f("screening_timing", h.screening_timing),
        refund_policy=f("refund_policy", h.refund_text),
        determination_notice=f("determination_notice", True),
        award_period_months=f("award_period_months", h.award_months),
    )

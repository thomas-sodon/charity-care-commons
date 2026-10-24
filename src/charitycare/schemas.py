"""Shared Pydantic v2 models: the Open FAP Schema and the registry data model.

This module is the contract between every stage of the pipeline (design doc
sections 5, 8 and 10). Other modules import these models; they must not
redefine them. Three families live here:

* **LLM-facing extraction models** (``Evidence``, ``Field[T]``, the rule
  sub-models and ``CompiledPolicy``). They are used as structured-output
  schemas, so they avoid JSON-Schema features the API cannot enforce
  (tuples, ``prefixItems``, numeric bounds are advisory only).
* **Code-produced models** (``Determination``, ``ReadbackResult``,
  ``CrossCheck``, ``ScheduleHFacility``). The LLM never produces these.
* **Registry rows** (``Organization`` ... ``PacketMetric``), one per table of
  section 10, persisted by :mod:`charitycare.store`.

``export_json_schema()`` writes the published "Open FAP Schema" v0.1.
"""

from __future__ import annotations

import json
import re
import typing
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic import Field as PField
from pydantic.json_schema import GenerateJsonSchema, models_json_schema

SCHEMA_VERSION = "0.1.0"
SCHEMA_ID = "https://charitycarecommons.org/schemas/open-fap-schema/0.1.json"

T = TypeVar("T")

ModelTier = Literal["haiku", "sonnet", "opus"]


def utcnow() -> datetime:
    """Timezone-aware current UTC time (single place so tests can patch it)."""
    return datetime.now(timezone.utc)


class _Base(BaseModel):
    """Base config shared by all models: forbid unknown keys, keep enums as values."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False, populate_by_name=True)


# --------------------------------------------------------------------------
# Status enums (section 5: the verification-carrying tuple)
# --------------------------------------------------------------------------


class FieldStatus(StrEnum):
    """Publication state of one compiled field."""

    provisional = "provisional"  # auto-extracted; cannot drive a determination
    verified = "verified"  # a human reviewer approved it
    disputed = "disputed"  # hospital or reviewer contests it; statement shown
    abstained = "abstained"  # no value published (not addressed, ambiguous, illegible ...)


class CrossCheckResult(StrEnum):
    """Outcome of comparing a compiled value with an external source."""

    agree = "agree"
    disagree_policy_newer_than_filing = "disagree_policy_newer_than_filing"
    disagree_unexplained = "disagree_unexplained"  # only this reaches "discrepancies for review"
    # values differ but the policy date is unknown,
    # so the filing may describe the prior policy: held for a reviewer, never public
    disagree_filing_lag_possible = "disagree_filing_lag_possible"
    not_reported = "not_reported"


AbstainReason = Literal["not_addressed", "ambiguous_may_clause", "conflicting_clauses", "illegible", "quote_unverified"]
"""Why no value is published. ``quote_unverified`` is set by code when the quote gate
(verbatim match, cited page, numeric grounding) still fails after one re-ask."""


# --------------------------------------------------------------------------
# Evidence and the generic field wrapper (LLM-facing)
# --------------------------------------------------------------------------


class Evidence(_Base):
    """A verbatim quote locating a value in a hashed source document."""

    doc_sha256: str = PField(description="sha256 hex digest of the source document bytes")
    page: int = PField(ge=1, description="1-indexed page number in the source document")
    quote: str = PField(max_length=400, description="Verbatim text copied from the page, <= 400 chars")
    section_label: str | None = PField(default=None, description='Section label as printed, e.g. "4.2"')
    char_start: int | None = PField(default=None, description="Start offset in the normalized page text (set by the verifier)")
    char_end: int | None = PField(default=None, description="End offset in the normalized page text (set by the verifier)")


class Field(_Base, Generic[T]):
    """One compiled value with its evidence, or an abstention.

    Abstention wins: if ``abstain_reason`` is set, ``value`` is forced to None.
    A non-abstained field without evidence is representable (models make
    mistakes) but the citation gate in ``compile/`` refuses to publish it.
    """

    value: T | None = None
    evidence: list[Evidence] = PField(default_factory=list, description=">= 1 unless abstained")
    confidence: float = PField(default=0.0, ge=0.0, le=1.0, description="Model self-report in [0,1]; calibrated in evals")
    abstain_reason: AbstainReason | None = None

    @model_validator(mode="after")
    def _abstention_clears_value(self) -> "Field[T]":
        if self.abstain_reason is not None and self.value is not None:
            object.__setattr__(self, "value", None)
        return self

    @classmethod
    def model_parametrized_name(cls, params: tuple[type[Any], ...]) -> str:
        """Readable schema names such as ``Field_FPGYearRule`` and ``Field_list_FPGTier``."""
        return "Field_" + "_".join(_type_name(p) for p in params)

    @property
    def is_abstained(self) -> bool:
        """True when the field abstains (explicitly, or by having no value)."""
        return self.abstain_reason is not None or self.value is None

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence)


# --------------------------------------------------------------------------
# Rule sub-models (LLM-facing)
# --------------------------------------------------------------------------


class FPGTier(_Base):
    """One income band, in percent of the federal poverty guideline."""

    min_pct_exclusive: float | None = PField(default=None, description="Lower bound, exclusive; None means 0")
    max_pct_inclusive: float = PField(description="Upper bound, inclusive, e.g. 200 for 200% FPG")
    benefit: Literal["free", "percent_discount", "agb_cap", "sliding_scale"]
    discount_pct: float | None = PField(default=None, description="Discount off gross charges for percent_discount tiers")
    applies_to: Literal["uninsured", "insured", "all"] = "all"


class AssetTest(_Base):
    applies: bool
    threshold_usd: float | None = PField(default=None, description="Assets above this amount disqualify or reduce aid")
    counted_assets: list[str] = PField(default_factory=list, description='e.g. "checking", "savings", "stocks"')
    exclusions: list[str] = PField(default_factory=list, description='e.g. "primary residence", "retirement accounts"')
    applies_to_tiers: Literal["all", "free_only", "discount_only"] = "all"


class Residency(_Base):
    required: bool
    area_type: Literal["counties", "state", "service_area", "none"] = "none"
    counties: list[str] = PField(default_factory=list, description="County names without the word County")
    states: list[str] = PField(default_factory=list, description="Two-letter state codes")
    emergency_exempt: bool = PField(default=False, description="Emergency care is covered regardless of residency")


class InsuredRule(_Base):
    insured_eligible: bool
    catastrophic_threshold_pct_income: float | None = PField(
        default=None, description="Out-of-pocket medical bills above this % of income qualify (medical indigency)"
    )
    notes: str | None = None


class ApplicationWindow(_Base):
    days: int | None = PField(default=None, description="Days the application stays open")
    anchor: Literal["first_post_discharge_statement", "date_of_service", "discharge", "unspecified"] = "unspecified"


RequiredDocType = Literal[
    "pay_stub",
    "tax_return",
    "w2",
    "bank_statement",
    "photo_id",
    "proof_of_residency",
    "medicaid_denial",
    "unemployment_benefits",
    "social_security_statement",
    "letter_of_support",
    "other",
]


class RequiredDoc(_Base):
    doc_type: RequiredDocType
    count: int | None = PField(default=None, description="How many, e.g. 2 pay stubs")
    period_months: int | None = PField(default=None, description="Covering this many months, e.g. 2 months of statements")
    description: str = PField(description="Short paraphrase of the requirement")


class Channel(_Base):
    kind: Literal["mail", "fax", "email", "portal", "in_person", "phone"]
    detail: str | None = PField(default=None, description="Address, number, e-mail or URL as printed")


class Presumptive(_Base):
    allowed: bool
    criteria: list[str] = PField(default_factory=list, description='e.g. "enrolled in SNAP", "homeless"')
    uses_credit_data: bool | None = None


FPGYearRule = Literal["current_at_application", "current_at_service", "stated_year", "unspecified"]
IncomeDefinition = Literal["gross", "net", "adjusted_gross", "unspecified"]
AGBMethod = Literal["lookback_medicare", "lookback_medicare_private", "lookback_medicaid", "prospective"]
ScreeningTiming = Literal["before_billing", "after_billing_before_collections", "before_writeoff", "unspecified"]


_ALIAS_NAMES: dict[Any, str] = {
    FPGYearRule: "FPGYearRule",
    IncomeDefinition: "IncomeDefinition",
    AGBMethod: "AGBMethod",
    ScreeningTiming: "ScreeningTiming",
}


def _type_name(tp: Any) -> str:
    if tp in _ALIAS_NAMES:
        return _ALIAS_NAMES[tp]
    origin = typing.get_origin(tp)
    if origin is list:
        return "list_" + "_".join(_type_name(a) for a in typing.get_args(tp))
    if origin is typing.Literal:
        return "Literal"
    return getattr(tp, "__name__", str(tp))


class CompiledPolicy(_Base):
    """The executable rule program compiled from one FAP bundle (section 8)."""

    policy_title: Field[str]
    effective_or_revision_date: Field[date]
    facilities_covered: Field[list[str]]
    fpg_tiers: Field[list[FPGTier]]
    fpg_year_rule: Field[FPGYearRule]
    income_definition: Field[IncomeDefinition]
    household_definition: Field[str]
    asset_test: Field[AssetTest]
    residency: Field[Residency]
    insured_patients: Field[InsuredRule]
    medicaid_application_required_first: Field[bool]
    excluded_services: Field[list[str]]
    provider_list_location: Field[str]
    agb_method: Field[AGBMethod]
    agb_pct: Field[float]
    application_window: Field[ApplicationWindow]
    required_documents: Field[list[RequiredDoc]]
    submission_channels: Field[list[Channel]]
    presumptive_eligibility: Field[Presumptive]
    screening_timing: Field[ScreeningTiming]
    refund_policy: Field[str]
    determination_notice: Field[bool]
    award_period_months: Field[int]

    @classmethod
    def field_keys(cls) -> list[str]:
        """Names of all compiled fields, in schema order."""
        return list(cls.model_fields)

    def fields(self) -> dict[str, Field[Any]]:
        """Mapping of field key to its ``Field`` wrapper."""
        return {k: getattr(self, k) for k in self.field_keys()}


# Fields the eligibility engine needs to be verified before giving a non-preliminary answer.
DETERMINATION_FIELDS: tuple[str, ...] = (
    "fpg_tiers",
    "fpg_year_rule",
    "asset_test",
    "residency",
    "insured_patients",
    "application_window",
)


# --------------------------------------------------------------------------
# Cross-source verification (code-produced)
# --------------------------------------------------------------------------


class CrossCheck(_Base):
    """Comparison of one compiled field with an external source."""

    field_id: str | None = None
    field_key: str
    source: Literal["schedule_h", "state_filing", "benchmark"]
    external_ref: str = PField(description='e.g. "irs:202601234567890123|Part V line 13a|group A"')
    external_value: Any = None
    filing_period_end: date | None = None
    policy_date: date | None = None
    result: CrossCheckResult
    note: str | None = None


ECAKind = Literal["credit_report", "sell_debt", "defer_deny_care", "legal_process", "other", "none"]


class ScheduleHFacility(_Base):
    """One Schedule H Part V Section B block (a facility or a facility reporting group)."""

    id: str = PField(description='"{object_id}:{group_letter or facility_num}"')
    ein: str
    org_name: str
    object_id: str = PField(description="IRS e-file object id of the return")
    tax_year: int
    tax_period_begin: date | None = None
    tax_period_end: date
    group_letter: str | None = None
    facility_numbers: list[int] = PField(default_factory=list)
    facility_names: list[str] = PField(default_factory=list)
    line_13_criteria_explained: bool | None = None
    line_13a_fpg_used: bool | None = None
    line_13a_free_pct: float | None = None
    line_13a_discount_pct: float | None = None
    line_13b_income_level: bool | None = None
    line_13c_asset_level: bool | None = None
    line_13d_medical_indigency: bool | None = None
    line_13e_insurance_status: bool | None = None
    line_13f_underinsurance_status: bool | None = None
    line_13g_residency: bool | None = None
    line_13h_other: bool | None = None
    line_15a_described_info: bool | None = None
    line_15b_described_docs: bool | None = None
    line_16a_fap_url: str | None = None
    line_16b_app_url: str | None = None
    line_16c_pls_url: str | None = None
    line_16i_translated: bool | None = None
    line_18_ecas_permitted: list[ECAKind] = PField(default_factory=list)
    line_22_agb_method: AGBMethod | None = None
    narrative_urls_recovered: list[str] = PField(default_factory=list, description="URLs found in Part V Section C text")
    section_c_text: list[str] = PField(default_factory=list)
    xpaths: dict[str, str] = PField(default_factory=dict, description="line -> xpath the value came from")


# --------------------------------------------------------------------------
# Engine / packet outputs (code-produced)
# --------------------------------------------------------------------------


MAX_HOUSEHOLD_USD = Decimal("1000000000")
HOUSEHOLD_DATE_MIN, HOUSEHOLD_DATE_MAX = date(2000, 1, 1), date(2100, 12, 31)


class Household(_Base):
    """Facts a navigator enters for screening. Held in memory only, never persisted."""

    household_size: int = PField(ge=1, le=50)
    annual_income: Decimal = PField(ge=0, le=MAX_HOUSEHOLD_USD, allow_inf_nan=False)
    liquid_assets: Decimal | None = PField(default=None, ge=0, le=MAX_HOUSEHOLD_USD, allow_inf_nan=False)
    county: str | None = PField(default=None, max_length=80)
    state: str = PField(default="TX", max_length=2)
    insured: bool | None = None
    out_of_pocket_medical_12m: Decimal | None = PField(default=None, ge=0, le=MAX_HOUSEHOLD_USD, allow_inf_nan=False)
    emergency_care: bool = False
    service_date: date | None = None
    first_statement_date: date | None = None
    application_date: date | None = None

    @field_validator("service_date", "first_statement_date", "application_date")
    @classmethod
    def _date_in_range(cls, v: date | None) -> date | None:
        if v is not None and not (HOUSEHOLD_DATE_MIN <= v <= HOUSEHOLD_DATE_MAX):
            raise ValueError(f"date must be between {HOUSEHOLD_DATE_MIN} and {HOUSEHOLD_DATE_MAX}")
        return v


DeterminationOutcome = Literal[
    "likely_free",
    "likely_discount",
    "likely_not_eligible_on_income",
    "preliminary_provisional_fields",
    "insufficient_information",
]


class Determination(_Base):
    """Deterministic, cited screening result. Produced by code, never by the LLM."""

    facility_id: str
    policy_version_id: str
    fpg_year: int
    household_size: int
    annual_income: Decimal
    pct_fpl: Decimal
    outcome: DeterminationOutcome
    matched_tier: FPGTier | None = None
    arithmetic: str = PField(default="", description='e.g. "$49,700 / $27,320 = 181.9% FPL"')
    unevaluated_conditions: list[str] = PField(default_factory=list)
    provisional_fields_used: list[str] = PField(default_factory=list)
    citations: list[Evidence] = PField(default_factory=list)
    federal_deadline: date | None = None
    policy_deadline: date | None = None
    disclaimer_version: str
    notes: list[str] = PField(default_factory=list)


class ReadbackField(_Base):
    key: str
    intended: str
    read: str
    match: bool
    write_by_hand: bool = False


class ReadbackResult(_Base):
    """Render-and-reread diff of a filled form. ``all_match`` is derived."""

    template_id: str
    fields: list[ReadbackField] = PField(default_factory=list)
    retries: int = 0
    all_match: bool = False

    @model_validator(mode="after")
    def _derive_all_match(self) -> "ReadbackResult":
        derived = bool(self.fields) and all(f.match for f in self.fields if not f.write_by_hand)
        object.__setattr__(self, "all_match", derived)
        return self


# --------------------------------------------------------------------------
# Registry rows (section 10)
# --------------------------------------------------------------------------


class Organization(_Base):
    ein: str
    name: str
    system_name: str | None = None


class Facility(_Base):
    id: str = PField(description='Stable id, "tx-{ccn}" when a CCN exists')
    ccn: str | None = None
    tx_license_no: str | None = None
    name: str
    address: str | None = None
    city: str | None = None
    zip: str | None = None
    county: str | None = None
    state: str = "TX"
    type: str | None = PField(default=None, description='CMS "Hospital Type"')
    ownership: str | None = PField(default=None, description='CMS "Hospital Ownership"')
    ein: str | None = None
    schedule_h_group_letter: str | None = None
    in_scope: bool = True


SourceKind = Literal["fap", "application", "pls", "billing_policy", "other"]


class Source(_Base):
    id: str
    url: str
    kind: SourceKind
    discovered_via: str | None = PField(default=None, description='e.g. "schedule_h_16a", "crawl", "wayback", "demo"')
    first_seen: datetime | None = None
    last_checked: datetime | None = None
    http_status: int | None = None
    blocked: bool = False
    facility_ids: list[str] = PField(default_factory=list)


class DocumentVersion(_Base):
    sha256: str
    source_id: str | None = None
    kind: SourceKind = "other"
    fetched_at: datetime
    mime: str = "application/pdf"
    pages: int | None = None
    has_text_layer: bool | None = None
    ocr_used: bool = False
    language: str | None = PField(default=None, description='BCP-47, e.g. "en", "es"')
    text_uri: str | None = None
    pdf_uri: str | None = None
    wayback_uri: str | None = None


class PolicyVersion(_Base):
    id: str
    organization_ein: str
    title: str | None = None
    effective_date: date | None = None
    doc_shas: dict[str, str] = PField(default_factory=dict, description="kind -> sha256 (fap, application, pls, billing)")
    supersedes: str | None = None
    status: Literal["current", "superseded"] = "current"
    as_of: date


class PolicyFacilityMap(_Base):
    policy_version_id: str
    facility_id: str
    method: Literal["named_in_policy", "schedule_h_group", "system_default", "manual"]
    confidence: float = PField(ge=0.0, le=1.0)
    evidence: list[Evidence] = PField(default_factory=list)
    note: str | None = None


class FieldRecord(_Base):
    """The verification-carrying tuple of section 5, as stored in the registry."""

    id: str = PField(description='"{policy_version_id}:{key}"')
    policy_version_id: str
    key: str = PField(description="A CompiledPolicy field name")
    field: Field[Any]
    status: FieldStatus = FieldStatus.provisional
    crosschecks: list[CrossCheck] = PField(default_factory=list)
    as_of: date
    extractor_model_tier: ModelTier | None = None
    prompt_version: str | None = None
    reviewer: str | None = None
    reviewed_at: datetime | None = None

    @staticmethod
    def make_id(policy_version_id: str, key: str) -> str:
        return f"{policy_version_id}:{key}"


ReviewKind = Literal[
    "field_verify",
    "quote_unverified",
    "crosscheck_disagreement",
    "round_trip_mismatch",
    "facility_mapping",
    "form_template",
    "change_review",
    "hospital_correction",
]


class ReviewTask(_Base):
    id: str
    kind: ReviewKind
    field_id: str | None = None
    facility_id: str | None = None
    template_id: str | None = None
    priority: Literal["high", "medium", "low"] = "medium"
    state: Literal["open", "approved", "corrected", "abstained", "hospital_query", "closed"] = "open"
    notes: str | None = None
    impact: int | None = PField(default=None, description="Queue impact score (higher first); None = derive from priority")
    batch: list[str] = PField(default_factory=list, description="Field ids promoted when this spot-check sample is approved")
    created_at: datetime = PField(default_factory=utcnow)


ReviewAction = Literal["approve", "edit", "abstain", "ask_hospital"]


class ReviewDecision(_Base):
    """One maintainer decision on a review task (H1/H2/H6), kept as an audit row and in the changelog."""

    task_id: str
    field_id: str | None = None
    action: ReviewAction
    reviewer: str = PField(min_length=1, max_length=80)
    note: str | None = PField(default=None, max_length=2000)
    value: Any = None
    quote: str | None = PField(default=None, max_length=400)
    page: int | None = PField(default=None, ge=1)
    abstain_reason: AbstainReason | None = None
    decided_at: datetime = PField(default_factory=utcnow)


class HospitalResponse(_Base):
    id: str
    facility_id: str
    field_key: str | None = None
    statement: str
    received_at: datetime
    published: bool = False


class HospitalNotice(_Base):
    """A hospital was (or is to be) told about a discrepancy for review; its right-of-reply clock.

    Recorded when a reviewer confirms a ``disagree_unexplained`` cross-check. The item
    is public only once ``reply_due`` has passed or the hospital's response is published.
    """

    id: str
    field_id: str
    field_key: str
    facility_ids: list[str] = PField(default_factory=list)
    notified_at: datetime
    reply_due: date
    channel: str | None = None


class ChangeEvent(_Base):
    id: str
    policy_version_id: str
    prev_version_id: str | None = None
    triage: Literal["substantive", "cosmetic", "date_only"]
    diff_summary: str
    published_at: datetime | None = None


class FormFieldSpec(_Base):
    """Where one canonical value goes on a hospital's application form."""

    key: str = PField(description='Canonical key, e.g. "patient_name", "household_size"')
    label: str = PField(description="Label as printed on the form")
    page: int = PField(ge=1)
    rect: list[float] = PField(description="[x0, y0, x1, y1] in PDF points, top-left origin (PyMuPDF)")
    field_type: Literal["text", "checkbox", "date", "money", "signature"] = "text"
    widget_name: str | None = PField(default=None, description="AcroForm field name for fillable forms")
    write_by_hand: bool = PField(default=False, description="Left blank on purpose (SSN, account no., signature)")


class FormTemplate(_Base):
    doc_sha256: str
    kind: Literal["fillable", "flat"]
    language: str = "en"
    field_map: list[FormFieldSpec] = PField(default_factory=list)
    verified_by: str | None = None
    verified_at: datetime | None = None


class EvalRun(_Base):
    id: str
    git_sha: str | None = None
    dataset_version: str
    suite: str
    metrics: dict[str, float] = PField(default_factory=dict)
    cost_usd: float = 0.0
    created_at: datetime = PField(default_factory=utcnow)
    notes: list[str] = PField(default_factory=list, description="Run caveats, e.g. simulated baseline answers or costs")
    simulated_cost: bool = False


class PacketMetric(_Base):
    """Per-packet telemetry. Deliberately holds NO household data."""

    id: str
    template_id: str
    n_fields: int
    all_match: bool
    retries: int = 0
    latency_ms: int
    cost_usd: float = 0.0


# --------------------------------------------------------------------------
# Open FAP Schema export
# --------------------------------------------------------------------------

PUBLISHED_MODELS: tuple[type[BaseModel], ...] = (
    CompiledPolicy,
    FieldRecord,
    CrossCheck,
    ScheduleHFacility,
    Determination,
    Household,
    ReadbackResult,
    Facility,
    PolicyVersion,
    PolicyFacilityMap,
    DocumentVersion,
    FormTemplate,
    ChangeEvent,
)


class OpenFapSchemaGenerator(GenerateJsonSchema):
    """Gives generic ``Field[...]`` definitions readable names, e.g. ``Field_FPGYearRule``."""

    def normalize_name(self, name: str) -> str:
        for alias, alias_name in _ALIAS_NAMES.items():
            name = name.replace("Literal[" + ", ".join(repr(a) for a in typing.get_args(alias)) + "]", alias_name)
        name = re.sub(r"_+", "_", super().normalize_name(name)).strip("_")
        return name


def open_fap_schema() -> dict[str, Any]:
    """Build the Open FAP Schema document (JSON Schema 2020-12)."""
    _, top = models_json_schema(
        [(m, "validation") for m in PUBLISHED_MODELS],
        ref_template="#/$defs/{model}",
        schema_generator=OpenFapSchemaGenerator,
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Open FAP Schema",
        "description": (
            "Machine-readable, clause-cited hospital financial-assistance policies (IRS 501(r)) "
            "with verification status and IRS Schedule H cross-checks. CC BY 4.0."
        ),
        "version": SCHEMA_VERSION,
        "$ref": "#/$defs/CompiledPolicy",
        "$defs": top["$defs"],
    }


def export_json_schema(path: str | Path | None = None) -> Path:
    """Write the Open FAP Schema to ``schemas/open-fap-schema.json`` (or ``path``)."""
    from charitycare.config import get_settings

    out = Path(path) if path else get_settings().schemas_dir / "open-fap-schema.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(open_fap_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


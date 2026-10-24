"""Specifications of the fictional Texas hospitals in the synthetic demo corpus.

Every name, EIN, CCN, address, phone number and domain here is fictional:
CCNs use the unassigned ``4599xx`` range, EINs the ``99-`` prefix, and domains
the reserved ``.example`` TLD. Each spec is the single source of truth from
which the PDFs, the gold labels, the Schedule H XML, the CMS rows, the demo
website and the replay fixtures are all derived, so they stay consistent.

Coverage of the variation the pipeline must handle:

========================  ==========================================================
hospital                  what it exercises
========================  ==========================================================
example-methodist         prints its own 2025 income table (round-trip match); asset
                          test the Schedule H omits (``disagree_unexplained``);
                          residency in 6 counties; fillable AcroForm application
lone-star-health          multi-facility system policy + facility addendum; Schedule
                          H group reporting (group A, 3 facilities); flat form
valley-childrens          Spanish application (flat) beside an English fillable one;
                          statewide residency; Spanish plain-language summary
pineywoods-regional       scanned-style: an image-only FAP page and an image-only
                          application; policy revised after the filing period
                          (``disagree_policy_newer_than_filing``)
gulf-coast-baptist        insured / catastrophic clause, Medicaid-first rule, a
                          discretionary "may" presumptive clause (abstain)
hill-country-community    critical access hospital, $5,000 asset test, no refund
                          clause (abstain: not addressed)
panhandle-university      sliding scale to 500%; printed table labelled 2026 but
                          computed from 2025 guidelines (round-trip mismatch)
brazos-valley             365-day window, presumptive with credit data, Schedule H
                          FAP URL given only in Part V Section C narrative
========================  ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal


@dataclass(frozen=True)
class FacilitySpec:
    ccn: str
    name: str
    address: str
    city: str
    county: str
    zip: str
    license_no: str
    cms_type: str = "Acute Care Hospitals"


@dataclass(frozen=True)
class Tier:
    lo: float | None
    hi: float
    benefit: Literal["free", "percent_discount", "agb_cap", "sliding_scale"]
    discount_pct: float | None = None


@dataclass(frozen=True)
class Doc:
    kind: Literal["pay_stub", "tax_return", "w2", "bank_statement", "photo_id", "proof_of_residency",
                  "medicaid_denial", "unemployment_benefits", "social_security_statement", "letter_of_support", "other"]
    count: int | None
    period_months: int | None
    text: str  # how the policy phrases it (also the RequiredDoc.description)


@dataclass(frozen=True)
class ScheduleHSpec:
    object_id: str
    tax_period_begin: date
    tax_period_end: date
    free_pct: float
    discount_pct: float
    asset: bool
    insurance: bool
    residency: bool
    underinsurance: bool = False
    medical_indigency: bool = False
    translated: bool = False
    ecas: tuple[str, ...] = ("none",)
    agb: str = "lookback_medicare_private"
    fap_url_in_narrative: bool = False
    pct_format: str = "{:g}"  # how the percentage is written in the XML
    bool_true: str = "true"


@dataclass(frozen=True)
class FormSpec:
    language: Literal["en", "es"]
    kind: Literal["fillable", "flat"]
    scanned: bool = False
    widget_style: Literal["spaced", "camel", "prefixed"] = "spaced"


@dataclass(frozen=True)
class HospitalSpec:
    id: str
    name: str
    system_name: str | None
    ein: str
    domain: str
    phone: str
    fax: str
    mail_address: str
    ownership: str
    facilities: tuple[FacilitySpec, ...]
    policy_title: str
    effective_date: date
    tiers: tuple[Tier, ...]
    fpg_year_rule: Literal["current_at_application", "current_at_service", "stated_year", "unspecified"]
    income_definition: Literal["gross", "net", "adjusted_gross", "unspecified"]
    household_text: str
    asset_threshold: float | None
    asset_exclusions: tuple[str, ...]
    residency_counties: tuple[str, ...]  # empty + residency_state => statewide
    residency_state: bool
    emergency_exempt: bool
    insured_eligible: bool
    catastrophic_pct: float | None
    medicaid_first: bool
    excluded_services: tuple[str, ...]
    provider_list_location: str
    agb_method: Literal["lookback_medicare", "lookback_medicare_private", "lookback_medicaid", "prospective"]
    agb_pct: float
    window_days: int
    required_docs: tuple[Doc, ...]
    channels: tuple[tuple[str, str], ...]  # (kind, detail)
    presumptive: Literal["none", "allowed", "may"]
    presumptive_criteria: tuple[str, ...]
    presumptive_credit_data: bool | None
    screening_timing: Literal["before_billing", "after_billing_before_collections", "before_writeoff", "unspecified"]
    refund_text: str | None
    determination_notice: bool
    award_months: int
    schedule_h: ScheduleHSpec
    forms: tuple[FormSpec, ...]
    income_table: tuple[int, int] | None = None  # (stated_year, computed_from_year)
    image_only_fap_pages: tuple[int, ...] = ()
    addendum: bool = False
    pls_languages: tuple[str, ...] = ("en",)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def primary(self) -> FacilitySpec:
        return self.facilities[0]

    @property
    def base_url(self) -> str:
        return f"https://www.{self.domain}"


SHARED_EXCLUSIONS = ("primary residence", "one vehicle per adult", "retirement accounts")

HOSPITALS: tuple[HospitalSpec, ...] = (
    HospitalSpec(
        id="example-methodist",
        name="Example Methodist Hospital",
        system_name=None,
        ein="990100101",
        domain="examplemethodist.example",
        phone="(210) 555-0100",
        fax="(210) 555-0199",
        mail_address="Financial Assistance Office, 7700 Medical Example Drive, San Antonio, TX 78229",
        ownership="Voluntary non-profit - Church",
        facilities=(
            FacilitySpec("459901", "Example Methodist Hospital", "7700 Medical Example Drive", "San Antonio", "Bexar", "78229", "000101"),
        ),
        policy_title="Financial Assistance Policy",
        effective_date=date(2025, 7, 1),
        tiers=(Tier(None, 200, "free"), Tier(200, 400, "agb_cap")),
        fpg_year_rule="current_at_application",
        income_definition="gross",
        household_text="the patient, the patient's spouse, and any dependents claimed on the household's federal tax return",
        asset_threshold=10_000,
        asset_exclusions=SHARED_EXCLUSIONS,
        residency_counties=("Bexar", "Atascosa", "Comal", "Guadalupe", "Kendall", "Medina"),
        residency_state=False,
        emergency_exempt=True,
        insured_eligible=True,
        catastrophic_pct=None,
        medicaid_first=False,
        excluded_services=("cosmetic procedures", "retail pharmacy purchases"),
        provider_list_location="Appendix B, Covered and Non-Covered Providers, posted at https://www.examplemethodist.example/fap/providers",
        agb_method="lookback_medicare_private",
        agb_pct=31.5,
        window_days=240,
        required_docs=(
            Doc("pay_stub", 2, None, "the two most recent pay stubs for each working household member"),
            Doc("bank_statement", 2, 2, "two months of bank statements for all checking and savings accounts"),
            Doc("photo_id", 1, None, "a government-issued photo identification"),
            Doc("medicaid_denial", 1, None, "a Medicaid denial letter, if one has been issued"),
        ),
        channels=(
            ("mail", "Financial Assistance Office, 7700 Medical Example Drive, San Antonio, TX 78229"),
            ("fax", "(210) 555-0199"),
            ("in_person", "Patient Financial Services, first floor, Monday through Friday, 8 a.m. to 5 p.m."),
        ),
        presumptive="none",
        presumptive_criteria=(),
        presumptive_credit_data=None,
        screening_timing="after_billing_before_collections",
        refund_text="If a patient is approved for financial assistance, the Hospital will refund any amount the patient paid that exceeds the amount the patient owes under this Policy, if the excess is $5 or more.",
        determination_notice=True,
        award_months=6,
        schedule_h=ScheduleHSpec(
            object_id="202601999349300101",
            tax_period_begin=date(2025, 1, 1),
            tax_period_end=date(2025, 12, 31),
            free_pct=200, discount_pct=400, asset=False, insurance=True, residency=True,
            translated=True, ecas=("credit_report",),
        ),
        forms=(FormSpec("en", "fillable", widget_style="spaced"),),
        income_table=(2025, 2025),
        notes=("Asset test in a policy effective 2025-07-01, but not checked on the calendar-2025 Schedule H: expected disagree_unexplained.",),
    ),
    HospitalSpec(
        id="lone-star-health",
        name="Lone Star Example Health System",
        system_name="Lone Star Example Health System",
        ein="990100202",
        domain="lonestarexamplehealth.example",
        phone="(214) 555-0200",
        fax="(214) 555-0299",
        mail_address="Lone Star Example Health System, Financial Assistance, P.O. Box 55020, Dallas, TX 75201",
        ownership="Voluntary non-profit - Private",
        facilities=(
            FacilitySpec("459902", "Lone Star Example Medical Center Dallas", "1200 Example Parkway", "Dallas", "Dallas", "75201", "000202"),
            FacilitySpec("459903", "Lone Star Example Medical Center Plano", "4500 Example Ridge Road", "Plano", "Collin", "75093", "000203"),
            FacilitySpec("459904", "Lone Star Example Medical Center Mesquite", "800 Example Town East Blvd", "Mesquite", "Dallas", "75150", "000204"),
        ),
        policy_title="System Policy FIN-201: Financial Assistance",
        effective_date=date(2025, 9, 1),
        tiers=(
            Tier(None, 250, "free"),
            Tier(250, 300, "percent_discount", 75),
            Tier(300, 350, "percent_discount", 50),
            Tier(350, 400, "percent_discount", 25),
        ),
        fpg_year_rule="current_at_application",
        income_definition="gross",
        household_text="all persons living in the same home who are related by birth, marriage or adoption",
        asset_threshold=None,
        asset_exclusions=(),
        residency_counties=(),
        residency_state=False,
        emergency_exempt=False,
        insured_eligible=True,
        catastrophic_pct=None,
        medicaid_first=True,
        excluded_services=("elective cosmetic surgery", "bariatric surgery", "services billed by independent physician groups"),
        provider_list_location="Addendum B to this Policy, available at https://www.lonestarexamplehealth.example/financial-assistance/providers",
        agb_method="lookback_medicare_private",
        agb_pct=28.0,
        window_days=240,
        required_docs=(
            Doc("tax_return", 1, None, "a copy of the most recent federal income tax return"),
            Doc("pay_stub", 3, None, "the three most recent pay stubs"),
            Doc("medicaid_denial", 1, None, "a Medicaid eligibility decision from Texas Health and Human Services"),
        ),
        channels=(
            ("mail", "Lone Star Example Health System, Financial Assistance, P.O. Box 55020, Dallas, TX 75201"),
            ("portal", "https://pay.lonestarexamplehealth.example/assistance"),
            ("email", "assistance@lonestarexamplehealth.example"),
        ),
        presumptive="allowed",
        presumptive_criteria=("enrolled in SNAP", "enrolled in WIC", "experiencing homelessness"),
        presumptive_credit_data=False,
        screening_timing="before_writeoff",
        refund_text="Payments made by an approved patient for eligible care in excess of the discounted amount will be refunded within 60 days of the determination.",
        determination_notice=True,
        award_months=12,
        schedule_h=ScheduleHSpec(
            object_id="202601999349300202",
            tax_period_begin=date(2024, 1, 1),
            tax_period_end=date(2024, 12, 31),
            free_pct=250, discount_pct=400, asset=False, insurance=True, residency=False,
            translated=True, ecas=("credit_report", "sell_debt"),
        ),
        forms=(FormSpec("en", "flat"),),
        addendum=True,
        notes=("System policy with Addendum A listing three facilities; Schedule H reports them as group A.",),
    ),
    HospitalSpec(
        id="valley-childrens",
        name="Example Valley Children's Hospital",
        system_name=None,
        ein="990100303",
        domain="examplevalleychildrens.example",
        phone="(956) 555-0300",
        fax="(956) 555-0399",
        mail_address="Patient Financial Services, 2100 Example Children's Way, McAllen, TX 78503",
        ownership="Voluntary non-profit - Private",
        facilities=(
            FacilitySpec("459905", "Example Valley Children's Hospital", "2100 Example Children's Way", "McAllen", "Hidalgo", "78503", "000303", "Childrens"),
        ),
        policy_title="Financial Assistance Policy (Charity Care)",
        effective_date=date(2025, 7, 1),
        tiers=(Tier(None, 300, "free"), Tier(300, 400, "agb_cap")),
        fpg_year_rule="current_at_service",
        income_definition="gross",
        household_text="the patient's parents or legal guardians and all children under 18 living in the same home",
        asset_threshold=None,
        asset_exclusions=(),
        residency_counties=(),
        residency_state=True,
        emergency_exempt=True,
        insured_eligible=True,
        catastrophic_pct=None,
        medicaid_first=False,
        excluded_services=("non-medically necessary services",),
        provider_list_location="Attachment 1 of this Policy",
        agb_method="lookback_medicaid",
        agb_pct=24.0,
        window_days=240,
        required_docs=(
            Doc("pay_stub", 2, None, "pay stubs for the last 30 days for each parent or guardian"),
            Doc("proof_of_residency", 1, None, "proof of Texas residency such as a utility bill or lease"),
        ),
        channels=(
            ("mail", "Patient Financial Services, 2100 Example Children's Way, McAllen, TX 78503"),
            ("in_person", "Admitting Office, main lobby"),
            ("phone", "(956) 555-0300"),
        ),
        presumptive="allowed",
        presumptive_criteria=("enrolled in CHIP or Medicaid in the prior 12 months", "eligible for free school lunch"),
        presumptive_credit_data=False,
        screening_timing="before_billing",
        refund_text="Any payment above the amount owed after assistance is applied will be refunded to the guarantor.",
        determination_notice=True,
        award_months=12,
        schedule_h=ScheduleHSpec(
            object_id="202601999349300303",
            tax_period_begin=date(2024, 7, 1),
            tax_period_end=date(2025, 6, 30),
            free_pct=300, discount_pct=400, asset=False, insurance=True, residency=True,
            translated=True, agb="lookback_medicaid", bool_true="1",
        ),
        forms=(FormSpec("en", "fillable", widget_style="camel"), FormSpec("es", "flat")),
        pls_languages=("en", "es"),
    ),
    HospitalSpec(
        id="pineywoods-regional",
        name="Example Pineywoods Regional Medical Center",
        system_name=None,
        ein="990100404",
        domain="examplepineywoods.example",
        phone="(936) 555-0400",
        fax="(936) 555-0499",
        mail_address="Business Office, 1500 Example Timberland Drive, Lufkin, TX 75904",
        ownership="Voluntary non-profit - Other",
        facilities=(
            FacilitySpec("459906", "Example Pineywoods Regional Medical Center", "1500 Example Timberland Drive", "Lufkin", "Angelina", "75904", "000404"),
        ),
        policy_title="Charity Care and Discount Policy",
        effective_date=date(2026, 3, 15),
        tiers=(Tier(None, 150, "free"), Tier(150, 250, "percent_discount", 60)),
        fpg_year_rule="current_at_application",
        income_definition="gross",
        household_text="the patient and any person the patient is legally responsible for supporting",
        asset_threshold=None,
        asset_exclusions=(),
        residency_counties=("Angelina", "Nacogdoches", "Polk", "Trinity", "Houston"),
        residency_state=False,
        emergency_exempt=True,
        insured_eligible=False,
        catastrophic_pct=None,
        medicaid_first=True,
        excluded_services=("physician professional fees billed separately",),
        provider_list_location="the Provider List posted at https://www.examplepineywoods.example/billing/providers",
        agb_method="lookback_medicare",
        agb_pct=33.0,
        window_days=240,
        required_docs=(
            Doc("w2", 1, None, "W-2 forms for the most recent tax year"),
            Doc("pay_stub", 4, None, "four weeks of pay stubs"),
            Doc("photo_id", 1, None, "photo identification"),
        ),
        channels=(
            ("mail", "Business Office, 1500 Example Timberland Drive, Lufkin, TX 75904"),
            ("fax", "(936) 555-0499"),
        ),
        presumptive="none",
        presumptive_criteria=(),
        presumptive_credit_data=None,
        screening_timing="unspecified",
        refund_text="Amounts paid in excess of the approved patient balance will be refunded.",
        determination_notice=True,
        award_months=6,
        schedule_h=ScheduleHSpec(
            object_id="202601999349300404",
            tax_period_begin=date(2024, 7, 1),
            tax_period_end=date(2025, 6, 30),
            free_pct=125, discount_pct=250, asset=False, insurance=True, residency=True,
            ecas=("none",), agb="lookback_medicare",
        ),
        forms=(FormSpec("en", "flat", scanned=True),),
        image_only_fap_pages=(3,),
        notes=(
            "FAP page 3 (eligibility) is image-only: without OCR its quotes cannot verify and must abstain as illegible.",
            "Policy revised 2026-03-15 raised free care from 125% to 150%; FY ending 2025-06-30 Schedule H reports 125%: expected disagree_policy_newer_than_filing.",
        ),
    ),
    HospitalSpec(
        id="gulf-coast-baptist",
        name="Example Gulf Coast Baptist Hospital",
        system_name=None,
        ein="990100505",
        domain="examplegulfcoastbaptist.example",
        phone="(409) 555-0500",
        fax="(409) 555-0599",
        mail_address="Financial Counseling, 3080 Example College Street, Beaumont, TX 77701",
        ownership="Voluntary non-profit - Church",
        facilities=(
            FacilitySpec("459907", "Example Gulf Coast Baptist Hospital", "3080 Example College Street", "Beaumont", "Jefferson", "77701", "000505"),
        ),
        policy_title="Patient Financial Assistance Policy",
        effective_date=date(2025, 1, 1),
        tiers=(Tier(None, 200, "free"), Tier(200, 300, "percent_discount", 50)),
        fpg_year_rule="current_at_service",
        income_definition="gross",
        household_text="the patient, spouse or domestic partner, and dependent children",
        asset_threshold=None,
        asset_exclusions=(),
        residency_counties=(),
        residency_state=False,
        emergency_exempt=False,
        insured_eligible=True,
        catastrophic_pct=20,
        medicaid_first=True,
        excluded_services=("services covered by a third-party liability settlement",),
        provider_list_location="Exhibit C, Provider Listing",
        agb_method="lookback_medicare_private",
        agb_pct=30.2,
        window_days=240,
        required_docs=(
            Doc("pay_stub", 2, None, "the last two pay stubs"),
            Doc("tax_return", 1, None, "the most recent federal tax return"),
            Doc("letter_of_support", 1, None, "a letter of support if the household reports no income"),
        ),
        channels=(
            ("mail", "Financial Counseling, 3080 Example College Street, Beaumont, TX 77701"),
            ("in_person", "Financial Counseling office, Monday through Friday"),
        ),
        presumptive="may",
        presumptive_criteria=("homelessness", "deceased with no estate"),
        presumptive_credit_data=None,
        screening_timing="after_billing_before_collections",
        refund_text="The Hospital will refund payments that exceed the patient's responsibility after assistance is applied.",
        determination_notice=True,
        award_months=12,
        schedule_h=ScheduleHSpec(
            object_id="202601999349300505",
            tax_period_begin=date(2024, 1, 1),
            tax_period_end=date(2024, 12, 31),
            free_pct=200, discount_pct=300, asset=False, insurance=True, residency=False,
            medical_indigency=True, underinsurance=True, ecas=("legal_process",), pct_format="{:.2f}",
        ),
        forms=(FormSpec("en", "fillable", widget_style="prefixed"),),
        notes=("Presumptive eligibility clause is discretionary ('may'): gold abstains with ambiguous_may_clause.",),
    ),
    HospitalSpec(
        id="hill-country-community",
        name="Example Hill Country Community Hospital",
        system_name=None,
        ein="990100606",
        domain="examplehillcountry.example",
        phone="(830) 555-0600",
        fax="(830) 555-0699",
        mail_address="Business Office, 1020 Example Main Street, Fredericksburg, TX 78624",
        ownership="Voluntary non-profit - Other",
        facilities=(
            FacilitySpec("459908", "Example Hill Country Community Hospital", "1020 Example Main Street", "Fredericksburg", "Gillespie", "78624", "000606", "Critical Access Hospitals"),
        ),
        policy_title="Financial Assistance and Uninsured Discount Policy",
        effective_date=date(2024, 10, 1),
        tiers=(Tier(None, 200, "free"), Tier(200, 300, "percent_discount", 50)),
        fpg_year_rule="unspecified",
        income_definition="net",
        household_text="the patient and all family members who live with the patient",
        asset_threshold=5_000,
        asset_exclusions=("primary residence", "household furnishings"),
        residency_counties=("Gillespie", "Kerr", "Kimble", "Llano", "Blanco"),
        residency_state=False,
        emergency_exempt=True,
        insured_eligible=False,
        catastrophic_pct=None,
        medicaid_first=False,
        excluded_services=(),
        provider_list_location="the list of providers available at the Business Office",
        agb_method="prospective",
        agb_pct=35.0,
        window_days=240,
        required_docs=(
            Doc("tax_return", 1, None, "the most recent tax return"),
            Doc("bank_statement", 3, 3, "the last three months of bank statements"),
        ),
        channels=(
            ("mail", "Business Office, 1020 Example Main Street, Fredericksburg, TX 78624"),
            ("in_person", "Business Office"),
        ),
        presumptive="none",
        presumptive_criteria=(),
        presumptive_credit_data=None,
        screening_timing="unspecified",
        refund_text=None,
        determination_notice=False,
        award_months=6,
        schedule_h=ScheduleHSpec(
            object_id="202601999349300606",
            tax_period_begin=date(2024, 10, 1),
            tax_period_end=date(2025, 9, 30),
            free_pct=200, discount_pct=300, asset=True, insurance=True, residency=True,
            agb="prospective", bool_true="1",
        ),
        forms=(FormSpec("en", "flat"),),
        notes=("No refund clause: gold abstains with not_addressed.",),
    ),
    HospitalSpec(
        id="panhandle-university",
        name="Example Panhandle University Medical Center",
        system_name=None,
        ein="990100707",
        domain="examplepanhandleumc.example",
        phone="(806) 555-0700",
        fax="(806) 555-0799",
        mail_address="Financial Assistance Program, 1600 Example Wallace Blvd, Amarillo, TX 79106",
        ownership="Voluntary non-profit - Private",
        facilities=(
            FacilitySpec("459909", "Example Panhandle University Medical Center", "1600 Example Wallace Blvd", "Amarillo", "Potter", "79106", "000707"),
        ),
        policy_title="Financial Assistance Program Policy",
        effective_date=date(2026, 2, 1),
        tiers=(
            Tier(None, 200, "free"),
            Tier(200, 300, "sliding_scale", 80),
            Tier(300, 400, "sliding_scale", 60),
            Tier(400, 500, "sliding_scale", 40),
        ),
        fpg_year_rule="stated_year",
        income_definition="gross",
        household_text="the patient and all individuals listed on the patient's most recent federal tax return",
        asset_threshold=None,
        asset_exclusions=(),
        residency_counties=(),
        residency_state=True,
        emergency_exempt=True,
        insured_eligible=True,
        catastrophic_pct=None,
        medicaid_first=False,
        excluded_services=("investigational treatments",),
        provider_list_location="Appendix A of this Policy",
        agb_method="lookback_medicare_private",
        agb_pct=27.4,
        window_days=240,
        required_docs=(
            Doc("tax_return", 1, None, "the prior year federal tax return"),
            Doc("pay_stub", 2, None, "two recent pay stubs"),
            Doc("social_security_statement", 1, None, "Social Security benefit statements, if applicable"),
        ),
        channels=(
            ("mail", "Financial Assistance Program, 1600 Example Wallace Blvd, Amarillo, TX 79106"),
            ("fax", "(806) 555-0799"),
            ("portal", "https://mychart.examplepanhandleumc.example/assistance"),
        ),
        presumptive="none",
        presumptive_criteria=(),
        presumptive_credit_data=None,
        screening_timing="before_billing",
        refund_text="Overpayments by approved patients will be refunded within 30 days.",
        determination_notice=True,
        award_months=12,
        schedule_h=ScheduleHSpec(
            object_id="202601999349300707",
            tax_period_begin=date(2024, 7, 1),
            tax_period_end=date(2025, 6, 30),
            free_pct=200, discount_pct=500, asset=False, insurance=True, residency=True,
            ecas=("credit_report",),
        ),
        forms=(FormSpec("en", "fillable", widget_style="spaced"),),
        income_table=(2026, 2025),
        notes=("Printed income table says 2026 but uses 2025 guidelines: round-trip test must flag a mismatch.",),
    ),
    HospitalSpec(
        id="brazos-valley",
        name="Example Brazos Valley Medical Center",
        system_name=None,
        ein="990100808",
        domain="examplebrazosvalley.example",
        phone="(979) 555-0800",
        fax="(979) 555-0899",
        mail_address="Financial Assistance, 2900 Example Briarcrest Drive, Bryan, TX 77802",
        ownership="Voluntary non-profit - Private",
        facilities=(
            FacilitySpec("459910", "Example Brazos Valley Medical Center", "2900 Example Briarcrest Drive", "Bryan", "Brazos", "77802", "000808"),
        ),
        policy_title="Financial Assistance Policy",
        effective_date=date(2025, 4, 1),
        tiers=(Tier(None, 200, "free"), Tier(200, 400, "agb_cap")),
        fpg_year_rule="current_at_application",
        income_definition="gross",
        household_text="the patient and all persons for whom the patient or the patient's spouse is financially responsible",
        asset_threshold=None,
        asset_exclusions=(),
        residency_counties=("Brazos", "Burleson", "Grimes", "Madison", "Robertson", "Washington"),
        residency_state=False,
        emergency_exempt=True,
        insured_eligible=True,
        catastrophic_pct=None,
        medicaid_first=False,
        excluded_services=("cosmetic services", "fertility treatment"),
        provider_list_location="https://www.examplebrazosvalley.example/fap/provider-list",
        agb_method="lookback_medicare_private",
        agb_pct=29.9,
        window_days=365,
        required_docs=(
            Doc("pay_stub", 2, None, "two recent pay stubs"),
            Doc("photo_id", 1, None, "a photo identification card"),
        ),
        channels=(
            ("mail", "Financial Assistance, 2900 Example Briarcrest Drive, Bryan, TX 77802"),
            ("email", "fap@examplebrazosvalley.example"),
            ("portal", "https://www.examplebrazosvalley.example/fap/apply"),
        ),
        presumptive="allowed",
        presumptive_criteria=("predictive scoring using third-party credit data",),
        presumptive_credit_data=True,
        screening_timing="before_billing",
        refund_text="If a patient qualifies after paying, the Hospital refunds amounts paid above the amount owed.",
        determination_notice=True,
        award_months=6,
        schedule_h=ScheduleHSpec(
            object_id="202601999349300808",
            tax_period_begin=date(2024, 4, 1),
            tax_period_end=date(2025, 3, 31),
            free_pct=200, discount_pct=400, asset=False, insurance=True, residency=True,
            ecas=("none",), fap_url_in_narrative=True,
        ),
        forms=(FormSpec("en", "fillable", widget_style="camel"),),
        notes=("Schedule H line 16a URL text is 'SEE PART V, SECTION C'; the URL is in the narrative.",),
    ),
)


def get(hospital_id: str) -> HospitalSpec:
    for h in HOSPITALS:
        if h.id == hospital_id:
            return h
    raise KeyError(hospital_id)

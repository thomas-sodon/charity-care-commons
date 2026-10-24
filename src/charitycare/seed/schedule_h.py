"""IRS Form 990 Schedule H Part V parser with pinned xpaths (design section 8, step S1).

Element names follow the IRSx variable catalogue for the e-file schema
(namespace ``http://www.irs.gov/efile``). Every value records the xpath it came
from in ``ScheduleHFacility.xpaths`` so a cross-check can cite
"object_id + line + xpath".

Conventions handled, as they appear in real filings:

* ``CheckboxType`` lines (``X``) are omitted when unchecked: inside a present
  Section B block an absent checkbox reads as ``False``.
* ``BooleanType`` lines are ``true``/``1``/``false``/``0``; absent means unknown (``None``).
* Percentages may be written ``200`` or ``200.00``.
* Facility reporting groups: a Section B block carrying ``FacilityReportingGroupCd``
  covers every Section A facility (``HospitalFacilitiesGrp``) with the same letter.
* Line 16a-c sometimes says "SEE PART V, SECTION C" instead of a URL; the
  URLs are then recovered from the Section C / supplemental narrative text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import get_args

from lxml import etree

from charitycare.schemas import AGBMethod, ECAKind, Organization, ScheduleHFacility

NS = "http://www.irs.gov/efile"
SCHED = "/Return/ReturnData/IRS990ScheduleH"
SECTION_B = f"{SCHED}/HospitalFcltyPoliciesPrctcGrp"
SECTION_A = f"{SCHED}/HospitalFacilitiesGrp"
SUPPLEMENTAL = f"{SCHED}/SupplementalInformationGrp"

# Header xpaths (absolute).
HEADER_XPATHS: dict[str, str] = {
    "ein": "/Return/ReturnHeader/Filer/EIN",
    "org_name": "/Return/ReturnHeader/Filer/BusinessName/BusinessNameLine1Txt",
    "org_name_2": "/Return/ReturnHeader/Filer/BusinessName/BusinessNameLine2Txt",
    "state": "/Return/ReturnHeader/Filer/USAddress/StateAbbreviationCd",
    "tax_period_end": "/Return/ReturnHeader/TaxPeriodEndDt",
    "tax_period_begin": "/Return/ReturnHeader/TaxPeriodBeginDt",
    "tax_year": "/Return/ReturnHeader/TaxYr",
    "website": "/Return/ReturnData/IRS990/WebsiteAddressTxt",
}

# Section B field -> (relative element name, kind). kind: "checkbox" | "bool" | "pct" | "text".
LINE_XPATHS: dict[str, tuple[str, str]] = {
    "line_13_criteria_explained": ("EligCriteriaExplainedInd", "bool"),
    "line_13a_fpg_used": ("FPGFamilyIncmLmtFreeDscntInd", "checkbox"),
    "line_13a_free_pct": ("FPGFamilyIncmLmtFreeCarePct", "pct"),
    "line_13a_discount_pct": ("FPGFamilyIncmLmtDscntCarePct", "pct"),
    "line_13b_income_level": ("IncomeLevelCriteriaInd", "checkbox"),
    "line_13c_asset_level": ("AssetLevelCriteriaInd", "checkbox"),
    "line_13d_medical_indigency": ("MedicalIndigencyCriteriaInd", "checkbox"),
    "line_13e_insurance_status": ("InsuranceStatusCriteriaInd", "checkbox"),
    "line_13f_underinsurance_status": ("UnderinsuranceStatCriteriaInd", "checkbox"),
    "line_13g_residency": ("ResidencyCriteriaInd", "checkbox"),
    "line_13h_other": ("OtherCriteriaInd", "checkbox"),
    "line_15a_described_info": ("DescribedInfoInd", "checkbox"),
    "line_15b_described_docs": ("DescribedSuprtDocInd", "checkbox"),
    "line_16a_fap_url": ("FAPAvailableOnWebsiteURLTxt", "text"),
    "line_16b_app_url": ("FAPAppAvailableOnWebsiteURLTxt", "text"),
    "line_16c_pls_url": ("FAPSummaryOnWebsiteURLTxt", "text"),
    "line_16i_translated": ("FAPTranslatedInd", "checkbox"),
}

ECA_XPATHS: dict[ECAKind, str] = {
    "credit_report": "PermitReportToCreditAgencyInd",
    "sell_debt": "PermitSellingDebtInd",
    "defer_deny_care": "PermitDeferDenyRqrPaymentInd",
    "legal_process": "PermitLegalJudicialProcessInd",
    "other": "PermitOtherActionsInd",
    "none": "PermitNoActionsInd",
}

AGB_XPATHS: dict[AGBMethod, str] = {
    "lookback_medicare": "LookBackMedicareInd",
    "lookback_medicare_private": "LookBackMedicarePrivateInd",
    "lookback_medicaid": "LookBackMedicaidMedcrPrvtInd",
    "prospective": "ProspectiveMedicareMedicaidInd",
}
assert set(AGB_XPATHS) == set(get_args(AGBMethod))

URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'(),;]+|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:org|com|net|edu|gov|us|health|example)/[^\s<>\"'(),;]*",
    re.IGNORECASE,
)
_TRUE = {"x", "true", "1"}
_FALSE = {"false", "0"}


@dataclass(frozen=True)
class ScheduleHHospital:
    """One Section A row (``HospitalFacilitiesGrp``): a licensed hospital facility of the filer."""

    facility_num: int
    name: str
    address: str | None
    city: str | None
    state: str | None
    zip: str | None
    license_no: str | None
    group_letter: str | None
    website: str | None


@dataclass
class ParsedReturn:
    """Everything the seed stage needs from one return."""

    object_id: str
    organization: Organization
    state: str | None
    website: str | None
    hospitals: list[ScheduleHHospital] = field(default_factory=list)
    sections: list[ScheduleHFacility] = field(default_factory=list)


class ScheduleHParseError(ValueError):
    """The document is not a parseable Form 990 return with Schedule H."""


# --------------------------------------------------------------------------
# XPath helpers (namespace-aware, with a fallback for un-namespaced documents)
# --------------------------------------------------------------------------


class _X:
    def __init__(self, root: etree._Element):
        self.ns = etree.QName(root).namespace
        self.nsmap = {"e": self.ns} if self.ns else {}

    def q(self, path: str) -> str:
        """Prefix every step of an IRSx-style path with the e-file namespace."""
        if not self.ns:
            return path
        steps = path.split("/")
        return "/".join(s if (not s or s in {".", ".."}) else f"e:{s}" for s in steps)

    def all(self, el: etree._Element, path: str) -> list[etree._Element]:
        """Elements at ``path``: absolute (from the document root) or relative to ``el``."""
        return el.xpath(self.q(path), namespaces=self.nsmap)  # type: ignore[no-any-return]

    def text(self, el: etree._Element, path: str) -> str | None:
        found = self.all(el, path)
        if not found:
            return None
        t = (found[0].text or "").strip()
        return t or None


def _bool(raw: str | None, kind: str) -> bool | None:
    if raw is None:
        return False if kind == "checkbox" else None
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def _pct(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.replace("%", "").strip())
    except ValueError:
        return None


def _date(raw: str | None) -> date | None:
    return date.fromisoformat(raw[:10]) if raw else None


def looks_like_url(text: str | None) -> bool:
    return bool(text) and bool(URL_RE.fullmatch(text.strip()))  # type: ignore[union-attr]


def normalize_url(raw: str) -> str:
    """Clean a URL as written in a filing: strip trailing punctuation, add a scheme."""
    u = raw.strip().rstrip(".,;:)]")
    if not re.match(r"https?://", u, re.IGNORECASE):
        u = "https://" + u
    scheme, _, rest = u.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


def extract_urls(texts: Iterable[str]) -> list[str]:
    """All URLs mentioned in narrative text, normalized and de-duplicated in order."""
    seen: dict[str, None] = {}
    for t in texts:
        for m in URL_RE.finditer(t):
            seen.setdefault(normalize_url(m.group(0)), None)
    return list(seen)


def section_id(object_id: str, group_letter: str | None, facility_num: int | None) -> str:
    return f"{object_id}:{group_letter or facility_num or 1}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_return(xml: bytes, object_id: str | None = None) -> ParsedReturn:
    """Parse one e-file return into its organization, Section A hospitals and Section B blocks."""
    try:
        root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True))
    except etree.XMLSyntaxError as exc:
        raise ScheduleHParseError(f"invalid XML: {exc}") from exc
    x = _X(root)
    if etree.QName(root).localname != "Return":
        raise ScheduleHParseError("root element is not <Return>")
    if not x.all(root, SCHED):
        raise ScheduleHParseError("return has no IRS990ScheduleH")

    ein = x.text(root, HEADER_XPATHS["ein"]) or ""
    name = " ".join(t for t in (x.text(root, HEADER_XPATHS["org_name"]), x.text(root, HEADER_XPATHS["org_name_2"])) if t)
    end = _date(x.text(root, HEADER_XPATHS["tax_period_end"]))
    if not ein or end is None:
        raise ScheduleHParseError("return header lacks EIN or TaxPeriodEndDt")
    begin = _date(x.text(root, HEADER_XPATHS["tax_period_begin"]))
    tax_year_raw = x.text(root, HEADER_XPATHS["tax_year"])
    tax_year = int(tax_year_raw) if tax_year_raw and tax_year_raw.isdigit() else (begin or end).year
    oid = object_id or ""
    website = x.text(root, HEADER_XPATHS["website"])

    hospitals = [_hospital(x, el) for el in x.all(root, SECTION_A)]
    entries = [(x.text(g, "FormAndLineReferenceDesc") or "", x.text(g, "ExplanationTxt") or "") for g in x.all(root, SUPPLEMENTAL)]
    narrative = [t for _, t in entries if t]
    # Prefer narrative entries that reference line 16 (the website lines); else any narrative URL.
    line16 = [t for ref, t in entries if t and re.search(r"\b16\s*[A-C]?\b", ref, re.IGNORECASE)]
    narrative_urls = extract_urls(line16) or extract_urls(narrative)

    sections = [
        _section(x, el, i, ein=ein, org_name=name, object_id=oid, tax_year=tax_year, begin=begin, end=end,
                 hospitals=hospitals, narrative=narrative, narrative_urls=narrative_urls)
        for i, el in enumerate(x.all(root, SECTION_B))
    ]
    return ParsedReturn(
        object_id=oid,
        organization=Organization(ein=ein, name=name, system_name=name if len(hospitals) > 1 else None),
        state=x.text(root, HEADER_XPATHS["state"]),
        website=website,
        hospitals=hospitals,
        sections=sections,
    )


def _hospital(x: _X, el: etree._Element) -> ScheduleHHospital:
    num = x.text(el, "FacilityNum")
    return ScheduleHHospital(
        facility_num=int(num) if num and num.isdigit() else 0,
        name=" ".join(t for t in (x.text(el, "BusinessName/BusinessNameLine1Txt"), x.text(el, "BusinessName/BusinessNameLine2Txt")) if t),
        address=x.text(el, "USAddress/AddressLine1Txt"),
        city=x.text(el, "USAddress/CityNm"),
        state=x.text(el, "USAddress/StateAbbreviationCd"),
        zip=(x.text(el, "USAddress/ZIPCd") or "")[:5] or None,
        license_no=x.text(el, "StateLicenseNum"),
        group_letter=x.text(el, "FacilityReportingGroupCd"),
        website=x.text(el, "WebsiteAddressTxt"),
    )


def _section(
    x: _X,
    el: etree._Element,
    index: int,
    *,
    ein: str,
    org_name: str,
    object_id: str,
    tax_year: int,
    begin: date | None,
    end: date,
    hospitals: list[ScheduleHHospital],
    narrative: list[str],
    narrative_urls: list[str],
) -> ScheduleHFacility:
    base = f"{SECTION_B}[{index + 1}]"
    xpaths: dict[str, str] = {}
    group = x.text(el, "FacilityReportingGroupCd")
    num_raw = x.text(el, "FacilityNum")
    if group:
        members = [h for h in hospitals if h.group_letter == group]
        xpaths["group_letter"] = f"{base}/FacilityReportingGroupCd"
    else:
        num = int(num_raw) if num_raw and num_raw.isdigit() else None
        members = [h for h in hospitals if num is not None and h.facility_num == num]
        if not members and len(hospitals) == 1:
            members = hospitals
    own_name = x.text(el, "HospitalFacilityName/BusinessNameLine1Txt")
    names = [h.name for h in members] or ([own_name] if own_name else [])
    numbers = [h.facility_num for h in members] or ([int(num_raw)] if num_raw and num_raw.isdigit() else [])

    values: dict[str, object] = {}
    for key, (tag, kind) in LINE_XPATHS.items():
        raw = x.text(el, tag)
        if raw is not None or kind == "checkbox":
            xpaths[key] = f"{base}/{tag}"
        if kind == "pct":
            values[key] = _pct(raw)
        elif kind == "text":
            values[key] = raw
        else:
            values[key] = _bool(raw, kind)

    recovered: list[str] = []
    for key in ("line_16a_fap_url", "line_16b_app_url", "line_16c_pls_url"):
        raw = values[key]
        if raw is None:
            continue
        if looks_like_url(raw):  # type: ignore[arg-type]
            values[key] = normalize_url(raw)  # type: ignore[arg-type]
        else:
            values[key] = None  # e.g. "SEE PART V, SECTION C": the URL lives in the narrative
            recovered = narrative_urls
            xpaths[f"{key}_narrative"] = f"{SUPPLEMENTAL}/ExplanationTxt"

    ecas = [kind for kind, tag in ECA_XPATHS.items() if _bool(x.text(el, tag), "checkbox")]
    for kind in ecas:
        xpaths[f"line_18_{kind}"] = f"{base}/{ECA_XPATHS[kind]}"
    agb = next((m for m, tag in AGB_XPATHS.items() if _bool(x.text(el, tag), "checkbox")), None)
    if agb:
        xpaths["line_22_agb_method"] = f"{base}/{AGB_XPATHS[agb]}"

    return ScheduleHFacility(
        id=section_id(object_id, group, numbers[0] if numbers else None),
        ein=ein,
        org_name=org_name,
        object_id=object_id,
        tax_year=tax_year,
        tax_period_begin=begin,
        tax_period_end=end,
        group_letter=group,
        facility_numbers=numbers,
        facility_names=names,
        line_18_ecas_permitted=ecas,
        line_22_agb_method=agb,
        narrative_urls_recovered=recovered,
        section_c_text=narrative,
        xpaths=xpaths,
        **values,  # type: ignore[arg-type]
    )


def parse_file(path: str, object_id: str | None = None) -> ParsedReturn:
    """Parse ``{OBJECT_ID}_public.xml`` (the object id is taken from the file name when not given)."""
    from pathlib import Path

    p = Path(path)
    return parse_return(p.read_bytes(), object_id or p.name.split("_")[0])


def url_seeds(sec: ScheduleHFacility) -> list[tuple[str, str, str]]:
    """(url, kind, discovered_via) discovery seeds from lines 16a-c and the recovered narrative URLs."""
    out: list[tuple[str, str, str]] = []
    for key, kind, via in (
        ("line_16a_fap_url", "fap", "schedule_h_16a"),
        ("line_16b_app_url", "application", "schedule_h_16b"),
        ("line_16c_pls_url", "pls", "schedule_h_16c"),
    ):
        if url := getattr(sec, key):
            out.append((url, kind, via))
    known = {u for u, _, _ in out}
    for url in sec.narrative_urls_recovered:
        if url not in known:
            out.append((url, "fap" if url.lower().endswith(".pdf") or "fap" in url.lower() else "other", "schedule_h_narrative"))
    return out

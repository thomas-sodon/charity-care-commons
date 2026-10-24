"""Synthetic IRS Form 990 e-file returns carrying Schedule H, in the real XML layout.

Element names follow the IRS e-file schema as catalogued by IRSx
(``990-xml-metadata/variables.csv``, current through the 2024 schema
versions), e.g. ``/IRS990ScheduleH/HospitalFcltyPoliciesPrctcGrp/
FPGFamilyIncmLmtFreeCarePct`` for Part V Section B line 13a. As in real
filings, unchecked ``CheckboxType`` lines are simply omitted, ``BooleanType``
values appear as ``true``/``1``/``false``/``0``, and percentages may be written
as ``200`` or ``200.00``. Files are named ``{OBJECT_ID}_public.xml`` and listed
in an index CSV with the IRS column layout, then zipped like an IRS batch.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

from lxml import etree

from charitycare.demo.spec import HospitalSpec

NS = "http://www.irs.gov/efile"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
RETURN_VERSION = "2024v5.0"
INDEX_COLUMNS = ["RETURN_ID", "FILING_TYPE", "EIN", "TAX_PERIOD", "SUB_DATE", "TAXPAYER_NAME", "RETURN_TYPE", "DLN", "OBJECT_ID", "XML_BATCH_ID"]
BATCH_ID = "2026_TEOS_XML_DEMO"
ZIP_EPOCH = (2026, 10, 1, 0, 0, 0)


def _e(parent: etree._Element, tag: str, text: str | None = None, **attrs: str) -> etree._Element:
    el = etree.SubElement(parent, f"{{{NS}}}{tag}", **attrs)
    if text is not None:
        el.text = text
    return el


def _name(parent: etree._Element, tag: str, line1: str) -> None:
    n = _e(parent, tag)
    _e(n, "BusinessNameLine1Txt", line1.upper()[:75])


def _addr(parent: etree._Element, street: str, city: str, zip_: str) -> None:
    a = _e(parent, "USAddress")
    _e(a, "AddressLine1Txt", street.upper())
    _e(a, "CityNm", city.upper())
    _e(a, "StateAbbreviationCd", "TX")
    _e(a, "ZIPCd", zip_)


def submission_date(h: HospitalSpec) -> date:
    end = h.schedule_h.tax_period_end
    return date(end.year + 1, min(end.month, 12), 15) if end.month <= 3 else date(end.year + 1, 5, 15)


def build_return(h: HospitalSpec) -> bytes:
    """One Form 990 return (header + minimal IRS990 + full Schedule H Part V) as XML bytes."""
    sh = h.schedule_h
    t = sh.bool_true
    f = "false" if t == "true" else "0"
    root = etree.Element(f"{{{NS}}}Return", nsmap={None: NS, "xsi": XSI}, returnVersion=RETURN_VERSION)
    hdr = _e(root, "ReturnHeader", binaryAttachmentCnt="0")
    _e(hdr, "ReturnTs", f"{submission_date(h).isoformat()}T10:15:00-05:00")
    _e(hdr, "TaxPeriodEndDt", sh.tax_period_end.isoformat())
    _e(hdr, "TaxPeriodBeginDt", sh.tax_period_begin.isoformat())
    filer = _e(hdr, "Filer")
    _e(filer, "EIN", h.ein)
    _name(filer, "BusinessName", h.system_name or h.name)
    _e(filer, "BusinessNameControlTxt", "".join(c for c in (h.system_name or h.name).upper() if c.isalpha())[:4])
    _e(filer, "PhoneNum", "".join(c for c in h.phone if c.isdigit()))
    _addr(filer, h.primary.address, h.primary.city, h.primary.zip)
    _e(hdr, "ReturnTypeCd", "990")
    _e(hdr, "TaxYr", str(sh.tax_period_begin.year))

    data = _e(root, "ReturnData", documentCnt="2")
    irs990 = _e(data, "IRS990", documentId="IRS990-01")
    _e(irs990, "PrincipalOfficerNm", "SYNTHETIC OFFICER")
    _e(irs990, "WebsiteAddressTxt", h.base_url.replace("https://", ""))
    _e(irs990, "Organization501c3Ind", "X")
    _e(irs990, "ActivityOrMissionDesc", "SYNTHETIC DEMO RETURN FOR A FICTIONAL TEXAS NONPROFIT HOSPITAL")

    sched = _e(data, "IRS990ScheduleH", documentId="IRS990ScheduleH-01")
    # Part I lines 1a-4 (system-level answers) ---------------------------------------------
    _e(sched, "FinancialAssistancePolicyInd", t)
    _e(sched, "WrittenPolicyInd", t)
    _e(sched, "AllHospitalsPolicyInd" if len(h.facilities) > 1 else "IndivHospitalTailoredPolicyInd", "X")
    _e(sched, "FPGReferenceFreeCareInd", t)
    free_box = {100: "Percent100Ind", 150: "Percent150Ind", 200: "Percent200Ind"}.get(int(sh.free_pct))
    if free_box and sh.free_pct == int(sh.free_pct):
        _e(sched, free_box, "X")
    else:
        g = _e(sched, "FreeCareOthPercentageGrp")
        _e(g, "OtherInd", "X")
        _e(g, "FreeCareOtherPct", sh.pct_format.format(sh.free_pct))
    _e(sched, "FPGReferenceDiscountedCareInd", t)
    disc_box = {200: "Percent200DInd", 250: "Percent250Ind", 300: "Percent300Ind", 350: "Percent350Ind", 400: "Percent400Ind"}.get(int(sh.discount_pct))
    if disc_box and sh.discount_pct == int(sh.discount_pct):
        _e(sched, disc_box, "X")
    else:
        g = _e(sched, "DiscountedCareOthPercentageGrp")
        _e(g, "OtherInd", "X")
        _e(g, "DiscountedCareOtherPct", sh.pct_format.format(sh.discount_pct))
    _e(sched, "FreeCareMedicallyIndigentInd", t if sh.medical_indigency else f)
    _e(sched, "HospitalFacilitiesCnt", str(len(h.facilities)))

    group = "A" if len(h.facilities) > 1 else None
    for i, fac in enumerate(h.facilities, start=1):
        g = _e(sched, "HospitalFacilitiesGrp")
        _e(g, "FacilityNum", str(i))
        _name(g, "BusinessName", fac.name)
        _addr(g, fac.address, fac.city, fac.zip)
        _e(g, "WebsiteAddressTxt", h.base_url.replace("https://", ""))
        _e(g, "StateLicenseNum", fac.license_no)
        _e(g, "LicensedHospitalInd", "X")
        if fac.cms_type == "Childrens":
            _e(g, "ChildrensHospitalInd", "X")
        elif fac.cms_type == "Critical Access Hospitals":
            _e(g, "CriticalAccessHospitalInd", "X")
        else:
            _e(g, "GeneralMedicalAndSurgicalInd", "X")
        _e(g, "EmergencyRoom24HrsInd", "X")
        if group:
            _e(g, "FacilityReportingGroupCd", group)

    b = _e(sched, "HospitalFcltyPoliciesPrctcGrp")
    if group:
        _e(b, "FacilityReportingGroupCd", group)
    else:
        _name(b, "HospitalFacilityName", h.primary.name)
        _e(b, "FacilityNum", "1")
    # Part V Section B, CHNA lines (abbreviated) ------------------------------------------
    _e(b, "FirstLicensedCYOrPYInd", f)
    _e(b, "TaxExemptHospitalCYOrPYInd", f)
    _e(b, "CHNAConductedInd", t)
    _e(b, "CHNAConductedYr", str(sh.tax_period_end.year - 1))
    _e(b, "ImplementationStrategyAdoptInd", t)
    # Line 13: eligibility criteria ---------------------------------------------------------
    _e(b, "EligCriteriaExplainedInd", t)
    _e(b, "FPGFamilyIncmLmtFreeDscntInd", "X")
    _e(b, "FPGFamilyIncmLmtFreeCarePct", sh.pct_format.format(sh.free_pct))
    _e(b, "FPGFamilyIncmLmtDscntCarePct", sh.pct_format.format(sh.discount_pct))
    _e(b, "IncomeLevelCriteriaInd", "X")
    if sh.asset:
        _e(b, "AssetLevelCriteriaInd", "X")
    if sh.medical_indigency:
        _e(b, "MedicalIndigencyCriteriaInd", "X")
    if sh.insurance:
        _e(b, "InsuranceStatusCriteriaInd", "X")
    if sh.underinsurance:
        _e(b, "UnderinsuranceStatCriteriaInd", "X")
    if sh.residency:
        _e(b, "ResidencyCriteriaInd", "X")
    # Lines 14-16: basis, application, publicity ------------------------------------------
    _e(b, "ExplainedBasisInd", t)
    _e(b, "AppFinancialAsstExplnInd", t)
    _e(b, "DescribedInfoInd", "X")
    _e(b, "DescribedSuprtDocInd", "X")
    _e(b, "ProvidedHospitalContactInd", "X")
    _e(b, "IncludesPublicityMeasuresInd", t)
    _e(b, "FAPAvailableOnWebsiteInd", "X")
    fap_url = f"{h.base_url}/financial-assistance/fap.pdf"
    _e(b, "FAPAvailableOnWebsiteURLTxt", "SEE PART V, SECTION C" if sh.fap_url_in_narrative else fap_url)
    _e(b, "FAPAppAvailableOnWebsiteInd", "X")
    _e(b, "FAPAppAvailableOnWebsiteURLTxt", f"{h.base_url}/financial-assistance/application.pdf")
    _e(b, "FAPSummaryOnWebsiteInd", "X")
    _e(b, "FAPSummaryOnWebsiteURLTxt", f"{h.base_url}/financial-assistance/summary.pdf")
    _e(b, "FAPAvlblOnRequestNoChargeInd", "X")
    _e(b, "FAPAppAvlblOnRequestNoChrgInd", "X")
    _e(b, "FAPSumAvlblOnRequestNoChrgInd", "X")
    _e(b, "NotifiedFAPCopyBillDisplayInd", "X")
    if sh.translated:
        _e(b, "FAPTranslatedInd", "X")
    # Lines 17-19: billing and collections ------------------------------------------------
    _e(b, "FAPActionsOnNonpaymentInd", t)
    eca_tags = {
        "credit_report": "PermitReportToCreditAgencyInd",
        "sell_debt": "PermitSellingDebtInd",
        "defer_deny_care": "PermitDeferDenyRqrPaymentInd",
        "legal_process": "PermitLegalJudicialProcessInd",
        "other": "PermitOtherActionsInd",
        "none": "PermitNoActionsInd",
    }
    for eca in sh.ecas:
        _e(b, eca_tags[eca], "X")
    _e(b, "CollectionActivitiesInd", f)
    _e(b, "ProvidedWrittenNoticeInd", "X")
    _e(b, "ProcessedFAPApplicationInd", "X")
    # Lines 21-24: emergency care, AGB, charges ---------------------------------------------
    _e(b, "NondisEmergencyCarePolicyInd", t)
    agb_tags = {
        "lookback_medicare": "LookBackMedicareInd",
        "lookback_medicare_private": "LookBackMedicarePrivateInd",
        "lookback_medicaid": "LookBackMedicaidMedcrPrvtInd",
        "prospective": "ProspectiveMedicareMedicaidInd",
    }
    _e(b, agb_tags[sh.agb], "X")
    _e(b, "AmountsGenerallyBilledInd", f)
    _e(b, "GrossChargesInd", f)

    sup = _e(sched, "SupplementalInformationGrp")
    if sh.fap_url_in_narrative:
        _e(sup, "FormAndLineReferenceDesc", "PART V, SECTION B, LINE 16A")
        _e(
            sup,
            "ExplanationTxt",
            f"{(h.system_name or h.name).upper()}: THE FINANCIAL ASSISTANCE POLICY IS AVAILABLE AT {fap_url} AND IN PRINT AT ALL REGISTRATION AREAS.",
        )
    else:
        _e(sup, "FormAndLineReferenceDesc", "PART V, SECTION B, LINE 13H")
        _e(sup, "ExplanationTxt", "THE ORGANIZATION CONSIDERS FAMILY SIZE AND HOUSEHOLD INCOME AS DESCRIBED IN ITS FINANCIAL ASSISTANCE POLICY.")
    return etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


def index_rows(hospitals: tuple[HospitalSpec, ...]) -> str:
    """IRS-style index CSV for the synthetic batch."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(INDEX_COLUMNS)
    for i, h in enumerate(hospitals, start=1):
        sh = h.schedule_h
        w.writerow(
            [
                str(9_000_000 + i),
                "EFILE",
                h.ein,
                sh.tax_period_end.strftime("%Y%m"),
                submission_date(h).isoformat(),
                (h.system_name or h.name).upper(),
                "990",
                f"93493{sh.object_id[-9:]}",
                sh.object_id,
                BATCH_ID,
            ]
        )
    return buf.getvalue()


def batch_zip(files: dict[str, bytes]) -> bytes:
    """Deterministic zip (fixed timestamps, sorted names) like an IRS XML batch."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"{BATCH_ID}/{name}", date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name])
    return buf.getvalue()

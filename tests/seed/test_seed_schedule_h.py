"""Schedule H Part V parser: values pinned on the demo XML, plus real-filing quirks."""

from __future__ import annotations

import pytest

from charitycare.seed.schedule_h import (
    LINE_XPATHS,
    ScheduleHParseError,
    extract_urls,
    looks_like_url,
    normalize_url,
    parse_file,
    parse_return,
    url_seeds,
)

# object_id -> pinned expectations (from demo/spec.py ScheduleHSpec).
PINNED = {
    "202601999349300101": dict(free=200, disc=400, asset=False, ins=True, res=True, tr=True, ecas=["credit_report"], agb="lookback_medicare_private"),
    "202601999349300202": dict(free=250, disc=400, asset=False, ins=True, res=False, tr=True, ecas=["credit_report", "sell_debt"], agb="lookback_medicare_private"),
    "202601999349300303": dict(free=300, disc=400, asset=False, ins=True, res=True, tr=True, ecas=["none"], agb="lookback_medicaid"),
    "202601999349300404": dict(free=125, disc=250, asset=False, ins=True, res=True, tr=False, ecas=["none"], agb="lookback_medicare"),
    "202601999349300505": dict(free=200, disc=300, asset=False, ins=True, res=False, tr=False, ecas=["legal_process"], agb="lookback_medicare_private"),
    "202601999349300606": dict(free=200, disc=300, asset=True, ins=True, res=True, tr=False, ecas=["none"], agb="prospective"),
    "202601999349300707": dict(free=200, disc=500, asset=False, ins=True, res=True, tr=False, ecas=["credit_report"], agb="lookback_medicare_private"),
    "202601999349300808": dict(free=200, disc=400, asset=False, ins=True, res=True, tr=False, ecas=["none"], agb="lookback_medicare_private"),
}


@pytest.mark.parametrize("oid", sorted(PINNED))
def test_pinned_part_v_values(demo_corpus, oid) -> None:
    ret = parse_file(str(demo_corpus.path(f"irs/xml/{oid}_public.xml")))
    assert len(ret.sections) == 1
    s = ret.sections[0]
    p = PINNED[oid]
    assert s.object_id == oid
    assert s.line_13a_free_pct == p["free"] and s.line_13a_discount_pct == p["disc"]
    assert s.line_13a_fpg_used is True
    assert s.line_13c_asset_level is p["asset"]
    assert s.line_13e_insurance_status is p["ins"]
    assert s.line_13g_residency is p["res"]
    assert s.line_15a_described_info is True and s.line_15b_described_docs is True
    assert s.line_16i_translated is p["tr"]
    assert s.line_18_ecas_permitted == p["ecas"]
    assert s.line_22_agb_method == p["agb"]
    assert s.line_13_criteria_explained is True
    assert s.xpaths["line_13a_free_pct"].endswith("/IRS990ScheduleH/HospitalFcltyPoliciesPrctcGrp[1]/FPGFamilyIncmLmtFreeCarePct")


def test_matches_gold_schedule_h_metadata(demo_corpus) -> None:
    import json

    for h in demo_corpus.manifest["hospitals"]:
        gold = json.loads(demo_corpus.path(h["gold"]).read_text())["schedule_h"]
        ret = parse_file(str(demo_corpus.path(f"irs/xml/{gold['object_id']}_public.xml")))
        s = ret.sections[0]
        assert s.ein == h["ein"]
        assert s.tax_period_end.isoformat() == gold["tax_period_end"]
        assert s.group_letter == gold["group_letter"]
        assert sorted(n.lower() for n in s.facility_names) == sorted(f["name"].lower() for f in h["facilities"])


def test_group_reporting_joins_section_a(demo_corpus) -> None:
    ret = parse_file(str(demo_corpus.path("irs/xml/202601999349300202_public.xml")))
    s = ret.sections[0]
    assert s.id == "202601999349300202:A"
    assert s.group_letter == "A" and s.facility_numbers == [1, 2, 3]
    assert s.facility_names == [
        "LONE STAR EXAMPLE MEDICAL CENTER DALLAS",
        "LONE STAR EXAMPLE MEDICAL CENTER PLANO",
        "LONE STAR EXAMPLE MEDICAL CENTER MESQUITE",
    ]
    assert [h.city for h in ret.hospitals] == ["DALLAS", "PLANO", "MESQUITE"]
    assert ret.organization.system_name == "LONE STAR EXAMPLE HEALTH SYSTEM"


def test_narrative_url_recovery(demo_corpus) -> None:
    ret = parse_file(str(demo_corpus.path("irs/xml/202601999349300808_public.xml")))
    s = ret.sections[0]
    assert s.line_16a_fap_url is None
    assert s.narrative_urls_recovered == ["https://www.examplebrazosvalley.example/financial-assistance/fap.pdf"]
    assert "line_16a_fap_url_narrative" in s.xpaths
    seeds = url_seeds(s)
    assert ("https://www.examplebrazosvalley.example/financial-assistance/fap.pdf", "fap", "schedule_h_narrative") in seeds
    assert any(via == "schedule_h_16b" for _, _, via in seeds)


def test_bool_and_pct_formats(demo_corpus) -> None:
    # valley-childrens writes booleans as 1/0; gulf-coast writes percentages as 200.00
    v = parse_file(str(demo_corpus.path("irs/xml/202601999349300303_public.xml"))).sections[0]
    g = parse_file(str(demo_corpus.path("irs/xml/202601999349300505_public.xml"))).sections[0]
    assert v.line_13_criteria_explained is True
    assert g.line_13a_free_pct == 200.0 and g.line_13d_medical_indigency is True and g.line_13f_underinsurance_status is True


MINIMAL = b"""<?xml version="1.0"?>
<Return xmlns="http://www.irs.gov/efile"><ReturnHeader>
<TaxPeriodEndDt>2024-12-31</TaxPeriodEndDt><Filer><EIN>990000001</EIN>
<BusinessName><BusinessNameLine1Txt>ACME HEALTH</BusinessNameLine1Txt></BusinessName>
<USAddress><StateAbbreviationCd>TX</StateAbbreviationCd></USAddress></Filer></ReturnHeader>
<ReturnData><IRS990ScheduleH>
<HospitalFacilitiesGrp><FacilityNum>1</FacilityNum><BusinessName><BusinessNameLine1Txt>ACME A</BusinessNameLine1Txt></BusinessName></HospitalFacilitiesGrp>
<HospitalFacilitiesGrp><FacilityNum>2</FacilityNum><BusinessName><BusinessNameLine1Txt>ACME B</BusinessNameLine1Txt></BusinessName></HospitalFacilitiesGrp>
<HospitalFcltyPoliciesPrctcGrp><HospitalFacilityName><BusinessNameLine1Txt>ACME B</BusinessNameLine1Txt></HospitalFacilityName><FacilityNum>2</FacilityNum>
<FPGFamilyIncmLmtFreeCarePct>150.00</FPGFamilyIncmLmtFreeCarePct>
<FAPAvailableOnWebsiteURLTxt>www.acme.org/fap</FAPAvailableOnWebsiteURLTxt>
<FAPAppAvailableOnWebsiteURLTxt>See Part V, Section C</FAPAppAvailableOnWebsiteURLTxt>
</HospitalFcltyPoliciesPrctcGrp>
<SupplementalInformationGrp><FormAndLineReferenceDesc>Part V Line 16b</FormAndLineReferenceDesc>
<ExplanationTxt>Facility 2: the application is at acme.org/fa/app.pdf.</ExplanationTxt></SupplementalInformationGrp>
</IRS990ScheduleH></ReturnData></Return>"""


def test_minimal_individual_facility_and_unknowns() -> None:
    ret = parse_return(MINIMAL, "OID1")
    s = ret.sections[0]
    assert s.id == "OID1:2" and s.facility_names == ["ACME B"] and s.facility_numbers == [2]
    assert s.tax_year == 2024 and s.tax_period_begin is None
    assert s.line_13a_free_pct == 150.0 and s.line_13a_discount_pct is None
    assert s.line_13_criteria_explained is None  # BooleanType absent -> unknown
    assert s.line_13c_asset_level is False  # CheckboxType absent -> unchecked
    assert s.line_16a_fap_url == "https://www.acme.org/fap"
    assert s.line_16b_app_url is None and s.narrative_urls_recovered == ["https://acme.org/fa/app.pdf"]
    assert s.line_22_agb_method is None and s.line_18_ecas_permitted == []


def test_unnamespaced_document_parses() -> None:
    ret = parse_return(MINIMAL.replace(b' xmlns="http://www.irs.gov/efile"', b""), "OID2")
    assert ret.sections[0].line_13a_free_pct == 150.0


@pytest.mark.parametrize(
    "xml",
    [b"not xml", b"<Other/>", b'<Return xmlns="http://www.irs.gov/efile"><ReturnData><IRS990/></ReturnData></Return>'],
)
def test_rejects_non_schedule_h(xml: bytes) -> None:
    with pytest.raises(ScheduleHParseError):
        parse_return(xml)


def test_url_helpers() -> None:
    assert looks_like_url("https://x.org/a.pdf") and looks_like_url("www.x.org")
    assert not looks_like_url("SEE PART V, SECTION C")
    assert normalize_url("WWW.X.ORG/FAP.pdf.") == "https://www.x.org/FAP.pdf"
    assert extract_urls(["See https://a.org/x and www.b.com/y; also https://a.org/x."]) == ["https://a.org/x", "https://www.b.com/y"]


def test_pinned_xpath_names_are_irsx_names() -> None:
    assert LINE_XPATHS["line_13a_free_pct"][0] == "FPGFamilyIncmLmtFreeCarePct"
    assert LINE_XPATHS["line_13c_asset_level"][0] == "AssetLevelCriteriaInd"
    assert LINE_XPATHS["line_13e_insurance_status"][0] == "InsuranceStatusCriteriaInd"
    assert LINE_XPATHS["line_13g_residency"][0] == "ResidencyCriteriaInd"
    assert LINE_XPATHS["line_16a_fap_url"][0] == "FAPAvailableOnWebsiteURLTxt"

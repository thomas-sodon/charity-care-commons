import csv
import json
import zipfile
from decimal import Decimal

import pymupdf
import pytest
from lxml import etree

from charitycare import fpg
from charitycare.config import PROMPT_VERSIONS
from charitycare.demo.build import build_corpus
from charitycare.demo.cms import COLUMNS
from charitycare.demo.fixtures import DELIBERATE_ERRORS
from charitycare.demo.spec import HOSPITALS
from charitycare.llm import LLMRequest, ReplayBackend
from charitycare.pdftext import find_quote, page_texts, sha256_bytes
from charitycare.schemas import CompiledPolicy, Field, FormTemplate

NS = {"e": "http://www.irs.gov/efile"}
IDS = [h.id for h in HOSPITALS]


def gold(corpus, hid):
    return json.loads(corpus.path(f"gold/{hid}.json").read_text())


def test_eight_fictional_hospitals(demo_corpus) -> None:
    m = demo_corpus.manifest
    assert m["synthetic"] is True and len(m["hospitals"]) == 8
    for h in m["hospitals"]:
        assert "Example" in h["name"] and h["domain"].endswith(".example")
        for f in h["facilities"]:
            assert f["ccn"].startswith("4599")


def test_deterministic(demo_corpus, tmp_path) -> None:
    again = build_corpus(tmp_path / "demo", tmp_path / "fx")
    assert again["checksum"] == demo_corpus.manifest["checksum"]
    for h in HOSPITALS:
        a = (tmp_path / "demo" / "docs" / h.id / "fap.pdf").read_bytes()
        assert a == demo_corpus.path(f"docs/{h.id}/fap.pdf").read_bytes()


@pytest.mark.parametrize("hid", IDS)
def test_gold_is_valid_and_quotes_verify(demo_corpus, hid) -> None:
    g = gold(demo_corpus, hid)
    policy = CompiledPolicy.model_validate(g["policy"])
    fap = g["documents"]["fap"]
    data = demo_corpus.path(fap["path"]).read_bytes()
    assert sha256_bytes(data) == fap["sha256"]
    texts = page_texts(data, ocr=False)
    assert len(texts) == fap["pages"]
    for key, field in policy.fields().items():
        assert field.abstain_reason is not None or field.evidence, key
        for e in field.evidence:
            assert e.doc_sha256 == fap["sha256"]
            page = texts[e.page - 1]
            if e.page in fap["image_only_pages"]:
                assert page.text == "" and page.ocr_unavailable is not None
            else:
                assert find_quote(page.text, e.quote), (key, e.page, e.quote)


def test_image_only_pages(demo_corpus) -> None:
    g = gold(demo_corpus, "pineywoods-regional")
    assert g["documents"]["fap"]["image_only_pages"] == [3]
    fap = pymupdf.open(demo_corpus.path(g["documents"]["fap"]["path"]))
    assert fap[2].get_text().strip() == "" and fap[2].get_images()
    assert fap[1].get_text().strip()
    app = pymupdf.open(demo_corpus.path(g["documents"]["application_en"]["path"]))
    assert all(not p.get_text().strip() for p in app) and not list(app[0].widgets())


def test_abstentions(demo_corpus) -> None:
    gb = CompiledPolicy.model_validate(gold(demo_corpus, "gulf-coast-baptist")["policy"])
    assert gb.presumptive_eligibility.abstain_reason == "ambiguous_may_clause" and gb.presumptive_eligibility.evidence
    hc = CompiledPolicy.model_validate(gold(demo_corpus, "hill-country-community")["policy"])
    assert hc.refund_policy.abstain_reason == "not_addressed"
    assert hc.screening_timing.abstain_reason == "not_addressed"
    assert hc.asset_test.value.threshold_usd == 5000


def test_multi_facility_addendum(demo_corpus) -> None:
    g = gold(demo_corpus, "lone-star-health")
    p = CompiledPolicy.model_validate(g["policy"])
    assert len(p.facilities_covered.value) == 3
    assert p.facilities_covered.evidence[0].page == 7
    assert p.facilities_covered.evidence[0].section_label == "Addendum A"
    assert g["schedule_h"]["group_letter"] == "A" and len(g["facility_ids"]) == 3


@pytest.mark.parametrize("hid,expected", [("example-methodist", "match"), ("panhandle-university", "mismatch")])
def test_income_table_round_trip(demo_corpus, hid, expected) -> None:
    g = gold(demo_corpus, hid)
    t = g["income_table"]
    assert t["expected_round_trip"] == expected
    reproduced = all(
        Decimal(r["amount"]) == fpg.income_at_pct(r["pct"], r["household_size"], t["stated_year"]).quantize(Decimal("1"))
        for r in t["rows"]
    )
    assert reproduced is (expected == "match")
    text = page_texts(demo_corpus.path(g["documents"]["fap"]["path"]).read_bytes())[t["page"] - 1].text
    assert f"${t['rows'][0]['amount']:,}" in text


def test_expected_crosschecks(demo_corpus) -> None:
    results = {h.id: gold(demo_corpus, h.id)["schedule_h"]["expected_crosschecks"] for h in HOSPITALS}
    assert results["example-methodist"]["asset_test"] == "disagree_unexplained"
    assert results["pineywoods-regional"]["fpg_tiers"] == "disagree_policy_newer_than_filing"
    others = [v for hid, r in results.items() for k, v in r.items() if (hid, k) not in {("example-methodist", "asset_test"), ("pineywoods-regional", "fpg_tiers")}]
    assert set(others) == {"agree"}


def test_schedule_h_xml(demo_corpus) -> None:
    rows = list(csv.DictReader(demo_corpus.path("irs/index_2026_demo.csv").open()))
    assert len(rows) == 8 and "OBJECT_ID" in rows[0]
    with zipfile.ZipFile(demo_corpus.path("irs/2026_TEOS_XML_DEMO.zip")) as zf:
        assert len(zf.namelist()) == 8
    by_id = {h.schedule_h.object_id: h for h in HOSPITALS}
    for row in rows:
        h = by_id[row["OBJECT_ID"]]
        root = etree.parse(str(demo_corpus.path(f"irs/xml/{row['OBJECT_ID']}_public.xml"))).getroot()
        assert root.find("e:ReturnHeader/e:Filer/e:EIN", NS).text == h.ein == row["EIN"]
        assert root.find("e:ReturnHeader/e:TaxPeriodEndDt", NS).text == h.schedule_h.tax_period_end.isoformat()
        b = root.find("e:ReturnData/e:IRS990ScheduleH/e:HospitalFcltyPoliciesPrctcGrp", NS)
        assert float(b.find("e:FPGFamilyIncmLmtFreeCarePct", NS).text) == h.schedule_h.free_pct
        assert float(b.find("e:FPGFamilyIncmLmtDscntCarePct", NS).text) == h.schedule_h.discount_pct
        assert (b.find("e:AssetLevelCriteriaInd", NS) is not None) == h.schedule_h.asset
        facs = root.findall("e:ReturnData/e:IRS990ScheduleH/e:HospitalFacilitiesGrp", NS)
        assert len(facs) == len(h.facilities)
        url = b.find("e:FAPAvailableOnWebsiteURLTxt", NS).text
        if h.schedule_h.fap_url_in_narrative:
            assert url == "SEE PART V, SECTION C"
            narrative = root.find("e:ReturnData/e:IRS990ScheduleH/e:SupplementalInformationGrp/e:ExplanationTxt", NS).text
            assert "https://www.examplebrazosvalley.example/financial-assistance/fap.pdf" in narrative
        else:
            assert url.startswith("https://")


def test_cms_csv(demo_corpus) -> None:
    text = demo_corpus.path("cms/Hospital_General_Information.csv").read_text()
    reader = csv.DictReader(text.splitlines())
    assert reader.fieldnames == COLUMNS
    rows = list(reader)
    assert len(rows) == 10 + 4
    ours = [r for r in rows if r["State"] == "TX" and r["Hospital Ownership"].startswith("Voluntary non-profit") and r["Hospital Type"] != "Psychiatric"]
    assert {r["Facility ID"] for r in ours} == {f.ccn for h in HOSPITALS for f in h.facilities}


@pytest.mark.parametrize("hid", IDS)
def test_forms_match_templates(demo_corpus, hid) -> None:
    g = gold(demo_corpus, hid)
    apps = {k: v for k, v in g["documents"].items() if v["kind"] == "application"}
    assert apps
    for doc in apps.values():
        t = FormTemplate.model_validate(doc["form_template"])
        pdf = pymupdf.open(demo_corpus.path(doc["path"]))
        assert sha256_bytes(demo_corpus.path(doc["path"]).read_bytes()) == t.doc_sha256
        assert {f.key for f in t.field_map} >= {"patient_name", "household_size", "annual_household_income", "ssn", "signature"}
        assert all(f.write_by_hand for f in t.field_map if f.key in {"ssn", "account_number", "date_of_birth", "signature"})
        widgets = {w.field_name for p in pdf for w in p.widgets()}
        if t.kind == "fillable":
            assert widgets == {f.widget_name for f in t.field_map}
        else:
            assert not widgets


def test_spanish_application(demo_corpus) -> None:
    g = gold(demo_corpus, "valley-childrens")
    es = g["documents"]["application_es"]
    assert es["language"] == "es" and es["form_kind"] == "flat"
    text = pymupdf.open(demo_corpus.path(es["path"]))[0].get_text()
    assert "Solicitud de Asistencia Financiera" in text and "Número de Seguro Social" in text
    assert "pls_es" in g["documents"]


def test_web_url_map(demo_corpus) -> None:
    url_map = json.loads(demo_corpus.path("web/url_map.json").read_text())
    for url, rel in url_map.items():
        assert url.startswith("https://www.") and demo_corpus.path(rel).exists(), url
    for h in demo_corpus.manifest["hospitals"]:
        assert h["documents"]["fap"]["url"] in url_map


@pytest.mark.parametrize("hid", IDS)
def test_replay_fixtures(demo_corpus, hid) -> None:
    g = gold(demo_corpus, hid)
    backend = ReplayBackend(demo_corpus.fixtures)
    r = LLMRequest(
        prompt_version=PROMPT_VERSIONS["compile_policy"], model_tier="sonnet", doc_sha256=g["documents"]["fap"]["sha256"],
        system="", user="", output_schema=CompiledPolicy,
    )
    res = backend.complete(r)
    gold_policy = CompiledPolicy.model_validate(g["policy"])
    errs = {e["field"]: e["kind"] for e in DELIBERATE_ERRORS if e["hospital_id"] == hid}
    texts = page_texts(demo_corpus.path(g["documents"]["fap"]["path"]).read_bytes(), ocr=False)
    for key, field in res.output.fields().items():
        g_field = gold_policy.fields()[key]
        if key not in errs:
            assert field.value == g_field.value and field.evidence == g_field.evidence, key
            continue
        kind = errs[key]
        if kind == "misread_value":
            assert field.value != g_field.value and field.evidence == g_field.evidence
        else:
            unverified = [e for e in field.evidence if not find_quote(texts[e.page - 1].text, e.quote)]
            assert unverified, f"{hid}.{key} deliberate {kind} should fail verification"
    assert 0 < res.cost_usd < 0.5


def test_reask_fixture(demo_corpus) -> None:
    g = gold(demo_corpus, "gulf-coast-baptist")
    r = LLMRequest(
        prompt_version=PROMPT_VERSIONS["reask_field"], model_tier="sonnet", doc_sha256=g["documents"]["fap"]["sha256"],
        system="", user="", output_schema=Field[list], variant="fpg_tiers",
    )
    res = ReplayBackend(demo_corpus.fixtures).complete(r)
    assert res.output.value[0]["max_pct_inclusive"] == 200


def test_committed_corpus_matches_generator(demo_corpus) -> None:
    from charitycare.demo.build import load_manifest

    committed = load_manifest()
    assert committed["checksum"] == demo_corpus.manifest["checksum"], "run `ccc demo build` and commit data/demo"

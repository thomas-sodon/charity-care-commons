"""CMS loader (API paging via a mock transport, scope filter) and IRS index/ZIP streaming."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from charitycare.seed import cms
from charitycare.seed.irs_batch import (
    build_zip,
    download_index,
    filer_state,
    iter_batch,
    iter_index,
    iter_schedule_h,
    object_id_of,
)

REPO = Path(__file__).resolve().parents[2]


def test_demo_csv_scope_filter(demo_corpus) -> None:
    load = cms.load_cms(csv_path=demo_corpus.path("cms/Hospital_General_Information.csv"), cache_path=demo_corpus.root / "none.csv")
    assert len(load.rows) == 10
    assert load.excluded == {"not_voluntary_nonprofit": 2, "psychiatric": 1, "out_of_state": 1}
    facs = load.facilities
    assert all(f.id == f"tx-{f.ccn}" and f.in_scope for f in facs)
    assert {f.ccn for f in facs} == {f["ccn"] for h in demo_corpus.manifest["hospitals"] for f in h["facilities"]}


def _api_row(ccn: str, own: str = "Voluntary non-profit - Private", typ: str = "Acute Care Hospitals") -> dict[str, str]:
    return {
        "facility_id": ccn, "facility_name": f"HOSP {ccn}", "address": "1 MAIN ST", "citytown": "AUSTIN", "state": "TX",
        "zip_code": "78701", "countyparish": "TRAVIS", "telephone_number": "", "hospital_type": typ,
        "hospital_ownership": own, "emergency_services": "Yes",
    }


def test_api_paging_and_cache(tmp_path) -> None:
    rows = [_api_row(f"45{i:04d}") for i in range(7)] + [_api_row("459999", own="Proprietary"), _api_row("459998", typ="Psychiatric")]
    seen: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        seen.append(q)
        assert q["conditions[0][property]"] == "state" and q["conditions[0][value]"] == "TX"
        off, lim = int(q["offset"]), int(q["limit"])
        return httpx.Response(200, json={"results": rows[off : off + lim], "count": len(rows)})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = list(cms.iter_cms_api("TX", client, page_size=4))
    assert len(got) == 9 and len(seen) == 3
    cache = tmp_path / "cache.csv"
    load = cms.load_cms(live=True, client=httpx.Client(transport=httpx.MockTransport(handler)), cache_path=cache)
    assert load.origin == "live" and len(load.rows) == 7
    assert load.excluded == {"not_voluntary_nonprofit": 1, "psychiatric": 1}
    # a failing live call falls back to the cache written above
    broken = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    again = cms.load_cms(live=True, client=broken, cache_path=cache)
    assert again.origin.startswith("cache:") and len(again.rows) == 7


def test_committed_live_tx_snapshot() -> None:
    """data/seed/cms_tx_hospitals.csv is a real live pull (public-domain CMS data)."""
    path = REPO / "data" / "seed" / "cms_tx_hospitals.csv"
    if not path.exists():
        pytest.skip("no live CMS snapshot committed")
    rows = cms.read_cms_csv(path)
    kept, excluded = cms.filter_in_scope(rows)
    assert len(rows) > 300 and all(r["State"] == "TX" for r in rows)
    assert 100 < len(kept) < len(rows)
    assert all(r["Hospital Ownership"].startswith("Voluntary non-profit") and r["Hospital Type"] != "Psychiatric" for r in kept)
    assert excluded.get("not_voluntary_nonprofit", 0) > 0


def test_normalize_row_pads_ccn() -> None:
    r = cms.normalize_row({"facility_id": "50001", "state": "TX"})
    assert r["Facility ID"] == "050001" and r["Facility Name"] == ""


# -- IRS index + batches -----------------------------------------------------


def test_index_reader_filters(demo_corpus) -> None:
    rows = list(iter_index(demo_corpus.path("irs/index_2026_demo.csv")))
    assert len(rows) == 8 and rows[0].object_id == "202601999349300101"
    csv_text = "RETURN_ID,FILING_TYPE,EIN,TAX_PERIOD,SUB_DATE,TAXPAYER_NAME,RETURN_TYPE,DLN,OBJECT_ID\n1,EFILE,1,202412,x,A,990EZ,d,111\n2,EFILE,2,202412,x,B,990,d,222\n"
    assert [r.object_id for r in iter_index(io.StringIO(csv_text))] == ["222"]
    assert [r.object_id for r in iter_index(io.StringIO(csv_text), return_types=("990", "990EZ"), eins=["1"])] == ["111"]


def test_batch_streaming_filters_state_and_schedule_h(demo_corpus) -> None:
    xml = demo_corpus.path("irs/xml/202601999349300101_public.xml").read_bytes()
    ok_state = xml.replace(b"<StateAbbreviationCd>TX</StateAbbreviationCd>", b"<StateAbbreviationCd>OK</StateAbbreviationCd>", 1)
    no_sched = b'<?xml version="1.0"?><Return xmlns="http://www.irs.gov/efile"><ReturnHeader><Filer><USAddress><StateAbbreviationCd>TX</StateAbbreviationCd></USAddress></Filer></ReturnHeader><ReturnData><IRS990/></ReturnData></Return>'
    data = build_zip({
        "111111111111111111_public.xml": xml,
        "222222222222222222_public.xml": ok_state,
        "333333333333333333_public.xml": no_sched,
        "README.txt": b"not a return",
    })
    got = list(iter_batch(io.BytesIO(data)))
    assert [oid for oid, _ in got] == ["111111111111111111"]
    assert [oid for oid, _ in iter_batch(io.BytesIO(data), state=None)] == ["111111111111111111", "222222222222222222"]
    assert list(iter_batch(io.BytesIO(data), object_ids=["999"])) == []
    parsed = list(iter_schedule_h(io.BytesIO(data), state=None))
    assert {p.object_id for p in parsed} == {"111111111111111111", "222222222222222222"}
    assert {p.state for p in parsed} == {"TX", "OK"}


def test_demo_batch_zip(demo_corpus) -> None:
    index = list(iter_index(demo_corpus.path("irs/index_2026_demo.csv")))
    rets = list(iter_schedule_h(demo_corpus.path("irs/2026_TEOS_XML_DEMO.zip"), index=index[:3]))
    assert [r.object_id for r in rets] == [r.object_id for r in index[:3]]


def test_helpers() -> None:
    assert object_id_of("2026_TEOS_XML_01A/202601234567890123_public.xml") == "202601234567890123"
    assert object_id_of("foo.txt") is None
    assert filer_state(b"<Return><ReturnHeader><Filer><USAddress><StateAbbreviationCd>TX</StateAbbreviationCd>") == "TX"


def test_download_index_streams(tmp_path) -> None:
    body = "RETURN_ID,OBJECT_ID\n1,2\n"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    out = download_index(2026, tmp_path / "i.csv", client=client)
    assert out.read_text() == body


def test_cms_cli(capsys) -> None:
    from charitycare.seed.cli import main

    assert main(["cms", "--csv", str(REPO / "data" / "demo" / "cms" / "Hospital_General_Information.csv")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["in_scope"] == 10 and out["origin"].startswith("csv:")

"""Facility universe reconciliation and registry persistence."""

from __future__ import annotations

from charitycare.schemas import Facility, ReviewTask, ScheduleHFacility, Source
from charitycare.seed.pipeline import seed_demo, source_id_for
from charitycare.seed.schedule_h import Organization, ParsedReturn, ScheduleHHospital
from charitycare.seed.universe import Candidate, best_match, name_similarity, normalize_name, read_external_list, reconcile


def _fac(ccn: str, name: str, city: str, address: str | None = None, zip_: str | None = None) -> Facility:
    return Facility(id=f"tx-{ccn}", ccn=ccn, name=name, city=city, address=address, zip=zip_)


def test_normalization() -> None:
    assert normalize_name("St. Luke's Hosp & Med Ctr") == "saint lukes hospital medical center"
    assert name_similarity("CHRISTUS St. Michael Health System", "CHRISTUS SAINT MICHAEL HEALTH SYSTEM") == 1.0
    assert name_similarity("Alpha Regional Medical Center", "Omega Childrens Hospital") < 0.6


def test_match_methods_and_confidence() -> None:
    pool = [
        _fac("450001", "SAINT LUKE'S HOSPITAL", "HOUSTON", "6720 BERTNER AVENUE", "77030"),
        _fac("450002", "MEMORIAL HERMANN NORTHEAST", "HUMBLE"),
        _fac("450003", "BAYLOR SCOTT & WHITE MEDICAL CENTER - TEMPLE", "TEMPLE"),
    ]
    f, m, c, _ = best_match(Candidate("external", "r", "x", ccn="450002"), pool)
    assert (f.id, m, c) == ("tx-450002", "ccn", 1.0)
    f, m, c, _ = best_match(Candidate("schedule_h", "r", "St. Luke's Hospital", city="Houston"), pool)
    assert (f.id, m) == ("tx-450001", "exact_name_city") and c >= 0.95
    f, m, _, _ = best_match(Candidate("schedule_h", "r", "SLH MAIN CAMPUS", address="6720 Bertner Ave", zip="77030-1234"), pool)
    assert (f.id, m) == ("tx-450001", "address_zip")
    f, m, _, _ = best_match(Candidate("schedule_h", "r", "Baylor Scott and White Med Ctr - Temple", city="TEMPLE"), pool)
    assert (f.id, m) == ("tx-450003", "exact_name_city")  # abbreviations and punctuation normalize away
    f, m, c, _ = best_match(Candidate("schedule_h", "r", "Baylor Scott White Temple Medical Center", city="TEMPLE"), pool)
    assert (f.id, m) == ("tx-450003", "fuzzy_name_city") and 0.7 <= c < 0.95
    f, m, c, _ = best_match(Candidate("schedule_h", "r", "Memorial Hermann Northeast Hospital", city="Kingwood"), pool)
    assert m in ("fuzzy_name", "unmatched")
    f, m, c, _ = best_match(Candidate("schedule_h", "r", "Completely Different Clinic", city="Waco"), pool)
    assert f is None and m == "unmatched" and c == 0.0


def _ret(oid: str, ein: str, hospitals: list[ScheduleHHospital]) -> ParsedReturn:
    sec = ScheduleHFacility(
        id=f"{oid}:1", ein=ein, org_name="ORG", object_id=oid, tax_year=2024, tax_period_end="2024-12-31",
        facility_numbers=[h.facility_num for h in hospitals], facility_names=[h.name for h in hospitals],
        line_16a_fap_url="https://org.example/fap.pdf",
    )
    return ParsedReturn(oid, Organization(ein=ein, name="ORG"), "TX", None, hospitals, [sec])


def test_reconcile_stable_ids_and_coverage(tmp_path) -> None:
    cms = [_fac("450010", "NORTH TEXAS MEDICAL CENTER", "DENTON"), _fac("450011", "LAKESIDE HOSPITAL", "WACO")]
    hospitals = [
        ScheduleHHospital(1, "NORTH TEXAS REGIONAL MEDICAL CENTER", "1 A ST", "DENTON", "TX", "76201", "L1", None, None),
        ScheduleHHospital(2, "NEW CAMPUS HOSPITAL", "2 B ST", "FRISCO", "TX", "75034", "L2", None, None),
    ]
    ext = tmp_path / "ext.csv"
    ext.write_text("name,city,url\nLakeside Hospital,Waco,https://lakeside.example/fa\nUnknown Rural Hospital,Marfa,\n")
    u = reconcile(cms, [_ret("OID9", "990009999", hospitals)], read_external_list(ext))
    assert set(u.facilities) == {"tx-450010", "tx-450011", "tx-irs-990009999-2", "tx-ext-unknown-rural-hospital"}
    assert u.facilities["tx-450010"].ein == "990009999"
    assert u.external_urls == {"tx-450011": ["https://lakeside.example/fa"]}
    cov = u.coverage()
    assert cov["cms_in_scope"] == 2 and cov["cms_with_schedule_h"] == 1 and cov["cms_without_schedule_h"] == ["tx-450011"]
    assert cov["schedule_h_unmatched"] == ["OID9:2"] and cov["external_unmatched"] == ["row2"]
    assert cov["needs_review"] == ["schedule_h:OID9:1->tx-450010"]
    # deterministic: same inputs, same ids
    again = reconcile(cms, [_ret("OID9", "990009999", hospitals)], read_external_list(ext))
    assert set(again.facilities) == set(u.facilities)


def test_seed_demo_into_registry(registry, demo_corpus) -> None:
    report = seed_demo(registry, demo_corpus.root)
    assert report.cms_in_scope == 10 and report.returns == 8 and report.sections == 8
    assert report.coverage["cms_schedule_h_coverage"] == 1.0 and report.review_tasks == 0
    facs = registry.list_facilities()
    assert len(facs) == 10 and all(f.ein for f in facs)
    lone = [f for f in facs if f.ein == "990100202"]
    assert len(lone) == 3 and {f.schedule_h_group_letter for f in lone} == {"A"}
    assert len(registry.schedule_h_for_ein("990100202")) == 1
    brazos_fap = "https://www.examplebrazosvalley.example/financial-assistance/fap.pdf"
    src = registry.get(Source, id=source_id_for(brazos_fap))
    brazos_ids = [f["facility_id"] for h in demo_corpus.manifest["hospitals"] if h["id"] == "brazos-valley" for f in h["facilities"]]
    assert src is not None and src.discovered_via == "schedule_h_narrative" and src.facility_ids == brazos_ids
    lone_fap = registry.get(Source, id=source_id_for("https://www.lonestarexamplehealth.example/financial-assistance/fap.pdf"))
    assert lone_fap is not None and len(lone_fap.facility_ids) == 3
    # idempotent
    seed_demo(registry, demo_corpus.root)
    assert registry.count(Facility) == 10 and registry.count(Source) == 24 and registry.count(ReviewTask) == 0


def test_zip_and_loose_xml_agree(demo_corpus) -> None:
    from charitycare.seed.pipeline import demo_returns
    from charitycare.seed.schedule_h import parse_file

    for ret in demo_returns(demo_corpus.root):
        loose = parse_file(str(demo_corpus.path(f"irs/xml/{ret.object_id}_public.xml")))
        assert [s.model_dump() for s in loose.sections] == [s.model_dump() for s in ret.sections]


def test_seed_cli_demo(demo_corpus, tmp_path, monkeypatch, capsys) -> None:
    import json

    from charitycare.config import get_settings
    from charitycare.seed.cli import main

    monkeypatch.setenv("CCC_DATA_DIR", str(demo_corpus.root.parent))  # demo_dir = <data>/demo
    get_settings.cache_clear()
    try:
        assert main(["demo", "--db", str(tmp_path / "r.sqlite")]) == 0
    finally:
        get_settings.cache_clear()
    out = json.loads(capsys.readouterr().out)
    assert out["returns"] == 8 and out["coverage"]["cms_schedule_h_coverage"] == 1.0

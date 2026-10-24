"""Classifier, document store, policy mapping, records queue, link health, link scoring."""

from __future__ import annotations

from datetime import date

import pytest

from charitycare import pdftext
from charitycare.discover.classify import (
    DocClassification,
    build_request,
    classify_document,
    detect_language,
    keyword_classify,
)
from charitycare.discover.docstore import DocumentStore, sniff_mime, uri_to_path
from charitycare.discover.linkhealth import check_sources, summarize, to_markdown
from charitycare.discover.links import extract_links, score_link
from charitycare.discover.models import DiscoveredDoc
from charitycare.discover.pipeline import corpus_fetcher, discover_demo
from charitycare.discover.policy import find_name_quote, guess_title, map_facility, parse_effective_date
from charitycare.discover.records import RecordsRequestQueue, render_batch_request
from charitycare.llm import ReplayBackend
from charitycare.schemas import DocumentVersion, Facility, PolicyVersion, ScheduleHFacility, Source
from charitycare.wording import find_banned_wording


def _docs(manifest):
    for h in manifest["hospitals"]:
        for d in h["documents"].values():
            yield h, d


def test_keyword_classifier_on_every_demo_doc(demo_corpus) -> None:
    for _h, d in _docs(demo_corpus.manifest):
        pages = pdftext.page_texts(demo_corpus.path(d["path"]).read_bytes())
        c = keyword_classify("\n".join(p.text for p in pages[:2]), url=d["url"])
        assert c.kind == d["kind"], d["path"]
        if any(p.has_text_layer for p in pages):
            assert c.language == d["language"], d["path"]


def test_classifier_llm_replay_and_fallback(demo_corpus, discovery_fixtures, tmp_path) -> None:
    _h, d = next(_docs(demo_corpus.manifest))
    text = "\n".join(p.text for p in pdftext.page_texts(demo_corpus.path(d["path"]).read_bytes())[:2])
    c = classify_document(d["sha256"], text, url=d["url"], backend=ReplayBackend(discovery_fixtures))
    assert c.method == "llm" and c.kind == d["kind"] and c.cost_usd > 0
    fb = classify_document(d["sha256"], text, url=d["url"], backend=ReplayBackend(tmp_path))  # no fixture
    assert fb.method == "keyword" and fb.kind == d["kind"]
    req = build_request("abc", "", url="u")
    assert req.model_tier == "haiku" and req.effort == "low" and "no text layer" in req.documents[0].data
    assert req.output_schema is DocClassification


def test_keyword_other_and_language() -> None:
    assert keyword_classify("Quarterly newsletter about our new parking garage.").kind == "other"
    assert keyword_classify("", url="https://h.example/billing-and-collections-policy.pdf").kind == "billing_policy"
    assert detect_language("La solicitud de asistencia financiera para su hogar y el ingreso de la familia") == "es"
    assert detect_language("too short") is None


def test_docstore_versions_and_text_layer(demo_corpus, tmp_path, registry) -> None:
    store = DocumentStore(tmp_path / "s", registry, ocr=False)
    piney = demo_corpus.path("docs/pineywoods-regional/fap.pdf").read_bytes()
    a = store.put(piney, source_id="src-1", kind="fap")
    assert a.is_new and a.version.sha256 == pdftext.sha256_bytes(piney)
    assert a.version.pages == 6 and a.version.has_text_layer is False and a.ocr_unavailable_pages == [3]
    assert uri_to_path(a.version.pdf_uri).read_bytes() == piney and uri_to_path(a.version.text_uri).exists()
    again = store.put(piney, source_id="src-1", kind="fap")
    assert not again.is_new and again.version == a.version
    other = demo_corpus.path("docs/example-methodist/fap.pdf").read_bytes()
    b = store.put(other, source_id="src-1", kind="fap")
    assert b.version.has_text_layer is True and b.version.language == "en"
    assert {v.sha256 for v in store.versions_for_source("src-1")} == {a.version.sha256, b.version.sha256}  # history kept
    assert store.get_bytes(a.version.sha256) == piney and store.page_texts(b.version.sha256)[0].page == 1
    html = store.put(b"<!doctype html><html><body><h1>Financial Assistance</h1><p>Call us.</p></body></html>", kind="other")
    assert html.version.mime == "text/html" and html.version.pages is None and "Financial Assistance" in html.text
    assert sniff_mime(b"%PDF-1.4") == "application/pdf" and sniff_mime(b"xx") == "application/octet-stream"
    assert registry.count(DocumentVersion) == 3


def test_effective_date_and_title() -> None:
    assert parse_effective_date("Policy\nEffective date: July 1, 2025\n") == date(2025, 7, 1)
    assert parse_effective_date("Revision date: March 15, 2026 (supersedes the policy dated July 1, 2023)") == date(2026, 3, 15)
    assert parse_effective_date("Approved: 03/04/2024") == date(2024, 3, 4)
    assert parse_effective_date("no date here") is None
    assert guess_title("Example Methodist Hospital\nFinancial Assistance Policy\nEffective date: July 1, 2025", ["EXAMPLE METHODIST HOSPITAL"]) == "Financial Assistance Policy"


def test_name_quote_spans_line_wraps() -> None:
    pages = [(1, "intro"), (7, "covered: Lone Star Example Medical\nCenter Plano; Lone Star Example")]
    assert find_name_quote(pages, "LONE STAR EXAMPLE MEDICAL CENTER PLANO") == (7, "Lone Star Example Medical Center Plano")
    assert find_name_quote(pages, "Nowhere General") is None


def test_mapping_methods() -> None:
    pv = PolicyVersion(id="pv-1", organization_ein="1", as_of=date(2026, 1, 1))
    fap = DiscoveredDoc(url="https://h.example/fap.pdf", kind="fap", discovered_via="crawl", sha256="s" * 64)
    sec = ScheduleHFacility(id="o:A", ein="1", org_name="SYS", object_id="o", tax_year=2024, tax_period_end=date(2024, 12, 31),
                            group_letter="A", facility_names=["NORTH CAMPUS"], line_16a_fap_url="https://h.example/fap.pdf")
    named = map_facility(Facility(id="f1", name="North Campus"), pv, fap, [(2, "applies to North Campus and others")], [sec])
    assert named.method == "named_in_policy" and named.evidence[0].page == 2 and named.confidence == 0.95
    grp = map_facility(Facility(id="f2", name="South Campus", schedule_h_group_letter="A"), pv, fap, [(1, "system policy")], [sec])
    assert grp.method == "schedule_h_group" and grp.confidence == 0.85
    dflt = map_facility(Facility(id="f3", name="East Campus"), pv, fap, [(1, "system policy")], [sec])
    assert dflt.method == "system_default" and dflt.confidence == 0.6


def test_records_queue_and_letter(tmp_path) -> None:
    q = RecordsRequestQueue(tmp_path / "q.json")
    q.enqueue("tx-1", "Alpha Hospital", "991", "blocked", "403")
    q.enqueue("tx-1", "Alpha Hospital", "991", "dead_link", "404")  # idempotent, reason refreshed
    q.enqueue("tx-2", "Beta Hospital", None, "not_found")
    q2 = RecordsRequestQueue(tmp_path / "q.json")  # persisted
    assert [(r.facility_id, r.reason) for r in q2.pending()] == [("tx-1", "dead_link"), ("tx-2", "not_found")]
    q2.resolve("tx-2")
    assert [r.facility_id for r in q2.pending()] == ["tx-1"]
    letter = render_batch_request(q2.pending(), today=date(2026, 10, 24))
    assert "Alpha Hospital (registry id tx-1, EIN 991)" in letter and "Public Information Act" in letter
    assert find_banned_wording(letter) == []


def test_link_health_after_demo_discovery(demo_corpus, discovery_fixtures, tmp_path, registry) -> None:
    rep = discover_demo(registry, demo_corpus.root, store_root=tmp_path / "s", queue_path=tmp_path / "q.json",
                        backend=ReplayBackend(discovery_fixtures))
    assert rep.summary()["policies_found"] == 8
    with corpus_fetcher(demo_corpus.root) as f:
        rows = check_sources(registry, f)
    assert summarize(rows)["by_status"] == {"ok": len(rows)} and len(rows) == 26
    # one link dies, one document changes
    fap_url = "https://www.examplemethodist.example/financial-assistance/fap.pdf"
    pls_url = "https://www.examplehillcountry.example/financial-assistance/summary.pdf"
    new_pdf = demo_corpus.path("docs/brazos-valley/pls_en.pdf").read_bytes()
    with corpus_fetcher(demo_corpus.root) as f:
        f.client._transport.extra = {fap_url: (404, "text/html", b"gone"), pls_url: (200, "application/pdf", new_pdf)}
        store = DocumentStore(tmp_path / "s", registry)
        rows = check_sources(registry, f, store=store)
    by = {r.url: r for r in rows}
    assert by[fap_url].status == "dead" and by[pls_url].status == "changed" and by[pls_url].sha256 != by[pls_url].previous_sha256
    src = registry.select(Source, url=fap_url)[0]
    assert src.http_status == 404 and src.last_checked is not None
    md = to_markdown(rows)
    assert "policy not currently reachable (link no longer works)" in md and find_banned_wording(md) == []


def test_link_scoring_and_extraction() -> None:
    html = b"""<html><body>
      <a href="/financial-assistance/">Financial Assistance</a>
      <a href="/es/ayuda.pdf">Solicitud de Asistencia Financiera (PDF)</a>
      <a href="/careers/charity-care-jobs">Charity care jobs</a>
      <a href="mailto:x@y.org">mail</a><a href="#top">top</a>
      <a href="https://other.example/pay"><img alt="x"></a>
      <a href="/financial-assistance/">dup low</a>
    </body></html>"""
    links = {lk.url: lk for lk in extract_links(html, "https://h.example/patients/")}
    assert set(links) == {"https://h.example/financial-assistance/", "https://h.example/es/ayuda.pdf", "https://h.example/careers/charity-care-jobs", "https://other.example/pay"}
    assert links["https://h.example/financial-assistance/"].anchor_text == "Financial Assistance"
    assert links["https://h.example/es/ayuda.pdf"].score >= 12 and links["https://h.example/es/ayuda.pdf"].is_pdf
    assert links["https://h.example/careers/charity-care-jobs"].score == 0
    assert score_link("Help paying your bill", "https://h.example/billing/") > score_link("About us", "https://h.example/about/")


@pytest.mark.parametrize("cmd", [["records"], ["records", "--letter"]])
def test_cli_records(isolated_env, capsys, cmd) -> None:
    from charitycare.discover.cli import main

    assert main(cmd) == 0
    assert capsys.readouterr().out.strip()


def test_committed_discovery_fixtures_match_generator(discovery_fixtures) -> None:
    """fixtures/llm/{classify_doc,discover_agent}@v1 are what demo_fixtures.write_all produces."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2] / "fixtures" / "llm"
    for sub in ("classify_doc@v1", "discover_agent@v1"):
        generated = sorted(p.relative_to(discovery_fixtures) for p in (discovery_fixtures / sub).rglob("*.json"))
        assert generated, sub
        for rel in generated:
            committed = repo / rel
            assert committed.exists(), f"run `python -m charitycare.discover.demo_fixtures` ({rel})"
            assert committed.read_text() == (discovery_fixtures / rel).read_text(), rel


def test_ingest_of_a_new_fap_version_records_a_change(tmp_path, registry) -> None:
    import pymupdf

    from charitycare.discover.models import DiscoveryResult
    from charitycare.discover.policy import ingest
    from charitycare.schemas import ChangeEvent

    def pdf(text: str) -> bytes:
        doc = pymupdf.open()
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=10)
        return doc.tobytes()

    store = DocumentStore(tmp_path / "s", registry, ocr=False)
    old = pdf("Financial Assistance Policy. Patients at or below 200% FPG qualify for free care.")
    new = pdf("Financial Assistance Policy. Patients at or below 200% FPG do not qualify for free care.")
    for raw in (old, new):
        sha = store.put(raw, kind="fap").version.sha256
        res = DiscoveryResult(target_id="ein:1", ein="990000001", candidates=[
            DiscoveredDoc(url="https://h.example/fap.pdf", kind="fap", discovered_via="crawl", sha256=sha)])
        pv = ingest(res, registry, store)
    assert pv is not None and pv.supersedes
    [ev] = registry.select(ChangeEvent)
    assert ev.policy_version_id == pv.id and ev.triage == "substantive" and ev.published_at is not None

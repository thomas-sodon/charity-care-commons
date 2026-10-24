"""Discovery agent and heuristic crawler against the local server (demo web + edge-case hosts)."""

from __future__ import annotations

from charitycare.discover.agent import AGENT_PROMPT_VERSION, DEFAULT_BUDGET, DiscoveryTools, Target, discover_target
from charitycare.discover.docstore import DocumentStore
from charitycare.discover.models import AgentAction
from charitycare.discover.pipeline import run_discovery, targets_from_registry
from charitycare.discover.records import RecordsRequestQueue
from charitycare.llm import ReplayBackend, Usage, write_fixture
from charitycare.schemas import Facility, PolicyFacilityMap, ReviewTask
from charitycare.seed.pipeline import seed_demo

PINEY = "https://www.examplepineywoods.example"


def _store(tmp_path, registry=None) -> DocumentStore:
    return DocumentStore(tmp_path / "store", registry)


def test_heuristic_crawl_two_clicks_deep_without_seeds(web, tmp_path) -> None:
    t = Target("t-piney", None, ["tx-459906"], ["Example Pineywoods Regional Medical Center"], [], [PINEY + "/"])
    with web.fetcher() as f:
        res = discover_target(t, f, _store(tmp_path), use_agent=False, use_llm_classifier=False)
    kinds = {c.kind for c in res.candidates if c.sha256}
    assert {"fap", "application", "pls"} <= kinds
    fap = res.best("fap")
    assert fap.discovered_via == "crawl" and fap.found_on == PINEY + "/financial-assistance/"
    assert fap.anchor_text == "Financial Assistance Policy (PDF)"
    assert res.pages_visited[:4] == [PINEY + "/", PINEY + "/patients/", PINEY + "/billing/", PINEY + "/financial-assistance/"]
    assert res.tool_calls <= DEFAULT_BUDGET and res.method == "heuristic" and res.reachable
    app = next(c for c in res.candidates if c.kind == "application")
    assert app.classifier == "keyword" and app.classifier_confidence <= 0.6  # scanned form: anchor/URL evidence only


def test_budget_is_enforced(web, tmp_path) -> None:
    t = Target("t-piney", None, ["tx-459906"], ["x"], [], [PINEY + "/"])
    with web.fetcher() as f:
        res = discover_target(t, f, _store(tmp_path), use_agent=False, use_llm_classifier=False, budget=3)
    assert res.tool_calls == 3 and "budget exhausted" in res.notes and not res.reachable
    with web.fetcher() as f:
        tools = DiscoveryTools(t, f, _store(tmp_path), budget=99)
    assert tools.budget == DEFAULT_BUDGET


def test_dead_seed_falls_back_to_wayback_provisional(web, tmp_path, registry) -> None:
    url = "https://dead.example/fa/fap.pdf"
    web.wayback_rows[url] = [["20250301000000", url, "200", "application/pdf", "D1"]]
    registry.upsert_facility(Facility(id="tx-450999", ccn="450999", name="Example Methodist Hospital", ein="990000999"))
    t = Target("ein:990000999", "990000999", ["tx-450999"], ["Example Methodist Hospital"], [(url, "fap", "schedule_h_16a")], ["https://dead.example/"])
    queue = RecordsRequestQueue(tmp_path / "q.json")
    with web.fetcher() as f:
        rep = run_discovery(registry, f, _store(tmp_path, registry), targets=[t], queue=queue, use_agent=False, use_llm_classifier=False)
    res = rep.results[0]
    assert url in res.dead_urls
    arch = res.best("fap")
    assert arch.provisional and arch.discovered_via == "wayback" and arch.captured_at.year == 2025
    assert arch.url == url and arch.archived_url.startswith("https://web.archive.org/web/20250301000000id_/")
    assert not res.reachable and res.status_label.startswith("policy not currently reachable")
    m = registry.select(PolicyFacilityMap, facility_id="tx-450999")[0]
    assert m.method == "named_in_policy" and m.confidence == 0.7  # capped for archived copies
    assert [t.kind for t in registry.select(ReviewTask)] == ["facility_mapping"]
    assert [(r.facility_id, r.reason) for r in queue.pending()] == [("tx-450999", "archived_only")]


def test_blocked_site_is_recorded_and_queued(web, tmp_path, registry) -> None:
    url = "https://blocked.example/fap.pdf"
    t = Target("ein:990000998", "990000998", ["tx-450998"], ["Blocked Hospital"], [(url, "fap", "schedule_h_16a")], ["https://blocked.example/"])
    queue = RecordsRequestQueue(tmp_path / "q.json")
    with web.fetcher() as f:
        rep = run_discovery(registry, f, _store(tmp_path, registry), targets=[t], queue=queue, use_agent=False, use_llm_classifier=False)
    res = rep.results[0]
    assert url in res.blocked_urls and res.candidates[0].blocked and res.candidates[0].sha256 is None
    assert rep.policy_versions == [] and [r.reason for r in queue.pending()] == ["blocked"]
    assert web.requested("blocked.example/fap.pdf") == 1  # never retried


def test_sitemap_fallback(web, tmp_path) -> None:
    t = Target("t-sm", None, ["f1"], ["x"], [], ["https://sitemap.example/"])
    with web.fetcher() as f:
        res = discover_target(t, f, _store(tmp_path), use_agent=False, use_llm_classifier=False)
    fap = res.best("fap")
    assert fap is not None and fap.discovered_via == "sitemap" and fap.url.endswith("/patients/financial-assistance/policy.pdf")


def _replay(fx_root):
    return ReplayBackend(fx_root)


def test_agent_replay_end_to_end(web, tmp_path, registry, demo_corpus, discovery_fixtures) -> None:
    seed_demo(registry, demo_corpus.root)
    queue = RecordsRequestQueue(tmp_path / "q.json")
    with web.fetcher() as f:
        rep = run_discovery(registry, f, _store(tmp_path, registry), queue=queue, backend=_replay(discovery_fixtures))
    s = rep.summary()
    assert s["targets"] == 8 and s["policies_found"] == 8 and s["methods"] == ["agent"]
    assert s["max_tool_calls"] <= DEFAULT_BUDGET and s["llm_cost_usd"] > 0  # simulated cost at section 13 prices
    piney = s["by_target"]["ein:990100404"]
    assert piney["tool_calls"] == 8  # schedule_h_context + 4 page hops + 3 records
    assert s["by_target"]["ein:990100303"]["documents"] == ["application:en", "application:es", "fap:en", "pls:en", "pls:es"]
    maps = registry.select(PolicyFacilityMap)
    assert len(maps) == 10 and {m.method for m in maps} == {"named_in_policy"}
    lone = [m for m in maps if m.facility_id in ("tx-459902", "tx-459903", "tx-459904")]
    assert len({m.policy_version_id for m in lone}) == 1
    # every mapping quote is verbatim on its cited page
    from charitycare.pdftext import find_quote

    store = _store(tmp_path, registry)
    for m in maps:
        ev = m.evidence[0]
        assert find_quote(store.page_texts(ev.doc_sha256)[ev.page - 1].text, ev.quote)
    assert queue.pending() == []
    # classifier ran through replay fixtures
    from charitycare.schemas import DocumentVersion

    dvs = registry.select(DocumentVersion)
    assert len(dvs) == 26 and all(d.source_id for d in dvs)
    pv = registry.policy_for_facility("tx-459905")
    assert set(pv.doc_shas) == {"fap", "application", "application_es", "pls", "pls_es"}


def test_agent_cannot_record_unobserved_url(web, tmp_path, demo_corpus) -> None:
    """Prompt-injection guard: an LLM-chosen URL must have been observed first."""
    t = Target("ein:1", "1", ["tx-459901"], ["Example Methodist Hospital"],
               [("https://www.examplemethodist.example/financial-assistance/fap.pdf", "fap", "schedule_h_16a")],
               ["https://www.examplemethodist.example/"])
    fx = tmp_path / "fx"
    steps = [
        AgentAction(tool="record_candidate", url="https://www.examplemethodist.example/injected.pdf", kind="fap"),
        AgentAction(tool="fetch_page", url="https://evil.example/"),
        AgentAction(tool="record_candidate", url="https://www.examplemethodist.example/financial-assistance/fap.pdf", kind="fap"),
        AgentAction(tool="finish"),
    ]
    for n, a in enumerate(steps):
        key = {"prompt_version": AGENT_PROMPT_VERSION, "doc_sha256": t.key(), "model_tier": "sonnet", "variant": f"step{n:02d}"}
        write_fixture(fx, key, a.model_dump(mode="json"), model="test", usage=Usage(10, 10), synthetic=True)
    with web.fetcher() as f:
        res = discover_target(t, f, _store(tmp_path), backend=ReplayBackend(fx), use_llm_classifier=False)
    assert web.requested("injected.pdf") == 0 and web.requested("evil.example") == 0
    urls = [c.url for c in res.candidates]
    assert "https://www.examplemethodist.example/injected.pdf" not in urls
    assert res.method == "agent+heuristic"  # agent finished without application/pls: heuristic topped up
    assert {"fap", "application", "pls"} <= {c.kind for c in res.candidates if c.sha256}
    assert res.candidates[0].discovered_via == "schedule_h_16a"


def test_agent_missing_fixture_falls_back(web, tmp_path) -> None:
    t = Target("ein:2", "2", ["f"], ["Example Methodist Hospital"],
               [("https://www.examplemethodist.example/financial-assistance/fap.pdf", "fap", "schedule_h_16a")], [])
    with web.fetcher() as f:
        res = discover_target(t, f, _store(tmp_path), backend=ReplayBackend(tmp_path / "empty"), use_llm_classifier=False)
    assert res.method == "heuristic" and any("FixtureMissing" in n for n in res.notes) and res.reachable


def test_targets_group_by_ein(registry, demo_corpus) -> None:
    seed_demo(registry, demo_corpus.root)
    ts = targets_from_registry(registry)
    assert len(ts) == 8
    lone = next(t for t in ts if t.ein == "990100202")
    assert lone.facility_ids == ["tx-459902", "tx-459903", "tx-459904"] and len(lone.seeds) == 3
    assert lone.homepages == ["https://www.lonestarexamplehealth.example/"]
    brazos = next(t for t in ts if t.ein == "990100808")
    assert ("https://www.examplebrazosvalley.example/financial-assistance/fap.pdf", "fap", "schedule_h_narrative") in brazos.seeds


def test_each_target_gets_its_own_allowlist(web, tmp_path) -> None:
    t = Target("t-piney", None, ["tx-459906"], ["Example Pineywoods Regional Medical Center"], [], [PINEY + "/"])
    with web.fetcher() as f:
        before = set(f.allowlist.domains)
        discover_target(t, f, _store(tmp_path), use_agent=False, use_llm_classifier=False)
        assert set(f.allowlist.domains) == before  # the target's domains do not leak to the next target

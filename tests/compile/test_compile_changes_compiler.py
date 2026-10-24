"""Change detection (S9), compile request construction / routing (S4), prompts and the CLI."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pymupdf
import pytest

from charitycare.compile import changes
from charitycare.compile.cli import COMMAND, configure, run
from charitycare.compile.compiler import compile_request, field_schema, reask_request
from charitycare.compile.prompts import compile_system, prompt_digest, reask_user
from charitycare.compile.textlayer import DocText, plan_routing
from charitycare.llm import AnthropicBackend, ReplayBackend, write_fixture
from charitycare.schemas import ChangeEvent, CompiledPolicy, Field, FPGTier
from charitycare.store import Registry
from charitycare.wording import find_banned_wording

BASE = [
    "4.2 Patients with household income at or below 200% of the Federal Poverty Guidelines qualify for free care.\n"
    "Effective date: July 1, 2025\nPage 1 of 2",
    "6.1 Patients may apply within 240 days after the first post-discharge billing statement.\nPage 2 of 2",
]


def make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=10)
    return doc.tobytes()


def dt(pages: list[str]) -> DocText:
    return DocText.from_pdf(make_pdf(pages))


# -- change detection ------------------------------------------------------------------


def test_reflowed_text_is_not_a_change() -> None:
    reflowed = [BASE[0].replace("Poverty Guidelines", "Poverty\nGuidelines").replace("Page 1 of 2", "Page 1 of 3"), BASE[1]]
    d = changes.diff_texts(BASE, reflowed)
    assert d.empty
    assert changes.heuristic_triage(d).label == "cosmetic"


def test_date_only_change() -> None:
    d = changes.diff_texts(BASE, [BASE[0].replace("July 1, 2025", "January 15, 2026"), BASE[1]])
    assert changes.heuristic_triage(d).label == "date_only"


def test_number_change_is_substantive() -> None:
    d = changes.diff_texts(BASE, [BASE[0].replace("200%", "250%"), BASE[1]])
    t = changes.heuristic_triage(d)
    assert t.label == "substantive" and changes.numbers_changed(d)


def test_punctuation_only_is_cosmetic() -> None:
    d = changes.diff_texts(BASE, [BASE[0], BASE[1].replace("post-discharge billing", "post-discharge, billing")])
    assert changes.heuristic_triage(d).label == "cosmetic"


def test_wording_change_defaults_to_substantive() -> None:
    d = changes.diff_texts(BASE, [BASE[0], BASE[1].replace("may apply", "must apply in person")])
    assert changes.heuristic_triage(d).label == "substantive"


def test_detect_change_uses_haiku_fixture_and_guard(tmp_path) -> None:
    old, new = dt(BASE), dt([BASE[0].replace("200%", "250%"), BASE[1]])
    key = {"prompt_version": "change_triage@v1", "doc_sha256": new.sha256, "model_tier": "haiku", "variant": f"prev-{old.sha256[:16]}"}
    write_fixture(tmp_path, key, {"label": "cosmetic", "rationale": "Formatting.", "changed_topics": []}, model="claude-haiku-5-5", synthetic=True)
    res = changes.detect_change(old, new, policy_version_id="pv:2", prev_version_id="pv:1", backend=ReplayBackend(tmp_path))
    assert res.source == "replay"
    assert res.triage.label == "substantive" and "raised to substantive by code" in res.triage.rationale
    ev = res.event
    assert isinstance(ev, ChangeEvent) and ev.triage == "substantive" and ev.published_at is not None
    assert "250%" in ev.diff_summary and not find_banned_wording(ev.diff_summary)
    reg = Registry(":memory:")
    reg.add_change_event(ev)
    assert [e.id for e in reg.changes_since()] == ["chg:pv:2:pv:1"]


def test_negation_change_labelled_cosmetic_is_raised(tmp_path) -> None:
    old = dt(BASE)
    new = dt([BASE[0].replace("qualify for free care", "do not qualify for free care"), BASE[1]])
    key = {"prompt_version": "change_triage@v1", "doc_sha256": new.sha256, "model_tier": "haiku", "variant": f"prev-{old.sha256[:16]}"}
    write_fixture(tmp_path, key, {"label": "cosmetic", "rationale": "Formatting.", "changed_topics": []}, model="claude-haiku-5-5", synthetic=True)
    res = changes.detect_change(old, new, policy_version_id="pv:2", prev_version_id="pv:1", backend=ReplayBackend(tmp_path))
    assert res.triage.label == "substantive" and "negation or modal" in res.triage.rationale
    assert res.event.published_at is not None


def test_detect_change_falls_back_to_heuristic(tmp_path) -> None:
    old, new = dt(BASE), dt([BASE[0].replace("July 1, 2025", "March 1, 2026"), BASE[1]])
    res = changes.detect_change(old, new, policy_version_id="pv:2", prev_version_id="pv:1", backend=ReplayBackend(tmp_path))
    assert res.source == "heuristic" and res.event.triage == "date_only" and res.event.published_at is None


def test_identical_hash_is_no_change() -> None:
    d = dt(BASE)
    res = changes.detect_change(d, d, policy_version_id="pv:1", prev_version_id=None)
    assert res.event is None and res.diff.empty


# -- compile request, routing and caching ------------------------------------------------


def test_routing_sends_image_only_and_table_pages_as_images(fap) -> None:
    pw = plan_routing(fap("pineywoods-regional"))
    assert pw.image_pages == (3,) and 3 not in pw.text_pages
    pu = plan_routing(fap("panhandle-university"))
    assert pu.image_pages == (3,) and 3 in pu.text_pages  # table page: text for quotes + image for layout
    bv = plan_routing(fap("brazos-valley"))
    assert bv.image_pages == () and bv.est_input_tokens == 12_000


def test_compile_request_shape(fap) -> None:
    doc = fap("pineywoods-regional")
    req = compile_request(doc)
    assert req.prompt_version == "compile_policy@v1" and req.model_tier == "sonnet" and req.output_schema is CompiledPolicy
    assert req.doc_sha256 == doc.sha256 and req.effort == "medium"
    assert [p.kind for p in req.documents] == ["text", "image"] and req.documents[1].title == "page 3"
    assert '<page number="3">' not in req.documents[0].data and "Page(s) 3 attached as page images" in req.user
    assert req.system == compile_request(fap("brazos-valley")).system == compile_system()  # static, cacheable prefix


def test_reask_shares_cached_prefix_and_documents(fap) -> None:
    doc = fap("gulf-coast-baptist")
    base = compile_request(doc)
    prev = Field[list[FPGTier]](value=[FPGTier(max_pct_inclusive=250, benefit="free")])
    req = reask_request(doc, "fpg_tiers", prev, ["number not printed in any quote: fpg_tiers[0].max_pct_inclusive = 250"])
    assert req.system == base.system and req.documents == base.documents
    assert req.variant == "fpg_tiers" and req.output_schema is field_schema("fpg_tiers")
    assert "max_pct_inclusive = 250" in req.user and "fpg_tiers" in req.user


def test_live_backend_message_layout_with_fake_client(fap) -> None:
    """The live path sends the static guide and the documents behind cache breakpoints."""
    doc = fap("brazos-valley")
    calls = []

    class Messages:
        def parse(self, **kw):
            calls.append(kw)
            gold = CompiledPolicy.model_validate({k: {"abstain_reason": "not_addressed"} for k in CompiledPolicy.field_keys()})
            return SimpleNamespace(stop_reason="end_turn", parsed_output=gold, usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                                   model="claude-sonnet-5-5", content=[])

    live = AnthropicBackend(client=SimpleNamespace(messages=Messages()))
    res = live.complete(compile_request(doc))
    kw = calls[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["output_format"] is CompiledPolicy
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"][0]["content"][-2]["cache_control"] == {"type": "ephemeral"}
    assert res.output.refund_policy.abstain_reason == "not_addressed" and res.backend == "live"


def test_prompts_are_versioned_and_pinned() -> None:
    sysp = compile_system()
    assert "verbatim" in sysp and "ambiguous_may_clause" in sysp and sysp.count("## ") >= 6
    for key in CompiledPolicy.field_keys():
        assert f"`{key}`" in sysp, key
    assert "{" not in reask_user("refund_policy", "a" * 64, ["x"], {"value": None}).split("```")[0]
    # Changing any prompt text must come with a PROMPT_VERSIONS bump (fixtures are keyed on it).
    assert prompt_digest() == PINNED_DIGEST, "prompt text changed: bump the prompt version and update PINNED_DIGEST"


def test_llm_output_schemas_are_structured_output_compatible() -> None:
    import json

    from anthropic.lib._parse._transform import transform_schema

    from charitycare.compile.adjudicate import Adjudication

    for model in (Adjudication, changes.ChangeTriage, field_schema("fpg_tiers"), field_schema("required_documents")):
        schema = transform_schema(model)
        assert "prefixItems" not in json.dumps(schema)


PINNED_DIGEST = "bee2526c991f9f21a8276ab585e495628aa104894246d0bef2624c7eb433719f"


# -- CLI ----------------------------------------------------------------------------------


def test_cli_demo_and_queue(tmp_path, demo_corpus, compile_fixtures, monkeypatch, capsys) -> None:
    from charitycare import llm

    monkeypatch.setattr(llm, "_backend", ReplayBackend(compile_fixtures))
    p = argparse.ArgumentParser()
    configure(p)
    db = tmp_path / "reg.sqlite"
    assert run(p.parse_args(["--db", str(db), "demo", "--demo-dir", str(demo_corpus.root)])) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 10 and all(o.startswith("{\"facility_id\"") for o in out)
    assert run(p.parse_args(["--db", str(db), "queue", "--limit", "3"])) == 0
    q = capsys.readouterr().out.strip().splitlines()
    assert len(q) == 3 and q[0].startswith("high")
    assert COMMAND[0] == "compile" and COMMAND[2] is configure


def test_cli_unknown_facility_raises(tmp_path) -> None:
    p = argparse.ArgumentParser()
    configure(p)
    from charitycare.compile import CompileError

    with pytest.raises(CompileError):
        run(p.parse_args(["--db", str(tmp_path / "r.sqlite"), "facility", "tx-1"]))

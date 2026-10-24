"""Naive-agent baseline: answer parsing, replay fixtures, and the live path with a fake client."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.baseline import (
    SIMULATED_MODES,
    WEB_SEARCH_TOOL,
    NaiveAgent,
    naive_key,
    naive_request,
    parse_answer,
    write_demo_fixtures,
)
from evals.cases import load_dataset


def test_parse_answer_takes_the_last_json_object() -> None:
    text = 'I found {"note": 1}. Final:\n{"free_care_max_pct_fpl": 200, "discount_max_pct_fpl": 400, "source_url": "https://x"}'
    a = parse_answer(text)
    assert a is not None and a.free_care_max_pct_fpl == 200 and a.discount_max_pct_fpl == 400
    assert parse_answer("no json here") is None
    assert parse_answer('{"free_care_max_pct_fpl": "lots"}') is None


def test_replay_reads_committed_synthetic_fixtures(offline) -> None:
    ds = load_dataset("demo")
    agent = NaiveAgent("replay")
    r = agent.ask(ds.gold("pineywoods-regional"))
    assert r.backend == "replay" and r.synthetic
    assert r.answer is not None and r.answer.free_care_max_pct_fpl == 125.0  # stale Schedule H value
    assert agent.ask(ds.gold("hill-country-community")).answer.free_care_max_pct_fpl is None
    assert r.cost_usd > 0


def test_fixture_writer_is_deterministic(offline, tmp_path: Path) -> None:
    ds = load_dataset("demo")
    paths = write_demo_fixtures(ds, tmp_path)
    assert len(paths) == len(SIMULATED_MODES) == 8
    committed = Path(__file__).resolve().parents[2] / "fixtures" / "llm"
    for p in paths:
        rel = p.relative_to(tmp_path)
        assert (committed / rel).read_text() == p.read_text(), rel
        payload = json.loads(p.read_text())
        assert payload["synthetic"] is True and "SIMULATED" in payload["notes"]


def test_missing_fixture_yields_no_answer(offline, tmp_path: Path) -> None:
    r = NaiveAgent("replay", fixtures_root=tmp_path).ask(load_dataset("demo").gold("brazos-valley"))
    assert r.answer is None and r.note and "no fixture" in r.note


class _FakeMessages:
    def __init__(self, responses: list[Any]):
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return self.responses.pop(0)


def _resp(text: str, stop: str, searches: int = 1) -> Any:
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                            server_tool_use=SimpleNamespace(web_search_requests=searches))
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, usage=usage, model="claude-sonnet-5-5")


def test_live_path_with_web_search_and_pause_turn(offline, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCC_DATA_DIR", str(tmp_path / "data"))  # keep the spend ledger out of the repo
    from charitycare.config import get_settings

    get_settings.cache_clear()
    gold = load_dataset("demo", Path(__file__).resolve().parents[2] / "data" / "demo").gold("brazos-valley")
    msgs = _FakeMessages([_resp("Searching...", "pause_turn"), _resp('Done.\n{"free_care_max_pct_fpl": 200}', "end_turn", 2)])
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    agent = NaiveAgent("record", client=client, fixtures_root=tmp_path / "fx")
    r = agent.ask(gold)
    assert r.answer is not None and r.answer.free_care_max_pct_fpl == 200
    assert len(msgs.calls) == 2
    first = msgs.calls[0]
    assert first["tools"] == [WEB_SEARCH_TOOL] and first["model"] == "claude-sonnet-5-5"
    assert first["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in first["betas"]
    assert msgs.calls[1]["messages"][-1]["role"] == "assistant"
    # 2,000 in / 200 out at Sonnet prices + 3 searches at $0.01
    assert r.cost_usd == pytest.approx(2000 * 2 / 1e6 + 200 * 10 / 1e6 + 0.03)
    key = naive_request(gold).key
    fx = tmp_path / "fx" / key["prompt_version"] / "sonnet" / f"{naive_key('brazos-valley')}.json"
    assert json.loads(fx.read_text())["output"]["free_care_max_pct_fpl"] == 200
    assert agent.ledger.summary()["calls"] == 1


def test_live_refusal_is_no_answer(offline, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCC_DATA_DIR", str(tmp_path / "data"))
    from charitycare.config import get_settings

    get_settings.cache_clear()
    gold = load_dataset("demo", Path(__file__).resolve().parents[2] / "data" / "demo").gold("brazos-valley")
    client = SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages([_resp("", "refusal")])))
    r = NaiveAgent("live", client=client).ask(gold)
    assert r.answer is None and "LLMRefusal" in (r.note or "")

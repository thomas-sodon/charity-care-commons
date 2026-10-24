"""Naive-agent baseline (section 12): same model tier plus web search, no pipeline.

The question is the one a navigator would type: "what is the free-care income
cutoff at <hospital>?". Live, the Sonnet-class model gets the server-side web
search tool and no other help (no corpus, no quote gate, no Schedule H). Its
final answer ends with a JSON object, parsed into :class:`NaiveAnswer`, and is
scored on the free-care cutoff only.

Offline, answers come from replay fixtures keyed by
``(naive_baseline@v1, sha256("naive:<hospital_id>"), sonnet)``. The committed
demo fixtures are **synthetic**: they simulate documented naive-agent failure
modes (stale Schedule H value, discount tier read as the free tier, no answer)
and are labelled as such in each fixture and in every report, so the demo gap
is illustrative, not measured. ``CCC_LLM=record`` replaces them with real runs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic import Field as PField

from charitycare.config import DEFAULT_MODELS, PROMPT_VERSIONS, get_settings
from charitycare.llm import (
    CostLedger,
    FixtureMissing,
    LLMError,
    LLMRefusal,
    LLMRequest,
    LLMResult,
    ReplayBackend,
    Usage,
    cost_usd,
    write_fixture,
)
from evals.cases import Case, Dataset, GoldPolicy
from evals.compare import thresholds

PROMPT_VERSION = PROMPT_VERSIONS["naive_baseline"]
TIER = "sonnet"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
WEB_SEARCH_USD_PER_SEARCH = 0.01  # $10 per 1,000 searches
MAX_CONTINUATIONS = 4

NAIVE_SYSTEM = (
    "You are a careful research assistant helping a hospital financial navigator. Use web search to find the "
    "hospital's current financial assistance (charity care) policy and answer the question. When you are done, end "
    "your reply with one line containing only a JSON object with the keys free_care_max_pct_fpl (number or null), "
    "discount_max_pct_fpl (number or null), source_url (string or null) and quote (string or null, copied from the "
    "source). Use null when you could not find the answer; do not guess."
)


class NaiveAnswer(BaseModel):
    """What the naive agent reports. Percentages are of the federal poverty guideline."""

    model_config = ConfigDict(extra="ignore")
    free_care_max_pct_fpl: float | None = PField(default=None, description="Highest %FPG that qualifies for free care")
    discount_max_pct_fpl: float | None = None
    source_url: str | None = None
    quote: str | None = None


def naive_key(hospital_id: str) -> str:
    """Fixture key for a question that has no source document: hash of the hospital id."""
    return hashlib.sha256(f"naive:{hospital_id}".encode()).hexdigest()


def question(gold: GoldPolicy) -> str:
    return (
        f"What is the household income cutoff, as a percentage of the federal poverty guidelines, for FREE care "
        f"under the financial assistance policy of {gold.name} in Texas? Also give the highest cutoff for "
        f"discounted care if there is one."
    )


def naive_request(gold: GoldPolicy) -> LLMRequest[NaiveAnswer]:
    return LLMRequest(
        prompt_version=PROMPT_VERSION,
        model_tier=TIER,
        doc_sha256=naive_key(gold.hospital_id),
        system=NAIVE_SYSTEM,
        user=question(gold),
        output_schema=NaiveAnswer,
    )


_JSON_OBJ = re.compile(r"\{[^{}]*\}", re.DOTALL)


def parse_answer(text: str) -> NaiveAnswer | None:
    """The last JSON object in the reply, validated; ``None`` if there is none."""
    for raw in reversed(_JSON_OBJ.findall(text or "")):
        try:
            return NaiveAnswer.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            continue
    return None


@dataclass
class NaiveResult:
    answer: NaiveAnswer | None
    cost_usd: float
    backend: str
    note: str | None = None
    synthetic: bool = False


class NaiveAgent:
    """Runs the baseline live (web search) or replays recorded answers."""

    def __init__(self, mode: str | None = None, *, client: Any = None, fixtures_root: Path | None = None):
        s = get_settings()
        self.mode = mode or s.llm_mode
        self.settings = s
        self.root = Path(fixtures_root) if fixtures_root else s.llm_fixtures_dir
        self._client = client
        self.ledger = CostLedger(s.run_token_ceiling, s.daily_spend_cap_usd, s.spend_ledger_path if self.mode != "replay" else None)

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def ask(self, gold: GoldPolicy) -> NaiveResult:
        req = naive_request(gold)
        if self.mode == "replay":
            try:
                res = ReplayBackend(self.root, ledger=self.ledger).complete(req)
            except FixtureMissing as exc:
                return NaiveResult(None, 0.0, "replay", f"no fixture: {exc}")
            synthetic = bool(json.loads(res.fixture_path.read_text(encoding="utf-8")).get("synthetic")) if res.fixture_path else False
            return NaiveResult(res.output, res.cost_usd, "replay", synthetic=synthetic)
        try:
            answer, usage, searches, model = self._live(req)
        except LLMError as exc:
            return NaiveResult(None, 0.0, self.mode, f"{type(exc).__name__}: {exc}")
        cost = cost_usd(TIER, usage) + searches * WEB_SEARCH_USD_PER_SEARCH
        out = answer or NaiveAnswer()
        self.ledger.record(req, LLMResult(out, out.model_dump(), usage, cost, model, "live"))
        if self.mode == "record":
            write_fixture(self.root, req.key, out.model_dump(mode="json"), model=model, usage=usage,
                          notes=f"live naive-agent run with web search ({searches} searches)")
        return NaiveResult(answer, cost, self.mode)

    def _live(self, req: LLMRequest[NaiveAnswer]) -> tuple[NaiveAnswer | None, Usage, int, str]:
        """One web-search conversation; continues ``pause_turn`` and parses the trailing JSON."""
        self.ledger.check(est_tokens=req.max_tokens)
        model = self.settings.model_id(TIER)
        messages: list[dict[str, Any]] = [{"role": "user", "content": req.user}]
        usage, searches, text = Usage(), 0, ""
        for _ in range(MAX_CONTINUATIONS + 1):
            resp = self.client.beta.messages.create(
                model=model,
                max_tokens=req.max_tokens,
                system=req.system,
                messages=messages,
                tools=[WEB_SEARCH_TOOL],
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            u = Usage.from_api(resp.usage)
            usage = Usage(usage.input_tokens + u.input_tokens, usage.output_tokens + u.output_tokens,
                          usage.cache_creation_input_tokens + u.cache_creation_input_tokens,
                          usage.cache_read_input_tokens + u.cache_read_input_tokens)
            stu = getattr(resp.usage, "server_tool_use", None)
            searches += int(getattr(stu, "web_search_requests", 0) or 0)
            text += "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
            if resp.stop_reason == "refusal":
                raise LLMRefusal(f"naive baseline refused for {req.doc_sha256[:12]}")
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages[:1], {"role": "assistant", "content": resp.content}]
        return parse_answer(text), usage, searches, getattr(resp, "model", model)


def attach_naive(cases: list[Case], dataset: Dataset, agent: NaiveAgent) -> list[NaiveResult]:
    """Run the baseline for each case and store its free-care answer on the case."""
    results = []
    for c in cases:
        r = agent.ask(dataset.gold(c.hospital_id))
        c.naive_ran = True
        c.naive_free_cutoff = r.answer.free_care_max_pct_fpl if r.answer else None
        c.naive_cost_usd = r.cost_usd
        results.append(r)
    return results


# --------------------------------------------------------------------------
# Synthetic demo fixtures
# --------------------------------------------------------------------------

# Simulated failure mode per demo hospital (documented naive-agent errors, section 2/4).
SIMULATED_MODES: dict[str, str] = {
    "example-methodist": "correct",
    "lone-star-health": "discount_tier_as_free",
    "valley-childrens": "correct",
    "pineywoods-regional": "stale_schedule_h",
    "gulf-coast-baptist": "correct",
    "hill-country-community": "not_found",
    "panhandle-university": "discount_tier_as_free",
    "brazos-valley": "correct",
}


def simulated_answer(gold: GoldPolicy, mode: str, schedule_h_free_pct: float | None) -> NaiveAnswer:
    t = thresholds(gold.fields)
    free = t["free_cutoff_pct"] if isinstance(t["free_cutoff_pct"], float) else None
    disc = t["discount_cutoff_pct"] if isinstance(t["discount_cutoff_pct"], float) else None
    if mode == "correct":
        return NaiveAnswer(free_care_max_pct_fpl=free, discount_max_pct_fpl=disc)
    if mode == "discount_tier_as_free":
        return NaiveAnswer(free_care_max_pct_fpl=disc, discount_max_pct_fpl=None)
    if mode == "stale_schedule_h":
        return NaiveAnswer(free_care_max_pct_fpl=schedule_h_free_pct, discount_max_pct_fpl=disc)
    return NaiveAnswer()


def write_demo_fixtures(dataset: Dataset, fixtures_root: Path | None = None) -> list[Path]:
    """Write synthetic ``naive_baseline@v1`` replay fixtures for every demo hospital."""
    from charitycare.demo.spec import HOSPITALS

    root = Path(fixtures_root) if fixtures_root else get_settings().llm_fixtures_dir
    specs = {h.id: h for h in HOSPITALS}
    paths = []
    for hid in dataset.hospitals("all"):
        gold = dataset.gold(hid)
        mode = SIMULATED_MODES.get(hid, "not_found")
        sh = specs.get(hid)
        ans = simulated_answer(gold, mode, float(sh.schedule_h.free_pct) if sh else None)
        req = naive_request(gold)
        paths.append(write_fixture(
            root, req.key, ans.model_dump(mode="json"), model=DEFAULT_MODELS[TIER],  # type: ignore[index]
            usage=Usage(input_tokens=18000, output_tokens=900), synthetic=True,
            notes=f"SIMULATED naive-agent answer (mode: {mode}); not a measured model output. Re-record with CCC_LLM=record.",
        ))
    return paths

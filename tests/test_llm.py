from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from charitycare import llm
from charitycare.config import PRICES, get_settings
from charitycare.llm import (
    AnthropicBackend,
    BudgetExceeded,
    CostLedger,
    DocumentPart,
    FixtureMissing,
    LLMRefusal,
    LLMRequest,
    ReplayBackend,
    Usage,
    cost_usd,
    fixture_path,
    write_fixture,
)


class Out(BaseModel):
    answer: str
    n: int


def req(**kw) -> LLMRequest[Out]:
    base = dict(
        prompt_version="test@v1", model_tier="sonnet", doc_sha256="c" * 64, system="static prefix", user="extract",
        output_schema=Out, documents=(DocumentPart("text", "<page n=1>hello</page>", title="fap"),),
    )
    base.update(kw)
    return LLMRequest(**base)


def test_cost_matches_section_13_prices() -> None:
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("sonnet", u) == 12.0
    assert cost_usd("opus", u) == 24.0
    assert cost_usd("haiku", u) == 0.6
    assert cost_usd("sonnet", u, batch=True) == 6.0
    assert cost_usd("sonnet", Usage(cache_read_input_tokens=1_000_000)) == PRICES["sonnet"].cache_read
    assert cost_usd("sonnet", Usage(cache_creation_input_tokens=1_000_000)) == 2.5


def test_replay_round_trip(tmp_path) -> None:
    r = req()
    path = write_fixture(tmp_path, r.key, {"answer": "yes", "n": 3}, model="claude-sonnet-5-5", usage=Usage(1000, 200), synthetic=True)
    assert path == fixture_path(tmp_path, r.key)
    backend = ReplayBackend(tmp_path)
    res = backend.complete(r)
    assert res.output == Out(answer="yes", n=3)
    assert res.backend == "replay" and res.simulated and res.cost_usd == pytest.approx(0.004)
    assert backend.ledger.summary()["simulated_cost_usd"] == pytest.approx(0.004)
    assert backend.ledger.run_cost_usd == 0
    with pytest.raises(FixtureMissing):
        backend.complete(req(variant="other"))
    with pytest.raises(FixtureMissing):
        backend.complete(req(model_tier="opus"))


def test_variant_in_fixture_path(tmp_path) -> None:
    p = fixture_path(tmp_path, req(variant="reask fpg/tiers").key)
    assert p.name == "c" * 64 + ".reask_fpg_tiers.json"
    assert p.parent.name == "sonnet" and p.parent.parent.name == "test@v1"


class FakeMessages:
    def __init__(self, stop_reason: str = "end_turn"):
        self.calls = []
        self.stop_reason = stop_reason

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            parsed_output=kwargs["output_format"](answer="live", n=1),
            content=[],
            model=kwargs["model"],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=9000, cache_read_input_tokens=0),
        )


def test_anthropic_backend_request_shape(isolated_env) -> None:
    fake = SimpleNamespace(messages=FakeMessages())
    backend = AnthropicBackend(client=fake)
    res = backend.complete(req(effort="medium"))
    call = fake.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_format"] is Out
    assert call["output_config"] == {"effort": "medium"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "document" and content[0]["cache_control"] == {"type": "ephemeral"}
    assert content[-1] == {"type": "text", "text": "extract"}
    assert res.output.answer == "live" and res.backend == "live" and not res.simulated
    assert backend.ledger.run_cost_usd == pytest.approx(cost_usd("sonnet", Usage(100, 50, 9000, 0)))
    assert get_settings().spend_ledger_path.exists()


def test_document_blocks() -> None:
    pdf = DocumentPart("pdf", b"%PDF-1.7").to_block()
    assert pdf["source"]["media_type"] == "application/pdf"
    img = DocumentPart("image", b"\x89PNG", media_type="image/png").to_block()
    assert img["type"] == "image"


def test_refusal_raises(isolated_env) -> None:
    backend = AnthropicBackend(client=SimpleNamespace(messages=FakeMessages("refusal")))
    with pytest.raises(LLMRefusal):
        backend.complete(req())


def test_budget_caps(tmp_path) -> None:
    ledger = CostLedger(run_token_ceiling=100, daily_cap_usd=1.0, ledger_path=tmp_path / "spend.json")
    with pytest.raises(BudgetExceeded):
        ledger.check(est_tokens=101)
    ledger.record(req(), llm.LLMResult(Out(answer="a", n=1), {}, Usage(10, 10), 1.5, "m", "live"))
    with pytest.raises(BudgetExceeded):
        ledger.check()


def test_get_backend_modes(isolated_env, monkeypatch) -> None:
    assert isinstance(llm.get_backend(), ReplayBackend)
    assert llm.get_backend() is llm.get_backend()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    get_settings.cache_clear()
    assert get_settings().llm_mode == "live"
    monkeypatch.setenv("CCC_LLM", "replay")
    get_settings.cache_clear()
    assert get_settings().llm_mode == "replay"
    with pytest.raises(ValueError):
        llm.get_backend("bogus")


def _fake_live() -> SimpleNamespace:
    def complete(r):
        return llm.LLMResult(Out(answer="Maria", n=1), {"answer": "Maria", "n": 1}, Usage(10, 10), 0.0, "m", "live")

    return SimpleNamespace(complete=complete, ledger=None)


def test_record_mode_never_persists_household_documents(tmp_path) -> None:
    from charitycare.config import PROMPT_VERSIONS
    from charitycare.packet.synthetic import synthetic_input_shas

    rec = llm.RecordingBackend(live=_fake_live(), fixtures_root=tmp_path)  # type: ignore[arg-type]
    real = req(prompt_version=PROMPT_VERSIONS["extract_bill"], doc_sha256="d" * 64)
    res = rec.complete(real)
    assert res.backend == "live" and res.fixture_path is None and not any(tmp_path.rglob("*.json"))
    demo = req(prompt_version=PROMPT_VERSIONS["extract_income"], doc_sha256=synthetic_input_shas()[1])
    assert rec.complete(demo).backend == "record" and len(list(tmp_path.rglob("*.json"))) == 1
    assert rec.complete(req()).backend == "record"  # non-PII prompts record as before

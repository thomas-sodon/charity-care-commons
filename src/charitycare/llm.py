"""LLM backend abstraction: live Anthropic calls or deterministic replay.

Every LLM step in the pipeline builds an :class:`LLMRequest` and calls
``get_backend().complete(request)``. The result is always a validated Pydantic
object of ``request.output_schema``; the model never decides eligibility, it
only extracts (section 11).

Backends
--------
* :class:`AnthropicBackend` (``CCC_LLM=live``, the default when
  ``ANTHROPIC_API_KEY`` is set): Messages API with structured outputs
  (``client.messages.parse`` + ``output_format=<Pydantic model>``, which the SDK
  sends as ``output_config.format``) and two prompt-cache breakpoints: one after
  the static system prefix, one after the documents, so a targeted re-ask on the
  same document reads it from cache.
* :class:`ReplayBackend` (default without a key): reads recorded fixtures keyed
  by ``(prompt_version, doc_sha256, model_tier[, variant])`` from
  ``fixtures/llm/``. Zero cost, deterministic, offline. A missing fixture raises
  :class:`FixtureMissing` rather than guessing.
* :class:`RecordingBackend` (``CCC_LLM=record``): live call, then writes the
  fixture so later runs replay it.

Fixture layout::

    fixtures/llm/<prompt_version>/<model_tier>/<doc_sha256>[.<variant>].json
    {"key": {...}, "model": "...", "output": {...}, "usage": {...},
     "recorded_at": "...", "synthetic": bool, "notes": "..."}

Cost accounting uses the section 13 prices (:data:`charitycare.config.PRICES`);
replayed calls report what the recorded usage *would* cost live
(``LLMResult.simulated = True``) so eval cost metrics work offline.
"""

from __future__ import annotations

import base64
import functools
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Generic, Literal, Protocol, TypeVar

from pydantic import BaseModel

from charitycare.config import BATCH_DISCOUNT, PII_PROMPTS, PRICES, Settings, get_settings
from charitycare.schemas import ModelTier, utcnow

M = TypeVar("M", bound=BaseModel)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Base class for LLM-step failures; callers fall back to abstention."""


class FixtureMissing(LLMError):
    """No recorded fixture for this request key (replay mode)."""


class LLMRefusal(LLMError):
    """The model declined (``stop_reason == "refusal"``)."""


class LLMTruncated(LLMError):
    """The response hit ``max_tokens`` before the JSON was complete."""


class BudgetExceeded(LLMError):
    """A per-run token ceiling or the daily spend cap would be exceeded."""


# --------------------------------------------------------------------------
# Request / result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentPart:
    """One input document block.

    ``kind="text"`` for born-digital page text (cheap, text-first routing),
    ``"pdf"`` for whole PDFs, ``"image"`` for scanned pages or crops.
    """

    kind: Literal["text", "pdf", "image"]
    data: str | bytes
    title: str | None = None
    media_type: str | None = None  # e.g. "image/png"; defaulted per kind

    def to_block(self) -> dict[str, Any]:
        if self.kind == "text":
            text = self.data if isinstance(self.data, str) else self.data.decode("utf-8")
            block: dict[str, Any] = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text}}
        elif self.kind == "pdf":
            raw = self.data if isinstance(self.data, bytes) else self.data.encode()
            block = {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(raw).decode("ascii")},
            }
        else:
            raw = self.data if isinstance(self.data, bytes) else self.data.encode()
            block = {
                "type": "image",
                "source": {"type": "base64", "media_type": self.media_type or "image/png", "data": base64.b64encode(raw).decode("ascii")},
            }
        if self.title and block["type"] == "document":
            block["title"] = self.title
        return block


@dataclass(frozen=True)
class LLMRequest(Generic[M]):
    """Everything needed to make (or replay) one structured LLM call.

    ``system`` must be static across documents (field guide, schema notes,
    few-shots) so it caches; per-document content goes in ``documents`` and the
    volatile instruction in ``user``.
    """

    prompt_version: str
    model_tier: ModelTier
    doc_sha256: str  # primary document hash; part of the fixture key
    system: str
    user: str
    output_schema: type[M]
    documents: tuple[DocumentPart, ...] = ()
    variant: str = ""  # disambiguates several calls on one doc, e.g. "reask-fpg_tiers"
    max_tokens: int = 16000
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    batch: bool = False  # priced at the Batch API discount

    @property
    def key(self) -> dict[str, str]:
        return {
            "prompt_version": self.prompt_version,
            "doc_sha256": self.doc_sha256,
            "model_tier": self.model_tier,
            "variant": self.variant,
        }


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @classmethod
    def from_api(cls, usage: Any) -> "Usage":
        return cls(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_creation_input_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Usage":
        d = d or {}
        return cls(**{k: int(d.get(k, 0) or 0) for k in asdict(cls())})


def cost_usd(tier: ModelTier, usage: Usage, *, batch: bool = False) -> float:
    """Dollar cost of one call at section 13 list prices."""
    p = PRICES[tier]
    dollars = (
        usage.input_tokens * p.input
        + usage.cache_creation_input_tokens * p.input * p.cache_write_multiplier
        + usage.cache_read_input_tokens * p.cache_read
        + usage.output_tokens * p.output
    ) / 1_000_000
    return round(dollars * (BATCH_DISCOUNT if batch else 1.0), 6)


@dataclass
class LLMResult(Generic[M]):
    output: M
    raw: dict[str, Any]
    usage: Usage
    cost_usd: float
    model: str
    backend: Literal["live", "replay", "record"]
    simulated: bool = False  # True when the cost is what a live call would have cost
    fixture_path: Path | None = None


# --------------------------------------------------------------------------
# Budget / cost ledger
# --------------------------------------------------------------------------


@dataclass
class CostLedger:
    """Accumulates cost per run and enforces the run token ceiling and daily cap.

    Live spend is persisted per UTC day in ``data/llm_spend.json`` so the daily
    cap holds across processes. Replayed (simulated) costs are tracked but never
    count toward the cap.
    """

    run_token_ceiling: int
    daily_cap_usd: float
    ledger_path: Path | None = None
    run_tokens: int = 0
    run_cost_usd: float = 0.0
    simulated_cost_usd: float = 0.0
    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _load_daily(self) -> dict[str, float]:
        if not self.ledger_path or not self.ledger_path.exists():
            return {}
        try:
            return {k: float(v) for k, v in json.loads(self.ledger_path.read_text()).items()}
        except (ValueError, OSError):
            return {}

    def spent_today(self) -> float:
        return self._load_daily().get(date.today().isoformat(), 0.0)

    def check(self, est_tokens: int = 0) -> None:
        """Raise :class:`BudgetExceeded` before a live call that would break a cap."""
        if self.run_tokens + est_tokens > self.run_token_ceiling:
            raise BudgetExceeded(f"run token ceiling {self.run_token_ceiling} reached ({self.run_tokens} used)")
        if self.spent_today() >= self.daily_cap_usd:
            raise BudgetExceeded(f"daily spend cap ${self.daily_cap_usd:.2f} reached; batch jobs paused")

    def record(self, req: LLMRequest[Any], result: LLMResult[Any]) -> None:
        with self._lock:
            self.calls.append(
                {
                    "prompt_version": req.prompt_version,
                    "model_tier": req.model_tier,
                    "backend": result.backend,
                    "tokens": result.usage.total_tokens,
                    "cost_usd": result.cost_usd,
                    "simulated": result.simulated,
                }
            )
            if result.simulated:
                self.simulated_cost_usd += result.cost_usd
                return
            self.run_tokens += result.usage.total_tokens
            self.run_cost_usd += result.cost_usd
            if self.ledger_path:
                daily = self._load_daily()
                today = date.today().isoformat()
                daily[today] = round(daily.get(today, 0.0) + result.cost_usd, 6)
                self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
                self.ledger_path.write_text(json.dumps(daily, indent=2, sort_keys=True))

    def summary(self) -> dict[str, Any]:
        return {
            "calls": len(self.calls),
            "run_tokens": self.run_tokens,
            "run_cost_usd": round(self.run_cost_usd, 6),
            "simulated_cost_usd": round(self.simulated_cost_usd, 6),
        }


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


class LLMBackend(Protocol):
    name: str
    ledger: CostLedger

    def complete(self, request: LLMRequest[M]) -> LLMResult[M]: ...


_SAFE = re.compile(r"[^A-Za-z0-9_.@-]+")


def fixture_path(root: Path, key: dict[str, str]) -> Path:
    """Path of the fixture file for a request key."""
    stem = key["doc_sha256"] + (f".{_SAFE.sub('_', key['variant'])}" if key.get("variant") else "")
    return root / _SAFE.sub("_", key["prompt_version"]) / key["model_tier"] / f"{stem}.json"


def write_fixture(
    root: Path,
    key: dict[str, str],
    output: dict[str, Any],
    *,
    model: str,
    usage: Usage | None = None,
    synthetic: bool = False,
    notes: str | None = None,
) -> Path:
    """Write a replay fixture (used by recording mode and by synthetic fixture generators)."""
    path = fixture_path(root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": key,
        "model": model,
        "output": output,
        "usage": asdict(usage or Usage()),
        "recorded_at": None if synthetic else utcnow().isoformat(),
        "synthetic": synthetic,
        "notes": notes,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


class ReplayBackend:
    """Deterministic backend that serves recorded fixtures."""

    name = "replay"

    def __init__(self, fixtures_root: Path | None = None, ledger: CostLedger | None = None, settings: Settings | None = None):
        s = settings or get_settings()
        self.root = Path(fixtures_root) if fixtures_root else s.llm_fixtures_dir
        self.ledger = ledger or CostLedger(s.run_token_ceiling, s.daily_spend_cap_usd, None)

    def has_fixture(self, request: LLMRequest[Any]) -> bool:
        return fixture_path(self.root, request.key).exists()

    def complete(self, request: LLMRequest[M]) -> LLMResult[M]:
        path = fixture_path(self.root, request.key)
        if not path.exists():
            raise FixtureMissing(f"no replay fixture for {request.key} at {path}; record one with CCC_LLM=record")
        payload = json.loads(path.read_text(encoding="utf-8"))
        output = request.output_schema.model_validate(payload["output"])
        usage = Usage.from_dict(payload.get("usage"))
        result: LLMResult[M] = LLMResult(
            output=output,
            raw=payload["output"],
            usage=usage,
            cost_usd=cost_usd(request.model_tier, usage, batch=request.batch),
            model=payload.get("model", ""),
            backend="replay",
            simulated=True,
            fixture_path=path,
        )
        self.ledger.record(request, result)
        return result


class AnthropicBackend:
    """Live backend using the Anthropic Messages API with structured outputs."""

    name = "live"

    def __init__(self, client: Any | None = None, ledger: CostLedger | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if client is None:
            import anthropic  # imported lazily so replay mode never needs credentials

            client = anthropic.Anthropic()
        self.client = client
        self.ledger = ledger or CostLedger(
            self.settings.run_token_ceiling, self.settings.daily_spend_cap_usd, self.settings.spend_ledger_path
        )

    @staticmethod
    def build_messages(request: LLMRequest[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """System blocks and messages with cache breakpoints after the static prefix and the documents."""
        system = [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}]
        content: list[dict[str, Any]] = [d.to_block() for d in request.documents]
        if content:
            content[-1]["cache_control"] = {"type": "ephemeral"}
        content.append({"type": "text", "text": request.user})
        return system, [{"role": "user", "content": content}]

    def complete(self, request: LLMRequest[M]) -> LLMResult[M]:
        self.ledger.check(est_tokens=request.max_tokens)
        system, messages = self.build_messages(request)
        kwargs: dict[str, Any] = {
            "model": self.settings.model_id(request.model_tier),
            "max_tokens": request.max_tokens,
            "system": system,
            "messages": messages,
            "output_format": request.output_schema,
        }
        if request.effort:
            kwargs["output_config"] = {"effort": request.effort}
        response = self.client.messages.parse(**kwargs)
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise LLMRefusal(f"model refused {request.prompt_version} for {request.doc_sha256[:12]}")
        if stop == "max_tokens":
            raise LLMTruncated(f"max_tokens={request.max_tokens} reached for {request.prompt_version}")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "{}")
            parsed = request.output_schema.model_validate_json(text)
        usage = Usage.from_api(response.usage)
        result: LLMResult[M] = LLMResult(
            output=parsed,
            raw=parsed.model_dump(mode="json"),
            usage=usage,
            cost_usd=cost_usd(request.model_tier, usage, batch=request.batch),
            model=getattr(response, "model", kwargs["model"]),
            backend="live",
        )
        self.ledger.record(request, result)
        return result


class RecordingBackend:
    """Live call followed by writing a replay fixture."""

    name = "record"

    def __init__(self, live: AnthropicBackend | None = None, fixtures_root: Path | None = None):
        self.live = live or AnthropicBackend()
        self.ledger = self.live.ledger
        self.root = Path(fixtures_root) if fixtures_root else self.live.settings.llm_fixtures_dir

    def complete(self, request: LLMRequest[M]) -> LLMResult[M]:
        result = self.live.complete(request)
        if request.prompt_version in PII_PROMPTS and request.doc_sha256 not in synthetic_shas():
            result.backend = "live"  # household data: answered live, never written to disk
            return result
        result.fixture_path = write_fixture(self.root, request.key, result.raw, model=result.model, usage=result.usage)
        result.backend = "record"
        return result


@functools.lru_cache(maxsize=1)
def synthetic_shas() -> frozenset[str]:
    """Input hashes of the synthetic demo documents, the only PII-prompt inputs record mode may persist."""
    from charitycare.packet.synthetic import synthetic_input_shas

    return frozenset(synthetic_input_shas())


_backend_lock = threading.Lock()
_backend: LLMBackend | None = None


def get_backend(mode: str | None = None) -> LLMBackend:
    """Backend for the configured mode: replay unless a key is set or ``CCC_LLM=live|record``.

    Passing ``mode`` returns a fresh backend; otherwise a process-wide singleton.
    """
    global _backend
    if mode is not None:
        return _make_backend(mode)
    with _backend_lock:
        if _backend is None:
            _backend = _make_backend(get_settings().llm_mode)
        return _backend


def reset_backend() -> None:
    """Forget the singleton (tests, or after changing env)."""
    global _backend
    with _backend_lock:
        _backend = None


def _make_backend(mode: str) -> LLMBackend:
    if mode == "live":
        return AnthropicBackend()
    if mode == "record":
        return RecordingBackend()
    if mode == "replay":
        return ReplayBackend()
    raise ValueError(f"unknown LLM mode {mode!r}; expected live, replay or record")

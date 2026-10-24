"""Fixtures for the evals tests. The ``evals`` package lives at the repo root."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replay mode against the committed corpus and fixtures; no key, no network."""
    from charitycare import llm
    from charitycare.config import get_settings

    for var in ("ANTHROPIC_API_KEY", "CCC_LLM", "CCC_DATA_DIR", "CCC_FIXTURES_DIR", "CCC_HOME", "CCC_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    llm.reset_backend()
    yield
    get_settings.cache_clear()
    llm.reset_backend()


def field(value: Any = None, *, quote: str | None = None, page: int = 1, sha: str = "a" * 64,
          confidence: float = 0.9, abstain: str | None = None) -> dict[str, Any]:
    """A Field JSON dict for synthetic predictions."""
    ev = [{"doc_sha256": sha, "page": page, "quote": quote}] if quote else []
    return {"value": None if abstain else value, "evidence": ev, "confidence": confidence, "abstain_reason": abstain}


TIERS = [
    {"min_pct_exclusive": None, "max_pct_inclusive": 200.0, "benefit": "free", "discount_pct": None, "applies_to": "all"},
    {"min_pct_exclusive": 200.0, "max_pct_inclusive": 400.0, "benefit": "percent_discount", "discount_pct": 60.0, "applies_to": "all"},
]
ASSET = {"applies": True, "threshold_usd": 10000.0, "counted_assets": ["checking"], "exclusions": [], "applies_to_tiers": "all"}


def gold_policy() -> dict[str, dict[str, Any]]:
    return {
        "fpg_tiers": field(TIERS, quote="free care up to 200% of the federal poverty guidelines"),
        "asset_test": field(ASSET, quote="liquid assets exceed $10,000 are not considered"),
        "fpg_year_rule": field("current_at_application", quote="guidelines in effect when the application is received"),
        "income_definition": field("gross", quote="household gross income from all sources"),
        "required_documents": field([{"doc_type": "pay_stub", "count": 2, "period_months": None, "description": "two pay stubs"},
                                     {"doc_type": "tax_return", "count": 1, "period_months": None, "description": "tax return"}],
                                    quote="two recent pay stubs and the most recent tax return"),
        "refund_policy": field(None, abstain="not_addressed"),
    }


@pytest.fixture
def make_field():
    return field


@pytest.fixture
def gold() -> dict[str, dict[str, Any]]:
    return gold_policy()

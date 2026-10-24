"""Runtime configuration: paths, model tiers, prices and budget caps.

Everything is overridable by environment variable so tests and CI can point
the pipeline at temporary directories:

* ``CCC_HOME``         repository root (defaults to the checkout containing this file)
* ``CCC_DATA_DIR``     data directory (default ``$CCC_HOME/data``)
* ``CCC_FIXTURES_DIR`` recorded fixtures (default ``$CCC_HOME/fixtures``)
* ``CCC_DB_PATH``      SQLite registry file (default ``$CCC_DATA_DIR/registry.sqlite``)
* ``CCC_LLM``          ``replay`` (default without a key), ``live`` or ``record``
* ``CCC_MODEL_HAIKU`` / ``CCC_MODEL_SONNET`` / ``CCC_MODEL_OPUS``  model id overrides
* ``CCC_DAILY_SPEND_CAP_USD``, ``CCC_RUN_TOKEN_CEILING``  budget caps
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from charitycare.schemas import ModelTier

# Model tiers -> concrete model ids (section 8 routing).
DEFAULT_MODELS: dict[ModelTier, str] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}


@dataclass(frozen=True)
class Price:
    """USD per million tokens. Cache writes (5-minute TTL) cost 1.25x input."""

    input: float
    output: float
    cache_read: float
    cache_write_multiplier: float = 1.25


# Section 13 price assumptions (checked 2026-10-24). Haiku cache read is 0.05x input.
PRICES: dict[ModelTier, Price] = {
    "haiku": Price(input=0.10, output=0.50, cache_read=0.005),
    "sonnet": Price(input=2.00, output=10.00, cache_read=0.10),
    "opus": Price(input=4.00, output=20.00, cache_read=0.20),
}
BATCH_DISCOUNT = 0.5

# Prompt versions shared between the compiler and the recorded fixtures.
# A fixture is keyed by (prompt_version, doc_sha256, model_tier); bump a version
# whenever the prompt text or output schema changes so stale fixtures miss loudly.
PROMPT_VERSIONS: dict[str, str] = {
    "compile_policy": "compile_policy@v1",
    "reask_field": "reask_field@v1",
    "classify_doc": "classify_doc@v1",
    "adjudicate": "adjudicate@v1",
    "change_triage": "change_triage@v1",
    "extract_bill": "extract_bill@v1",
    "extract_income": "extract_income@v1",
    "form_template": "form_template@v1",
    "naive_baseline": "naive_baseline@v1",
    "discover_agent": "discover_agent@v1",
    "readback_crop": "readback_crop@v1",
    "hardship_letter": "hardship_letter@v1",
}

# Packet-stage prompts whose inputs are household documents or narratives (PII). Record
# mode never writes fixtures for them unless the input is a known synthetic demo document.
PII_PROMPTS: frozenset[str] = frozenset(
    PROMPT_VERSIONS[k] for k in ("extract_bill", "extract_income", "hardship_letter", "readback_crop")
)

# Every user-facing determination and page carries this text (section 11).
DISCLAIMER_VERSION = "disclaimer@2026-10-24"
DISCLAIMER_TEXT = (
    "Charity Care Commons provides legal information, not legal advice. 'Likely eligible' is an "
    "estimate based on the hospital's published policy as of {as_of}, quoted above, and the facts "
    "you entered; the hospital makes the final decision. We are not affiliated with any hospital or "
    "the IRS. Confirm details with the hospital. For free full-service help, contact Dollar For."
)


def _repo_root() -> Path:
    env = os.environ.get("CCC_HOME")
    if env:
        return Path(env).resolve()
    # src/charitycare/config.py -> repo root is two levels above the package dir
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. Obtain via :func:`get_settings`."""

    home: Path
    data_dir: Path
    fixtures_dir: Path
    db_path: Path
    llm_mode: str
    models: dict[ModelTier, str] = field(default_factory=dict)
    daily_spend_cap_usd: float = 5.0
    run_token_ceiling: int = 2_000_000
    user_agent: str = "CharityCareCommons/0.1 (+https://charitycarecommons.org; research@charitycarecommons.org)"

    @property
    def demo_dir(self) -> Path:
        return self.data_dir / "demo"

    @property
    def llm_fixtures_dir(self) -> Path:
        return self.fixtures_dir / "llm"

    @property
    def schemas_dir(self) -> Path:
        return self.home / "schemas"

    @property
    def spend_ledger_path(self) -> Path:
        return self.data_dir / "llm_spend.json"

    def model_id(self, tier: ModelTier) -> str:
        return self.models[tier]


def _llm_mode() -> str:
    mode = os.environ.get("CCC_LLM", "").strip().lower()
    if mode in {"live", "replay", "record"}:
        return mode
    return "live" if os.environ.get("ANTHROPIC_API_KEY") else "replay"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings from the environment once; call ``get_settings.cache_clear()`` after changing env."""
    home = _repo_root()
    data_dir = Path(os.environ.get("CCC_DATA_DIR", home / "data")).resolve()
    fixtures_dir = Path(os.environ.get("CCC_FIXTURES_DIR", home / "fixtures")).resolve()
    db_path = Path(os.environ.get("CCC_DB_PATH", data_dir / "registry.sqlite")).resolve()
    models = {tier: os.environ.get(f"CCC_MODEL_{tier.upper()}", mid) for tier, mid in DEFAULT_MODELS.items()}
    return Settings(
        home=home,
        data_dir=data_dir,
        fixtures_dir=fixtures_dir,
        db_path=db_path,
        llm_mode=_llm_mode(),
        models=models,  # type: ignore[arg-type]
        daily_spend_cap_usd=float(os.environ.get("CCC_DAILY_SPEND_CAP_USD", "5.0")),
        run_token_ceiling=int(os.environ.get("CCC_RUN_TOKEN_CEILING", "2000000")),
    )

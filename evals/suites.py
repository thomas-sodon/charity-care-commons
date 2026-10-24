"""YAML suite definitions (``evals/suites/<name>.yaml``)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SUITES_DIR = Path(__file__).resolve().parent / "suites"


class Target(BaseModel):
    """A section 12 target. ``hard`` targets also fail the CI gate."""

    model_config = ConfigDict(extra="forbid")
    min: float | None = None
    max: float | None = None
    hard: bool = False

    def met(self, value: float | None) -> bool | None:
        if value is None:
            return None
        return (self.min is None or value >= self.min - 1e-9) and (self.max is None or value <= self.max + 1e-9)

    def describe(self) -> str:
        parts = ([f">= {self.min:g}"] if self.min is not None else []) + ([f"<= {self.max:g}"] if self.max is not None else [])
        return " and ".join(parts) + (" (hard)" if self.hard else "")


class Bootstrap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resamples: int = 1000
    seed: int = 20261024


class GateConfig(BaseModel):
    """Regression gate vs the baseline metrics file (section 12, CI)."""

    model_config = ConfigDict(extra="forbid")
    max_drop_points: float = 2.0  # any gated metric may not get worse by more than this (percentage points)
    max_cost_rise_pct: float = 25.0
    citation_validity_min: float = 1.0


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    kind: Literal["compile", "engine", "packet"]
    dataset: str = "demo"
    split: str | None = None
    modes: list[Literal["replay", "live", "record"]] = Field(default_factory=lambda: ["replay", "live", "record"])
    naive_baseline: bool = False
    release_gated: bool = False
    est_live_cost_usd: float | None = None
    bootstrap: Bootstrap = Field(default_factory=Bootstrap)
    targets: dict[str, Target] = Field(default_factory=dict)
    gates: GateConfig = Field(default_factory=GateConfig)

    @model_validator(mode="after")
    def _known_metrics(self) -> Suite:
        from evals.scorers import METRICS

        unknown = sorted(set(self.targets) - set(METRICS))
        if unknown:
            raise ValueError(f"suite {self.name}: unknown metrics in targets: {unknown}")
        return self


def suite_names() -> list[str]:
    return sorted(p.stem for p in SUITES_DIR.glob("*.yaml"))


def load_suite(name_or_path: str | Path) -> Suite:
    """Load a suite by name (``smoke``) or by path to a YAML file."""
    p = Path(name_or_path)
    if not p.suffix:
        p = SUITES_DIR / f"{name_or_path}.yaml"
    if not p.is_file():
        raise FileNotFoundError(f"no suite {name_or_path!r}; known: {', '.join(suite_names())}")
    return Suite.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))

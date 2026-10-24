"""CI regression gates (section 12): compare a run with the committed baseline metrics file.

A run fails when

* any gated metric gets worse than the baseline by more than ``max_drop_points``
  percentage points (2 by default; rates and ECE are on a 0-1 scale, so 2 points = 0.02);
* citation validity is below 100% (``citation_validity_min``);
* cost per hospital (or per packet) rises by more than ``max_cost_rise_pct`` (25%);
* a lower-is-better count (verified packets with a mismatch) rises at all;
* a metric present in the baseline is missing from the run;
* a ``hard`` suite target is not met (and, with ``enforce_targets``, any target).

Baselines live in ``evals/baselines/<suite>.json`` and are refreshed from a run
on ``main`` with ``python -m evals gate --suite <s> --update-baseline``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.runner import RunResult
from evals.scorers import METRICS
from evals.suites import GateConfig, Suite

BASELINES_DIR = Path(__file__).resolve().parent / "baselines"


@dataclass(frozen=True)
class GateFailure:
    metric: str
    rule: str
    baseline: float | None
    current: float | None
    message: str


def baseline_path(suite: str, mode: str = "replay", *, for_write: bool = False) -> Path:
    """``<suite>.json`` holds replay (offline) baselines; live runs use ``<suite>.live.json`` once one exists."""
    if mode in ("live", "record"):
        live = BASELINES_DIR / f"{suite}.live.json"
        if for_write or live.exists():
            return live
    return BASELINES_DIR / f"{suite}.json"


def load_baseline(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: float(v) for k, v in data.get("metrics", data).items() if v is not None}


def write_baseline(run: RunResult, path: Path) -> Path:
    gated = {k: v for k, v in run.values().items() if METRICS[k].direction != "info"}
    payload: dict[str, Any] = {
        "suite": run.suite,
        "source_run": run.id,
        "git_sha": run.git_sha,
        "dataset_version": run.dataset_version,
        "mode": run.mode,
        "metrics": dict(sorted(gated.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def check(run: RunResult, baseline: dict[str, float], cfg: GateConfig | None = None, *,
          suite: Suite | None = None, enforce_targets: bool = False) -> list[GateFailure]:
    """All gate failures for ``run`` against ``baseline`` (empty list means pass)."""
    cfg = cfg or (suite.gates if suite else GateConfig())
    current = run.values()
    out: list[GateFailure] = []

    cv = run.metrics.get("citation_validity")
    if cv is not None and cv.n and (cv.value is None or cv.value < cfg.citation_validity_min - 1e-9):
        out.append(GateFailure("citation_validity", "citation_validity_min", baseline.get("citation_validity"), cv.value,
                               f"citation validity {cv.value} < {cfg.citation_validity_min:g}: a published field has an unverifiable quote"))

    for name, base in sorted(baseline.items()):
        spec = METRICS.get(name)
        if spec is None or spec.direction == "info":
            continue
        cur = current.get(name)
        if cur is None:
            out.append(GateFailure(name, "missing", base, None, f"{name} is in the baseline but missing from this run"))
            continue
        if spec.unit == "usd":
            limit = base * (1 + cfg.max_cost_rise_pct / 100)
            if base > 0 and cur > limit + 1e-12:
                out.append(GateFailure(name, "cost_rise", base, cur,
                                       f"{name} rose {100 * (cur / base - 1):.1f}% (> {cfg.max_cost_rise_pct:g}%): ${base:.4f} -> ${cur:.4f}"))
            continue
        if spec.unit == "count":
            worse = cur - base if spec.direction == "lower" else base - cur
            if worse > 0:
                out.append(GateFailure(name, "count_regression", base, cur, f"{name} got worse: {base:g} -> {cur:g}"))
            continue
        if spec.unit == "points":
            drop = base - cur if spec.direction == "higher" else cur - base
        else:
            drop = 100 * ((base - cur) if spec.direction == "higher" else (cur - base))
        if drop > cfg.max_drop_points + 1e-9:
            out.append(GateFailure(name, "metric_drop", base, cur,
                                   f"{name} worsened by {drop:.2f} points (> {cfg.max_drop_points:g}): {base:.4f} -> {cur:.4f}"))

    for name, m in sorted(run.metrics.items()):
        if m.target and m.met is False and (m.hard or enforce_targets):
            if name == "citation_validity" and any(f.metric == name for f in out):
                continue
            out.append(GateFailure(name, "target", baseline.get(name), m.value, f"{name} = {m.value} misses target {m.target}"))
    return out


def format_failures(failures: list[GateFailure]) -> str:
    if not failures:
        return "gate: PASS"
    return "gate: FAIL\n" + "\n".join(f"  - [{f.rule}] {f.message}" for f in failures)

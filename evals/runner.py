"""Suite runner: build units, score every metric with bootstrap CIs, write an eval_run.

``python -m evals run --suite smoke`` compiles the suite's hospitals with the
configured LLM backend (replay fixtures offline), scores them, writes
``evals/runs/<run_id>.json`` and records an :class:`~charitycare.schemas.EvalRun`
row in the registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from charitycare.schemas import EvalRun, utcnow
from evals import scorers as S
from evals.cases import Case, load_dataset
from evals.stats import bootstrap_ci
from evals.suites import Suite

EVALS_DIR = Path(__file__).resolve().parent
RUNS_DIR = EVALS_DIR / "runs"


@dataclass
class MetricResult:
    name: str
    value: float | None
    n: int
    ci_low: float | None = None
    ci_high: float | None = None
    direction: str = "higher"
    unit: str = "rate"
    description: str = ""
    target: str | None = None
    target_min: float | None = None
    target_max: float | None = None
    met: bool | None = None
    hard: bool = False


@dataclass
class RunResult:
    id: str
    suite: str
    kind: str
    dataset: str
    dataset_version: str
    split: str | None
    units: list[str]
    mode: str
    git_sha: str | None
    created_at: str
    metrics: dict[str, MetricResult]
    cost_usd: float = 0.0
    simulated_cost: bool = True
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["metrics"] = {k: asdict(v) for k, v in self.metrics.items()}
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RunResult:
        d = dict(d)
        order = {name: i for i, name in enumerate(S.METRICS)}
        items = sorted(d["metrics"].items(), key=lambda kv: order.get(kv[0], len(order)))
        d["metrics"] = {k: MetricResult(**v) for k, v in items}
        return cls(**d)

    def values(self) -> dict[str, float]:
        return {k: m.value for k, m in self.metrics.items() if m.value is not None}

    def eval_run(self) -> EvalRun:
        return EvalRun(id=self.id, git_sha=self.git_sha, dataset_version=self.dataset_version, suite=self.suite,
                       metrics=self.values(), cost_usd=round(self.cost_usd, 6), created_at=datetime.fromisoformat(self.created_at),
                       notes=list(self.notes), simulated_cost=self.simulated_cost)


def git_sha(cwd: Path | None = None) -> str | None:
    """Current commit, from ``GITHUB_SHA`` or ``git rev-parse``; ``None`` outside a repo or before the first commit."""
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd or EVALS_DIR, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def score_units(suite: Suite, units: Sequence[Any]) -> dict[str, MetricResult]:
    """Score every metric of the suite's kind, with bootstrap CIs over units, and check targets."""
    out: dict[str, MetricResult] = {}
    for spec in S.metrics_for(suite.kind):
        if spec.name in S.BASELINE_METRICS and not suite.naive_baseline:
            continue
        value, n = spec.fn(units)
        ci = bootstrap_ci(units, lambda us, fn=spec.fn: fn(us)[0], resamples=suite.bootstrap.resamples, seed=suite.bootstrap.seed) \
            if spec.unit != "count" and value is not None else None
        t = suite.targets.get(spec.name)
        out[spec.name] = MetricResult(
            name=spec.name,
            value=None if value is None else round(float(value), 6),
            n=n,
            ci_low=ci.low if ci else None,
            ci_high=ci.high if ci else None,
            direction=spec.direction,
            unit=spec.unit,
            description=spec.description,
            target=t.describe() if t else None,
            target_min=t.min if t else None,
            target_max=t.max if t else None,
            met=t.met(value) if t else None,
            hard=bool(t and t.hard),
        )
    return out


def _case_detail(c: Case) -> dict[str, Any]:
    from evals.compare import thresholds

    statuses: dict[str, int] = {}
    for k in c.pred:
        statuses[c.field_status(k)] = statuses.get(c.field_status(k), 0) + 1
    return {
        "hospital_id": c.hospital_id,
        "policy_version_id": c.policy_version_id,
        "error": c.error,
        "status": statuses,
        "thresholds": {"gold": thresholds(c.gold), "pred": thresholds({k: f for k, f in c.pred.items() if c.published(k)})},
        "citation_failures": sorted(k for k, ok in c.citations.items() if not ok),
        "crosschecks": {k: S.primary_result(c, k) for k in sorted(set(c.expected_crosschecks) | set(c.pred_crosschecks))},
        "expected_crosschecks": c.expected_crosschecks,
        "cost_usd": round(c.cost_usd, 6),
        **({"naive_free_cutoff": c.naive_free_cutoff} if c.naive_ran else {}),
    }


def _run_id(suite: str, created: datetime, payload: str) -> str:
    return f"{suite}-{created.strftime('%Y%m%dT%H%M%SZ')}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def run_suite(
    suite: Suite,
    *,
    mode: str | None = None,
    dataset_root: Path | None = None,
    registry_path: str | Path | None = None,
    store: bool = True,
    db_path: str | Path | None = None,
    out_dir: Path | None = RUNS_DIR,
    hospitals: Sequence[str] | None = None,
) -> RunResult:
    """Run one suite end to end.

    ``registry_path`` scores an existing registry instead of compiling; the
    EvalRun row goes to ``db_path`` (default: that registry, else ``CCC_DB_PATH``).
    """
    from charitycare.config import get_settings

    settings = get_settings()
    mode = mode or settings.llm_mode
    if mode not in suite.modes:
        raise ValueError(f"suite {suite.name} does not allow mode {mode!r} (allowed: {suite.modes})")
    if mode in ("live", "record") and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("live evaluation needs ANTHROPIC_API_KEY; use --mode replay for the offline fixtures")
    dataset = load_dataset(suite.dataset, dataset_root)
    notes: list[str] = []
    if dataset.name == "demo":
        notes.append("Dataset is the synthetic demo corpus (fictional hospitals, synthetic gold).")
    details: dict[str, Any] = {}
    units: list[Any]
    cost = 0.0

    if suite.kind == "compile":
        from charitycare.llm import get_backend
        from evals.predict import compile_cases, registry_cases

        ids = list(hospitals or dataset.hospitals(suite.split))
        if registry_path is not None:
            from charitycare.store import Registry

            with Registry(registry_path) as reg:
                cases = registry_cases(reg, dataset, ids)
        else:
            cases = compile_cases(dataset, ids, backend=get_backend(mode))
        if suite.naive_baseline:
            from evals.baseline import NaiveAgent, attach_naive

            results = attach_naive(cases, dataset, NaiveAgent(mode))
            if any(r.synthetic for r in results):
                notes.append("Naive-agent baseline answers are SIMULATED synthetic fixtures (documented failure modes), "
                             "not measured model outputs; re-record with CCC_LLM=record.")
            missing = [c.hospital_id for c, r in zip(cases, results, strict=True) if r.note]
            if missing:
                notes.append(f"Naive baseline had no answer source for: {', '.join(missing)}.")
        units = cases
        cost = sum(c.cost_usd + c.naive_cost_usd for c in cases)
        details["hospitals"] = [_case_detail(c) for c in cases]
        details["per_field"] = S.per_field_table(cases)
        details["crosscheck_reasons"] = S.crosscheck_reason_counts(cases)
        unit_ids = ids
    elif suite.kind == "engine":
        from evals.engine_suite import engine_units

        units = engine_units(dataset.root)
        details["mismatches"] = [u for u in units if not u["agree"]][:50]
        unit_ids = [u["id"] for u in units]
    else:
        from charitycare.llm import get_backend
        from evals.packet_suite import packet_units

        units = packet_units(dataset.root, backend=get_backend(mode))
        cost = sum(u["cost_usd"] for u in units)
        details["units"] = units
        unit_ids = [u["id"] for u in units]

    if mode == "replay" and cost:
        notes.append("Costs are simulated: recorded token usage priced at section 13 list prices.")
    metrics = score_units(suite, units)
    created = utcnow().replace(microsecond=0)
    result = RunResult(
        id=_run_id(suite.name, created, json.dumps({k: m.value for k, m in metrics.items()}, sort_keys=True)),
        suite=suite.name, kind=suite.kind, dataset=dataset.name, dataset_version=dataset.version, split=suite.split,
        units=unit_ids, mode=mode, git_sha=git_sha(settings.home), created_at=created.isoformat(), metrics=metrics,
        cost_usd=round(cost, 6), simulated_cost=mode == "replay", notes=[n for n in notes if n], details=details,
    )
    if out_dir is not None:
        write_run(result, out_dir)
    if store:
        from charitycare.store import Registry

        with Registry(db_path or registry_path or None) as reg:
            reg.add_eval_run(result.eval_run())
    return result


def write_run(result: RunResult, out_dir: Path = RUNS_DIR) -> Path:
    """Write ``<run_id>.json`` and refresh ``latest-<suite>.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result.to_json(), indent=2, sort_keys=True, default=str) + "\n"
    path = out_dir / f"{result.id}.json"
    path.write_text(text, encoding="utf-8")
    (out_dir / f"latest-{result.suite}.json").write_text(text, encoding="utf-8")
    return path


def load_run(path_or_suite: str | Path, runs_dir: Path = RUNS_DIR) -> RunResult:
    """Load a run file, or the latest run of a suite by name."""
    p = Path(path_or_suite)
    if not p.suffix:
        p = runs_dir / f"latest-{path_or_suite}.json"
    return RunResult.from_json(json.loads(p.read_text(encoding="utf-8")))

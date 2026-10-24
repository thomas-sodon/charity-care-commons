"""Eval report (screen 5): JSON for the static site and a Markdown rendering.

``python -m evals report`` reads ``evals/runs/latest-<suite>.json`` for every
suite that has run and writes ``evals/reports/latest.json`` and ``latest.md``.
Every number carries its run id, git SHA and dataset version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from charitycare.config import DISCLAIMER_VERSION
from charitycare.schemas import utcnow
from charitycare.wording import assert_safe_wording
from evals.runner import RUNS_DIR, MetricResult, RunResult, load_run
from evals.suites import suite_names

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
REPORT_VERSION = "eval-report@v1"

_HEADLINE = (
    "numeric_threshold_exact", "threshold_abstention_rate", "categorical_macro_f1", "list_item_f1", "citation_validity",
    "schedule_h_free_pct_agreement", "calibration_ece", "cost_per_hospital_usd", "commons_free_cutoff_accuracy",
    "naive_free_cutoff_accuracy", "naive_baseline_gap_points", "engine_agreement", "readback_fillable_exact",
    "readback_flat_exact", "verified_packets_with_mismatch", "cost_per_packet_usd",
)


def fmt(m: MetricResult, v: float | None) -> str:
    if v is None:
        return "n/a"
    if m.unit == "usd":
        return f"${v:.4f}"
    if m.unit == "count":
        return f"{v:g}"
    if m.unit == "points":
        return f"{v:+.1f} pts"
    if m.unit == "ece":
        return f"{v:.3f}"
    return f"{100 * v:.1f}%"


def _status(m: MetricResult) -> str:
    if m.met is None:
        return "report" if m.target is None else "n/a"
    return "meets target" if m.met else "misses target"


def fmt_target(m: MetricResult) -> str:
    parts = ([f">= {fmt(m, m.target_min)}"] if m.target_min is not None else []) + \
            ([f"<= {fmt(m, m.target_max)}"] if m.target_max is not None else [])
    return " and ".join(parts) + (" (hard gate)" if m.hard and parts else "")


def build_report(runs: list[RunResult]) -> dict[str, Any]:
    """Machine-readable report consumed by the site."""
    suites: dict[str, Any] = {}
    for r in runs:
        entry: dict[str, Any] = {
            "run_id": r.id, "git_sha": r.git_sha, "dataset_version": r.dataset_version, "mode": r.mode,
            "created_at": r.created_at, "kind": r.kind, "split": r.split, "units": len(r.units),
            "cost_usd": r.cost_usd, "simulated_cost": r.simulated_cost, "notes": r.notes,
            "metrics": {k: vars(m) for k, m in r.metrics.items()},
        }
        for key in ("per_field", "crosscheck_reasons"):
            if key in r.details:
                entry[key] = r.details[key]
        if "hospitals" in r.details:
            entry["hospitals"] = [{k: h.get(k) for k in ("hospital_id", "error", "crosschecks", "expected_crosschecks",
                                                         "citation_failures", "naive_free_cutoff", "cost_usd")}
                                  for h in r.details["hospitals"]]
        suites[r.suite] = entry
    headline: dict[str, Any] = {}
    for r in runs:
        for name in _HEADLINE:
            m = r.metrics.get(name)
            if m is not None and name not in headline:
                headline[name] = {"value": m.value, "ci_low": m.ci_low, "ci_high": m.ci_high, "n": m.n, "unit": m.unit,
                                  "suite": r.suite, "run_id": r.id, "target": m.target, "met": m.met}
    return {"version": REPORT_VERSION, "generated_at": utcnow().replace(microsecond=0).isoformat(),
            "disclaimer_version": DISCLAIMER_VERSION, "headline": headline, "suites": suites}


def render_markdown(runs: list[RunResult]) -> str:
    """Human-readable report. Passes the banned-wording guard (section 11)."""
    lines = ["# Charity Care Commons evaluation report", "",
             "Accuracy of the compiled policy data, measured against hand-labelled gold policies (design section 12). "
             "95% confidence intervals come from a bootstrap that resamples whole hospitals, households or forms.", ""]
    for r in runs:
        lines += [f"## Suite `{r.suite}` ({r.kind})", "",
                  f"Run `{r.id}` · git `{(r.git_sha or 'uncommitted')[:12]}` · dataset `{r.dataset_version}` · "
                  f"mode `{r.mode}` · {len(r.units)} units · {r.created_at}", ""]
        lines += [f"> {n}" for n in r.notes] + ([""] if r.notes else [])
        lines += ["| Metric | Value | 95% CI | n | Target | Status |", "|---|---|---|---|---|---|"]
        for m in r.metrics.values():
            ci = f"{fmt(m, m.ci_low)} to {fmt(m, m.ci_high)}" if m.ci_low is not None else ""
            lines.append(f"| {m.name} | {fmt(m, m.value)} | {ci} | {m.n} | {fmt_target(m)} | {_status(m)} |")
        lines.append("")
        reasons = r.details.get("crosscheck_reasons")
        if reasons:
            lines += ["Schedule H cross-checks by result (only `disagree_unexplained` can become a public discrepancy "
                      "for review, after the hospital's right of reply):", ""]
            lines += [f"- `{k}`: {v}" for k, v in reasons.items()] + [""]
        per_field = r.details.get("per_field")
        if per_field:
            lines += ["| Field | Gold answered | Published | Correct | Precision | Recall | Gold abstained | Abstentions matched |",
                      "|---|---|---|---|---|---|---|---|"]
            for k, row in per_field.items():
                p = "n/a" if row["precision"] is None else f"{100 * row['precision']:.0f}%"
                rc = "n/a" if row["recall"] is None else f"{100 * row['recall']:.0f}%"
                lines.append(f"| {k} | {row['gold_answered']} | {row['published']} | {row['correct']} | {p} | {rc} | "
                             f"{row['gold_abstained']} | {row['abstentions_matched']} |")
            lines.append("")
        hosp = r.details.get("hospitals")
        if hosp and any("naive_free_cutoff" in h for h in hosp):
            lines += ["Naive-agent baseline vs the Commons on the free-care cutoff (%FPG):", "",
                      "| Hospital | Gold | Commons | Naive agent |", "|---|---|---|---|"]
            for h in hosp:
                g, p = h["thresholds"]["gold"]["free_cutoff_pct"], h["thresholds"]["pred"]["free_cutoff_pct"]
                lines.append(f"| {h['hospital_id']} | {_t(g)} | {_t(p)} | {_t(h.get('naive_free_cutoff'))} |")
            lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    assert_safe_wording(text)
    return text


def _t(v: Any) -> str:
    if v is None or v == "__abstain__":
        return "abstained" if v else "no answer"
    if v == "__none__":
        return "none"
    return f"{float(v):g}%"


def latest_runs(runs_dir: Path = RUNS_DIR, suites: list[str] | None = None) -> list[RunResult]:
    out = []
    for name in suites or suite_names():
        p = runs_dir / f"latest-{name}.json"
        if p.exists():
            out.append(load_run(p))
    return out


def write_report(runs: list[RunResult], out_dir: Path = REPORTS_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j, md = out_dir / "latest.json", out_dir / "latest.md"
    j.write_text(json.dumps(build_report(runs), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    md.write_text(render_markdown(runs), encoding="utf-8")
    return j, md

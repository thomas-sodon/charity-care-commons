"""CI gates, inter-rater agreement and the schema-compatibility check."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evals.agreement import interrater, label, load_coder_dir
from evals.gates import check, format_failures, load_baseline, write_baseline
from evals.runner import MetricResult, RunResult
from evals.schema_compat import check_fresh, compare
from evals.scorers import METRICS
from evals.suites import GateConfig, Suite, load_suite, suite_names


def _run(**values: float | None) -> RunResult:
    metrics = {}
    for k, v in values.items():
        spec = METRICS[k]
        metrics[k] = MetricResult(k, v, n=10, direction=spec.direction, unit=spec.unit)
    return RunResult(id="r1", suite="smoke", kind="compile", dataset="demo", dataset_version="v", split="smoke",
                     units=["h1"], mode="replay", git_sha=None, created_at="2026-10-24T00:00:00+00:00", metrics=metrics)


BASE = {"numeric_threshold_exact": 0.97, "threshold_abstention_rate": 0.10, "citation_validity": 1.0,
        "cost_per_hospital_usd": 0.20, "calibration_ece": 0.04}


def test_gate_passes_within_tolerance() -> None:
    run = _run(numeric_threshold_exact=0.955, threshold_abstention_rate=0.115, citation_validity=1.0,
               cost_per_hospital_usd=0.24, calibration_ece=0.055)
    assert check(run, BASE) == []


@pytest.mark.parametrize(
    ("override", "rule"),
    [
        ({"numeric_threshold_exact": 0.94}, "metric_drop"),
        ({"threshold_abstention_rate": 0.13}, "metric_drop"),
        ({"calibration_ece": 0.07}, "metric_drop"),
        ({"citation_validity": 0.99}, "citation_validity_min"),
        ({"cost_per_hospital_usd": 0.26}, "cost_rise"),
        ({"numeric_threshold_exact": None}, "missing"),
    ],
)
def test_gate_failures(override, rule) -> None:
    vals = dict(BASE) | override
    failures = check(_run(**vals), BASE)
    assert rule in {f.rule for f in failures}, format_failures(failures)


def test_citation_validity_gate_applies_even_without_baseline() -> None:
    assert [f.rule for f in check(_run(citation_validity=0.5), {})] == ["citation_validity_min"]


def test_count_metrics_may_not_rise_and_info_metrics_are_ignored() -> None:
    run = _run(verified_packets_with_mismatch=1.0, naive_baseline_gap_points=-50.0)
    rules = {f.rule for f in check(run, {"verified_packets_with_mismatch": 0.0, "naive_baseline_gap_points": 40.0})}
    assert rules == {"count_regression"}


def test_hard_and_enforced_targets() -> None:
    suite = Suite(name="x", kind="compile", targets={"list_item_f1": {"min": 0.85}, "engine_agreement": {"min": 1.0, "hard": True}})
    run = _run(list_item_f1=0.8, engine_agreement=0.99)
    run.metrics["list_item_f1"].target, run.metrics["list_item_f1"].met = suite.targets["list_item_f1"].describe(), False
    run.metrics["engine_agreement"].target, run.metrics["engine_agreement"].met = "x", False
    run.metrics["engine_agreement"].hard = True
    assert [f.metric for f in check(run, {}, suite=suite)] == ["engine_agreement"]
    assert {f.metric for f in check(run, {}, suite=suite, enforce_targets=True)} == {"engine_agreement", "list_item_f1"}


def test_custom_gate_config() -> None:
    run = _run(numeric_threshold_exact=0.90)
    assert check(run, {"numeric_threshold_exact": 0.97}, GateConfig(max_drop_points=10)) == []


def test_baseline_round_trip(tmp_path: Path) -> None:
    run = _run(numeric_threshold_exact=0.97, naive_baseline_gap_points=40.0)
    p = write_baseline(run, tmp_path / "b.json")
    assert load_baseline(p) == {"numeric_threshold_exact": 0.97}  # info metrics are not gated


def test_committed_suites_and_baselines_load() -> None:
    assert {"smoke", "full", "test", "engine", "packet"} <= set(suite_names())
    for name in suite_names():
        s = load_suite(name)
        base = Path(__file__).resolve().parents[2] / "evals" / "baselines" / f"{name}.json"
        assert base.exists(), name
        assert set(load_baseline(base)) <= set(METRICS)
        if s.kind == "compile":
            assert s.targets["citation_validity"].hard and s.targets["citation_validity"].min == 1.0
    with pytest.raises(ValueError):
        Suite(name="bad", kind="compile", targets={"no_such_metric": {"min": 1}})


# --------------------------------------------------------------------------
# Inter-rater agreement
# --------------------------------------------------------------------------


def _coder(make_field, refund: str, tiers_max: float, abstain_presumptive: bool) -> dict:
    return {
        "refund_policy": make_field(refund),
        "fpg_tiers": make_field([{"max_pct_inclusive": tiers_max, "benefit": "free"}]),
        "presumptive_eligibility": make_field(abstain="ambiguous_may_clause") if abstain_presumptive
        else make_field({"allowed": True, "criteria": ["SNAP"]}),
    }


def test_interrater_agreement_and_kappa(make_field, tmp_path: Path) -> None:
    keys = ["refund_policy", "fpg_tiers", "presumptive_eligibility"]
    a = {"h1": _coder(make_field, "Refunds over $5 are returned.", 200, True),
         "h2": _coder(make_field, "Overpayments are refunded.", 250, False),
         "h3": _coder(make_field, "No refunds.", 300, False)}
    b = {"h1": _coder(make_field, "refunds over $5 are returned", 200, True),   # same text, different case
         "h2": _coder(make_field, "Overpayments are refunded.", 300, False),     # tier disagreement
         "h3": _coder(make_field, "No refunds.", 300, True)}                     # abstain disagreement
    rep = interrater(a, b, keys)
    assert rep.fields["refund_policy"].agreement == 1.0
    assert rep.fields["fpg_tiers"].agreement == pytest.approx(2 / 3)
    assert rep.fields["presumptive_eligibility"].agreement == pytest.approx(2 / 3)
    assert rep.overall_agreement == pytest.approx(7 / 9)
    assert rep.overall_kappa is not None and 0 < rep.overall_kappa < 1
    assert {(d["hospital_id"], d["field"]) for d in rep.disagreements} == {("h2", "fpg_tiers"), ("h3", "presumptive_eligibility")}
    assert label(make_field(abstain="illegible")) == "ABSTAIN:illegible"
    (tmp_path / "h1.json").write_text(json.dumps({"hospital_id": "h1", "policy": a["h1"]}))
    assert load_coder_dir(tmp_path) == {"h1": a["h1"]}


def test_agreement_on_the_demo_gold_against_itself() -> None:
    root = Path(__file__).resolve().parents[2] / "data" / "demo" / "gold"
    coder = load_coder_dir(root)
    rep = interrater(coder, coder)
    assert rep.overall_agreement == 1.0 and rep.overall_kappa == 1.0 and len(rep.hospitals) == 8


# --------------------------------------------------------------------------
# Schema compatibility
# --------------------------------------------------------------------------


def _schema() -> dict:
    return json.loads((Path(__file__).resolve().parents[2] / "schemas" / "open-fap-schema.json").read_text())


def test_committed_schema_is_fresh_and_self_compatible() -> None:
    p = Path(__file__).resolve().parents[2] / "schemas" / "open-fap-schema.json"
    assert check_fresh(p)
    r = compare(_schema(), _schema())
    assert r.ok and not r.breaking and not r.additive


def test_schema_breaking_changes_detected() -> None:
    old, new = _schema(), _schema()
    del new["$defs"]["Evidence"]["properties"]["section_label"]
    new["$defs"]["Facility"]["required"] = [*new["$defs"]["Facility"].get("required", []), "county"]
    new["$defs"]["Residency"]["properties"]["area_type"]["enum"] = ["counties", "state"]
    new["$defs"]["Facility"]["properties"]["ccn"] = {"type": "integer"}
    r = compare(old, new)
    text = "\n".join(r.breaking)
    assert "Evidence.section_label: property removed" in text
    assert "Facility.county: became required" in text
    assert "enum value removed" in text
    assert "Facility.ccn: shape changed" in text
    assert not r.ok
    bumped = copy.deepcopy(new)
    bumped["version"] = "0.2.0"
    assert compare(old, bumped).ok


def test_schema_additions_are_not_breaking() -> None:
    old, new = _schema(), _schema()
    new["$defs"]["Facility"]["properties"]["npi"] = {"type": "string", "default": None}
    new["$defs"]["Facility"]["properties"]["name"]["description"] = "changed text only"
    r = compare(old, new)
    assert r.ok and not r.breaking and any("npi" in x for x in r.additive)

"""End-to-end offline runs on the committed demo corpus and replay fixtures, plus the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from charitycare.store import Registry
from charitycare.wording import find_banned_wording
from evals.cli import main
from evals.gates import check, load_baseline
from evals.report import build_report, render_markdown, write_report
from evals.runner import load_run, run_suite
from evals.suites import load_suite

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def smoke_run(tmp_path_factory: pytest.TempPathFactory):
    import os

    from charitycare import llm
    from charitycare.config import get_settings

    saved = {k: os.environ.pop(k) for k in ("ANTHROPIC_API_KEY", "CCC_LLM", "CCC_DATA_DIR", "CCC_FIXTURES_DIR") if k in os.environ}
    get_settings.cache_clear()
    llm.reset_backend()
    tmp = tmp_path_factory.mktemp("evals")
    run = run_suite(load_suite("full"), mode="replay", db_path=tmp / "reg.sqlite", out_dir=tmp / "runs")
    yield run, tmp
    os.environ.update(saved)
    get_settings.cache_clear()


def test_full_suite_offline_scores(smoke_run) -> None:
    run, _ = smoke_run
    m = {k: v.value for k, v in run.metrics.items()}
    assert m["citation_validity"] == 1.0  # hard gate: every published quote verifies
    assert m["numeric_threshold_exact"] == 1.0  # the gulf-coast misread was corrected by the re-ask
    assert m["compile_error_rate"] == 0.0
    assert m["threshold_abstention_rate"] > 0  # pineywoods page 3 is image-only: honest OCR gap
    assert m["disagreement_triage_rate"] == 1.0
    assert m["schedule_h_disagree_unexplained_count"] == 1  # example-methodist asset test
    assert m["cost_per_hospital_usd"] > 0 and run.simulated_cost
    assert m["naive_free_cutoff_accuracy"] < m["commons_free_cutoff_accuracy"]
    assert any("SIMULATED" in n for n in run.notes)
    for metric in run.metrics.values():
        if metric.value is not None and metric.unit == "rate":
            assert metric.ci_low is not None and metric.ci_low <= metric.value <= metric.ci_high
    hosp = {h["hospital_id"]: h for h in run.details["hospitals"]}
    assert hosp["pineywoods-regional"]["thresholds"]["pred"]["free_cutoff_pct"] == "__abstain__"
    assert hosp["example-methodist"]["crosschecks"]["asset_test"] == "disagree_unexplained"


def test_run_is_recorded_and_reloadable(smoke_run) -> None:
    run, tmp = smoke_run
    with Registry(tmp / "reg.sqlite") as reg:
        rows = reg.eval_runs("full")
    assert [r.id for r in rows] == [run.id]
    assert rows[0].metrics["citation_validity"] == 1.0
    again = load_run(tmp / "runs" / "latest-full.json")
    assert again.values() == run.values() and list(again.metrics) == list(run.metrics)


def test_offline_run_matches_committed_baseline(smoke_run) -> None:
    run, _ = smoke_run
    assert check(run, load_baseline(REPO / "evals" / "baselines" / "full.json"), suite=load_suite("full")) == []


def test_report_json_and_markdown(smoke_run, tmp_path: Path) -> None:
    run, _ = smoke_run
    rep = build_report([run])
    assert rep["headline"]["citation_validity"]["value"] == 1.0
    assert rep["suites"]["full"]["run_id"] == run.id and "per_field" in rep["suites"]["full"]
    md = render_markdown([run])
    assert "| citation_validity | 100.0% |" in md and "Naive-agent baseline" in md
    assert find_banned_wording(md, ignore_quoted=False) == []
    j, m = write_report([run], tmp_path)
    assert json.loads(j.read_text())["version"] == "eval-report@v1" and m.exists()


def test_engine_and_packet_suites_offline(offline, tmp_path: Path) -> None:
    eng = run_suite(load_suite("engine"), mode="replay", store=False, out_dir=None)
    assert eng.metrics["engine_agreement"].value == 1.0 and eng.metrics["engine_agreement"].n == 300
    pkt = run_suite(load_suite("packet"), mode="replay", store=False, out_dir=None)
    v = {k: x.value for k, x in pkt.metrics.items()}
    assert v["verified_packets_with_mismatch"] == 0 and v["readback_fillable_exact"] == 1.0
    assert v["extraction_field_exact"] == 1.0 and 0 < v["cost_per_packet_usd"] <= 0.10


def test_mode_guards(offline) -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        run_suite(load_suite("smoke"), mode="live", store=False, out_dir=None)


def test_cli_run_gate_report_and_schema(offline, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs, reports = tmp_path / "runs", tmp_path / "reports"
    assert main(["run", "--suite", "smoke", "--mode", "replay", "--no-store", "--out-dir", str(runs), "--report-dir", str(reports)]) == 0
    assert (runs / "latest-smoke.json").exists() and (reports / "latest.md").exists()
    assert main(["gate", "--suite", "smoke", "--runs-dir", str(runs)]) == 0
    assert "gate: PASS" in capsys.readouterr().out
    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps({"metrics": {"cost_per_hospital_usd": 0.01, "numeric_threshold_exact": 1.0}}))
    assert main(["gate", "--suite", "smoke", "--runs-dir", str(runs), "--baseline", str(strict)]) == 1
    assert "cost_rise" in capsys.readouterr().out
    assert main(["gate", "--suite", "smoke", "--runs-dir", str(runs), "--enforce-targets"]) == 1  # ECE / abstention targets
    assert main(["gate", "--suite", "engine", "--runs-dir", str(runs)]) == 2  # no engine run yet
    assert main(["report", "--runs-dir", str(runs), "--out-dir", str(reports)]) == 0
    assert main(["schema-compat", "--base", str(REPO / "schemas" / "open-fap-schema.json")]) == 0
    assert main(["schema-compat", "--base", str(tmp_path / "missing.json")]) == 0
    assert main(["list"]) == 0
    sample = tmp_path / "support.csv"
    assert main(["support-sample", "--suite", "smoke", "--n", "5", "--mode", "replay", "--out", str(sample)]) == 0
    lines = sample.read_text().splitlines()
    assert len(lines) == 6
    judged = sample.read_text().replace(",,\r\n", ",yes,\r\n").replace(",,\n", ",yes,\n")
    sample.write_text(judged)
    assert main(["support-score", str(sample)]) == 0

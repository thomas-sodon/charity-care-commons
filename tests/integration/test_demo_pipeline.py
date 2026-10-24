"""End to end: ``ccc demo`` on the synthetic corpus, through the real seed -> discover -> compile stages."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from charitycare.demo.pipeline import MARIA_FACILITY, DemoReport, run_demo
from charitycare.schemas import CrossCheck, CrossCheckResult, FieldStatus, ReviewDecision, ReviewTask
from charitycare.store import Registry
from charitycare.wording import find_banned_wording


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory: pytest.TempPathFactory) -> DemoReport:
    base = tmp_path_factory.mktemp("demo-run")
    lines: list[str] = []
    rep = run_demo(base / "work", site_dir=base / "site", echo=lines.append)
    assert len(lines) == 10  # one progress line per stage
    return rep


def test_every_output_exists(demo_run: DemoReport) -> None:
    assert demo_run.ok(), {k: str(v) for k, v in demo_run.outputs.items() if not v.exists()}
    assert demo_run.stages["corpus"]["matches_committed"] is True
    assert (demo_run.outputs["site_index"].parent / "hospitals" / f"{MARIA_FACILITY}.html").is_file()


def test_pipeline_stages_chain(demo_run: DemoReport) -> None:
    s = demo_run.stages
    assert s["seed"]["facilities"] == 10 and s["seed"]["schedule_h_coverage"] == 1.0
    assert s["discover"]["policies_found"] == "8/8"
    assert s["compile"]["crosschecks"]["disagree_unexplained"] == 1
    # The three deliberate fixture errors: one corrected by the re-ask, two abstained by the quote gate (plus no-OCR page).
    assert "tx-459907:fpg_tiers" in s["compile"]["reasked"]
    assert {"tx-459901:refund_policy", "tx-459902:required_documents"} <= set(s["compile"]["quote_gate_abstained"])
    assert "tx-459909:mismatch" in s["compile"]["round_trips"]


def test_demo_reviewer_leaves_deliberate_errors_queued(demo_run: DemoReport) -> None:
    rv = demo_run.stages["review"]
    assert rv["left_open"] == {"quote_unverified": 3, "round_trip_mismatch": 1}
    assert rv["gold_mismatches"] == [] and rv["templates_open"] == 0
    with Registry(demo_run.outputs["registry"]) as reg:
        open_kinds = sorted(t.kind for t in reg.open_review_tasks())
        assert open_kinds == ["quote_unverified"] * 3 + ["round_trip_mismatch"]
        decisions = reg.select(ReviewDecision)
        assert decisions and all(d.reviewer.startswith("demo:") for d in decisions)
        forced = [r for pv in {t.field_id.rsplit(":", 1)[0] for t in reg.select(ReviewTask, kind="quote_unverified")}
                  for r in reg.fields_for_policy(pv) if r.status is FieldStatus.abstained]
        assert any(r.field.abstain_reason == "quote_unverified" for r in forced)
        # The Schedule H discrepancy stays public after the field is verified.
        assert [c.field_key for c in reg.select(CrossCheck, result=CrossCheckResult.disagree_unexplained.value)] == ["asset_test"]


def test_maria_screen_and_packet(demo_run: DemoReport) -> None:
    scr, pk = demo_run.stages["screen"], demo_run.stages["packet"]
    assert scr["outcome"] == "likely_free" and scr["provisional_fields_used"] == []
    assert "181.9% FPL ≤ 200%" in scr["arithmetic"] and scr["federal_deadline"] == "2027-06-07"
    assert pk["verified"] is True and pk["readback"] == "13/13 fields match"
    assert pk["statement_date"] == "2026-10-10" and pk["bill_amount"] == "9,400.00"
    text = (demo_run.workdir / "screen" / "determination.txt").read_text(encoding="utf-8")
    assert text.startswith("LIKELY ELIGIBLE FOR FREE CARE") and not find_banned_wording(text)
    doc = pymupdf.open(demo_run.outputs["packet_pdf"])
    assert doc.page_count == pk["pages"] and "1.501(r)-6" in doc[0].get_text()


def test_evals_and_export(demo_run: DemoReport) -> None:
    ev = demo_run.stages["evals"]
    assert ev["full"] == ev["engine"] == ev["packet"] == "ok"
    assert demo_run.stages["export"]["rows"]["fields"] > 150
    import pyarrow.parquet as pq

    t = pq.read_table(demo_run.outputs["parquet"])
    assert t.num_rows == demo_run.stages["export"]["rows"]["fields"]
    assert not any("household" in c or "income" == c for c in t.column_names)
    evals_html = Path(demo_run.outputs["site_index"]).parent / "evals.html"
    assert "full-" in evals_html.read_text(encoding="utf-8")


def test_auto_review_requires_demo_flag(tmp_path: Path) -> None:
    from charitycare.demo.reviewer import auto_review, verify_templates

    with Registry(":memory:") as reg:
        with pytest.raises(PermissionError):
            auto_review(reg, tmp_path)
        with pytest.raises(PermissionError):
            verify_templates(reg, tmp_path)


def test_demo_refuses_to_clear_foreign_directories(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("precious", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_demo(tmp_path, site_dir=tmp_path / "site", echo=lambda _: None)
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "precious"

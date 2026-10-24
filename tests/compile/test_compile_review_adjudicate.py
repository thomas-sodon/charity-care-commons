"""Review gating, spot-check sampling and the advisory adjudicator."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from charitycare.compile import adjudicate as adj
from charitycare.compile.crosscheck import cross_check
from charitycare.compile.demo import policy_version_id, schedule_h_from_spec
from charitycare.compile.fixtures import write_adjudication_fixtures
from charitycare.compile.pipeline import compile_facility
from charitycare.compile.review import (
    FieldContext,
    impact,
    is_spot_checkable,
    plan_tasks,
    promote_spot_checked,
    review_queue,
    spot_check_sample,
    task_impact,
)
from charitycare.config import get_settings
from charitycare.demo.spec import HOSPITALS
from charitycare.llm import ReplayBackend
from charitycare.schemas import (
    CrossCheck,
    CrossCheckResult,
    Evidence,
    Field,
    FieldRecord,
    FieldStatus,
    ReviewTask,
)

SPECS = {h.id: h for h in HOSPITALS}


def _rec(key: str, conf: float, results: list[CrossCheckResult], *, abstain: bool = False, pv: str = "pv:x") -> FieldRecord:
    f = Field[str](value=None if abstain else "v", confidence=conf, abstain_reason="not_addressed" if abstain else None,
                   evidence=[] if abstain else [Evidence(doc_sha256="a" * 64, page=1, quote="some quote here")])
    ccs = [CrossCheck(field_key=key, source="schedule_h", external_ref=f"irs:1|Part V line 13|facility 1#{i}", result=r)
           for i, r in enumerate(results)]
    return FieldRecord(id=f"{pv}:{key}", policy_version_id=pv, key=key, field=f, crosschecks=ccs, as_of=date(2026, 10, 1),
                       status=FieldStatus.abstained if abstain else FieldStatus.provisional)


# -- spot-check sampling ---------------------------------------------------------------


def test_spot_check_sample_is_deterministic_ten_percent() -> None:
    ids = [f"pv:{i}:fpg_tiers" for i in range(95)]
    s = spot_check_sample(ids)
    assert len(s) == 10 and s == spot_check_sample(list(reversed(ids)))
    assert spot_check_sample(["only"]) == ["only"] and spot_check_sample([]) == []


def test_spot_checkable_needs_agreement_and_confidence() -> None:
    assert is_spot_checkable(_rec("residency", 0.95, [CrossCheckResult.agree]))
    assert not is_spot_checkable(_rec("residency", 0.85, [CrossCheckResult.agree]))
    assert not is_spot_checkable(_rec("residency", 0.95, []))
    assert not is_spot_checkable(_rec("residency", 0.95, [CrossCheckResult.agree, CrossCheckResult.disagree_unexplained]))


def test_plan_tasks_priorities() -> None:
    ctxs = [
        FieldContext(_rec("fpg_tiers", 0.97, [CrossCheckResult.disagree_unexplained])),
        FieldContext(_rec("asset_test", 0.95, [CrossCheckResult.agree])),
        FieldContext(_rec("residency", 0.95, [CrossCheckResult.agree])),
        FieldContext(_rec("refund_policy", 0.0, [], abstain=True), forced_abstention=True, problems=("p.6 quote not verified",)),
        FieldContext(_rec("screening_timing", 0.5, [], abstain=True)),
        FieldContext(_rec("policy_title", 0.97, [])),
        FieldContext(_rec("application_window", 0.93, [])),
        FieldContext(_rec("agb_method", 0.95, [CrossCheckResult.disagree_policy_newer_than_filing])),
    ]
    tasks = {t.id: t for t in plan_tasks(ctxs)}
    assert tasks["rt:crosscheck_disagreement:pv:x:fpg_tiers"].priority == "high"
    assert "rt:field_verify:pv:x:fpg_tiers" not in tasks  # the disagreement task covers it
    assert tasks["rt:crosscheck_disagreement:pv:x:agb_method"].priority == "medium"
    assert tasks["rt:quote_unverified:pv:x:refund_policy"].priority == "medium"
    assert "confirm abstention" in tasks["rt:field_verify:pv:x:screening_timing"].notes
    assert tasks["rt:field_verify:pv:x:application_window"].priority == "high"
    assert tasks["rt:field_verify:pv:x:policy_title"].priority == "low"
    spot = [t for t in tasks.values() if "spot-check sample" in t.notes]
    assert len(spot) == 1 and "batch: asset_test,residency" in spot[0].notes
    unsampled = {"asset_test", "residency"} - {spot[0].field_id.split(":")[-1]}
    assert all(f"rt:field_verify:pv:x:{k}" not in tasks for k in unsampled)


def test_impact_scores() -> None:
    assert impact("crosscheck_disagreement", "fpg_tiers", 3) > impact("crosscheck_disagreement", "policy_title", 1)
    assert impact("field_verify", "fpg_tiers", 1, 0.5) > impact("field_verify", "fpg_tiers", 1, 0.99)
    assert task_impact(ReviewTask(id="t", kind="field_verify", notes="[impact 42] x")) == 42
    assert task_impact(ReviewTask(id="t", kind="field_verify")) == 0


def test_spot_check_promotion_after_approval(demo_registry, backend) -> None:
    reg = demo_registry
    compile_facility(reg, "tx-459905", backend=backend)
    pv = policy_version_id("valley-childrens")
    batch = [r for r in reg.fields_for_policy(pv) if is_spot_checkable(r)]
    spot = [t for t in reg.select(ReviewTask, kind="field_verify") if "spot-check sample" in (t.notes or "") and t.field_id.startswith(pv)]
    assert len(batch) >= 4 and len(spot) == 1
    assert promote_spot_checked(reg, pv, "reviewer@example.org") == []  # sample still open
    reg.update_review_task(spot[0].id, "approved")
    reg.set_field_status(spot[0].field_id, FieldStatus.verified, reviewer="reviewer@example.org")
    promoted = promote_spot_checked(reg, pv, "reviewer@example.org")
    assert len(promoted) == len(batch) - 1
    for fid in promoted:
        r = reg.get(FieldRecord, id=fid)
        assert r.status is FieldStatus.verified and r.reviewer == "spot-check:reviewer@example.org"
    assert all(t.id != spot[0].id for t in review_queue(reg))


# -- adjudicator ---------------------------------------------------------------------------


def test_schedule_h_context_lines() -> None:
    ctx = adj.schedule_h_context(schedule_h_from_spec(SPECS["example-methodist"]))
    assert "line 13a free_pct = 200" in ctx and "line 13c asset_level = False" in ctx
    assert "line 22 agb_method = lookback_medicare_private" in ctx
    assert ctx[1].startswith("section A facility_names = EXAMPLE METHODIST HOSPITAL")


def _setup(gold_policy, fap, hid: str = "example-methodist"):
    pol, doc, sec = gold_policy(hid), fap(hid), schedule_h_from_spec(SPECS[hid])
    return pol, doc, sec, cross_check(pol, sec)


def test_replayed_adjudication_validates(gold_policy, fap, backend) -> None:
    pol, doc, sec, checks = _setup(gold_policy, fap)
    rec = adj.adjudicate("asset_test", field_value=pol.asset_test, checks=checks["asset_test"], section=sec, doc=doc,
                         docs={doc.sha256: doc}, backend=backend)
    assert rec.valid and rec.source == "replay" and rec.cost_usd > 0
    assert rec.adjudication.recommended_result is CrossCheckResult.disagree_unexplained
    assert "discrepancy for review" in rec.summary()


def test_missing_fixture_falls_back_to_code_draft(gold_policy, fap, tmp_path) -> None:
    pol, doc, sec, checks = _setup(gold_policy, fap, "pineywoods-regional")
    rec = adj.adjudicate("fpg_tiers", field_value=pol.fpg_tiers, checks=checks["fpg_tiers"], section=sec, doc=doc,
                         docs={doc.sha256: doc}, backend=ReplayBackend(tmp_path))
    # gold quotes are on the image-only page: no verifiable policy quote, so no draft at all
    assert not rec.valid and rec.adjudication is None and "FixtureMissing" in rec.problems[0]
    rec = adj.adjudicate("agb_method", field_value=pol.agb_method, checks=checks["agb_method"], section=sec, doc=doc,
                         docs={doc.sha256: doc}, backend=ReplayBackend(tmp_path))
    assert rec.valid and rec.source == "heuristic" and rec.adjudication.schedule_h_quote.startswith("line 22 ")


def test_validate_rejects_unquoted_or_loaded_drafts(gold_policy, fap) -> None:
    pol, doc, sec, _ = _setup(gold_policy, fap)
    ctx = adj.schedule_h_context(sec)
    good = adj.Adjudication(subject="asset_test", recommended_result=CrossCheckResult.disagree_unexplained,
                            policy_evidence=pol.asset_test.evidence[0], schedule_h_quote="line 13c asset_level = False",
                            rationale="Discrepancy for review.", confidence=0.8)
    docs = {doc.sha256: doc}
    assert adj.validate(good, docs, ctx) == []
    assert adj.validate(good.model_copy(update={"schedule_h_quote": "asset box unchecked"}), docs, ctx)
    bad_quote = good.policy_evidence.model_copy(update={"quote": "The hospital ignores assets entirely when deciding."})
    assert adj.validate(good.model_copy(update={"policy_evidence": bad_quote}), docs, ctx)
    assert adj.validate(good.model_copy(update={"rationale": "This is a violation."}), docs, ctx)


def test_subjects_for() -> None:
    hi = Field[str](value="x", confidence=0.95, evidence=[])
    lo = Field[str](value="x", confidence=0.6, evidence=[])
    checks = {"a": [CrossCheck(field_key="a", source="schedule_h", external_ref="r", result=CrossCheckResult.disagree_unexplained)]}
    assert adj.subjects_for({"a": hi, "b": lo, "c": hi}, checks, multi_facility=False) == ["a", "b"]
    assert adj.subjects_for({"c": hi}, {}, multi_facility=True) == ["facilities_covered"]


def test_committed_adjudication_fixtures_match_generator(tmp_path: Path, demo_corpus) -> None:
    import shutil

    root = tmp_path / "llm"
    shutil.copytree(demo_corpus.fixtures, root)
    write_adjudication_fixtures(root, demo_corpus.root)
    committed = get_settings().llm_fixtures_dir / "adjudicate@v1"
    generated = root / "adjudicate@v1"
    names = sorted(p.relative_to(generated).as_posix() for p in generated.rglob("*.json"))
    assert names == sorted(p.relative_to(committed).as_posix() for p in committed.rglob("*.json"))
    for n in names:
        assert json.loads((generated / n).read_text()) == json.loads((committed / n).read_text()), n

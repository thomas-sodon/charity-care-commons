"""End-to-end compile over the demo corpus with replay fixtures: the deliberate errors get caught."""

from __future__ import annotations

import json
from typing import Any

import pytest

from charitycare.compile import CompileError, compile_facility, compile_policy_version
from charitycare.compile.demo import policy_version_id
from charitycare.compile.pipeline import (
    default_pdf_loader,
    policy_from_records,
    typed_field,
)
from charitycare.compile.review import review_queue
from charitycare.compile.textlayer import DocText
from charitycare.compile.verify import check_field
from charitycare.llm import FixtureMissing, LLMRefusal, LLMResult, ReplayBackend, Usage
from charitycare.schemas import (
    CompiledPolicy,
    CrossCheckResult,
    DocumentVersion,
    Evidence,
    FieldRecord,
    FieldStatus,
    PolicyFacilityMap,
    ReviewTask,
)
from charitycare.wording import find_banned_wording


def compile_all(reg, backend) -> dict[str, Any]:
    reports = {}
    for fac in reg.list_facilities(state=None):
        reports[fac.id] = compile_facility(reg, fac.id, backend=backend)
    return reports


def rec(reg, hospital_id: str, key: str) -> FieldRecord:
    r = reg.get_field(policy_version_id(hospital_id), key)
    assert r is not None
    return r


@pytest.fixture
def compiled(demo_registry, backend):
    return demo_registry, compile_all(demo_registry, backend)


def test_no_quote_no_publish_invariant(compiled, demo_corpus, gold) -> None:
    """Every non-abstained stored field has >= 1 evidence item and every quote verifies."""
    reg, _ = compiled
    docs = {}
    for f in reg.select(FieldRecord):
        if f.status is FieldStatus.abstained:
            assert f.field.value is None
            continue
        sha = f.field.evidence[0].doc_sha256
        if sha not in docs:
            docs[sha] = DocText.from_pdf(default_pdf_loader(reg.get_document_version(sha)), sha)
        assert f.field.evidence, f.id
        assert check_field(f.key, typed_field(f), docs).ok, f.id
        assert all(e.char_start is not None and e.char_end is not None for e in f.field.evidence)
    assert reg.count(FieldRecord) == 8 * len(CompiledPolicy.field_keys())


def test_code_never_marks_fields_verified(compiled) -> None:
    reg, _ = compiled
    assert {f.status for f in reg.select(FieldRecord)} <= {FieldStatus.provisional, FieldStatus.abstained}


def test_paraphrased_quote_is_reasked_then_abstained(compiled) -> None:
    reg, reports = compiled
    r = reports["tx-459901"]
    out = r.fields["refund_policy"]
    assert out.reasked and out.forced_abstention and out.reask_source.startswith("unavailable")
    stored = rec(reg, "example-methodist", "refund_policy")
    assert stored.status is FieldStatus.abstained and stored.field.value is None and stored.field.evidence == []
    task = reg.get(ReviewTask, id=f"rt:quote_unverified:{stored.id}")
    assert task.state == "open" and "not_found" in task.notes and task.priority == "medium"


def test_wrong_page_citation_is_caught_and_located(compiled) -> None:
    reg, reports = compiled
    out = reports["tx-459902"].fields["required_documents"]
    assert out.forced_abstention and any("found verbatim on page(s) 5" in p for p in out.problems)
    assert rec(reg, "lone-star-health", "required_documents").status is FieldStatus.abstained
    assert out.candidate is not None and out.candidate.value  # kept for the reviewer, never published


def test_misread_tier_is_corrected_by_targeted_reask(compiled, gold_policy) -> None:
    reg, reports = compiled
    out = reports["tx-459907"].fields["fpg_tiers"]
    assert out.reasked and out.reask_source == "replay" and not out.forced_abstention
    stored = rec(reg, "gulf-coast-baptist", "fpg_tiers")
    assert stored.status is FieldStatus.provisional and stored.prompt_version == "reask_field@v1"
    assert typed_field(stored).value == gold_policy("gulf-coast-baptist").fpg_tiers.value
    assert [c.result for c in stored.crosschecks] == [CrossCheckResult.agree]


def test_image_only_page_abstains_as_illegible(compiled) -> None:
    reg, reports = compiled
    stored = rec(reg, "pineywoods-regional", "fpg_tiers")
    assert stored.field.abstain_reason == "illegible" and stored.status is FieldStatus.abstained
    r = reports["tx-459906"]
    assert r.routing.image_pages == (3,)
    # the unverified candidate's Schedule H comparison is kept for the reviewer only
    assert [c.result for c in r.candidate_crosschecks["fpg_tiers"]] == [CrossCheckResult.disagree_policy_newer_than_filing]
    task = reg.get(ReviewTask, id=f"rt:quote_unverified:{stored.id}")
    assert task.priority == "high" and "disagree_policy_newer_than_filing" in task.notes


def test_model_abstentions_are_kept(compiled) -> None:
    reg, _ = compiled
    may = rec(reg, "gulf-coast-baptist", "presumptive_eligibility")
    assert may.field.abstain_reason == "ambiguous_may_clause" and may.field.evidence  # "hospital discretion, see quote"
    for key in ("refund_policy", "screening_timing", "determination_notice"):
        assert rec(reg, "hill-country-community", key).field.abstain_reason == "not_addressed"


def test_schedule_h_cross_checks_stored(compiled, gold) -> None:
    reg, reports = compiled
    asset = rec(reg, "example-methodist", "asset_test")
    assert [c.result for c in asset.crosschecks] == [CrossCheckResult.disagree_unexplained]
    disc = reg.discrepancies()
    assert [c.field_id for c in disc] == [asset.id]
    assert reports["tx-459901"].fields["asset_test"].reasked  # cross-check triggered re-ask (no fixture -> kept)
    for hid in ("valley-childrens", "hill-country-community", "panhandle-university", "brazos-valley", "gulf-coast-baptist"):
        expected = gold(hid)["schedule_h"]["expected_crosschecks"]
        for key, result in expected.items():
            assert [c.result.value for c in rec(reg, hid, key).crosschecks] == [result], (hid, key)


def test_round_trip_mismatch_opens_high_priority_task(compiled) -> None:
    reg, reports = compiled
    assert [rt.result for rt in reports["tx-459909"].round_trips] == ["mismatch"]
    assert [rt.result for rt in reports["tx-459901"].round_trips] == ["match"]
    fid = rec(reg, "panhandle-university", "fpg_tiers").id
    task = reg.get(ReviewTask, id=f"rt:round_trip_mismatch:{fid}")
    assert task.priority == "high" and "2025 guidelines" in task.notes
    assert reg.get(ReviewTask, id=f"rt:round_trip_mismatch:{rec(reg, 'example-methodist', 'fpg_tiers').id}") is None


def test_adjudication_is_advisory_and_quotes_both_sources(compiled) -> None:
    reg, reports = compiled
    [a] = reports["tx-459901"].adjudications
    assert a.subject == "asset_test" and a.source == "replay" and a.valid
    assert a.adjudication.schedule_h_quote == "line 13c asset_level = False"
    # advisory: the stored cross-check result is the code's, the adjudication only annotates the task
    task = reg.get(ReviewTask, id=f"rt:crosscheck_disagreement:{rec(reg, 'example-methodist', 'asset_test').id}")
    assert "adjudicator (replay)" in task.notes
    # multi-facility policy: the deliberately bad fixture is discarded, a code draft replaces it
    [b] = reports["tx-459902"].adjudications
    assert b.subject == "facilities_covered" and b.source == "heuristic" and b.valid
    assert any("not a line of the supplied context" in p for p in b.problems)


def test_facility_mappings(compiled) -> None:
    reg, reports = compiled
    maps = {m.facility_id: m for m in reg.select(PolicyFacilityMap)}
    assert len(maps) == 10
    assert {m.policy_version_id for f, m in maps.items() if f in ("tx-459902", "tx-459903", "tx-459904")} == {policy_version_id("lone-star-health")}
    assert all(m.method == "named_in_policy" and m.confidence == 0.95 and m.evidence for m in maps.values())
    assert reports["tx-459903"].cached and reports["tx-459904"].cached
    assert reg.policy_for_facility("tx-459903").id == policy_version_id("lone-star-health")


def test_review_queue_sorted_by_priority_then_impact(compiled) -> None:
    reg, _ = compiled
    q = review_queue(reg)
    order = {"high": 0, "medium": 1, "low": 2}
    assert [order[t.priority] for t in q] == sorted(order[t.priority] for t in q)
    assert q[0].kind == "crosscheck_disagreement"
    spot = [t for t in q if "spot-check sample" in (t.notes or "")]
    assert spot and all(t.priority == "low" for t in spot)
    for t in q:
        assert not find_banned_wording(t.notes or "")


def test_document_level_checks(compiled) -> None:
    reg, reports = compiled
    assert [c.result for c in reports["tx-459910"].document_checks] == [CrossCheckResult.agree] * 3  # 16a via Section C
    assert any(c.external_ref.endswith("line 16i|facility 1") for c in reports["tx-459905"].document_checks)


def test_recompile_is_idempotent_and_keeps_human_review(demo_registry, backend) -> None:
    reg = demo_registry
    pv = policy_version_id("valley-childrens")
    first = compile_policy_version(reg, pv, backend=backend)
    assert not first.cached
    assert compile_policy_version(reg, pv, backend=backend).cached
    fid = FieldRecord.make_id(pv, "residency")
    reg.set_field_status(fid, FieldStatus.verified, reviewer="reviewer@example.org")
    task_id = f"rt:field_verify:{FieldRecord.make_id(pv, 'policy_title')}"
    reg.update_review_task(task_id, "approved")
    again = compile_policy_version(reg, pv, backend=backend, force=True)
    assert reg.get_field(pv, "residency").status is FieldStatus.verified
    assert again.fields["residency"].status is FieldStatus.verified
    assert reg.get(ReviewTask, id=task_id).state == "approved"
    assert reg.count(FieldRecord) == len(CompiledPolicy.field_keys())


class _Fabricating(ReplayBackend):
    """A model (or an injected document) that invents a quote: it must never be published."""

    def complete(self, request):  # type: ignore[override]
        res = super().complete(request)
        if request.prompt_version.startswith("compile_policy"):
            out = res.output
            fake = out.agb_pct.model_copy(update={"value": 5.0, "evidence": [
                Evidence(doc_sha256=request.doc_sha256, page=6, quote="The current AGB percentage is 5% of gross charges.")]})
            res = LLMResult(out.model_copy(update={"agb_pct": fake}), res.raw, res.usage, res.cost_usd, res.model, res.backend, True)
        return res


def test_invented_quote_is_never_published(demo_registry, compile_fixtures) -> None:
    r = compile_policy_version(demo_registry, policy_version_id("brazos-valley"), backend=_Fabricating(compile_fixtures))
    stored = demo_registry.get_field(policy_version_id("brazos-valley"), "agb_pct")
    assert stored.status is FieldStatus.abstained and stored.field.value is None
    assert r.fields["agb_pct"].forced_abstention


class _Refusing(ReplayBackend):
    def complete(self, request):  # type: ignore[override]
        raise LLMRefusal("refused")


def test_llm_failure_stores_nothing(demo_registry, compile_fixtures) -> None:
    r = compile_policy_version(demo_registry, policy_version_id("brazos-valley"), backend=_Refusing(compile_fixtures))
    assert r.error and "LLMRefusal" in r.error
    assert demo_registry.fields_for_policy(policy_version_id("brazos-valley")) == []


def test_missing_compile_fixture_is_an_error_not_a_guess(demo_registry, tmp_path) -> None:
    r = compile_policy_version(demo_registry, policy_version_id("brazos-valley"), backend=ReplayBackend(tmp_path))
    assert r.error and "FixtureMissing" in r.error


def test_hash_mismatch_is_refused(demo_registry, backend) -> None:
    dv = demo_registry.get_document_version(demo_registry.get_policy_version(policy_version_id("brazos-valley")).doc_shas["fap"])
    demo_registry.add_document_version(dv.model_copy(update={"pdf_uri": demo_registry.get_document_version(
        demo_registry.get_policy_version(policy_version_id("example-methodist")).doc_shas["fap"]).pdf_uri}))
    with pytest.raises(CompileError, match="does not hash"):
        compile_policy_version(demo_registry, policy_version_id("brazos-valley"), backend=backend)


def test_unknown_inputs(demo_registry, backend) -> None:
    with pytest.raises(CompileError):
        compile_facility(demo_registry, "tx-000000", backend=backend)
    with pytest.raises(CompileError):
        compile_policy_version(demo_registry, "pv:nope", backend=backend)


def test_policy_from_records_round_trips(compiled, gold_policy) -> None:
    reg, _ = compiled
    pol = policy_from_records(reg.fields_for_policy(policy_version_id("valley-childrens")))
    assert pol.fpg_tiers.value == gold_policy("valley-childrens").fpg_tiers.value


def test_costs_are_simulated_at_section_13_prices(compiled) -> None:
    _, reports = compiled
    r = reports["tx-459907"]
    assert 0.05 < r.cost_usd < 0.2  # compile + one re-ask, replayed at live prices
    json.dumps(r.summary())


def test_default_loader_errors_without_bytes(tmp_path) -> None:
    dv = DocumentVersion(sha256="f" * 64, fetched_at="2026-10-01T00:00:00Z", pdf_uri=str(tmp_path / "missing.pdf"))
    with pytest.raises(CompileError, match="no stored bytes"):
        default_pdf_loader(dv)


def test_fixture_missing_is_llm_error() -> None:
    assert issubclass(FixtureMissing, Exception) and Usage().total_tokens == 0


def _qfield(*quotes: str):
    from charitycare.schemas import Evidence, Field

    return Field(value={"applies": True}, evidence=[Evidence(doc_sha256="a" * 64, page=1, quote=q) for q in quotes],
                 confidence=0.9)


def test_discretionary_guard() -> None:
    from charitycare.compile.pipeline import FieldOutcome, _discretion_guard, is_discretionary

    assert is_discretionary(_qfield("The hospital may grant a discount to patients with high medical bills."))
    assert is_discretionary(_qfield("Assets may be considered at the hospital's sole discretion."))
    assert not is_discretionary(_qfield("Patients may apply within 240 days of the first statement."))
    assert not is_discretionary(_qfield("The hospital may consider assets.", "Applicants with assets over $5,000 shall not qualify."))
    assert not is_discretionary(_qfield("The hospital may not charge more than AGB."))
    assert not is_discretionary(_qfield("Patients at or below 200% FPG will receive free care."))
    o = FieldOutcome("asset_test", FieldStatus.provisional)
    out = _discretion_guard("asset_test", _qfield("The hospital may consider assets."), o)
    assert out.value is None and out.abstain_reason == "ambiguous_may_clause" and out.evidence and o.status is FieldStatus.abstained
    kept = _discretion_guard("policy_title", _qfield("The hospital may consider assets."), FieldOutcome("policy_title", FieldStatus.provisional))
    assert kept.abstain_reason is None

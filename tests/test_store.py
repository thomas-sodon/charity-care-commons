from datetime import date, datetime, timezone

import pytest

from charitycare.schemas import (
    ChangeEvent,
    CrossCheck,
    CrossCheckResult,
    DocumentVersion,
    EvalRun,
    Evidence,
    Facility,
    Field,
    FieldRecord,
    FieldStatus,
    FormFieldSpec,
    FormTemplate,
    HospitalResponse,
    Organization,
    PacketMetric,
    PolicyFacilityMap,
    PolicyVersion,
    ReviewTask,
    ScheduleHFacility,
    Source,
)
from charitycare.store import TABLES, Registry

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
SHA = "b" * 64


def samples() -> list:
    return [
        Organization(ein="990100101", name="Example Methodist Hospital"),
        Facility(id="tx-459901", ccn="459901", name="Example Methodist Hospital", county="Bexar", ein="990100101"),
        ScheduleHFacility(
            id="202601999349300101:1", ein="990100101", org_name="EXAMPLE", object_id="202601999349300101", tax_year=2025,
            tax_period_end=date(2025, 12, 31), facility_names=["EXAMPLE METHODIST HOSPITAL"], line_13a_free_pct=200,
            line_18_ecas_permitted=["credit_report"], line_22_agb_method="lookback_medicare_private",
        ),
        Source(id="src1", url="https://www.examplemethodist.example/financial-assistance/fap.pdf", kind="fap", first_seen=NOW),
        DocumentVersion(sha256=SHA, source_id="src1", kind="fap", fetched_at=NOW, pages=6, has_text_layer=True, language="en"),
        PolicyVersion(id="pv1", organization_ein="990100101", title="FAP", effective_date=date(2025, 7, 1), doc_shas={"fap": SHA}, as_of=date(2026, 10, 1)),
        PolicyFacilityMap(policy_version_id="pv1", facility_id="tx-459901", method="named_in_policy", confidence=0.98),
        ReviewTask(id="rt1", kind="field_verify", field_id="pv1:fpg_tiers", priority="high", created_at=NOW),
        HospitalResponse(id="hr1", facility_id="tx-459901", field_key="asset_test", statement="Added in 2025.", received_at=NOW),
        ChangeEvent(id="ce1", policy_version_id="pv1", triage="substantive", diff_summary="free 150->200", published_at=NOW),
        FormTemplate(doc_sha256=SHA, kind="fillable", field_map=[FormFieldSpec(key="patient_name", label="Name", page=1, rect=[1, 2, 3, 4])]),
        EvalRun(id="ev1", dataset_version="demo-2026.10", suite="smoke", metrics={"citation_validity": 1.0}, created_at=NOW),
        PacketMetric(id="pm1", template_id=SHA, n_fields=18, all_match=True, latency_ms=900),
    ]


def test_every_table_round_trips(registry: Registry) -> None:
    for obj in samples():
        registry.put(obj)
        spec = TABLES[type(obj)]
        got = registry.get(type(obj), **{k: getattr(obj, k) for k in spec.pk})
        assert got == obj, spec.name
        registry.put(obj)  # upsert is idempotent
        assert registry.count(type(obj)) == 1


def test_section_10_tables_exist(registry: Registry) -> None:
    names = {r[0] for r in registry.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "organization", "facility", "schedule_h_section", "source", "document_version", "policy_version", "policy_facility_map",
        "field", "crosscheck", "review_task", "hospital_response", "change_event", "form_template", "eval_run", "packet_metric",
    }
    assert expected <= names
    assert not any("household" in n or "patient" in n for n in names)
    cols = {r[1] for r in registry.conn.execute("PRAGMA table_info(packet_metric)")}
    assert cols == {"id", "template_id", "n_fields", "all_match", "retries", "latency_ms", "cost_usd"}


def _field(key: str = "fpg_tiers", result: CrossCheckResult = CrossCheckResult.agree) -> FieldRecord:
    return FieldRecord(
        id=FieldRecord.make_id("pv1", key), policy_version_id="pv1", key=key,
        field=Field(value=[{"max_pct_inclusive": 200, "benefit": "free"}], evidence=[Evidence(doc_sha256=SHA, page=3, quote="q")], confidence=0.9),
        crosschecks=[CrossCheck(field_key=key, source="schedule_h", external_ref="irs:1|13a", external_value=200, result=result)],
        as_of=date(2026, 10, 1), extractor_model_tier="sonnet", prompt_version="compile_policy@v1",
    )


def test_fields_and_crosschecks(registry: Registry) -> None:
    registry.put(_field())
    registry.put(_field("asset_test", CrossCheckResult.disagree_unexplained))
    got = registry.get_field("pv1", "fpg_tiers")
    assert got is not None and got.crosschecks[0].field_id == "pv1:fpg_tiers"
    assert [f.key for f in registry.fields_for_policy("pv1")] == ["asset_test", "fpg_tiers"]
    assert [c.field_key for c in registry.discrepancies()] == ["asset_test"]
    # replacing a field replaces its crosschecks
    registry.put(_field("asset_test", CrossCheckResult.agree))
    assert registry.discrepancies() == []
    rec = registry.set_field_status("pv1:fpg_tiers", FieldStatus.verified, reviewer="maintainer")
    assert registry.get_field("pv1", "fpg_tiers").status is FieldStatus.verified and rec.reviewer == "maintainer"
    registry.delete(FieldRecord, id="pv1:fpg_tiers")
    assert registry.get_field("pv1", "fpg_tiers") is None
    assert registry.count(CrossCheck) == 1


def test_policy_versions_and_mapping(registry: Registry) -> None:
    registry.put(PolicyVersion(id="pv1", organization_ein="e", as_of=date(2025, 1, 1)))
    registry.map_policy_facility(PolicyFacilityMap(policy_version_id="pv1", facility_id="f1", method="named_in_policy", confidence=0.9))
    registry.add_policy_version(PolicyVersion(id="pv2", organization_ein="e", as_of=date(2026, 3, 1), supersedes="pv1"))
    registry.map_policy_facility(PolicyFacilityMap(policy_version_id="pv2", facility_id="f1", method="named_in_policy", confidence=0.9))
    assert registry.get_policy_version("pv1").status == "superseded"
    assert registry.policy_for_facility("f1").id == "pv2"
    assert registry.policy_for_facility("f1", as_of=date(2025, 6, 1)).id == "pv1"
    assert registry.policy_for_facility("f1", as_of=date(2024, 6, 1)) is None
    assert registry.policy_for_facility("nope") is None


def test_review_queue_and_queries(registry: Registry) -> None:
    for i, prio in enumerate(["low", "high", "medium"]):
        registry.add_review_task(ReviewTask(id=f"t{i}", kind="field_verify", priority=prio, created_at=NOW))
    assert [t.priority for t in registry.open_review_tasks()] == ["high", "medium", "low"]
    registry.update_review_task("t1", "approved", notes="ok")
    assert [t.id for t in registry.open_review_tasks()] == ["t2", "t0"]
    registry.upsert_facility(Facility(id="a", name="B Hospital"))
    registry.upsert_facility(Facility(id="b", name="A Hospital", in_scope=False))
    assert [f.id for f in registry.list_facilities()] == ["b", "a"]
    assert [f.id for f in registry.list_facilities(in_scope=True)] == ["a"]
    with pytest.raises(ValueError):
        registry.select(Facility, nonexistent=1)
    with pytest.raises(ValueError):
        registry.get(Facility, name="x")


def test_transaction_rollback(registry: Registry) -> None:
    with pytest.raises(RuntimeError):
        with registry.transaction():
            registry.put(Organization(ein="1", name="x"))
            raise RuntimeError("boom")
    assert registry.count(Organization) == 0


def test_file_persistence(tmp_path) -> None:
    path = tmp_path / "r.sqlite"
    with Registry(path) as reg:
        reg.put(Organization(ein="1", name="x"))
    with Registry(path) as reg:
        assert reg.get(Organization, ein="1").name == "x"

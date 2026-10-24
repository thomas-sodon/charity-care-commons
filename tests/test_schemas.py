import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from charitycare.schemas import (
    CompiledPolicy,
    CrossCheck,
    CrossCheckResult,
    Determination,
    Evidence,
    Field,
    FieldRecord,
    FieldStatus,
    FPGTier,
    ReadbackField,
    ReadbackResult,
    export_json_schema,
    open_fap_schema,
)

SHA = "a" * 64


def ev(page: int = 1, quote: str = "Patients at or below 200% qualify for free care.") -> Evidence:
    return Evidence(doc_sha256=SHA, page=page, quote=quote, section_label="4.2")


def test_field_abstention_clears_value() -> None:
    f = Field[int](value=5, abstain_reason="ambiguous_may_clause", confidence=0.4)
    assert f.value is None and f.is_abstained
    assert Field[int](value=5, evidence=[ev()]).is_abstained is False
    assert Field[int]().is_abstained


def test_field_generic_validation() -> None:
    f = Field[list[FPGTier]].model_validate(
        {"value": [{"max_pct_inclusive": 200, "benefit": "free"}], "evidence": [ev().model_dump()], "confidence": 0.9}
    )
    assert f.value[0].min_pct_exclusive is None and f.value[0].applies_to == "all"
    with pytest.raises(ValidationError):
        Field[list[FPGTier]].model_validate({"value": [{"max_pct_inclusive": 200, "benefit": "gratis"}]})
    with pytest.raises(ValidationError):
        Field[int](value=1, confidence=1.5)


def test_evidence_constraints() -> None:
    with pytest.raises(ValidationError):
        Evidence(doc_sha256=SHA, page=0, quote="x")
    with pytest.raises(ValidationError):
        Evidence(doc_sha256=SHA, page=1, quote="x" * 401)
    with pytest.raises(ValidationError):
        Evidence.model_validate({"doc_sha256": SHA, "page": 1, "quote": "x", "unknown": 1})


def test_compiled_policy_has_all_design_fields() -> None:
    keys = CompiledPolicy.field_keys()
    assert len(keys) == 23
    for k in ("fpg_tiers", "asset_test", "residency", "required_documents", "presumptive_eligibility", "award_period_months"):
        assert k in keys


def test_readback_all_match_is_derived() -> None:
    ok = ReadbackResult(
        template_id="t",
        fields=[ReadbackField(key="a", intended="1", read="1", match=True), ReadbackField(key="ssn", intended="", read="", match=False, write_by_hand=True)],
        all_match=False,
    )
    assert ok.all_match is True
    bad = ReadbackResult(template_id="t", fields=[ReadbackField(key="a", intended="1", read="7", match=False)], all_match=True)
    assert bad.all_match is False
    assert ReadbackResult(template_id="t").all_match is False


def test_field_record_round_trip() -> None:
    rec = FieldRecord(
        id=FieldRecord.make_id("pv1", "fpg_tiers"),
        policy_version_id="pv1",
        key="fpg_tiers",
        field=Field[list[FPGTier]](value=[FPGTier(max_pct_inclusive=200, benefit="free")], evidence=[ev()], confidence=0.9),
        crosschecks=[
            CrossCheck(field_key="fpg_tiers", source="schedule_h", external_ref="irs:1|13a", external_value=200, result=CrossCheckResult.agree)
        ],
        as_of=date(2026, 10, 1),
    )
    back = FieldRecord.model_validate_json(rec.model_dump_json())
    assert back.status is FieldStatus.provisional
    assert back.field.value[0]["max_pct_inclusive"] == 200  # Field[Any] keeps JSON shape
    assert back.crosschecks[0].result is CrossCheckResult.agree


def test_determination_decimal() -> None:
    d = Determination(
        facility_id="tx-459901", policy_version_id="pv1", fpg_year=2026, household_size=3,
        annual_income=Decimal("49700"), pct_fpl=Decimal("181.92"), outcome="likely_free", disclaimer_version="d",
    )
    assert json.loads(d.model_dump_json())["pct_fpl"] == "181.92"
    with pytest.raises(ValidationError):
        Determination.model_validate({**d.model_dump(), "outcome": "eligible"})


def test_open_fap_schema(tmp_path) -> None:
    s = open_fap_schema()
    assert s["title"] == "Open FAP Schema" and s["version"] == "0.1.0"
    assert s["$ref"] == "#/$defs/CompiledPolicy"
    for name in ("CompiledPolicy", "FieldRecord", "CrossCheck", "ScheduleHFacility", "Determination", "Field_FPGYearRule", "Field_list_FPGTier"):
        assert name in s["$defs"], name
    out = export_json_schema(tmp_path / "s.json")
    assert json.loads(out.read_text()) == s


def test_committed_schema_is_current() -> None:
    from charitycare.config import get_settings

    committed = get_settings().schemas_dir / "open-fap-schema.json"
    assert committed.exists(), "run `ccc schema export`"
    assert json.loads(committed.read_text()) == open_fap_schema(), "schema changed: run `ccc schema export`"


def test_structured_output_schema_compatible() -> None:
    from anthropic.lib._parse._transform import transform_schema

    schema = transform_schema(CompiledPolicy)
    text = json.dumps(schema)
    assert "prefixItems" not in text  # tuples are not supported by structured outputs
    assert schema["$defs"]["Evidence"]["additionalProperties"] is False

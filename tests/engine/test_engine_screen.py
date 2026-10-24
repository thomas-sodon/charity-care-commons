from __future__ import annotations

from datetime import date

import pytest
from engine_helpers import TODAY, hh

from charitycare.compile.demo import load_demo_registry
from charitycare.engine import UnknownFacility, render_text, screen, screen_detailed
from charitycare.engine.policy import demo_policies
from charitycare.schemas import FieldStatus, PolicyFacilityMap
from charitycare.store import Registry
from charitycare.wording import find_banned_wording


def test_screen_demo_every_facility() -> None:
    pols = demo_policies()
    assert len(pols) == 10
    for fid in pols:
        d = screen(fid, hh(county="Bexar"), today=TODAY)
        assert d.facility_id == fid and d.policy_version_id.startswith("pv:")
        assert d.outcome in ("likely_free", "likely_discount")
        text = render_text(d, policy_as_of=date(2026, 10, 1))
        assert "2026-10-01" in text and find_banned_wording(text) == []


def test_screen_unknown_facility() -> None:
    with pytest.raises(UnknownFacility):
        screen("tx-000000", hh(), today=TODAY)


def test_screen_explicit_records() -> None:
    recs = next(iter(demo_policies().values())).records
    d = screen("tx-x", hh(), records=recs, today=TODAY)
    assert d.policy_version_id == recs[0].policy_version_id


def test_screen_from_registry(registry: Registry) -> None:
    load_demo_registry(registry)
    fid = "tx-459901"
    # No policy mapping yet -> insufficient information.
    assert screen(fid, hh(), registry=registry, today=TODAY).outcome == "insufficient_information"
    dp = demo_policies()[fid]
    registry.map_policy_facility(PolicyFacilityMap(policy_version_id=dp.policy_version_id, facility_id=fid,
                                                   method="named_in_policy", confidence=1.0))
    assert screen(fid, hh(), registry=registry, today=TODAY).outcome == "insufficient_information"  # no fields yet
    for r in dp.records:
        registry.put_field(r.model_copy(update={"status": FieldStatus.provisional} if r.key == "fpg_tiers" else {}))
    d = screen(fid, hh(county="Bexar"), registry=registry, today=TODAY)
    assert d.outcome == "preliminary_provisional_fields" and d.provisional_fields_used == ["fpg_tiers"]
    registry.set_field_status(f"{dp.policy_version_id}:fpg_tiers", FieldStatus.verified, reviewer="test")
    res = screen_detailed(fid, hh(county="Bexar"), registry=registry, today=TODAY)
    assert res.determination.outcome == "likely_free"
    with pytest.raises(UnknownFacility):
        screen("tx-000000", hh(), registry=registry)


def test_render_flags_conditions_not_met() -> None:
    d = screen("tx-459908", hh(county="Travis", insured=True, liquid_assets="6000"), today=TODAY)
    text = render_text(d)
    assert "CAUTION: 3 other policy condition(s) likely not met" in text
    assert text.splitlines()[0] == "LIKELY ELIGIBLE FOR FREE CARE"


def test_engine_cli(capsys: pytest.CaptureFixture[str]) -> None:
    from charitycare.engine.cli import main

    assert main(["screen", "tx-459901", "--size", "3", "--income", "49700", "--county", "Bexar",
                 "--statement-date", "2026-10-10", "--insured", "no"]) == 0
    out = capsys.readouterr().out
    assert "181.9% FPL" in out and "2027-06-07" in out
    assert main(["screen", "tx-000000", "--size", "1", "--income", "1"]) == 2
    assert main(["households", "score"]) == 0

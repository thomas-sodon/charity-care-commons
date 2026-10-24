"""MCP server tools: find_facility, get_policy, screen_household, list_changes."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from api_helpers import EM, EM_PV, TODAY

from charitycare.mcp_server import build_server
from charitycare.schemas import Determination
from charitycare.store import Registry
from charitycare.wording import find_banned_wording


def call(server, name: str, args: dict) -> dict:
    res = asyncio.run(server.call_tool(name, args))
    assert not res.is_error, res
    return json.loads(res.content[0].text)


def test_tools_listed_read_only(seeded: Registry):
    server = build_server(seeded, today=lambda: TODAY)
    tools = asyncio.run(server.list_tools())
    assert [t.name for t in tools] == ["find_facility", "get_policy", "screen_household", "list_changes"]
    for t in tools:
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert t.description
    screen = next(t for t in tools if t.name == "screen_household")
    assert set(screen.input_schema["required"]) == {"facility_id", "household_size", "annual_income"}


def test_find_facility(seeded: Registry):
    server = build_server(seeded)
    out = call(server, "find_facility", {"query": "lone star"})
    assert out["count"] == 3 and {f["id"] for f in out["facilities"]} == {"tx-459902", "tx-459903", "tx-459904"}
    assert call(server, "find_facility", {"county": "Collin"})["facilities"][0]["city"] == "Plano"
    assert call(server, "find_facility", {"ccn": "459901"})["facilities"][0]["id"] == EM
    assert call(server, "find_facility", {"query": "nowhere general"})["count"] == 0


def test_get_policy(seeded: Registry):
    server = build_server(seeded)
    pol = call(server, "get_policy", {"facility_id": EM})
    assert pol["policy_version_id"] == EM_PV and len(pol["fields"]) == 23
    tiers = next(f for f in pol["fields"] if f["key"] == "fpg_tiers")
    assert tiers["status"] == "verified" and tiers["quotes"][0]["page"] == 3 and len(tiers["quotes"][0]["doc_sha256"]) == 64
    asset = next(f for f in pol["fields"] if f["key"] == "asset_test")
    assert asset["irs_crosschecks"][0]["label"] == "Discrepancy for review"
    assert "not legal advice" in pol["disclaimer"]
    no_prov = call(server, "get_policy", {"facility_id": EM, "include_provisional": False})
    assert "screening_timing" not in {f["key"] for f in no_prov["fields"]}
    assert "error" in call(server, "get_policy", {"facility_id": "tx-000000"})
    assert call(server, "get_policy", {"facility_id": EM, "as_of": "2026-01-01"})["coverage"] == "not_found"


def test_screen_household_engine(seeded: Registry):
    server = build_server(seeded, today=lambda: TODAY)
    out = call(server, "screen_household", {"facility_id": EM, "household_size": 3, "annual_income": 49700,
                                            "county": "Bexar", "insured": False, "first_statement_date": "2026-10-10"})
    assert out["outcome"] == "likely_free" and out["label"] == "Likely eligible for free care"
    assert out["pct_fpl"] == "181.92" and out["federal_deadline"] == "2027-06-07" and out["quotes"]
    assert not find_banned_wording(out["label"])
    assert "error" in call(server, "screen_household", {"facility_id": EM, "household_size": 0, "annual_income": 1})
    assert "error" in call(server, "screen_household", {"facility_id": "tx-000000", "household_size": 2, "annual_income": 1})


def test_screen_household_fake_engine(seeded: Registry):
    def fake(facility_id, household, *, registry, today):
        return Determination(facility_id=facility_id, policy_version_id=EM_PV, fpg_year=2026, household_size=household.household_size,
                             annual_income=household.annual_income, pct_fpl=Decimal(500), outcome="likely_not_eligible_on_income",
                             disclaimer_version="d")

    server = build_server(seeded, screen_fn=fake)
    out = call(server, "screen_household", {"facility_id": EM, "household_size": 2, "annual_income": 200000})
    assert out["label"] == "Likely not eligible on income alone"


def test_list_changes(seeded: Registry):
    server = build_server(seeded)
    out = call(server, "list_changes", {})
    assert out["count"] == 8 and all("\n---\n" not in c["diff_summary"] for c in out["changes"])
    assert call(server, "list_changes", {"since": "2026-10-02"})["count"] == 0
    assert "error" in call(server, "list_changes", {"since": "last week"})

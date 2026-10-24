"""Read API (design section 2b): facilities, policies, fields, cross-checks, changes, Atom, link health."""

from __future__ import annotations

from datetime import UTC, date, datetime
from xml.etree import ElementTree as ET

from api_helpers import EM, EM_PV, TODAY, make_client

from charitycare.schemas import ChangeEvent, PolicyFacilityMap, PolicyVersion
from charitycare.store import Registry
from charitycare.wording import find_banned_wording


def test_facilities_list_and_filters(client):
    r = client.get("/v1/facilities", params={"state": "TX"})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["count"] == 10 and len(body["data"]) == 10
    assert {f["state"] for f in body["data"]} == {"TX"}
    assert {f["coverage"] for f in body["data"]} == {"verified"}
    em = next(f for f in body["data"] if f["id"] == EM)
    assert em["free_max_pct"] == 200 and em["discount_max_pct"] == 400 and em["asset_test"] is True
    assert em["irs_check"] == "discrepancy"
    assert client.get("/v1/facilities", params={"coverage": "partial"}).json()["data"] == []
    hits = client.get("/v1/facilities", params={"q": "methodist"}).json()["data"]
    assert [f["id"] for f in hits] == [EM]
    assert client.get("/v1/facilities", params={"state": "OK"}).json()["data"] == []


def test_meta_envelope_and_freshness(client):
    meta = client.get("/v1/facilities").json()["meta"]
    assert meta["as_of"] == TODAY.isoformat()
    assert meta["last_verified"] == "2026-10-03"  # demo reviewer decisions
    assert meta["stale"] is False
    assert meta["license"] == "CC-BY-4.0"
    assert meta["disclaimer_version"].startswith("disclaimer@")


def test_stale_flag_after_sunset_window(seeded):
    late = make_client(seeded, today=lambda: date(2027, 3, 1))
    r = late.get("/v1/facilities")
    assert r.json()["meta"]["stale"] is True
    assert r.headers["X-CCC-Stale"] == "true"
    forced = make_client(seeded, sunset=True)
    assert forced.get(f"/v1/facilities/{EM}").json()["meta"]["stale"] is True
    assert "X-CCC-Stale" not in make_client(seeded).get("/v1/facilities").headers


def test_facility_detail(client):
    r = client.get(f"/v1/facilities/{EM}")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["name"] == "Example Methodist Hospital" and d["policy_version_id"] == EM_PV
    assert d["schedule_h"][0]["line_13a_free_pct"] == 200
    assert d["hospital_responses"][0]["field_key"] == "asset_test"
    assert d["versions"][0]["id"] == EM_PV and d["versions"][0]["changes"]
    assert client.get("/v1/facilities/tx-000000").status_code == 404


def test_policy_as_of_and_supersession(seeded: Registry):
    client = make_client(seeded)
    r = client.get(f"/v1/facilities/{EM}/policy")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["policy"]["id"] == EM_PV
    assert data["mapping"]["method"] == "named_in_policy"
    assert {d["name"] for d in data["documents"]} == {"fap", "application_en", "pls_en"}
    assert all(len(d["sha256"]) == 64 and d["url"].startswith("https://") for d in data["documents"])
    assert len(data["fields"]) == 23 and data["fields"][0]["key"] == "fpg_tiers"
    # Before the demo as_of there is no governing version.
    assert client.get(f"/v1/facilities/{EM}/policy", params={"as_of": "2026-09-01"}).status_code == 404
    # A newer version supersedes the demo one; as_of picks the right one.
    old = seeded.get_policy_version(EM_PV)
    seeded.add_policy_version(old.model_copy(update={"id": "pv:example-methodist:v2", "supersedes": EM_PV,
                                                      "as_of": date(2026, 10, 20), "status": "current"}))
    seeded.map_policy_facility(PolicyFacilityMap(policy_version_id="pv:example-methodist:v2", facility_id=EM,
                                                 method="named_in_policy", confidence=0.99))
    assert seeded.get(PolicyVersion, id=EM_PV).status == "superseded"
    assert client.get(f"/v1/facilities/{EM}/policy").json()["data"]["policy"]["id"] == "pv:example-methodist:v2"
    r = client.get(f"/v1/facilities/{EM}/policy", params={"as_of": "2026-10-10"})
    assert r.json()["data"]["policy"]["id"] == EM_PV
    assert r.json()["meta"]["requested_as_of"] == "2026-10-10"
    assert client.get(f"/v1/facilities/{EM}/policy", params={"as_of": "not-a-date"}).status_code == 422


def test_policy_fields_and_status_filter(client):
    r = client.get(f"/v1/policies/{EM_PV}/fields")
    assert r.status_code == 200
    fields = {f["key"]: f for f in r.json()["data"]}
    tiers = fields["fpg_tiers"]
    assert tiers["status"] == "verified" and tiers["badge"]["symbol"] == "✓"
    assert tiers["display"] == "Free care up to 200% FPL; Charges capped at AGB over 200% to 400% FPL"
    assert tiers["field"]["evidence"][0]["page"] == 3 and tiers["usable_for_screening"] is True
    assert fields["screening_timing"]["status"] == "provisional"
    assert fields["screening_timing"]["usable_for_screening"] is False
    prov = client.get(f"/v1/policies/{EM_PV}/fields", params={"status": "provisional"}).json()["data"]
    assert [f["key"] for f in prov] == ["screening_timing"]
    assert client.get("/v1/policies/pv:nope/fields").status_code == 404
    for f in fields.values():
        assert not find_banned_wording(f["display"]) and not find_banned_wording(f["label"])


def test_abstentions_are_first_class(client):
    fields = {f["key"]: f for f in client.get("/v1/policies/pv:gulf-coast-baptist:demo/fields").json()["data"]}
    pres = fields["presumptive_eligibility"]
    assert pres["status"] == "abstained" and pres["field"]["value"] is None
    assert pres["field"]["abstain_reason"] == "ambiguous_may_clause"
    assert pres["display"] == "Hospital discretion, see quote" and pres["field"]["evidence"]


def test_crosschecks_and_discrepancies(client):
    rows = client.get(f"/v1/policies/{EM_PV}/crosschecks").json()["data"]
    by_key = {r["field_key"]: r for r in rows}
    asset = by_key["asset_test"]
    assert asset["result"] == "disagree_unexplained" and asset["public_label"] == "Discrepancy for review"
    assert asset["hospital_responses"][0]["statement"].startswith("The asset test was added")
    assert by_key["fpg_tiers"]["public_label"] == "matches IRS filing"
    assert by_key["fpg_tiers"]["hospital_responses"] == []
    newer = client.get("/v1/policies/pv:pineywoods-regional:demo/crosschecks").json()["data"]
    assert any(r["result"] == "disagree_policy_newer_than_filing" and not r["is_discrepancy"] for r in newer)
    disc = client.get("/v1/discrepancies").json()["data"]
    assert [(d["field_key"], d["facility_ids"]) for d in disc] == [("asset_test", [EM])]
    for r in rows + disc:
        assert not find_banned_wording(r["public_label"]) and "violation" not in str(r).lower()


def test_changes_since_and_atom(seeded: Registry):
    client = make_client(seeded)
    seeded.add_change_event(ChangeEvent(id="ce:test:later", policy_version_id=EM_PV, triage="substantive",
                                        diff_summary="Free-care cutoff raised.", published_at=datetime(2026, 10, 20, tzinfo=UTC)))
    seeded.add_change_event(ChangeEvent(id="ce:test:draft", policy_version_id=EM_PV, triage="cosmetic",
                                        diff_summary="unpublished", published_at=None))
    all_ = client.get("/v1/changes").json()["data"]
    assert len(all_) == 9 and all_[0]["id"] == "ce:test:later"  # newest first, drafts excluded
    assert all_[0]["facility_ids"] == [EM]
    recent = client.get("/v1/changes", params={"since": "2026-10-10"}).json()["data"]
    assert [c["id"] for c in recent] == ["ce:test:later"]
    assert client.get("/v1/changes", params={"since": "2026-10-10T00:00:00Z"}).json()["meta"]["count"] == 1
    assert client.get("/v1/changes", params={"since": "yesterday"}).status_code == 422
    r = client.get("/v1/changes.atom")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/atom+xml")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    feed = ET.fromstring(r.content)
    entries = feed.findall("a:entry", ns)
    assert len(entries) == 9
    assert entries[0].find("a:summary", ns).text == "Free-care cutoff raised."
    assert feed.find("a:updated", ns).text.startswith("2026-10-20")
    assert entries[0].find("a:link", ns).attrib["href"].endswith(f"/v1/policies/{EM_PV}/fields")
    assert len(ET.fromstring(client.get("/v1/changes.atom", params={"since": "2026-10-10"}).content).findall("a:entry", ns)) == 1


def test_link_health(client):
    d = client.get("/v1/link-health").json()["data"]
    assert d["summary"] == {"total": 26, "ok": 25, "broken": 1, "blocked": 0, "unchecked": 0}
    broken = [s for s in d["sources"] if s["state"] == "broken"]
    assert broken[0]["http_status"] == 404 and broken[0]["has_snapshot"] is True


def test_root_and_openapi(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/v1/facilities", "/v1/facilities/{facility_id}", "/v1/facilities/{facility_id}/policy",
              "/v1/policies/{version_id}/fields", "/v1/policies/{version_id}/crosschecks", "/v1/changes",
              "/v1/changes.atom", "/v1/link-health", "/v1/screen", "/v1/corrections"):
        assert p in paths
    assert not any(p.startswith("/console") for p in paths)

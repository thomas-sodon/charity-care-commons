"""``POST /v1/screen``, ``POST /v1/corrections``, the 60/min rate limit and the no-body logging rule."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from api_helpers import EM, EM_PV, TODAY, make_client

from charitycare.api import app as app_module
from charitycare.api.middleware import LOG_ALLOWLIST, SlidingWindow
from charitycare.schemas import Determination, HospitalResponse, ReviewTask
from charitycare.store import Registry
from charitycare.wording import find_banned_wording

HH = {"household_size": 3, "annual_income": "49700", "county": "Bexar", "insured": False}


def test_screen_with_engine(client):
    r = client.post("/v1/screen", json={"facility_id": EM, "household": HH})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    d = r.json()["data"]
    det = d["determination"]
    assert det["outcome"] == "likely_free" and det["pct_fpl"] == "181.92" and det["fpg_year"] == 2026
    assert det["policy_version_id"] == EM_PV and det["citations"][0]["page"] == 3
    assert d["label"] == "Likely eligible for free care"
    assert "2026-10-01" in d["disclaimer"] and "not legal advice" in d["disclaimer"]
    assert not find_banned_wording(d["label"] + " " + d["disclaimer"] + " " + d["decided_by"])


def test_screen_uses_monkeypatched_engine(client, monkeypatch):
    calls = []

    def fake_screen(facility_id, household, *, registry, today):
        calls.append((facility_id, household.annual_income, today))
        return Determination(facility_id=facility_id, policy_version_id=EM_PV, fpg_year=2026, household_size=3,
                             annual_income=household.annual_income, pct_fpl=Decimal("300.00"), outcome="likely_discount",
                             disclaimer_version="d")

    monkeypatch.setattr(app_module, "load_screen", lambda: fake_screen)
    r = client.post("/v1/screen", json={"facility_id": EM, "household": HH})
    assert r.json()["data"]["label"] == "Likely eligible for discounted care"
    assert calls == [(EM, Decimal(49700), TODAY)]


def test_screen_errors(client, monkeypatch):
    assert client.post("/v1/screen", json={"facility_id": "tx-000000", "household": HH}).status_code == 404
    bad = client.post("/v1/screen", json={"facility_id": EM, "household": {**HH, "household_size": 0, "annual_income": "987654"}})
    assert bad.status_code == 422
    assert "987654" not in bad.text  # validation errors never echo household values
    assert client.post("/v1/screen", json={"facility_id": EM, "household": HH, "ssn": "123-45-6789"}).status_code == 422

    def missing():
        raise ImportError("engine not built")

    monkeypatch.setattr(app_module, "load_screen", missing)
    assert client.post("/v1/screen", json={"facility_id": EM, "household": HH}).status_code == 503


def test_request_bodies_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    secret_income, secret_county = "987654.21", "Zzyzxcounty"
    r = client.post("/v1/screen?note=secretquery", json={"facility_id": EM, "household": {**HH, "annual_income": secret_income,
                                                                                           "county": secret_county}})
    assert r.status_code == 200
    client.get(f"/v1/facilities/{EM}?q=private-query")
    access = [rec for rec in caplog.records if rec.name == "charitycare.api.access"]
    assert len(access) == 2
    for rec in access:
        assert set(rec.ccc) <= set(LOG_ALLOWLIST)
    assert access[0].ccc["route"] == "/v1/screen" and access[1].ccc["route"] == "/v1/facilities/{facility_id}"
    # Everything logged server side (the test client's own httpx logger records its URLs; that is the caller).
    server = [rec for rec in caplog.records if not rec.name.startswith("httpx")]
    blob = " ".join(f"{rec.getMessage()} {rec.__dict__}" for rec in server)
    for secret in (secret_income, secret_county, "secretquery", "private-query", EM):
        assert secret not in blob


def test_rate_limit_anonymous_and_keyed(seeded: Registry):
    client = make_client(seeded, rate_limit_per_min=3, keyed_rate_limit_per_min=5, api_keys=frozenset({"k1"}))
    codes = [client.get("/v1/link-health").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = client.get("/v1/facilities")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert r.json()["limit_per_minute"] == 3
    keyed = [client.get("/v1/link-health", headers={"X-API-Key": "k1"}) for _ in range(6)]
    assert [k.status_code for k in keyed] == [200] * 5 + [429]
    assert keyed[0].headers["X-RateLimit-Limit"] == "5"
    # An unknown key counts as anonymous; non-API pages are not limited.
    assert client.get("/v1/link-health", headers={"X-API-Key": "nope"}).status_code == 429
    assert client.get("/healthz").status_code == 200


def test_sliding_window_frees_slots():
    t = [0.0]
    w = SlidingWindow(60, clock=lambda: t[0])
    assert [w.hit("a", 2)[0] for _ in range(3)] == [True, True, False]
    t[0] = 59.9
    assert w.hit("a", 2)[0] is False
    assert w.hit("a", 2)[2] == pytest.approx(0.1)
    t[0] = 60.0  # both earlier hits have aged out
    assert w.hit("a", 2) == (True, 1, 0.0)


def test_hospital_correction_creates_review_task(client, seeded: Registry):
    r = client.post("/v1/corrections", json={
        "facility_id": EM, "field_key": "asset_test", "submitter_role": "hospital",
        "statement": "The asset test applies to free care only. Contact 123-45-6789 or account 1234567890123.",
    })
    assert r.status_code == 202, r.text
    task_id = r.json()["data"]["review_task_id"]
    task = seeded.get(ReviewTask, id=task_id)
    assert task.kind == "hospital_correction" and task.priority == "high" and task.state == "open"
    assert f"{EM_PV}:asset_test" in task.notes
    assert "123-45-6789" not in task.notes and "1234567890123" not in task.notes
    resp = seeded.get(HospitalResponse, id=task_id.removeprefix("rt:hospital_correction:"))
    assert resp is not None and resp.published is False and "[number removed]" in resp.statement
    # Unpublished responses are not public.
    assert len(client.get(f"/v1/facilities/{EM}").json()["data"]["hospital_responses"]) == 1


@pytest.mark.parametrize("body,code", [
    ({"facility_id": "tx-000000", "statement": "Wrong residency counties listed."}, 404),
    ({"facility_id": EM, "field_key": "not_a_field", "statement": "Wrong residency counties listed."}, 422),
    ({"facility_id": EM, "statement": "short"}, 422),
])
def test_correction_validation(client, body, code):
    assert client.post("/v1/corrections", json=body).status_code == code


def test_navigator_correction_has_no_hospital_response(client, seeded: Registry):
    r = client.post("/v1/corrections", json={"facility_id": EM, "submitter_role": "navigator",
                                             "statement": "The fax number on page 5 is out of service."})
    task = seeded.get(ReviewTask, id=r.json()["data"]["review_task_id"])
    assert task.priority == "medium"
    assert len(seeded.responses_for_facility(EM)) == 1  # only the seeded, published one

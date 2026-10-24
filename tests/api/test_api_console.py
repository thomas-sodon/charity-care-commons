"""Maintainer review console (Screen 2): access, impact-sorted queue, side-by-side source, decisions."""

from __future__ import annotations

import json
import re

from api_helpers import EM, EM_PV, make_client

from charitycare.api import review
from charitycare.schemas import (
    ChangeEvent,
    FieldRecord,
    FieldStatus,
    HospitalResponse,
    ReviewTask,
)
from charitycare.store import Registry

ASSET_TASK = f"rt:crosscheck_disagreement:{EM_PV}:asset_test"
SCREEN_TASK = f"rt:field_verify:{EM_PV}:screening_timing"


def test_console_access_control(seeded: Registry, auth):
    closed = make_client(seeded, console_token=None)
    assert closed.get("/console").status_code == 403
    assert make_client(seeded, console_token=None, console_open=True).get("/console").status_code == 200
    client = make_client(seeded)
    assert client.get("/console").status_code == 401
    assert client.get("/console", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/console", headers=auth).status_code == 200
    assert client.get("/console/login", params={"token": "wrong"}).status_code == 401
    r = client.get("/console/login", params={"token": "test-maintainer-token"}, follow_redirects=False)
    assert r.status_code == 303 and "samesite=strict" in r.headers["set-cookie"].lower()
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/console").status_code == 200  # cookie now set
    assert client.post(f"/console/tasks/{ASSET_TASK}/decision", data={"action": "approve"},
                       headers={"Authorization": "Bearer nope"}).status_code == 401


def test_queue_sorted_by_impact(seeded: Registry):
    q = review.queue(seeded)
    impacts = [review.task_impact(t) for t in q]
    assert impacts == sorted(impacts, reverse=True)
    assert q[0].id == ASSET_TASK and q[0].priority == "high"
    assert len(q) == len(seeded.select(ReviewTask, state="open"))


def test_task_impact_and_notes():
    t = ReviewTask(id="t", kind="field_verify", priority="low", notes="[impact 42] x | adjudicator: filing predates policy")
    assert review.task_impact(t) == 42 and review.task_note(t).startswith("x")
    assert review.adjudicator_note(t) == "filing predates policy"
    assert review.task_impact(ReviewTask(id="u", kind="field_verify", priority="high")) == 60


def test_console_page_side_by_side(client, auth):
    r = client.get("/console", headers=auth)
    html = r.text
    assert "Review queue" in html and "Asset test" in html and "Example Methodist Hospital" in html
    marked = re.search(r"<mark>(.*?)</mark>", html, re.DOTALL).group(1)
    assert " ".join(marked.split()) == ("Patients whose household liquid assets exceed $10,000 are not eligible for free "
                                        "or discounted care.")
    assert "exact ✓" in html
    assert "Discrepancy for review" in html  # Schedule H value with public label
    assert "Adjudicator (advisory)" in html and "the filing should reflect it" in html
    for action in ("approve", "edit", "abstain", "ask_hospital"):
        assert f'value="{action}"' in html
    partial = client.get(f"/console/tasks/{SCREEN_TASK}", headers={**auth, "HX-Request": "true"})
    assert partial.status_code == 200 and "<html" not in partial.text and "Screening" in partial.text
    assert client.get(f"/console/tasks/{SCREEN_TASK}", headers=auth, follow_redirects=False).status_code == 303


def test_approve_writes_changelog(client, auth, seeded: Registry):
    r = client.post(f"/console/tasks/{SCREEN_TASK}/decision", headers=auth, follow_redirects=False,
                    data={"action": "approve", "reviewer": "alice", "note": "matches §8"})
    assert r.status_code == 303 and "done=approve" in r.headers["location"]
    rec = seeded.get(FieldRecord, id=f"{EM_PV}:screening_timing")
    assert rec.status == FieldStatus.verified and rec.reviewer == "alice" and rec.reviewed_at is not None
    assert seeded.get(ReviewTask, id=SCREEN_TASK).state == "approved"
    evs = [e for e in seeded.select(ChangeEvent, policy_version_id=EM_PV) if e.id.startswith("rd:")]
    assert len(evs) == 1 and evs[0].published_at is not None and evs[0].triage == "cosmetic"
    human, decision = evs[0].diff_summary.split("\n---\n")
    assert "Screening approved by alice" in human
    assert json.loads(decision)["action"] == "approve" and json.loads(decision)["field_id"] == rec.id
    # The approval is now in the public change feed and the facility is fully verified.
    assert any(c["id"] == evs[0].id for c in client.get("/v1/changes").json()["data"])
    again = client.post(f"/console/tasks/{SCREEN_TASK}/decision", headers=auth, follow_redirects=False,
                        data={"action": "approve", "reviewer": "alice"})
    assert "error=" in again.headers["location"]


def test_htmx_decision_returns_partial_with_oob_queue(client, auth, seeded: Registry):
    r = client.post(f"/console/tasks/{SCREEN_TASK}/decision", headers={**auth, "HX-Request": "true"},
                    data={"action": "approve", "reviewer": "bob"})
    assert r.status_code == 200
    assert "Saved: approve" in r.text and 'hx-swap-oob="true"' in r.text and "<html" not in r.text
    assert SCREEN_TASK not in r.text.split('id="queue-list"')[1]


def test_edit_requires_valid_value_and_verifiable_quote(client, auth, seeded: Registry):
    url = f"/console/tasks/{ASSET_TASK}/decision"
    h = {**auth, "HX-Request": "true"}
    bad = client.post(url, headers=h, data={"action": "edit", "reviewer": "carol", "value": '{"applies": "maybe"}'})
    assert "does not fit Asset test" in bad.text
    assert seeded.get(ReviewTask, id=ASSET_TASK).state == "open"
    value = json.dumps({"applies": True, "threshold_usd": 10000, "counted_assets": ["checking accounts"],
                        "exclusions": [], "applies_to_tiers": "free_only"})
    noquote = client.post(url, headers=h, data={"action": "edit", "reviewer": "carol", "value": value,
                                                "quote": "Assets never matter to us.", "page": "4"})
    assert "no quote, no publish" in noquote.text
    ok = client.post(url, headers=h, data={
        "action": "edit", "reviewer": "carol", "value": value, "page": "4",
        "quote": "Patients whose household liquid assets exceed $10,000 are not eligible for free or discounted care.",
    })
    assert "Saved: edit" in ok.text
    rec = seeded.get(FieldRecord, id=f"{EM_PV}:asset_test")
    assert rec.status == FieldStatus.verified and rec.field.value["applies_to_tiers"] == "free_only"
    assert rec.field.evidence[0].char_start is not None and rec.field.confidence == 1.0
    assert rec.crosschecks  # cross-checks survive an edit
    assert seeded.get(ReviewTask, id=ASSET_TASK).state == "corrected"


def test_abstain_and_ask_hospital(client, auth, seeded: Registry):
    h = {**auth, "HX-Request": "true"}
    client.post(f"/console/tasks/{SCREEN_TASK}/decision", headers=h,
                data={"action": "abstain", "reviewer": "dan", "abstain_reason": "ambiguous_may_clause"})
    rec = seeded.get(FieldRecord, id=f"{EM_PV}:screening_timing")
    assert rec.status == FieldStatus.abstained and rec.field.value is None
    assert rec.field.abstain_reason == "ambiguous_may_clause" and rec.field.evidence
    n_public = len(client.get("/v1/changes").json()["data"])
    client.post(f"/console/tasks/{ASSET_TASK}/decision", headers=h, data={"action": "ask_hospital", "reviewer": "dan"})
    assert seeded.get(ReviewTask, id=ASSET_TASK).state == "hospital_query"
    drafts = [e for e in seeded.select(ChangeEvent) if e.id.startswith(f"rd:{ASSET_TASK}")]
    assert drafts and drafts[0].published_at is None
    assert len(client.get("/v1/changes").json()["data"]) == n_public  # hospital queries are not public
    assert seeded.get(FieldRecord, id=f"{EM_PV}:asset_test").status == FieldStatus.verified  # unchanged


def test_approving_hospital_correction_publishes_response(client, auth, seeded: Registry):
    r = client.post("/v1/corrections", json={"facility_id": EM, "field_key": "residency",
                                             "statement": "Wilson County was added to our service area in 2026."})
    task_id = r.json()["data"]["review_task_id"]
    page = client.get("/console", params={"task": task_id}, headers=auth).text
    assert "Wilson County was added" in page
    client.post(f"/console/tasks/{task_id}/decision", headers=auth, data={"action": "approve", "reviewer": "erin"})
    resp = seeded.get(HospitalResponse, id=task_id.removeprefix("rt:hospital_correction:"))
    assert resp.published is True
    assert len(client.get(f"/v1/facilities/{EM}").json()["data"]["hospital_responses"]) == 2


def test_source_context_handles_missing_pdf(seeded: Registry):
    rec = seeded.get(FieldRecord, id=f"{EM_PV}:fpg_tiers")
    ev = rec.field.evidence[0].model_copy(update={"doc_sha256": "0" * 64})
    assert review.source_context(seeded, ev)["available"] is False
    assert review.highlight("Free care  for\nall", "free care for all") == ("", "Free care  for\nall", "")
    assert review.highlight("abc", "zzz") is None

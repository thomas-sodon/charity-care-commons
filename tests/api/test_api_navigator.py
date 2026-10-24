"""Navigator packet page (Screen 3): household form -> determination -> packet PDF, nothing stored."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api_helpers import EM

from charitycare.api import navigator
from charitycare.schemas import PacketMetric
from charitycare.store import TABLES, Registry

FORM = {"facility_id": EM, "household_size": "3", "annual_income": "49,700", "county": "Bexar", "insured": "no",
        "first_statement_date": "2026-10-10", "patient_name": "Maria Example", "address": "1 Example St",
        "city": "San Antonio", "zip": "78229", "phone": "210-555-0100"}


def _counts(reg: Registry) -> dict[str, int]:
    return {spec.name: reg.count(model) for model, spec in TABLES.items()}


def test_navigator_page(client):
    r = client.get("/navigator", params={"facility_id": EM})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert f'<option value="{EM}" selected>' in r.text and "Nothing you enter is stored" in r.text
    assert 'formaction="/packet"' in r.text


def test_screen_step_htmx_and_plain(client, seeded: Registry):
    before = _counts(seeded)
    r = client.post("/navigator/screen", data=FORM, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<html" not in r.text
    assert "Likely eligible for free care" in r.text and "181.9% FPL" in r.text
    assert "2027-06-07" in r.text  # 240 days after the first statement
    assert "not legal advice" in r.text
    full = client.post("/navigator/screen", data=FORM)
    assert "<html" in full.text and "Likely eligible for free care" in full.text
    assert 'value="Maria Example"' in full.text  # form re-rendered from the request, not from storage
    assert _counts(seeded) == before


@pytest.mark.parametrize("bad,msg", [({"household_size": "0"}, "household size"), ({"annual_income": "lots"}, "must be a number"),
                                     ({"facility_id": "tx-000000"}, "unknown facility")])
def test_screen_step_errors(client, bad, msg):
    r = client.post("/navigator/screen", data={**FORM, **bad}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and msg in r.text


def test_packet_with_fake_compiler(client, seeded: Registry, monkeypatch):
    seen = {}

    class Applicant(SimpleNamespace):
        pass

    class PacketRequest(SimpleNamespace):
        pass

    def fake_compile(req, *, today, registry):
        seen["req"], seen["today"] = req, today
        return SimpleNamespace(pdf=b"%PDF-1.7 fake", verified=True)

    monkeypatch.setattr(navigator, "load_packet", lambda: (fake_compile, Applicant, PacketRequest))
    r = client.post("/packet", data=FORM)
    assert r.status_code == 200 and r.content == b"%PDF-1.7 fake"
    assert r.headers["content-type"] == "application/pdf" and r.headers["cache-control"] == "no-store"
    assert "attachment" in r.headers["content-disposition"] and r.headers["x-ccc-packet-verified"] == "true"
    req = seen["req"]
    pv = seeded.get_policy_version(req.determination.policy_version_id)
    from charitycare.pdftext import sha256_bytes

    assert sha256_bytes(req.form_pdf) == pv.doc_shas["application_en"]
    assert req.template.doc_sha256 == pv.doc_shas["application_en"] and req.template.verified_by
    assert req.policy is not None and req.applicant.patient_name == "Maria Example"
    assert str(req.household.annual_income) == "49700" and req.determination.outcome == "likely_free"


def test_packet_unavailable(client, monkeypatch):
    def missing():
        raise ImportError("packet stage not built")

    monkeypatch.setattr(navigator, "load_packet", missing)
    r = client.post("/packet", data=FORM)
    assert r.status_code == 503
    assert client.post("/packet", data={**FORM, "household_size": "x"}).status_code == 422


def test_packet_end_to_end(client, seeded: Registry):
    try:
        navigator.load_packet()
    except ImportError:
        pytest.skip("packet stage not installed")
    before = _counts(seeded)
    r = client.post("/packet", data=FORM)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF") and r.headers["x-ccc-packet-verified"] in {"true", "false"}
    after = _counts(seeded)
    # The only row a packet may add is its PacketMetric (no household data).
    assert {k: after[k] - before[k] for k in after if after[k] != before[k]} == {"packet_metric": 1}
    assert set(PacketMetric.model_fields) == {"id", "template_id", "n_fields", "all_match", "retries", "latency_ms", "cost_usd"}


def _demo_images() -> list[bytes]:
    from charitycare.packet.synthetic import demo_documents

    return [d.image for d in demo_documents()]


def test_extract_then_confirm_blocks_until_checked(client, seeded: Registry):
    before = _counts(seeded)
    bill, *stubs = _demo_images()
    files = [("bill", ("bill.png", bill, "image/png"))] + [("stubs", (f"s{i}.png", s, "image/png")) for i, s in enumerate(stubs)]
    r = client.post("/navigator/extract", data={"facility_id": EM}, files=files)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "Step 0" in r.text and 'name="img__bill"' in r.text and "data:image/png;base64," in r.text
    if "ok__bill__account_last4" not in r.text:
        pytest.skip("no recorded extraction fixture for the demo bill in this environment")
    import re

    form = dict(re.findall(r'name="((?:img__|bill__|stub\d__)[^"]+)" value="([^"]*)"', r.text))
    form["facility_id"] = EM
    blocked = client.post("/navigator/confirm", data=form)
    assert blocked.status_code == 422 and "confirm these extracted fields first" in blocked.text
    ok = client.post("/navigator/confirm", data=form | {"ok__bill__account_last4": "1"})
    assert ok.status_code == 200 and 'value="2026-10-10"' in ok.text and 'value="49700.04"' in ok.text
    assert _counts(seeded) == before  # nothing stored


def test_confirm_edit_counts_as_confirmation():
    from charitycare.packet.extract import BillFields, Extraction, XField

    ex = Extraction("bill", BillFields(amount_due=XField[str](value="9,400.00", confidence=0.5)), b"", "x")
    assert "amount_due" in ex.needs_confirmation()
    edited = ex.confirm({"amount_due": "9,400.00"})
    assert "amount_due" in edited.needs_confirmation()  # unchanged and not ticked
    edited = ex.confirm({"amount_due": "9,500.00", "statement_date": "2026-10-10"})
    assert "amount_due" not in edited.needs_confirmation() and edited.fields.statement_date.value.isoformat() == "2026-10-10"
    with pytest.raises(ValueError):
        ex.confirm({"statement_date": "10/10/2026"})

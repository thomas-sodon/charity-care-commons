"""End-to-end packet compilation: verification label, fallbacks, privacy, telemetry."""

from __future__ import annotations

import json
import os
import time
from datetime import date

import pymupdf
import pytest

from charitycare.packet import compile_packet, purge_stale
from charitycare.packet.compiler import TMP_PREFIX
from charitycare.schemas import PacketMetric, ReviewTask
from charitycare.store import Registry
from charitycare.wording import find_banned_wording


def _pdf_text(pdf: bytes) -> list[str]:
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        return [" ".join(p.get_text().split()) for p in d]


@pytest.mark.parametrize(
    "hospital, doc",
    [("example-methodist", "application_en"), ("lone-star-health", "application_en"), ("valley-childrens", "application_es"), ("pineywoods-regional", "application_en")],
)
def test_verified_packet(form_by_id, kit, tmp_path, hospital, doc) -> None:
    f = form_by_id(hospital, doc)
    res = compile_packet(kit.packet_request(f), tmp_root=tmp_path)
    assert res.verified and res.template_verified and res.form_mode == "filled"
    n = sum(1 for x in res.readback.fields if not x.write_by_hand)
    assert n == 13 and all(x.match for x in res.readback.fields)
    assert set(res.write_by_hand) == {"ssn", "date_of_birth", "account_number", "signature", "signature_date"}
    pages = _pdf_text(res.pdf)
    assert res.page_count == len(pages) == 1 + 2 + 1  # letter, form, checklist
    assert "26 CFR 1.501(r)-6(c)(5)(i)(A)" in pages[0]
    assert "Example Grocery Co." in pages[1]
    assert "Documents to attach" in pages[-1]
    assert b"DTSTART;VALUE=DATE:20270607" in res.ics
    assert find_banned_wording(res.cover_letter) == []
    assert list(tmp_path.iterdir()) == []  # ephemeral temp dir purged


def test_unverified_template_is_never_labelled_verified(form_by_id, kit, tmp_path) -> None:
    f = form_by_id("lone-star-health")
    res = compile_packet(kit.packet_request(f, template=f.gold.model_copy(update={"verified_by": None})), tmp_root=tmp_path)
    assert res.readback.all_match and not res.verified and not res.template_verified
    assert any("not been verified" in n for n in res.notes)


def test_withheld_field_blocks_verified_label(form_by_id, kit, tmp_path) -> None:
    f = form_by_id("example-methodist")
    app = kit.APPLICANT.model_copy(update={"city": "Example Municipal Utility District Number Forty-Seven Annex, North Campus, Building C, Suite 1200"})
    res = compile_packet(kit.packet_request(f, applicant=app), tmp_root=tmp_path)
    assert not res.verified
    assert "city" in res.write_by_hand
    assert any("could not be printed and verified" in c.text for c in res.checklist)
    assert res.metric.all_match is False and res.metric.retries == 1


@pytest.mark.parametrize("case", ["hash_mismatch", "no_template"])
def test_answer_sheet_fallback(form_by_id, kit, tmp_path, case) -> None:
    f = form_by_id("hill-country-community")
    if case == "hash_mismatch":
        template = form_by_id("lone-star-health").gold.model_copy(update={"verified_by": "r"})  # another form's template
    else:
        template = None
    reg = Registry(":memory:")
    res = compile_packet(kit.packet_request(f, template=template), registry=reg, tmp_root=tmp_path)
    assert res.form_mode == "answer_sheet" and not res.verified
    assert res.readback.template_id == f"answer-sheet:{f.gold.doc_sha256}"
    assert all(x.match for x in res.readback.fields)
    pages = _pdf_text(res.pdf)
    assert res.page_count == 1 + 2 + 1 + 1  # letter, blank official form (2), answer sheet, checklist
    assert "Example Grocery Co." not in pages[1] + pages[2]  # the official form stays blank
    assert "Answer sheet" in pages[3] and "Example Grocery Co." in pages[3]
    task = res.review_tasks[0]
    assert task.kind == "form_template" and task.template_id == f.gold.doc_sha256 and task.priority == "high"
    assert reg.get(ReviewTask, id=task.id) is not None
    compile_packet(kit.packet_request(f, template=template), registry=reg, tmp_root=tmp_path)  # idempotent task
    assert len(reg.open_review_tasks()) == 1


def test_packet_metric_holds_no_household_data(form_by_id, kit, tmp_path) -> None:
    reg = Registry(":memory:")
    res = compile_packet(kit.packet_request(form_by_id("example-methodist")), registry=reg, tmp_root=tmp_path, extra_cost_usd=0.03)
    rows = reg.select(PacketMetric)
    assert len(rows) == 1 and rows[0] == res.metric
    assert rows[0].cost_usd == pytest.approx(0.03) and rows[0].n_fields == 18
    dumped = json.dumps(rows[0].model_dump(mode="json"))
    for secret in ("María", "Example Lane", "49700", "49,700", "555-0142", "Bexar", "78229"):
        assert secret not in dumped
    assert set(PacketMetric.model_fields) == {"id", "template_id", "n_fields", "all_match", "retries", "latency_ms", "cost_usd"}


def test_logs_are_allowlisted(form_by_id, kit, tmp_path, caplog) -> None:
    caplog.set_level("INFO", logger="charitycare.packet")
    compile_packet(kit.packet_request(form_by_id("example-methodist")), tmp_root=tmp_path)
    text = caplog.text
    assert "template=" in text and "María" not in text and "49,700" not in text


def test_hardship_draft_is_appended(form_by_id, kit, tmp_path) -> None:
    res = compile_packet(kit.packet_request(form_by_id("example-methodist"), hardship_circumstances="Hours were cut."), tmp_root=tmp_path)
    assert res.hardship_letter and "Hours were cut." in res.hardship_letter
    assert res.page_count == 5 and "DRAFT" in _pdf_text(res.pdf)[-1]


def test_deadline_computed_when_engine_gave_none(form_by_id, kit, tmp_path) -> None:
    req = kit.packet_request(form_by_id("example-methodist"), determination=kit.determination(federal_deadline=None, policy_deadline=date(2027, 1, 1)))
    res = compile_packet(req, tmp_root=tmp_path)
    assert b"20270607" in res.ics and b"20270101" in res.ics


def test_temp_dir_purged_on_error(form_by_id, kit, tmp_path) -> None:
    req = kit.packet_request(form_by_id("example-methodist"), form_pdf=b"not a pdf", template=None)
    with pytest.raises(pymupdf.FileDataError):
        compile_packet(req, tmp_root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_purge_stale(tmp_path) -> None:
    old, new = tmp_path / f"{TMP_PREFIX}old", tmp_path / f"{TMP_PREFIX}new"
    old.mkdir()
    new.mkdir()
    (old / "form.pdf").write_bytes(b"x")
    past = time.time() - 3600
    os.utime(old, (past, past))
    assert purge_stale(tmp_path) == 1
    assert not old.exists() and new.exists()

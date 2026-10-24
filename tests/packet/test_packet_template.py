"""Form templates: widget mapping, candidate detection, vision labelling and fallbacks."""

from __future__ import annotations

import pymupdf
import pytest

from charitycare.packet.template import (
    CandidateLabel,
    TemplateLabeling,
    build_template,
    detect_candidates,
    heuristic_labels,
    labeling_request,
    score_template,
)


def test_fillable_templates_match_gold(demo_forms) -> None:
    fillable = [f for f in demo_forms if f.kind == "fillable"]
    assert len(fillable) >= 5  # spaced, camel and prefixed widget names
    for f in fillable:
        b = build_template(f.pdf, use_llm=False)
        assert b.method == "widgets"
        assert b.template.verified_by is None  # H3: a person still has to verify it
        assert score_template(b.template, f.gold)["f1"] == 1.0, f.hospital
        gold_widgets = {s.key: s.widget_name for s in f.gold.field_map}
        assert {s.key: s.widget_name for s in b.template.field_map} == gold_widgets


def test_vector_flat_forms_heuristic_matches_gold(demo_forms) -> None:
    flats = [f for f in demo_forms if f.kind == "flat" and f.hospital != "pineywoods-regional"]
    assert {f.doc for f in flats} >= {"application_en", "application_es"}
    for f in flats:
        b = build_template(f.pdf, use_llm=False)
        assert b.method == "heuristic"
        assert all(c.source == "vector" for c in b.candidates)
        assert score_template(b.template, f.gold)["f1"] == 1.0, (f.hospital, f.doc)
        hand = {s.key for s in b.template.field_map if s.write_by_hand}
        assert hand == {"ssn", "date_of_birth", "account_number", "signature"}


def test_scanned_form_candidates_come_from_pixels(form_by_id) -> None:
    f = form_by_id("pineywoods-regional")
    cands = detect_candidates(f.pdf)
    assert cands and all(c.source == "raster" for c in cands)
    assert sum(c.kind == "box" for c in cands if c.page == 2) == 3
    # Without OCR there is no nearby text, so heuristics cannot label anything.
    assert all(c.context == "" for c in cands)
    assert heuristic_labels(cands).labels == []


def test_scanned_form_without_llm_has_no_template(form_by_id, empty_replay) -> None:
    b = build_template(form_by_id("pineywoods-regional").pdf, backend=empty_replay)
    assert b.method == "none" and b.template.field_map == []
    assert any("unavailable" in n for n in b.notes)
    assert b.review_task().priority == "high"


def test_scanned_form_vision_labels_from_replay(form_by_id, replay) -> None:
    f = form_by_id("pineywoods-regional")
    b = build_template(f.pdf, backend=replay)
    assert b.method == "vision"
    assert score_template(b.template, f.gold)["f1"] == 1.0
    assert b.cost_usd > 0  # simulated Opus-class cost of the one-time mapping
    task = b.review_task()
    assert task.kind == "form_template" and task.template_id == f.gold.doc_sha256


def test_vision_labels_cannot_invent_candidates_or_keys(form_by_id, kit) -> None:
    f = form_by_id("lone-star-health")

    def answer(req):
        return TemplateLabeling(
            labels=[
                CandidateLabel(candidate_id=0, key="patient_name", label="Patient name"),
                CandidateLabel(candidate_id=0, key="ssn", label="dup candidate"),
                CandidateLabel(candidate_id=999, key="city", label="made up"),
                CandidateLabel(candidate_id=3, key="favourite_colour", label="nope"),
                CandidateLabel(candidate_id=4, key="patient_name", label="dup key"),
            ]
        )

    backend = kit.FakeBackend(answer)
    b = build_template(f.pdf, backend=backend)
    assert [s.key for s in b.template.field_map] == ["patient_name", "ssn"]
    # the request carries page images and the candidate list, on the Opus tier
    req = backend.requests[0]
    assert req.model_tier == "opus" and req.prompt_version == "form_template@v1"
    assert sum(d.kind == "image" for d in req.documents) == 2


def test_labeling_request_is_keyed_by_form_hash(form_by_id) -> None:
    f = form_by_id("hill-country-community")
    req = labeling_request(f.pdf, detect_candidates(f.pdf))
    assert req.doc_sha256 == f.gold.doc_sha256


def test_score_template_penalizes_wrong_rects(form_by_id) -> None:
    f = form_by_id("lone-star-health")
    moved = f.gold.model_copy(deep=True)
    moved.field_map[0].rect = [v + 100 for v in moved.field_map[0].rect]
    s = score_template(moved, f.gold)
    assert s["recall"] == pytest.approx(17 / 18)


def test_outlined_entry_boxes_are_candidates() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 100), "Employer name", fontsize=9)
    page.draw_rect(pymupdf.Rect(60, 104, 400, 122), width=0.6)
    pdf = doc.tobytes()
    b = build_template(pdf, use_llm=False)
    assert [s.key for s in b.template.field_map] == ["employer"]

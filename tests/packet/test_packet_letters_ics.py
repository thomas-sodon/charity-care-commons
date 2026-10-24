"""Cover letter, checklist, hardship draft and .ics generation."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pymupdf
import pytest

from charitycare.packet.ics import DeadlineEvent, build_ics, deadline_events
from charitycare.packet.letters import (
    HardshipDraft,
    checklist_items,
    checklist_text,
    cover_letter,
    federal_deadline,
    hardship_letter,
    navigator_summary,
    render,
    text_to_pdf,
)
from charitycare.wording import find_banned_wording

SUBMITTED = date(2026, 10, 24)


def test_cover_letter_requests_and_never_asserts(form_by_id, kit) -> None:
    req = kit.packet_request(form_by_id("example-methodist"))
    text = cover_letter(req, submitted=SUBMITTED)
    assert "26 CFR 1.501(r)-6(c)(5)(i)(A)" in text and "(c)(6)(i)(A)" in text
    assert "26 CFR 1.501(r)-6(c)(5)(i)(B)" in text  # written notice of missing information
    assert "Suspend any extraordinary collection actions" in text
    assert "2027-06-07" in text and "240th day" in text
    assert "7700 Medical Example Drive" in text and "(210) 555-0199" in text
    assert "“Patients may apply at any time within 240 days" in text and "(§6.1, p. 5)" in text
    assert find_banned_wording(text) == []
    lowered = text.lower()
    for asserted in ("likely", "free care", "qualif", "discount", "181.9"):
        assert asserted not in lowered  # the letter never states an outcome


def test_cover_letter_without_policy_or_dates(form_by_id, kit) -> None:
    req = kit.packet_request(
        form_by_id("example-methodist"), policy=None, policy_as_of=None,
        household=kit.household(first_statement_date=None), determination=kit.determination(federal_deadline=None),
    )
    text = cover_letter(req, submitted=SUBMITTED)
    assert "your Financial Assistance Policy" in text
    assert "240th day" not in text and find_banned_wording(text) == []


def test_federal_deadline_fallback(kit) -> None:
    det = kit.determination(federal_deadline=None)
    assert federal_deadline(det, date(2026, 10, 10)) == date(2027, 6, 7)
    assert federal_deadline(det, None) is None


def test_checklist_cites_required_documents(form_by_id, kit) -> None:
    req = kit.packet_request(form_by_id("example-methodist"))
    hand = [s for s in req.template.field_map if s.write_by_hand or s.key == "signature_date"]
    sec = checklist_items(req, hand, [])
    docs = sec["documents"]
    assert len(docs) == 4
    assert all(d.citation and d.citation.startswith("§6.2, p. 5") for d in docs)
    assert sec["conditions"][0].text.startswith("asset test")
    assert any("Social Security number" in a.text for a in sec["actions"])
    assert any(c.text.startswith("Fax: (210) 555-0199") for c in sec["channels"])
    text = checklist_text(req, sec, submitted=SUBMITTED)
    assert "2027-06-07" in text and "Dollar For" in text
    assert find_banned_wording(text) == []


def test_checklist_when_documents_abstained(form_by_id, kit) -> None:
    f = form_by_id("hill-country-community")
    pol = f.policy.model_copy(deep=True)
    pol.required_documents = type(pol.required_documents)(abstain_reason="not_addressed")
    req = kit.packet_request(f, policy=pol)
    text = checklist_text(req, checklist_items(req, [], []), submitted=SUBMITTED)
    assert "does not list required documents (not_addressed)" in text


def test_checklist_lists_withheld_fields(form_by_id, kit) -> None:
    f = form_by_id("lone-star-health")
    req = kit.packet_request(f)
    spec = next(s for s in f.gold.field_map if s.key == "employer")
    sec = checklist_items(req, [], [(spec, "Example Grocery Co.")])
    assert any("could not be printed and verified" in a.text and "Example Grocery Co." in a.text for a in sec["actions"])


def test_hardship_letter_template_and_llm(form_by_id, kit) -> None:
    req = kit.packet_request(form_by_id("example-methodist"), hardship_circumstances="Hours cut in August. SSN 123-45-6789")
    text, cost = hardship_letter(req, submitted=SUBMITTED)
    assert text.startswith("DRAFT") and "Hours cut in August" in text and "123-45-6789" not in text and cost == 0

    good = kit.FakeBackend(lambda r: HardshipDraft(body="My hours at work were cut in August."))
    text, cost = hardship_letter(req, submitted=SUBMITTED, use_llm=True, backend=good)
    assert "My hours at work were cut in August." in text and cost > 0
    assert "123-45-6789" not in good.requests[0].user

    bad = kit.FakeBackend(lambda r: HardshipDraft(body="I am eligible for free care."))
    text, _ = hardship_letter(req, submitted=SUBMITTED, use_llm=True, backend=bad)
    assert "I am eligible" not in text and "Hours cut in August" in text


def test_hardship_letter_placeholder_without_notes(form_by_id, kit, empty_replay) -> None:
    req = kit.packet_request(form_by_id("example-methodist"), hardship_circumstances="")
    text, _ = hardship_letter(req, submitted=SUBMITTED, use_llm=True, backend=empty_replay)
    assert "[Describe what changed" in text


def test_render_rejects_banned_wording() -> None:
    with pytest.raises(ValueError):
        render("checklist.txt.j2", hospital_name="H", actions=[], documents=[], no_documents_note="You are eligible",
               conditions=[], channels=[], deadline_text=None, disclaimer="")


def test_navigator_summary(kit) -> None:
    text = navigator_summary(kit.determination(provisional_fields_used=["fpg_tiers"]), SUBMITTED)
    assert text.startswith("Likely eligible for free care")
    assert "fpg_tiers" in text and "2027-06-07" in text


def test_text_to_pdf_paginates() -> None:
    pdf = text_to_pdf("Title\n" + "\n\n".join(f"Paragraph {i} " * 20 for i in range(60)), title="T", heading_first=True)
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        assert d.page_count > 1 and d.metadata["title"] == "T"
        assert "Paragraph 59" in "".join(p.get_text() for p in d)


def test_ics_structure() -> None:
    stamp = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    events = deadline_events("Example Methodist Hospital, San Antonio", date(2027, 6, 7), date(2027, 6, 7))
    assert len(events) == 1  # the policy deadline is not repeated when equal
    raw = build_ics(events, stamp=stamp)
    text = raw.decode()
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20270607" in text and "DTEND;VALUE=DATE:20270608" in text
    assert "TRIGGER:-P14D" in text and "DTSTAMP:20261024T120000Z" in text
    assert "Example Methodist Hospital\\, San" in text.replace("\r\n ", "")
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert build_ics(events, stamp=stamp) == raw  # deterministic for a fixed stamp


def test_ics_two_deadlines_and_escaping() -> None:
    events = deadline_events("H", date(2027, 6, 7), date(2027, 1, 1))
    assert [e.day for e in events] == [date(2027, 6, 7), date(2027, 1, 1)]
    text = build_ics([DeadlineEvent(date(2027, 1, 1), "a;b,c\\d", "line1\nline2 ñ" * 20)]).decode()
    assert r"SUMMARY:a\;b\,c\\d" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    assert "line1\\nline2" in text.replace("\r\n ", "")

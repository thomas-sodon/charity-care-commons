"""Quote verifier and numeric grounding: no quote, no publish."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from charitycare.compile.textlayer import DocText
from charitycare.compile.verify import (
    FUZZY_THRESHOLD,
    best_similarity,
    check_field,
    match_on_page,
    normalize_variants,
    numbers_in,
    ungrounded_numbers,
    verify_evidence,
    with_spans,
)
from charitycare.demo.spec import HOSPITALS
from charitycare.pdftext import PageText, normalize_text
from charitycare.schemas import CompiledPolicy, Evidence, Field, FPGTier

HOSPITAL_IDS = [h.id for h in HOSPITALS]

PAGE = (
    "4.2 Free Care. Patients with household income at or below 200% of the Federal Poverty Guide-\n"
    "lines qualify for free care. Apply within 240 days after the first\npost-\ndischarge statement.\n"
    "The Hospital will refund “any payments” over the amount owed.  ﬁnancial counselors help.\n"
)
SHA = "a" * 64


def doc(*pages: str) -> dict[str, DocText]:
    d = DocText(SHA, tuple(PageText(i, t, bool(t.strip()), ocr_unavailable=not t.strip()) for i, t in enumerate(pages, 1)))
    return {SHA: d}


def ev(quote: str, page: int = 1, sha: str = SHA) -> Evidence:
    return Evidence(doc_sha256=sha, page=page, quote=quote)


@pytest.mark.parametrize(
    "quote,method",
    [
        ("Patients with household income at or below 200%", "exact"),
        ("PATIENTS WITH   household\tincome", "exact"),  # case and whitespace
        ('refund "any payments" over', "exact"),  # smart quotes
        ("financial counselors help", "exact"),  # NFKC ligature
        ("Federal Poverty Guidelines qualify for free care", "exact_dehyphenated"),  # assis-\ntance style
        ("after the first post-discharge statement", "exact_dehyphenated"),  # compound kept
    ],
)
def test_normalized_exact_matches(quote: str, method: str) -> None:
    qc = verify_evidence(ev(quote), doc(PAGE))
    assert qc.ok and qc.method == method, qc.describe()
    assert qc.span is not None


def test_exact_span_is_in_normalized_page_coordinates() -> None:
    qc = verify_evidence(ev("household income at or below 200%"), doc(PAGE))
    start, end = qc.span
    assert normalize_text(PAGE)[start:end] == "household income at or below 200%"


def test_fuzzy_accepts_ocr_noise_at_98_percent() -> None:
    page = "Patients whose out-of-pocket medical bills for the prior 12 months exceed 20% of household income also qualify for a discount."
    noisy = page.replace("discount", "disc0unt")
    qc = verify_evidence(ev(noisy), doc(page))
    assert qc.ok and qc.method == "fuzzy" and qc.similarity >= FUZZY_THRESHOLD


def test_paraphrase_is_rejected() -> None:
    qc = verify_evidence(ev("The Hospital will refund payments over what the patient owes."), doc(PAGE))
    assert not qc.ok and qc.reason == "not_found" and qc.similarity < FUZZY_THRESHOLD


def test_changed_number_is_rejected_even_when_similar() -> None:
    qc = verify_evidence(ev("Patients with household income at or below 250% of the Federal Poverty"), doc(PAGE))
    assert not qc.ok


def test_wrong_page_reports_where_the_quote_is() -> None:
    qc = verify_evidence(ev("qualify for free care", page=2), doc("Nothing relevant on this page.", PAGE))
    assert qc.ok
    qc = verify_evidence(ev("a sentence printed nowhere at all", page=2), doc("Nothing relevant on this page.", PAGE))
    assert not qc.ok and qc.reason == "not_found" and qc.found_on_pages == ()
    qc = verify_evidence(ev("qualify for free care", page=1), doc("Nothing relevant on this page.", PAGE))
    assert not qc.ok and qc.reason == "wrong_page" and qc.found_on_pages == (2,)


@pytest.mark.parametrize(
    "evidence,reason",
    [
        (ev("qualify for free care", sha="b" * 64), "unknown_document"),
        (ev("qualify for free care", page=9), "page_out_of_range"),
        (ev("free", page=1), "quote_too_short"),
        (ev("   ", page=1), "empty_quote"),
    ],
)
def test_structural_failures(evidence: Evidence, reason: str) -> None:
    assert verify_evidence(evidence, doc(PAGE)).reason == reason


def test_image_only_page_cannot_verify() -> None:
    qc = verify_evidence(ev("qualify for free care", page=2), doc(PAGE, ""))
    assert not qc.ok and qc.reason == "no_text_layer"


def test_numbers_in_reads_digits_money_and_words() -> None:
    assert {200, 10000, 31.5, 2, 12} <= {float(n) for n in numbers_in("200% and $10,000, 31.5%, two stubs, twelve months")}


def test_numeric_grounding_catches_misread_tier() -> None:
    quote = "Patients with household income at or below 200% of the Federal Poverty Guidelines qualify for free care"
    good = Field[list[FPGTier]](value=[FPGTier(max_pct_inclusive=200, benefit="free")], evidence=[ev(quote)])
    bad = Field[list[FPGTier]](value=[FPGTier(max_pct_inclusive=250, benefit="free")], evidence=[ev(quote)])
    assert ungrounded_numbers("fpg_tiers", good) == []
    assert ungrounded_numbers("fpg_tiers", bad) == ["fpg_tiers[0].max_pct_inclusive = 250"]


def test_check_field_requires_evidence_for_values() -> None:
    fc = check_field("policy_title", Field[str](value="Financial Assistance Policy"), doc(PAGE))
    assert not fc.ok and fc.missing_evidence


def test_abstention_with_quote_is_checked_but_not_grounded() -> None:
    f = Field[str](abstain_reason="ambiguous_may_clause", evidence=[ev("qualify for free care")])
    fc = check_field("refund_policy", f, doc(PAGE))
    assert fc.ok and with_spans(f, fc).evidence[0].char_start is not None


def test_with_spans_drops_failed_quotes() -> None:
    f = Field[str](value="x", evidence=[ev("qualify for free care"), ev("this text is not on the page")])
    out = with_spans(f, check_field("policy_title", f, doc(PAGE)))
    assert [e.quote for e in out.evidence] == ["qualify for free care"]


@pytest.mark.parametrize("hospital_id", HOSPITAL_IDS)
def test_every_gold_quote_verifies_except_image_pages(hospital_id: str, gold_policy, fap) -> None:
    d = fap(hospital_id)
    for key, f in gold_policy(hospital_id).fields().items():
        fc = check_field(key, f, {d.sha256: d})
        if any(e.page in d.unreadable_pages for e in f.evidence):
            assert fc.illegible, key
        else:
            assert fc.ok or f.abstain_reason, (key, fc.problems())


def test_deliberate_fixture_errors_fail_verification(demo_corpus, gold, fap) -> None:
    errors = json.loads((demo_corpus.fixtures / "DELIBERATE_ERRORS.json").read_text())
    assert {e["kind"] for e in errors} == {"quote_not_verbatim", "wrong_page", "misread_value"}
    for e in errors:
        d = fap(e["hospital_id"])
        out = json.loads((demo_corpus.fixtures / "compile_policy@v1" / "sonnet" / f"{d.sha256}.json").read_text())["output"]
        f = CompiledPolicy.model_validate(out).fields()[e["field"]]
        fc = check_field(e["field"], f, {d.sha256: d})
        assert not fc.ok, e
        if e["kind"] == "wrong_page":
            assert any(q.reason == "wrong_page" for q in fc.quote_checks)
        if e["kind"] == "misread_value":
            assert fc.quotes_ok and fc.ungrounded


def test_best_similarity_handles_empty_inputs() -> None:
    assert best_similarity("", "abc") == (0.0, -1, -1)
    assert match_on_page("", "anything at all goes here and it is long enough")[0] == "none"


_word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789%$", min_size=1, max_size=8)


@settings(max_examples=60, deadline=None)
@given(words=st.lists(_word, min_size=12, max_size=40), data=st.data())
def test_property_any_word_span_of_the_page_verifies(words: list[str], data: st.DataObject) -> None:
    seps = data.draw(st.lists(st.sampled_from([" ", "\n", "  ", "\t"]), min_size=len(words), max_size=len(words)))
    page = "".join(w + s for w, s in zip(words, seps))
    i = data.draw(st.integers(0, len(words) - 4))
    j = data.draw(st.integers(i + 3, len(words)))
    quote = " ".join(words[i:j])
    if len(normalize_variants(quote)["plain"]) < 8:
        return
    assert verify_evidence(ev(quote), doc(page)).ok


@settings(max_examples=40, deadline=None)
@given(words=st.lists(st.text(alphabet="abcdefghij", min_size=3, max_size=8), min_size=10, max_size=20), data=st.data())
def test_property_quotes_absent_from_the_page_fail(words: list[str], data: st.DataObject) -> None:
    page = " ".join(words)
    foreign = " ".join(data.draw(st.lists(st.text(alphabet="klmnopqrstuvwxyz", min_size=4, max_size=8), min_size=6, max_size=10)))
    assert not verify_evidence(ev(foreign), doc(page)).ok

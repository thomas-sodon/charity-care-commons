"""Round-trip tests (S5), Schedule H cross-check (S6) and facility -> policy resolution."""

from __future__ import annotations

from datetime import date

import pytest

from charitycare.compile.crosscheck import (
    cross_check,
    document_cross_check,
    resolve,
    select_section,
    worst,
)
from charitycare.compile.demo import schedule_h_from_spec
from charitycare.compile.mapping import (
    PolicyCandidate,
    name_similarity,
    needs_review,
    resolve_facility,
)
from charitycare.compile.roundtrip import parse_tables, round_trip
from charitycare.compile.textlayer import DocText
from charitycare.compile.verify import fuzzy_safe, verify_evidence
from charitycare.demo.spec import HOSPITALS
from charitycare.pdftext import PageText
from charitycare.schemas import (
    CrossCheckResult,
    Evidence,
    Facility,
    Field,
    FPGTier,
    PolicyFacilityMap,
    PolicyVersion,
)
from charitycare.wording import find_banned_wording

SPECS = {h.id: h for h in HOSPITALS}


# -- round trip -----------------------------------------------------------------


def test_methodist_printed_table_matches_2025(gold_policy, fap, gold) -> None:
    [rt] = round_trip(gold_policy("example-methodist"), fap("example-methodist"))
    assert rt.result == "match" and rt.year_used == 2025 == gold("example-methodist")["income_table"]["stated_year"]
    assert rt.mismatched_cells == [] and rt.unexplained_columns == []


def test_panhandle_table_labelled_2026_but_computed_from_2025(gold_policy, fap, gold) -> None:
    [rt] = round_trip(gold_policy("panhandle-university"), fap("panhandle-university"))
    expected = gold("panhandle-university")["income_table"]
    assert rt.result == "mismatch" == expected["expected_round_trip"]
    assert rt.stated_year == 2026 and rt.best_matching_year == expected["computed_from_year"] == 2025
    assert len(rt.mismatched_cells) == len(expected["rows"])


def test_parsed_table_equals_gold_rows(fap, gold) -> None:
    [t] = parse_tables(fap("panhandle-university"))
    rows = {(r["household_size"], float(r["pct"])): r["amount"] for r in gold("panhandle-university")["income_table"]["rows"]}
    parsed = {(size, pct): int(a) for size, amounts in t.rows for pct, a in zip(t.pcts, amounts)}
    assert parsed == rows and t.page == 3


def test_misread_tier_shows_up_as_an_unexplained_column(gold_policy, fap) -> None:
    pol = gold_policy("example-methodist")
    tiers = [t.model_copy(update={"max_pct_inclusive": 250.0}) if t.benefit == "free" else t.model_copy(update={"min_pct_exclusive": 250.0})
             for t in pol.fpg_tiers.value]
    bad = pol.model_copy(update={"fpg_tiers": pol.fpg_tiers.model_copy(update={"value": tiers})})
    [rt] = round_trip(bad, fap("example-methodist"))
    assert rt.result == "mismatch" and rt.unexplained_columns == [200.0] and 250.0 in rt.boundaries_not_in_table


def test_policies_without_tables_have_no_round_trip(gold_policy, fap) -> None:
    assert round_trip(gold_policy("brazos-valley"), fap("brazos-valley")) == []


def test_table_without_stated_year_uses_effective_date_year(gold_policy) -> None:
    text = "Income limits:\nFamily size   100%   200%\n  1   $15,650   $31,300\n  2   $21,150   $42,300\n"
    doc = DocText("c" * 64, (PageText(1, text, True),))
    pol = gold_policy("example-methodist")  # effective 2025-07-01 -> 2025 guidelines
    [rt] = round_trip(pol, doc)
    assert rt.stated_year is None and rt.year_used == 2025 and rt.result == "match"


# -- cross-check ---------------------------------------------------------------------


@pytest.mark.parametrize("h", HOSPITALS, ids=lambda h: h.id)
def test_gold_policy_cross_checks_match_expected(h, gold_policy, gold) -> None:
    """The cross-check on the true policy reproduces the corpus's expected results."""
    checks = cross_check(gold_policy(h.id), schedule_h_from_spec(h))
    for key, expected in gold(h.id)["schedule_h"]["expected_crosschecks"].items():
        assert worst(checks[key]).value == expected, key
    for cs in checks.values():
        for c in cs:
            assert not find_banned_wording(c.note or "")


def test_filing_period_awareness() -> None:
    assert resolve(True, date(2030, 1, 1), date(2025, 6, 30)) is CrossCheckResult.agree
    assert resolve(False, date(2026, 3, 15), date(2025, 6, 30)) is CrossCheckResult.disagree_policy_newer_than_filing
    assert resolve(False, date(2025, 1, 1), date(2025, 6, 30)) is CrossCheckResult.disagree_unexplained
    assert resolve(False, None, date(2025, 6, 30)) is CrossCheckResult.disagree_filing_lag_possible


def test_methodist_asset_test_is_a_discrepancy_for_review(gold_policy) -> None:
    [c] = cross_check(gold_policy("example-methodist"), schedule_h_from_spec(SPECS["example-methodist"]))["asset_test"]
    assert c.result is CrossCheckResult.disagree_unexplained
    assert c.external_ref == "irs:202601999349300101|Part V line 13c|facility 1"
    assert "discrepancy for review" in c.note and c.filing_period_end == date(2025, 12, 31)


def test_pineywoods_tiers_disagree_because_policy_is_newer(gold_policy) -> None:
    [c] = cross_check(gold_policy("pineywoods-regional"), schedule_h_from_spec(SPECS["pineywoods-regional"]))["fpg_tiers"]
    assert c.result is CrossCheckResult.disagree_policy_newer_than_filing
    assert c.external_value == {"free_pct": 125.0, "discount_pct": 250.0} and c.policy_date == date(2026, 3, 15)


def test_unpublished_fields_are_not_compared(gold_policy) -> None:
    checks = cross_check(gold_policy("example-methodist"), schedule_h_from_spec(SPECS["example-methodist"]), published={"residency"})
    assert set(checks) == {"residency"}


def test_missing_lines_are_not_reported(gold_policy) -> None:
    sec = schedule_h_from_spec(SPECS["example-methodist"]).model_copy(
        update={"line_13_criteria_explained": None, "line_13c_asset_level": None, "line_13a_free_pct": None,
                "line_13a_discount_pct": None, "line_22_agb_method": None}
    )
    checks = cross_check(gold_policy("example-methodist"), sec)
    for key in ("asset_test", "fpg_tiers", "agb_method"):
        assert [c.result for c in checks[key]] == [CrossCheckResult.not_reported]


def test_select_section_by_group_name_or_single() -> None:
    lone, meth = schedule_h_from_spec(SPECS["lone-star-health"]), schedule_h_from_spec(SPECS["example-methodist"])
    fac = Facility(id="tx-459903", name="Lone Star Example Medical Center Plano", ein=lone.ein, schedule_h_group_letter="A")
    other = lone.model_copy(update={"id": lone.object_id + ":B", "group_letter": "B", "facility_names": ["X"]})
    assert select_section([other, lone], [fac]) is lone
    assert select_section([meth], [], ["Example Methodist Hospital"]) is meth
    assert select_section([meth]) is meth
    assert select_section([]) is None
    older = meth.model_copy(update={"id": "old:1", "object_id": "old", "tax_period_end": date(2023, 12, 31)})
    assert select_section([older, meth]) is meth


def test_document_checks_use_section_c_narrative_urls() -> None:
    h = SPECS["brazos-valley"]
    sec = schedule_h_from_spec(h)
    assert sec.line_16a_fap_url == "SEE PART V, SECTION C"
    urls = {"fap": f"{h.base_url}/financial-assistance/fap.pdf", "application": f"{h.base_url}/financial-assistance/application.pdf"}
    checks = document_cross_check(sec, urls, ["en"])
    assert [c.result for c in checks] == [CrossCheckResult.agree, CrossCheckResult.agree]
    moved = document_cross_check(sec, {"application": "https://elsewhere.example/app.pdf"}, ["en"], date(2025, 1, 1))
    assert moved[0].result is CrossCheckResult.disagree_unexplained


def test_translations_checked_only_when_found() -> None:
    sec = schedule_h_from_spec(SPECS["valley-childrens"])
    assert [c.external_ref.split("|")[1] for c in document_cross_check(sec, {}, ["en", "es"])] == ["Part V line 16i"]
    assert document_cross_check(sec, {}, ["en"]) == []


# -- fuzzy guard (semantic edits never pass the similarity threshold) -----------------------


def test_fuzzy_guard_rejects_number_and_negation_edits() -> None:
    base = "Patients whose household liquid assets exceed the threshold are not eligible for free or discounted care under this policy."
    assert not fuzzy_safe(base, base.replace("are not", "are"))
    assert not fuzzy_safe("within 240 days", "within 210 days")
    assert fuzzy_safe("disc0unt applies", "discount applies")
    sha = "d" * 64
    doc = DocText(sha, (PageText(1, base, True),))
    assert not verify_evidence(Evidence(doc_sha256=sha, page=1, quote=base.replace("are not", "are")), {sha: doc}).ok


# -- facility -> policy resolution ----------------------------------------------------------


def _pv(i: str, ein: str = "990100202") -> PolicyVersion:
    return PolicyVersion(id=i, organization_ein=ein, as_of=date(2026, 10, 1))


def _covered(*names: str) -> Field[list[str]]:
    return Field[list[str]](value=list(names), evidence=[Evidence(doc_sha256="e" * 64, page=7, quote="covered by this Policy")])


def test_named_in_policy_is_high_confidence() -> None:
    fac = Facility(id="tx-459903", name="Lone Star Example Medical Center Plano", ein="990100202")
    m = resolve_facility(fac, [PolicyCandidate(_pv("p1"), _covered("Lone Star Example Medical Center Dallas",
                                                                     "Lone Star Example Medical Center Plano"), True)])
    assert m.method == "named_in_policy" and m.confidence == 0.95 and m.evidence and not needs_review(m)


def test_fuzzy_name_is_lower_confidence() -> None:
    fac = Facility(id="tx-1", name="Lone Star Example Med Center Plano", ein="990100202")
    m = resolve_facility(fac, [PolicyCandidate(_pv("p1"), _covered("Lone Star Example Medical Center Plano"), True)])
    assert m.method == "named_in_policy" and 0.7 <= m.confidence < 0.95


def test_schedule_h_group_mapping() -> None:
    sec = schedule_h_from_spec(SPECS["lone-star-health"])
    fac = Facility(id="tx-9", name="Lone Star Example Children's Annex", ein=sec.ein, schedule_h_group_letter="A")
    m = resolve_facility(fac, [PolicyCandidate(_pv("p1"), _covered("Lone Star Example Medical Center Dallas"), True)], [sec])
    assert m.method == "schedule_h_group" and m.confidence == 0.85


def test_system_default_and_review_threshold() -> None:
    fac = Facility(id="tx-9", name="Unlisted Campus", ein="990100202")
    m = resolve_facility(fac, [PolicyCandidate(_pv("p1"))])
    assert m.method == "system_default" and m.confidence == 0.6 and needs_review(m)
    assert resolve_facility(fac, [PolicyCandidate(_pv("p1")), PolicyCandidate(_pv("p2"))]) is None
    assert needs_review(None)


def test_manual_mapping_wins() -> None:
    fac = Facility(id="tx-9", name="Lone Star Example Medical Center Plano", ein="990100202")
    manual = PolicyFacilityMap(policy_version_id="p2", facility_id="tx-9", method="manual", confidence=1.0)
    m = resolve_facility(fac, [PolicyCandidate(_pv("p1"), _covered("Lone Star Example Medical Center Plano"), True)], existing=[manual])
    assert m is manual and not needs_review(m)


def test_name_similarity() -> None:
    assert name_similarity("Example Valley Children's Hospital", "EXAMPLE VALLEY CHILDRENS HOSPITAL") == 1.0
    assert name_similarity("Lone Star Example Medical Center Dallas", "Lone Star Example Medical Center Plano") < 0.9


def test_tier_helper_types() -> None:
    assert FPGTier(max_pct_inclusive=200, benefit="free").min_pct_exclusive is None

"""Scorers on synthetic predictions (no pipeline needed)."""

from __future__ import annotations

import copy
from datetime import date

import pytest

from evals import scorers as S
from evals.cases import Case
from evals.citations import PageStore, verify_field, verify_published

SHA = "a" * 64


def _case(gold, pred=None, **kw) -> Case:
    return Case(hospital_id=kw.pop("hospital_id", "h1"), gold=gold, pred=copy.deepcopy(pred if pred is not None else gold), **kw)


def test_perfect_prediction_scores_one(gold) -> None:
    c = _case(gold)
    assert S.numeric_threshold_exact([c]) == (1.0, 3)
    assert S.threshold_abstention_rate([c]) == (0.0, 3)
    assert S.categorical_macro_f1([c])[0] == 1.0
    assert S.list_item_f1([c]) == (1.0, 2)
    assert S.field_precision([c]) == (1.0, 5)
    assert S.field_recall([c]) == (1.0, 5)
    assert S.correct_abstention_rate([c]) == (1.0, 1)


def test_misread_threshold_and_abstention(gold, make_field) -> None:
    pred = copy.deepcopy(gold)
    pred["fpg_tiers"]["value"][0]["max_pct_inclusive"] = 250.0  # the gulf-coast style misread
    pred["asset_test"] = make_field(abstain="illegible")
    c = _case(gold, pred)
    # pairs: free 200 vs 250 (wrong), discount 400 vs 400 (right), asset abstained (excluded)
    assert S.numeric_threshold_exact([c]) == (0.5, 2)
    assert S.threshold_abstention_rate([c]) == (pytest.approx(1 / 3), 3)
    assert S.field_recall([c])[0] == pytest.approx(3 / 5)


def test_unpublished_fields_do_not_count_as_answers(gold) -> None:
    c = _case(gold, status={"fpg_tiers": "abstained"})
    assert S.commons_free_cutoff_accuracy([c]) == (0.0, 1)
    assert S.field_precision([c]) == (1.0, 4)


def test_list_f1_counts_items(gold, make_field) -> None:
    pred = copy.deepcopy(gold)
    pred["required_documents"] = make_field([{"doc_type": "pay_stub"}, {"doc_type": "bank_statement"}])
    tp, fp, fn = S.list_counts([_case(gold, pred)])
    assert (tp, fp, fn) == (1, 1, 1)
    assert S.list_item_f1([_case(gold, pred)])[0] == pytest.approx(0.5)


def test_categorical_macro_f1_penalizes_wrong_class(gold) -> None:
    pred = copy.deepcopy(gold)
    pred["income_definition"]["value"] = "net"
    assert S.categorical_macro_f1([_case(gold, pred)])[0] < 1.0


def test_citation_validity_with_synthetic_pages(gold, make_field) -> None:
    store = PageStore.from_texts({SHA: ["Patients receive free care up to 200% of the federal poverty guidelines.\nOther text."]})
    ok = make_field(1, quote="free care up to 200% of the federal poverty guidelines")
    paraphrase = make_field(1, quote="free care for people under twice the poverty line")
    wrong_page = make_field(1, quote="free care up to 200% of the federal poverty guidelines", page=2)
    assert verify_field(ok, store) == (True, [])
    assert verify_field(paraphrase, store)[0] is False
    assert verify_field(wrong_page, store)[0] is False
    assert verify_field(make_field(1), store) == (False, ["no_evidence"])
    assert verify_field(make_field(1, quote="anything at all here", sha="b" * 64), store)[1] == ["unknown_document"]
    pred = {"fpg_tiers": ok, "refund_policy": paraphrase, "asset_test": make_field(abstain="not_addressed")}
    c = _case(gold, pred)
    c.citations = verify_published(pred, c.published, store)
    assert c.citations == {"fpg_tiers": True, "refund_policy": False}
    assert S.citation_validity([c]) == (0.5, 2)


def test_schedule_h_scorers(gold) -> None:
    ccs = {
        "fpg_tiers": [{"source": "schedule_h", "result": "agree"}],
        "asset_test": [{"source": "schedule_h", "result": "disagree_unexplained"}],
    }
    tasks = [{"kind": "crosscheck_disagreement", "field_id": "pv:h1:asset_test"}]
    c = _case(gold, pred_crosschecks=ccs, tasks=tasks, policy_version_id="pv:h1",
              expected_crosschecks={"fpg_tiers": "agree", "asset_test": "disagree_unexplained", "residency": "agree"},
              policy_date=date(2025, 1, 1), tax_period_end=date(2025, 12, 31))
    assert S.crosscheck_expected_match([c]) == (pytest.approx(2 / 3), 3)
    assert S.schedule_h_free_pct_agreement([c]) == (1.0, 1)
    counts = S.crosscheck_reason_counts([c])
    assert counts["agree"] == 1 and counts["disagree_unexplained"] == 1 and counts["missing"] == 1
    assert S.disagreement_triage_rate([c]) == (1.0, 1)
    c.tasks = []
    assert S.disagreement_triage_rate([c]) == (0.0, 1)
    newer = _case(gold, pred_crosschecks=ccs, policy_date=date(2026, 3, 1), tax_period_end=date(2025, 6, 30))
    assert S.schedule_h_free_pct_agreement([newer]) == (None, 0)


def test_calibration_and_cost(gold) -> None:
    c = _case(gold, cost_usd=0.3)
    ece, n = S.calibration_ece([c])
    assert n == 5 and ece == pytest.approx(0.1)  # confidence 0.9, all correct
    assert S.cost_per_hospital_usd([c, _case(gold, cost_usd=0.1)]) == (pytest.approx(0.2), 2)


def test_naive_baseline_gap(gold) -> None:
    right = _case(gold, naive_ran=True, naive_free_cutoff=200.0)
    wrong = _case(gold, naive_ran=True, naive_free_cutoff=400.0, hospital_id="h2")
    none = _case(gold, naive_ran=True, naive_free_cutoff=None, hospital_id="h3")
    assert S.naive_free_cutoff_accuracy([right, wrong, none]) == (pytest.approx(1 / 3), 3)
    assert S.commons_free_cutoff_accuracy([right, wrong, none]) == (1.0, 3)
    assert S.naive_baseline_gap_points([right, wrong, none])[0] == pytest.approx(66.666667)
    assert S.naive_baseline_gap_points([_case(gold)]) == (None, 0)


def test_engine_and_packet_scorers() -> None:
    assert S.engine_agreement([{"agree": True}, {"agree": False}]) == (0.5, 2)
    units = [
        {"type": "form", "form_kind": "fillable", "n_fields": 10, "n_match": 10, "verified": True, "cost_usd": 0.0, "template_f1": 1.0},
        {"type": "form", "form_kind": "flat", "n_fields": 10, "n_match": 9, "verified": False, "cost_usd": 0.002, "template_f1": 0.8},
        {"type": "extraction", "n_fields": 5, "n_match": 5, "cost_usd": 0.01},
        {"type": "extraction", "n_fields": 8, "n_match": 7, "cost_usd": 0.01},
    ]
    assert S.METRICS["readback_fillable_exact"].fn(units) == (1.0, 10)
    assert S.METRICS["readback_flat_exact"].fn(units) == (0.9, 10)
    assert S.verified_packets_with_mismatch(units) == (0.0, 2)
    bad = [dict(units[1], verified=True)]
    assert S.verified_packets_with_mismatch(bad) == (1.0, 1)
    assert S.packet_verified_rate(units) == (0.5, 2)
    assert S.template_f1(units) == (pytest.approx(0.9), 2)
    assert S.extraction_field_exact(units) == (pytest.approx(12 / 13), 13)
    assert S.cost_per_packet_usd(units)[0] == pytest.approx(0.021)


def test_every_metric_is_documented_and_registered() -> None:
    for m in S.METRICS.values():
        assert m.description and m.description != m.name, m.name
        assert m.direction in ("higher", "lower", "info")

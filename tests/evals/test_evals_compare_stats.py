"""Value comparison and statistics utilities."""

from __future__ import annotations

import pytest

from evals import compare as C
from evals.stats import (
    abstain_threshold,
    bootstrap_ci,
    cohen_kappa,
    expected_calibration_error,
    f1,
    macro_f1,
)


def test_thresholds_derived_from_tiers_and_asset_test(gold) -> None:
    t = C.thresholds(gold)
    assert t == {"free_cutoff_pct": 200.0, "discount_cutoff_pct": 400.0, "asset_threshold_usd": 10000.0}


def test_thresholds_abstain_and_none(make_field) -> None:
    t = C.thresholds({"fpg_tiers": make_field(abstain="illegible"),
                      "asset_test": make_field({"applies": False, "threshold_usd": None})})
    assert t == {"free_cutoff_pct": C.ABSTAIN, "discount_cutoff_pct": C.ABSTAIN, "asset_threshold_usd": C.NONE}
    assert C.thresholds({})["asset_threshold_usd"] == C.ABSTAIN


def test_insured_only_tiers_do_not_set_the_free_cutoff(make_field) -> None:
    tiers = [{"max_pct_inclusive": 300.0, "benefit": "free", "applies_to": "insured"},
             {"max_pct_inclusive": 150.0, "benefit": "free", "applies_to": "uninsured"}]
    assert C.thresholds({"fpg_tiers": make_field(tiers)})["free_cutoff_pct"] == 150.0


def test_values_equal_normalizes() -> None:
    assert C.values_equal("Gross ", "gross")
    assert C.values_equal(200, 200.0)
    assert not C.values_equal(True, 1)
    assert C.values_equal([{"a": 1, "b": "X"}, {"a": 2}], [{"a": 2.0}, {"b": "x", "a": 1}])
    assert not C.values_equal(["a"], ["a", "b"])
    long_a = "If a patient is approved, the Hospital will refund any amount paid that exceeds the amount owed."
    long_b = "If a patient is approved the hospital will refund any amount paid that exceeds the amount owed"
    assert C.values_equal(long_a, long_b, free_text=True)
    assert not C.values_equal(long_a, "The Hospital does not refund payments.", free_text=True)


def test_field_correct(make_field) -> None:
    g = make_field(10.0)
    assert C.field_correct("agb_pct", g, make_field(10.0)) is True
    assert C.field_correct("agb_pct", g, make_field(11.0)) is False
    assert C.field_correct("agb_pct", g, make_field(abstain="not_addressed")) is None
    assert C.field_correct("agb_pct", make_field(abstain="not_addressed"), make_field(3.0)) is False


def test_categorical_and_list_items(gold) -> None:
    assert C.categorical_label(gold, "asset_test", "applies") == "true"
    assert C.categorical_label(gold, "fpg_year_rule", "") == "current_at_application"
    assert C.categorical_label(gold, "residency", "area_type") == C.ABSTAIN
    assert C.list_items(gold, "required_documents", "doc_type") == {"pay_stub", "tax_return"}
    assert C.list_items(gold, "refund_policy", "") is None


def test_f1_and_macro_f1() -> None:
    assert f1(0, 0, 0) is None
    assert f1(2, 1, 1) == pytest.approx(2 / 3)
    assert macro_f1([("a", "a"), ("b", "b")]) == 1.0
    # class a: tp=1 fp=1 fn=0 -> 2/3 ; class b: tp=0 fp=0 fn=1 -> 0
    assert macro_f1([("a", "a"), ("b", "a")]) == pytest.approx(1 / 3)
    assert macro_f1([]) is None


def test_bootstrap_is_deterministic_and_brackets_the_mean() -> None:
    units = [0.0] * 20 + [1.0] * 80
    stat = lambda xs: sum(xs) / len(xs)  # noqa: E731
    a = bootstrap_ci(units, stat, resamples=500, seed=1)
    assert a == bootstrap_ci(units, stat, resamples=500, seed=1)
    assert a.low is not None and a.high is not None
    assert a.low < 0.8 < a.high and 0.65 < a.low and a.high < 0.95
    assert bootstrap_ci([], stat) .low is None
    assert bootstrap_ci([1.0, 1.0], stat).low == 1.0


def test_ece() -> None:
    assert expected_calibration_error([]) is None
    assert expected_calibration_error([(1.0, True)] * 10) == pytest.approx(0.0)
    assert expected_calibration_error([(0.9, True)] * 10) == pytest.approx(0.1)
    # two bins: conf 0.9 acc 0.5 (n=2) and conf 0.2 acc 0 (n=2) -> (0.4*2 + 0.2*2)/4
    assert expected_calibration_error([(0.9, True), (0.9, False), (0.2, False), (0.2, False)]) == pytest.approx(0.3)


def test_abstain_threshold() -> None:
    pts = [(0.95, True)] * 9 + [(0.95, False)] + [(0.99, True)] * 10 + [(0.5, False)] * 5
    assert abstain_threshold(pts, 0.97) == 0.99
    assert abstain_threshold(pts, 0.6) == 0.5
    assert abstain_threshold([(0.9, False)], 0.97) is None


def test_cohen_kappa_textbook_example() -> None:
    # 50 items: both yes 20, A yes/B no 5, A no/B yes 10, both no 15 -> po=0.7, pe=0.5 -> kappa 0.4
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert cohen_kappa(a, b) == pytest.approx(0.4)
    assert cohen_kappa(["x", "x"], ["x", "x"]) == 1.0
    assert cohen_kappa([], []) is None
    with pytest.raises(ValueError):
        cohen_kappa(["a"], [])

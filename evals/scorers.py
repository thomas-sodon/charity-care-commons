"""Scorers (design section 12). Each takes a list of units and returns ``(value, n)``.

Compile scorers take :class:`~evals.cases.Case` objects; engine and packet
scorers take plain dicts produced by :mod:`evals.engine_suite` and
:mod:`evals.packet_suite`. All are pure functions, so they can be unit-tested
with synthetic predictions and bootstrapped by resampling units.

Rates are in [0, 1]; "points" are percentage points (0-100).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from charitycare.schemas import CompiledPolicy, CrossCheckResult
from evals import compare as C
from evals.cases import Case
from evals.stats import expected_calibration_error, f1, macro_f1, mean, ratio

Score = tuple[float | None, int]
Direction = Literal["higher", "lower", "info"]
Unit = Literal["rate", "usd", "count", "points", "ece"]


# --------------------------------------------------------------------------
# Compile: field-level scorers
# --------------------------------------------------------------------------


def threshold_pairs(cases: Sequence[Case]) -> list[tuple[str, Any, Any]]:
    """(name, gold, pred) for every derived threshold where gold has an answer."""
    out = []
    for c in cases:
        g, p = C.thresholds(c.gold), C.thresholds(c.pred)
        for k in C.THRESHOLD_KEYS:
            if g[k] != C.ABSTAIN:
                out.append((k, g[k], p[k]))
    return out


def numeric_threshold_exact(cases: Sequence[Case]) -> Score:
    """Exact match of free/discount %FPG and asset $ on non-abstained predictions."""
    pairs = [(g, p) for _, g, p in threshold_pairs(cases) if p != C.ABSTAIN]
    return ratio(sum(C.threshold_equal(g, p) for g, p in pairs), len(pairs)), len(pairs)


def threshold_abstention_rate(cases: Sequence[Case]) -> Score:
    """Share of thresholds with a gold answer on which the pipeline abstained."""
    pairs = threshold_pairs(cases)
    return ratio(sum(p == C.ABSTAIN for _, _, p in pairs), len(pairs)), len(pairs)


def correct_abstention_rate(cases: Sequence[Case]) -> Score:
    """Of fields where gold abstains, the share the pipeline also left unpublished."""
    items = [not c.published(k) or C.is_abstained(c.pred.get(k)) for c in cases for k, g in c.gold.items() if C.is_abstained(g)]
    return ratio(sum(items), len(items)), len(items)


def _published_value(c: Case, key: str) -> C.FieldDict | None:
    return c.pred.get(key) if c.published(key) else None


def categorical_macro_f1(cases: Sequence[Case]) -> Score:
    """Macro-F1 over (field, class) labels of the categorical items; abstention is a class."""
    pairs = []
    for c in cases:
        pub = {k: _published_value(c, k) for k in c.pred}
        for key, path in C.CATEGORICAL_ITEMS:
            g = C.categorical_label(c.gold, key, path)
            p = C.categorical_label(pub, key, path)  # type: ignore[arg-type]
            pairs.append((f"{key}.{path}={g}", f"{key}.{path}={p}"))
    return macro_f1(pairs), len(pairs)


def list_counts(cases: Sequence[Case]) -> tuple[int, int, int]:
    tp = fp = fn = 0
    for c in cases:
        pub = {k: _published_value(c, k) for k in c.pred}
        for _, key, ex in C.LIST_ITEMS:
            g = C.list_items(c.gold, key, ex) or set()
            p = C.list_items(pub, key, ex) or set()  # type: ignore[arg-type]
            tp += len(g & p)
            fp += len(p - g)
            fn += len(g - p)
    return tp, fp, fn


def list_item_f1(cases: Sequence[Case]) -> Score:
    """Item-level (micro) F1 of required docs, channels, presumptive criteria, counties, ..."""
    tp, fp, fn = list_counts(cases)
    return f1(tp, fp, fn), tp + fp + fn


def citation_validity(cases: Sequence[Case]) -> Score:
    """Share of published fields whose every quote is on its cited page (hard gate: 100%)."""
    verdicts = [ok for c in cases for k, ok in c.citations.items() if c.published(k)]
    return ratio(sum(verdicts), len(verdicts)), len(verdicts)


def field_precision(cases: Sequence[Case]) -> Score:
    """Of published fields, the share equal to gold."""
    items = [C.field_correct(k, c.gold.get(k), c.pred.get(k)) for c in cases for k in c.pred if c.published(k)]
    items = [x for x in items if x is not None]
    return ratio(sum(items), len(items)), len(items)


def field_recall(cases: Sequence[Case]) -> Score:
    """Of gold-answered fields, the share published and equal to gold."""
    items = [bool(c.published(k) and C.field_correct(k, g, c.pred.get(k))) for c in cases for k, g in c.gold.items()
             if not C.is_abstained(g)]
    return ratio(sum(items), len(items)), len(items)


def calibration_points(cases: Sequence[Case]) -> list[tuple[float, bool]]:
    pts = []
    for c in cases:
        for k, f in c.pred.items():
            if c.published(k):
                ok = C.field_correct(k, c.gold.get(k), f)
                if ok is not None:
                    pts.append((float(f.get("confidence") or 0.0), ok))
    return pts


def calibration_ece(cases: Sequence[Case]) -> Score:
    """Expected calibration error of self-reported confidence vs correctness (10 bins)."""
    pts = calibration_points(cases)
    return expected_calibration_error(pts), len(pts)


def per_field_table(cases: Sequence[Case]) -> dict[str, dict[str, Any]]:
    """Per-field precision/recall for the public eval report (screen 5)."""
    out: dict[str, dict[str, Any]] = {}
    for key in CompiledPolicy.field_keys():
        gold_n = pub_n = correct = abst_ok = gold_abst = 0
        for c in cases:
            g = c.gold.get(key)
            if C.is_abstained(g):
                gold_abst += 1
                abst_ok += int(not c.published(key) or C.is_abstained(c.pred.get(key)))
            else:
                gold_n += 1
            if c.published(key) and not C.is_abstained(c.pred.get(key)):
                pub_n += 1
                correct += int(bool(C.field_correct(key, g, c.pred.get(key))))
        out[key] = {
            "gold_answered": gold_n,
            "published": pub_n,
            "correct": correct,
            "precision": ratio(correct, pub_n),
            "recall": ratio(correct, gold_n),
            "gold_abstained": gold_abst,
            "abstentions_matched": abst_ok,
        }
    return out


# --------------------------------------------------------------------------
# Compile: Schedule H cross-check scorers (split by reason)
# --------------------------------------------------------------------------

CROSSCHECK_RESULTS = [r.value for r in CrossCheckResult] + ["missing"]


def primary_result(c: Case, key: str) -> str:
    """The first Schedule H cross-check result for a field, or ``missing`` (field not published)."""
    for cc in c.pred_crosschecks.get(key, []):
        if cc.get("source", "schedule_h") == "schedule_h":
            return str(cc["result"])
    return "missing"


def crosscheck_expected_match(cases: Sequence[Case]) -> Score:
    """Share of gold-expected cross-check outcomes the pipeline reproduced."""
    items = [primary_result(c, k) == v for c in cases for k, v in c.expected_crosschecks.items()]
    return ratio(sum(items), len(items)), len(items)


def schedule_h_free_pct_agreement(cases: Sequence[Case]) -> Score:
    """Agreement on free %FPG (13a) where the policy date is on or before the filing period end."""
    items = []
    for c in cases:
        if c.policy_date and c.tax_period_end and c.policy_date <= c.tax_period_end:
            r = primary_result(c, "fpg_tiers")
            if r != "missing":
                items.append(r == CrossCheckResult.agree.value)
    return ratio(sum(items), len(items)), len(items)


def crosscheck_reason_counts(cases: Sequence[Case]) -> dict[str, int]:
    """Every Schedule H cross-check the pipeline produced, counted by result."""
    counts = dict.fromkeys(CROSSCHECK_RESULTS, 0)
    for c in cases:
        for ccs in c.pred_crosschecks.values():
            for cc in ccs:
                if cc.get("source", "schedule_h") == "schedule_h":
                    counts[str(cc["result"])] = counts.get(str(cc["result"]), 0) + 1
        for k in c.expected_crosschecks:
            if primary_result(c, k) == "missing":
                counts["missing"] += 1
    return counts


def _reason_scorer(result: str) -> Callable[[Sequence[Case]], Score]:
    def score(cases: Sequence[Case]) -> Score:
        n = crosscheck_reason_counts(cases)[result]
        return float(n), n

    score.__doc__ = f"Count of Schedule H cross-checks resolving to {result}."
    return score


def disagreement_triage_rate(cases: Sequence[Case]) -> Score:
    """Share of unexplained Schedule H disagreements that opened a review task (target 100%)."""
    items = []
    for c in cases:
        for key, ccs in c.pred_crosschecks.items():
            if any(cc.get("result") == CrossCheckResult.disagree_unexplained.value for cc in ccs):
                fid = f"{c.policy_version_id}:{key}" if c.policy_version_id else None
                items.append(any(t.get("kind") == "crosscheck_disagreement" and (fid is None or t.get("field_id") == fid)
                                 for t in c.tasks))
    return ratio(sum(items), len(items)), len(items)


# --------------------------------------------------------------------------
# Compile: cost, errors, naive baseline
# --------------------------------------------------------------------------


def cost_per_hospital_usd(cases: Sequence[Case]) -> Score:
    """Mean inference cost per hospital compile (simulated at section 13 prices on replay)."""
    return mean([c.cost_usd for c in cases]), len(cases)


def compile_error_rate(cases: Sequence[Case]) -> Score:
    """Share of hospitals whose compile failed (LLM error, missing fixture or document)."""
    return ratio(sum(c.error is not None for c in cases), len(cases)), len(cases)


def _free_answer_correct(c: Case, answer: Any) -> bool:
    gold = C.thresholds(c.gold)["free_cutoff_pct"]
    return answer is not None and answer != C.ABSTAIN and gold != C.ABSTAIN and C.threshold_equal(gold, answer)


def naive_free_cutoff_accuracy(cases: Sequence[Case]) -> Score:
    """Naive agent (same tier + web search, no pipeline): free-care cutoff correct, over all hospitals."""
    items = [_free_answer_correct(c, c.naive_free_cutoff) for c in cases if c.naive_ran]
    return ratio(sum(items), len(items)), len(items)


def commons_free_cutoff_accuracy(cases: Sequence[Case]) -> Score:
    """Commons free-care cutoff published and correct, over all hospitals (abstention counts as wrong)."""
    items = [_free_answer_correct(c, C.thresholds({k: f for k, f in c.pred.items() if c.published(k)})["free_cutoff_pct"])
             for c in cases]
    return ratio(sum(items), len(items)), len(items)


def naive_baseline_gap_points(cases: Sequence[Case]) -> Score:
    """Commons minus naive free-cutoff accuracy, in percentage points (expect >= 20)."""
    ran = [c for c in cases if c.naive_ran]
    a, _ = commons_free_cutoff_accuracy(ran)
    b, n = naive_free_cutoff_accuracy(ran)
    return (None if a is None or b is None else round(100 * (a - b), 6)), n


# --------------------------------------------------------------------------
# Engine and packet scorers (units are dicts)
# --------------------------------------------------------------------------


def engine_agreement(units: Sequence[dict[str, Any]]) -> Score:
    """Engine vs oracle on synthetic households: every compared key equal (target 100%)."""
    return ratio(sum(bool(u["agree"]) for u in units), len(units)), len(units)


def _forms(units: Sequence[dict[str, Any]], kind: str | None = None) -> list[dict[str, Any]]:
    return [u for u in units if u.get("type") == "form" and (kind is None or u.get("form_kind") == kind)]


def _readback(kind: str) -> Callable[[Sequence[dict[str, Any]]], Score]:
    def score(units: Sequence[dict[str, Any]]) -> Score:
        forms = _forms(units, kind)
        n = sum(u["n_fields"] for u in forms)
        return ratio(sum(u["n_match"] for u in forms), n), n

    score.__doc__ = f"Field-level readback exact match on {kind} forms (after at most one retry)."
    return score


def verified_packets_with_mismatch(units: Sequence[dict[str, Any]]) -> Score:
    """Packets labelled verified while any filled field mismatched (must be 0)."""
    forms = _forms(units)
    return float(sum(1 for u in forms if u["verified"] and u["n_match"] < u["n_fields"])), len(forms)


def packet_verified_rate(units: Sequence[dict[str, Any]]) -> Score:
    """Share of demo forms whose every filled field read back exactly (packet labelled verified)."""
    forms = _forms(units)
    return ratio(sum(bool(u["verified"]) for u in forms), len(forms)), len(forms)


def template_f1(units: Sequence[dict[str, Any]]) -> Score:
    """Mean F1 of automatically built form templates vs the gold templates."""
    vals = [u["template_f1"] for u in _forms(units) if u.get("template_f1") is not None]
    return mean(vals), len(vals)


def extraction_field_exact(units: Sequence[dict[str, Any]]) -> Score:
    """Bill and pay-stub field exact match against synthetic ground truth."""
    ex = [u for u in units if u.get("type") == "extraction"]
    n = sum(u["n_fields"] for u in ex)
    return ratio(sum(u["n_match"] for u in ex), n), n


def cost_per_packet_usd(units: Sequence[dict[str, Any]]) -> Score:
    """One packet = one bill + two pay stubs extracted + one form filled and read back."""
    ex = [u for u in units if u.get("type") == "extraction"]
    forms = _forms(units)
    if not forms or not ex:
        return None, len(forms)
    per_set = {u.get("doc_set", 0) for u in ex} or {0}
    extraction = sum(u["cost_usd"] for u in ex) / len(per_set)
    return extraction + (mean([u["cost_usd"] for u in forms]) or 0.0), len(forms)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSpec:
    name: str
    fn: Callable[[Sequence[Any]], Score]
    direction: Direction
    unit: Unit
    kind: Literal["compile", "engine", "packet"]

    @property
    def description(self) -> str:
        lines = (self.fn.__doc__ or "").strip().splitlines()
        return lines[0] if lines else self.name


_SPECS: list[MetricSpec] = [
    MetricSpec("numeric_threshold_exact", numeric_threshold_exact, "higher", "rate", "compile"),
    MetricSpec("threshold_abstention_rate", threshold_abstention_rate, "lower", "rate", "compile"),
    MetricSpec("correct_abstention_rate", correct_abstention_rate, "higher", "rate", "compile"),
    MetricSpec("categorical_macro_f1", categorical_macro_f1, "higher", "rate", "compile"),
    MetricSpec("list_item_f1", list_item_f1, "higher", "rate", "compile"),
    MetricSpec("citation_validity", citation_validity, "higher", "rate", "compile"),
    MetricSpec("field_precision", field_precision, "higher", "rate", "compile"),
    MetricSpec("field_recall", field_recall, "higher", "rate", "compile"),
    MetricSpec("crosscheck_expected_match", crosscheck_expected_match, "higher", "rate", "compile"),
    MetricSpec("schedule_h_free_pct_agreement", schedule_h_free_pct_agreement, "higher", "rate", "compile"),
    *[MetricSpec(f"schedule_h_{r}_count", _reason_scorer(r), "info", "count", "compile") for r in CROSSCHECK_RESULTS],
    MetricSpec("disagreement_triage_rate", disagreement_triage_rate, "higher", "rate", "compile"),
    MetricSpec("calibration_ece", calibration_ece, "lower", "ece", "compile"),
    MetricSpec("cost_per_hospital_usd", cost_per_hospital_usd, "lower", "usd", "compile"),
    MetricSpec("compile_error_rate", compile_error_rate, "lower", "rate", "compile"),
    MetricSpec("commons_free_cutoff_accuracy", commons_free_cutoff_accuracy, "higher", "rate", "compile"),
    MetricSpec("naive_free_cutoff_accuracy", naive_free_cutoff_accuracy, "info", "rate", "compile"),
    MetricSpec("naive_baseline_gap_points", naive_baseline_gap_points, "info", "points", "compile"),
    MetricSpec("engine_agreement", engine_agreement, "higher", "rate", "engine"),
    MetricSpec("readback_fillable_exact", _readback("fillable"), "higher", "rate", "packet"),
    MetricSpec("readback_flat_exact", _readback("flat"), "higher", "rate", "packet"),
    MetricSpec("verified_packets_with_mismatch", verified_packets_with_mismatch, "lower", "count", "packet"),
    MetricSpec("packet_verified_rate", packet_verified_rate, "higher", "rate", "packet"),
    MetricSpec("template_f1", template_f1, "higher", "rate", "packet"),
    MetricSpec("extraction_field_exact", extraction_field_exact, "higher", "rate", "packet"),
    MetricSpec("cost_per_packet_usd", cost_per_packet_usd, "lower", "usd", "packet"),
]

METRICS: dict[str, MetricSpec] = {m.name: m for m in _SPECS}

BASELINE_METRICS = ("naive_free_cutoff_accuracy", "naive_baseline_gap_points")


def metrics_for(kind: str) -> list[MetricSpec]:
    return [m for m in _SPECS if m.kind == kind]

"""Statistics: bootstrap confidence intervals, F1 helpers, calibration (ECE), Cohen's kappa."""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

U = TypeVar("U")

Statistic = Callable[[Sequence[U]], float | None]


@dataclass(frozen=True)
class CI:
    low: float | None
    high: float | None


def bootstrap_ci(
    units: Sequence[U],
    statistic: Statistic[U],
    *,
    resamples: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> CI:
    """Percentile bootstrap CI, resampling whole units (hospitals, households, forms).

    Resampling the unit rather than individual fields respects within-hospital
    correlation. Resamples on which the statistic is undefined are skipped.
    """
    if not units or resamples <= 0:
        return CI(None, None)
    rng = random.Random(seed)
    n = len(units)
    vals: list[float] = []
    for _ in range(resamples):
        sample = [units[rng.randrange(n)] for _ in range(n)]
        v = statistic(sample)
        if v is not None:
            vals.append(v)
    if not vals:
        return CI(None, None)
    vals.sort()
    k = len(vals)
    lo = vals[min(k - 1, int(math.floor((alpha / 2) * k)))]
    hi = vals[max(0, min(k - 1, int(math.ceil((1 - alpha / 2) * k)) - 1))]
    return CI(round(lo, 6), round(hi, 6))


def f1(tp: int, fp: int, fn: int) -> float | None:
    """F1 from counts; ``None`` when there is nothing to score."""
    if tp + fp + fn == 0:
        return None
    return 2 * tp / (2 * tp + fp + fn)


def macro_f1(pairs: Sequence[tuple[Hashable, Hashable]]) -> float | None:
    """Macro-averaged F1 over every class seen in gold or prediction; pairs are (gold, pred)."""
    if not pairs:
        return None
    classes = {g for g, _ in pairs} | {p for _, p in pairs}
    scores = []
    for c in classes:
        tp = sum(1 for g, p in pairs if g == c and p == c)
        fp = sum(1 for g, p in pairs if g != c and p == c)
        fn = sum(1 for g, p in pairs if g == c and p != c)
        v = f1(tp, fp, fn)
        if v is not None:
            scores.append(v)
    return sum(scores) / len(scores) if scores else None


def expected_calibration_error(points: Sequence[tuple[float, bool]], bins: int = 10) -> float | None:
    """ECE with equal-width confidence bins: sum_b |acc_b - conf_b| * n_b / n."""
    if not points:
        return None
    buckets: dict[int, list[tuple[float, bool]]] = {}
    for conf, ok in points:
        c = min(max(conf, 0.0), 1.0)
        buckets.setdefault(min(int(c * bins), bins - 1), []).append((c, ok))
    n = len(points)
    total = 0.0
    for items in buckets.values():
        conf = sum(c for c, _ in items) / len(items)
        acc = sum(1 for _, ok in items if ok) / len(items)
        total += abs(acc - conf) * len(items) / n
    return total


def abstain_threshold(points: Sequence[tuple[float, bool]], target_precision: float = 0.97) -> float | None:
    """Lowest confidence cutoff whose retained predictions reach ``target_precision``.

    Used to set the compile stage's abstain threshold from calibration data.
    Returns ``None`` when no cutoff reaches the target.
    """
    for t in sorted({c for c, _ in points}):
        kept = [ok for c, ok in points if c >= t]
        if kept and sum(kept) / len(kept) >= target_precision:
            return t
    return None


def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float | None:
    """Cohen's kappa for two raters over the same items (``None`` for no items).

    When both raters use a single identical label everywhere, chance agreement
    is 1 and kappa is undefined; we return 1.0 for perfect agreement then.
    """
    if len(a) != len(b):
        raise ValueError("raters must label the same items")
    n = len(a)
    if n == 0:
        return None
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in ca.keys() | cb.keys()) / (n * n)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def describe(x: Any) -> str:
    """Compact human formatting used in reports."""
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.3f}"
    return str(x)

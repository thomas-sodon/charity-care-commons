"""Citation support (section 12): does the quote actually support the value? Human-judged.

``python -m evals support-sample`` draws a seeded random sample of published
fields from a run (default 100) into a CSV with an empty ``supports`` column;
a reviewer writes ``yes`` / ``no`` / ``unsure``; ``support-score`` reports the
share of ``yes`` among judged rows (target >= 95%).
"""

from __future__ import annotations

import csv
import json
import random
from collections.abc import Sequence
from pathlib import Path

from evals.cases import Case

COLUMNS = ["hospital_id", "field", "value", "page", "doc_sha256", "quote", "supports", "reviewer_note"]


def sample_rows(cases: Sequence[Case], n: int = 100, seed: int = 20261024) -> list[dict[str, str]]:
    rows = []
    for c in cases:
        for key, f in sorted(c.pred.items()):
            if c.published(key) and f.get("value") is not None:
                ev = (f.get("evidence") or [{}])
                rows.append({
                    "hospital_id": c.hospital_id,
                    "field": key,
                    "value": json.dumps(f["value"], sort_keys=True, ensure_ascii=False),
                    "page": ";".join(str(e.get("page", "")) for e in ev),
                    "doc_sha256": ";".join(str(e.get("doc_sha256", ""))[:12] for e in ev),
                    "quote": " | ".join(str(e.get("quote", "")) for e in ev),
                    "supports": "",
                    "reviewer_note": "",
                })
    rng = random.Random(seed)
    return sorted(rng.sample(rows, min(n, len(rows))), key=lambda r: (r["hospital_id"], r["field"]))


def write_sample(rows: list[dict[str, str]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


def score_judged(path: Path) -> tuple[float | None, int]:
    """Share of ``yes`` among rows judged ``yes`` or ``no``; ``unsure`` and blank rows are excluded."""
    with path.open(newline="", encoding="utf-8") as fh:
        judged = [r["supports"].strip().lower() for r in csv.DictReader(fh) if r.get("supports", "").strip().lower() in ("yes", "no")]
    return (sum(j == "yes" for j in judged) / len(judged) if judged else None), len(judged)

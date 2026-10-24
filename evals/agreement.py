"""Inter-rater agreement for double-coded gold (section 12): per-field agreement and Cohen's kappa.

Each coder's labels are CompiledPolicy JSON per hospital (the gold format, or a
bare policy object). Values are compared with the scorers' normalization, so
two coders who wrote the same refund clause with different whitespace agree.
Disagreements are listed for adjudication; the adjudicated label becomes gold.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from charitycare.schemas import CompiledPolicy
from evals.compare import FREE_TEXT_FIELDS, canonical, is_abstained, value_of, values_equal
from evals.stats import cohen_kappa

Policy = Mapping[str, Mapping[str, Any]]


def label(f: Mapping[str, Any] | None) -> str:
    """Canonical label of one coded field: the abstain reason, or the normalized value."""
    if is_abstained(f):
        return f"ABSTAIN:{(f or {}).get('abstain_reason') or 'not_addressed'}"
    return json.dumps(canonical(value_of(f)), default=str)


@dataclass
class FieldAgreement:
    key: str
    n: int
    agreement: float | None
    kappa: float | None


@dataclass
class AgreementReport:
    hospitals: list[str]
    fields: dict[str, FieldAgreement]
    overall_agreement: float | None
    overall_kappa: float | None
    disagreements: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "hospitals": self.hospitals,
            "overall_agreement": self.overall_agreement,
            "overall_kappa": self.overall_kappa,
            "fields": {k: vars(v) for k, v in self.fields.items()},
            "disagreements": self.disagreements,
        }


def _pair_labels(key: str, a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> tuple[str, str]:
    la, lb = label(a), label(b)
    if la != lb and not is_abstained(a) and not is_abstained(b) and values_equal(value_of(a), value_of(b), free_text=key in FREE_TEXT_FIELDS):
        lb = la
    return la, lb


def interrater(coder_a: Mapping[str, Policy], coder_b: Mapping[str, Policy], keys: list[str] | None = None) -> AgreementReport:
    """Agreement and kappa over the hospitals both coders labelled."""
    hospitals = sorted(set(coder_a) & set(coder_b))
    keys = keys or CompiledPolicy.field_keys()
    fields: dict[str, FieldAgreement] = {}
    all_a: list[str] = []
    all_b: list[str] = []
    disagreements = []
    for key in keys:
        la, lb = [], []
        for h in hospitals:
            a, b = _pair_labels(key, coder_a[h].get(key), coder_b[h].get(key))
            la.append(a)
            lb.append(b)
            if a != b:
                disagreements.append({"hospital_id": h, "field": key, "coder_a": a, "coder_b": b})
        n = len(la)
        fields[key] = FieldAgreement(key, n, sum(x == y for x, y in zip(la, lb, strict=True)) / n if n else None, cohen_kappa(la, lb))
        all_a += [f"{key}={x}" for x in la]
        all_b += [f"{key}={y}" for y in lb]
    total = len(all_a)
    return AgreementReport(
        hospitals=hospitals,
        fields=fields,
        overall_agreement=sum(x == y for x, y in zip(all_a, all_b, strict=True)) / total if total else None,
        overall_kappa=cohen_kappa(all_a, all_b),
        disagreements=disagreements,
    )


def load_coder_dir(path: Path) -> dict[str, Policy]:
    """``<hospital_id>.json`` files holding gold JSON (with ``policy``) or a bare policy object."""
    out: dict[str, Policy] = {}
    for p in sorted(Path(path).glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out[d.get("hospital_id", p.stem)] = d.get("policy", d)
    return out

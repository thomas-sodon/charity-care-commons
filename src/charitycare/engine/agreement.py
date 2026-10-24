"""Score the engine against the oracle-labelled synthetic households."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from charitycare.engine.core import CONDITION_KEYS, EngineResult, evaluate
from charitycare.engine.policy import demo_policies
from charitycare.schemas import Field, FieldRecord, FieldStatus, Household


def apply_scenario(records: tuple[FieldRecord, ...] | list[FieldRecord], scenario: str) -> list[FieldRecord]:
    """``provisional:<key>`` marks that record provisional; ``abstained:<key>`` replaces it with an abstention."""
    kind, _, key = scenario.partition(":")
    out = []
    for r in records:
        if r.key == key and kind == "provisional":
            r = r.model_copy(update={"status": FieldStatus.provisional, "reviewer": None})
        elif r.key == key and kind == "abstained":
            r = r.model_copy(update={"status": FieldStatus.abstained, "reviewer": None,
                                     "field": Field[Any](abstain_reason="not_addressed")})
        out.append(r)
    return out


def observed(res: EngineResult) -> dict[str, Any]:
    """The engine's answer in the oracle's vocabulary."""
    d = res.determination
    full = d.outcome not in ("preliminary_provisional_fields", "insufficient_information")
    return {
        "outcome": d.outcome, "fpg_year": d.fpg_year, "pct_fpl": f"{d.pct_fpl:.2f}",
        "federal_deadline": d.federal_deadline.isoformat() if d.federal_deadline else None,
        "policy_deadline": d.policy_deadline.isoformat() if d.policy_deadline else None,
        "provisional_fields_used": d.provisional_fields_used, "matched_tier_index": res.matched_tier_index,
        "prior_year_flip": res.prior_year_flip,
        "conditions": {k: res.statuses()[k] for k in CONDITION_KEYS} if full else None,
    }


def run_case(case: dict[str, Any], demo_dir: str | Path | None = None) -> EngineResult:
    dp = demo_policies(demo_dir)[case["facility_id"]]
    hh = Household.model_validate(case["household"])
    return evaluate(hh, apply_scenario(dp.records, case["scenario"]), facility_id=case["facility_id"],
                    policy_version_id=dp.policy_version_id, today=date.fromisoformat(case["today"]))


@dataclass
class Agreement:
    total: int = 0
    agree: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.agree / self.total if self.total else 0.0


def score(cases: list[dict[str, Any]], demo_dir: str | Path | None = None) -> Agreement:
    """Agreement of engine and oracle across all cases (every compared key must match)."""
    a = Agreement()
    for c in cases:
        got, want = observed(run_case(c, demo_dir)), c["expected"]
        a.total += 1
        diff = {k: (want[k], got[k]) for k in want if got.get(k) != want[k]}
        if diff:
            a.mismatches.append({"id": c["id"], "diff": diff})
        else:
            a.agree += 1
    return a

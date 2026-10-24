"""Helpers for engine tests: verified demo policies and record surgery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from charitycare.engine.policy import demo_policies, records_from_policy
from charitycare.schemas import CompiledPolicy, FieldRecord, FieldStatus, Household

TODAY = date(2026, 10, 24)


def gold_policy(hospital_id: str) -> dict[str, Any]:
    for dp in demo_policies().values():
        if dp.hospital_id == hospital_id:
            return dp.gold["policy"]
    raise KeyError(hospital_id)


def make_records(hospital_id: str, *, edit: Callable[[dict[str, Any]], None] | None = None,
                 status: dict[str, FieldStatus] | None = None) -> list[FieldRecord]:
    """Verified records for a demo hospital, optionally with edited values or statuses."""
    import copy

    pol = copy.deepcopy(gold_policy(hospital_id))
    if edit:
        edit(pol)
    recs = records_from_policy(CompiledPolicy.model_validate(pol), f"pv:{hospital_id}:test")
    status = status or {}
    return [r.model_copy(update={"status": status[r.key]}) if r.key in status else r for r in recs]


def hh(**kw: Any) -> Household:
    base: dict[str, Any] = {"household_size": 3, "annual_income": "49700", "insured": False, "state": "TX",
                            "application_date": date(2026, 10, 20)}
    base.update(kw)
    return Household.model_validate(base)

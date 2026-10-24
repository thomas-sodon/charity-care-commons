"""Typed, status-aware view of a policy's ``FieldRecord`` rows.

The engine never reads raw JSON values. :class:`PolicyView` re-validates each
record's ``Field`` against the ``CompiledPolicy`` annotation for its key (so a
``fpg_tiers`` value comes back as ``list[FPGTier]``) and classifies it:

* ``usable``      verified and carrying a value: may drive a determination;
* ``provisional`` extracted but not yet verified: must never drive one;
* ``abstained``   no value (abstained, disputed or value-less);
* ``missing``     no record at all.

:func:`demo_policy_records` turns the demo gold policies into verified records
(the offline stand-in for a reviewed registry).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from charitycare.schemas import (
    CompiledPolicy,
    Evidence,
    Field,
    FieldRecord,
    FieldStatus,
)

FieldState = Literal["usable", "provisional", "abstained", "missing"]

DEMO_POLICY_AS_OF = date(2026, 10, 1)


def demo_policy_version_id(hospital_id: str) -> str:
    """Policy version id used for the demo corpus (matches ``compile.demo``)."""
    return f"pv:{hospital_id}:demo"


@dataclass(frozen=True)
class FieldView:
    """One field as the engine sees it."""

    key: str
    state: FieldState
    field: Field[Any] | None
    record: FieldRecord | None

    @property
    def value(self) -> Any:
        """The typed value, or None when the field cannot be used."""
        return self.field.value if self.field is not None else None

    @property
    def evidence(self) -> list[Evidence]:
        return list(self.field.evidence) if self.field is not None else []

    @property
    def abstain_reason(self) -> str | None:
        return self.field.abstain_reason if self.field is not None else None

    @property
    def is_discretionary(self) -> bool:
        """An abstention on a discretionary ("may") clause: shown as hospital discretion."""
        return self.abstain_reason == "ambiguous_may_clause"


def _typed_field(key: str, raw: Field[Any]) -> Field[Any]:
    info = CompiledPolicy.model_fields.get(key)
    if info is None or info.annotation is None:
        return raw
    return info.annotation.model_validate(raw.model_dump(mode="json"))


class PolicyView:
    """Status-aware accessors over the field records of one policy version."""

    def __init__(self, policy_version_id: str, records: list[FieldRecord] | tuple[FieldRecord, ...]):
        self.policy_version_id = policy_version_id
        self._records = {r.key: r for r in records}
        self._cache: dict[str, FieldView] = {}

    @property
    def keys(self) -> list[str]:
        return sorted(self._records)

    def get(self, key: str) -> FieldView:
        """Classify and type the record for ``key``."""
        if key in self._cache:
            return self._cache[key]
        rec = self._records.get(key)
        if rec is None:
            view = FieldView(key, "missing", None, None)
        else:
            fld = _typed_field(key, rec.field)
            if rec.status == FieldStatus.provisional:
                state: FieldState = "provisional"
            elif rec.status == FieldStatus.verified and not fld.is_abstained:
                state = "usable"
            else:  # abstained, disputed, or verified-but-empty
                state = "abstained"
            view = FieldView(key, state, fld, rec)
        self._cache[key] = view
        return view

    def usable(self, key: str) -> Any:
        """The value if the field may drive a determination, else None."""
        v = self.get(key)
        return v.value if v.state == "usable" else None

    def provisional(self, keys: tuple[str, ...] | list[str]) -> list[str]:
        """Which of ``keys`` are provisional (sorted)."""
        return sorted(k for k in keys if self.get(k).state == "provisional")

    def discretionary(self) -> list[FieldView]:
        """All fields that abstained on a discretionary clause and carry a quote."""
        out = [self.get(k) for k in self.keys]
        return [v for v in out if v.is_discretionary and v.evidence]


# --------------------------------------------------------------------------
# Demo gold -> verified records
# --------------------------------------------------------------------------


def records_from_policy(
    policy: dict[str, Any] | CompiledPolicy,
    policy_version_id: str,
    *,
    as_of: date = DEMO_POLICY_AS_OF,
    status: FieldStatus = FieldStatus.verified,
    reviewer: str | None = "gold",
) -> list[FieldRecord]:
    """Wrap every field of a compiled policy as a ``FieldRecord``.

    Abstained fields get status ``abstained``; the rest get ``status``.
    """
    cp = policy if isinstance(policy, CompiledPolicy) else CompiledPolicy.model_validate(policy)
    out: list[FieldRecord] = []
    for key, fld in cp.fields().items():
        st = FieldStatus.abstained if fld.abstain_reason is not None else status
        out.append(
            FieldRecord(
                id=FieldRecord.make_id(policy_version_id, key),
                policy_version_id=policy_version_id,
                key=key,
                field=Field[Any].model_validate(fld.model_dump(mode="json")),
                status=st,
                as_of=as_of,
                reviewer=reviewer if st == FieldStatus.verified else None,
            )
        )
    return out


@dataclass(frozen=True)
class DemoPolicy:
    hospital_id: str
    policy_version_id: str
    facility_ids: tuple[str, ...]
    records: tuple[FieldRecord, ...]
    gold: dict[str, Any]


def _demo_dir(demo_dir: str | Path | None) -> Path:
    from charitycare.config import get_settings

    return Path(demo_dir) if demo_dir else get_settings().demo_dir


@lru_cache(maxsize=8)
def _load_demo(demo_dir: str) -> dict[str, DemoPolicy]:
    out: dict[str, DemoPolicy] = {}
    for path in sorted((Path(demo_dir) / "gold").glob("*.json")):
        gold = json.loads(path.read_text(encoding="utf-8"))
        pv = demo_policy_version_id(gold["hospital_id"])
        dp = DemoPolicy(gold["hospital_id"], pv, tuple(gold["facility_ids"]),
                        tuple(records_from_policy(gold["policy"], pv)), gold)
        for fid in dp.facility_ids:
            out[fid] = dp
    return out


def demo_policies(demo_dir: str | Path | None = None) -> dict[str, DemoPolicy]:
    """facility_id -> verified demo policy, read from ``data/demo/gold``."""
    return _load_demo(str(_demo_dir(demo_dir).resolve()))


def demo_policy_records(facility_id: str, demo_dir: str | Path | None = None) -> DemoPolicy:
    """The verified demo policy governing ``facility_id`` (KeyError if unknown)."""
    pols = demo_policies(demo_dir)
    if facility_id not in pols:
        raise KeyError(facility_id)
    return pols[facility_id]

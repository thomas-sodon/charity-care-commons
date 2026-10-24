"""``screen(facility_id, household)``: the function behind ``POST /v1/screen``.

Resolves the facility's governing policy and runs the engine. Nothing about
the household is stored or logged. Policy sources, in order:

1. ``records=`` passed explicitly (with ``policy_version_id``);
2. a :class:`~charitycare.store.Registry` passed as ``registry=``
   (``policy_for_facility`` then ``fields_for_policy``);
3. otherwise the verified demo gold policies in ``data/demo/gold``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

from charitycare.engine.core import EngineResult, evaluate, no_policy_determination
from charitycare.engine.policy import demo_policy_records
from charitycare.schemas import Determination, FieldRecord, Household
from charitycare.store import Registry


class UnknownFacility(KeyError):
    """The facility id is not in the registry (or the demo corpus)."""


def screen_detailed(
    facility_id: str,
    household: Household,
    *,
    registry: Registry | None = None,
    records: Sequence[FieldRecord] | None = None,
    policy_version_id: str | None = None,
    today: date | None = None,
    demo_dir: str | Path | None = None,
) -> EngineResult | Determination:
    """Like :func:`screen` but returns the :class:`EngineResult` (conditions included) when a policy exists."""
    if records is not None:
        pv = policy_version_id or (records[0].policy_version_id if records else "")
        return evaluate(household, records, facility_id=facility_id, policy_version_id=pv, today=today)
    if registry is not None:
        if registry.get_facility(facility_id) is None:
            raise UnknownFacility(facility_id)
        as_of = household.application_date or today
        pol = registry.policy_for_facility(facility_id, as_of) or registry.policy_for_facility(facility_id)
        if pol is None:
            return no_policy_determination(facility_id, household, today=today)
        recs = registry.fields_for_policy(pol.id)
        if not recs:
            return no_policy_determination(facility_id, household, today=today)
        return evaluate(household, recs, facility_id=facility_id, policy_version_id=pol.id, today=today)
    try:
        dp = demo_policy_records(facility_id, demo_dir)
    except KeyError as exc:
        raise UnknownFacility(facility_id) from exc
    return evaluate(household, dp.records, facility_id=facility_id, policy_version_id=dp.policy_version_id, today=today)


def screen(
    facility_id: str,
    household: Household,
    *,
    registry: Registry | None = None,
    records: Sequence[FieldRecord] | None = None,
    policy_version_id: str | None = None,
    today: date | None = None,
    demo_dir: str | Path | None = None,
) -> Determination:
    """Deterministic, cited screening of ``household`` at ``facility_id``.

    Raises :class:`UnknownFacility` for an unknown facility; returns an
    ``insufficient_information`` determination when no policy is on file.
    """
    res = screen_detailed(facility_id, household, registry=registry, records=records,
                          policy_version_id=policy_version_id, today=today, demo_dir=demo_dir)
    return res.determination if isinstance(res, EngineResult) else res

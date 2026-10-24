"""Turn pipeline outputs into scored :class:`~evals.cases.Case` objects.

Two providers:

* :func:`compile_cases` runs the compile stage (``charitycare.compile``,
  imported lazily) on the demo corpus in a fresh in-memory registry, with the
  configured LLM backend (replay fixtures offline, live with an API key);
* :func:`registry_cases` scores what an existing registry already holds, for
  real-state runs where discovery and compile happened elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from charitycare.schemas import ReviewTask
from charitycare.store import Registry
from evals.cases import Case, Dataset, GoldPolicy
from evals.citations import PageStore, verify_published


def _case_from_registry(reg: Registry, gold: GoldPolicy, pv_id: str | None, store: PageStore) -> Case:
    case = Case(
        hospital_id=gold.hospital_id,
        gold=gold.fields,
        pred={},
        expected_crosschecks=gold.expected_crosschecks,
        policy_date=gold.policy_date,
        tax_period_end=gold.tax_period_end,
        policy_version_id=pv_id,
    )
    if pv_id is None:
        case.error = "no governing policy version"
        return case
    for rec in reg.fields_for_policy(pv_id):
        case.pred[rec.key] = rec.field.model_dump(mode="json")
        case.status[rec.key] = rec.status.value
        if rec.crosschecks:
            case.pred_crosschecks[rec.key] = [c.model_dump(mode="json") for c in rec.crosschecks]
    prefix = f"{pv_id}:"
    # Every task, whatever its state: a disagreement a reviewer already handled was still triaged.
    case.tasks = [t.model_dump(mode="json") for t in reg.select(ReviewTask) if (t.field_id or "").startswith(prefix)]
    case.citations = verify_published(case.pred, case.published, store)
    return case


def compile_cases(dataset: Dataset, hospitals: Iterable[str], *, backend: Any = None, store: PageStore | None = None) -> list[Case]:
    """Compile each hospital's policy bundle and build its Case (errors become cases with ``error`` set)."""
    from charitycare.compile.demo import load_demo_registry, policy_version_id
    from charitycare.compile.pipeline import compile_policy_version

    store = store or PageStore.from_paths(dataset.documents())
    cases: list[Case] = []
    with Registry(":memory:") as reg:
        load_demo_registry(reg, dataset.root)
        for hid in hospitals:
            gold = dataset.gold(hid)
            pv = policy_version_id(hid)
            try:
                report = compile_policy_version(reg, pv, backend=backend)
            except Exception as exc:  # noqa: BLE001 - one hospital failing must not hide the others' scores
                case = _case_from_registry(reg, gold, None, store)
                case.error = f"{type(exc).__name__}: {exc}"
                cases.append(case)
                continue
            case = _case_from_registry(reg, gold, pv, store)
            case.cost_usd = float(report.cost_usd)
            case.simulated_cost = report.backend == "replay"
            case.error = report.error
            cases.append(case)
    return cases


def registry_cases(reg: Registry, dataset: Dataset, hospitals: Iterable[str], store: PageStore | None = None) -> list[Case]:
    """Score the policy currently governing each gold hospital's first facility."""
    store = store or PageStore.from_paths(dataset.documents())
    out = []
    for hid in hospitals:
        gold = dataset.gold(hid)
        pv = next((p for f in gold.facility_ids if (p := reg.policy_for_facility(f)) is not None), None)
        out.append(_case_from_registry(reg, gold, pv.id if pv else None, store))
    return out

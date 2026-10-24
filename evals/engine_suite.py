"""Engine suite units: one per synthetic household, scored against the independent oracle."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def engine_units(demo_dir: Path) -> list[dict[str, Any]]:
    """``{"id", "category", "agree", "diff"}`` for every household in ``households.json``."""
    from charitycare.engine.agreement import observed, run_case
    from charitycare.engine.households import load_households

    units = []
    for case in load_households(demo_dir)["households"]:
        try:
            got = observed(run_case(case, demo_dir))
            diff = {k: [v, got.get(k)] for k, v in case["expected"].items() if got.get(k) != v}
        except Exception as exc:  # noqa: BLE001 - an engine crash is a disagreement, not a harness crash
            diff = {"error": f"{type(exc).__name__}: {exc}"}
        units.append({"id": case["id"], "category": case.get("category"), "agree": not diff, "diff": diff})
    return units

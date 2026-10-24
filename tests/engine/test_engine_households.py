from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from charitycare import fpg
from charitycare.engine import oracle
from charitycare.engine.agreement import run_case, score
from charitycare.engine.households import (
    TARGET,
    build_households,
    dumps,
    households_path,
    load_households,
)
from charitycare.wording import find_banned_wording


def test_committed_file_matches_generator() -> None:
    assert households_path().read_text(encoding="utf-8") == dumps(build_households())


def test_count_and_coverage() -> None:
    doc = load_households()
    cases = doc["households"]
    assert doc["count"] == len(cases) == TARGET == 300
    assert len({c["id"] for c in cases}) == 300
    cats = Counter(c["category"] for c in cases)
    for cat in ("tier_boundary", "household_size_change", "asset_edge", "residency_edge", "insured",
                "insured_catastrophic", "fpg_year", "deadline", "field_status", "random"):
        assert cats[cat] > 0, cat
    outcomes = Counter(c["expected"]["outcome"] for c in cases)
    assert set(outcomes) == {"likely_free", "likely_discount", "likely_not_eligible_on_income",
                             "preliminary_provisional_fields", "insufficient_information"}
    statuses = Counter((k, v) for c in cases if c["expected"]["conditions"] for k, v in c["expected"]["conditions"].items())
    for key in ("asset_test", "residency", "insured_patients", "medical_hardship"):
        for st in ("met", "not_met", "unknown"):
            assert statuses[(key, st)] > 0, (key, st)
    assert any(c["expected"]["prior_year_flip"] for c in cases)
    assert len({c["hospital_id"] for c in cases}) == 8


def test_exact_cutoff_cases_present() -> None:
    """At least one boundary household sits exactly on each tier cutoff."""
    cases = [c for c in load_households()["households"] if c["category"] == "tier_boundary"]
    exact = 0
    for c in cases:
        e = c["expected"]
        line = oracle.poverty_line(e["fpg_year"], c["household"]["household_size"])
        inc = oracle.frac(c["household"]["annual_income"])
        exact += any(inc * 100 == oracle.frac(p) * line for p in (150, 200, 250, 300, 350, 400, 500))
    assert exact >= 15


def test_engine_agrees_with_oracle_on_all_households() -> None:
    res = score(load_households()["households"])
    assert res.mismatches == []
    assert res.rate == 1.0 and res.total == 300


def test_every_output_uses_safe_wording() -> None:
    for c in load_households()["households"]:
        d = run_case(c).determination
        assert find_banned_wording("\n".join([d.arithmetic, *d.notes, *d.unevaluated_conditions])) == [], c["id"]


def test_oracle_is_independent() -> None:
    """The oracle imports neither the engine nor the fpg module, and its table matches HHS values."""
    src = Path(oracle.__file__).read_text(encoding="utf-8")
    imported = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith("charitycare") for m in imported), imported
    for y, (base, inc, pub) in oracle.ORACLE_FPG.items():
        assert fpg.fpg(y, 1) == base and fpg.fpg(y, 2) - fpg.fpg(y, 1) == inc and fpg.GUIDELINES[y].published == pub

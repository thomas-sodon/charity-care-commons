"""300 synthetic households generated from the demo gold rules (design section 12).

Every case is built to sit on or near a rule edge: exactly at a tier cutoff,
one dollar or one cent either side, household-size changes across a cutoff,
asset-threshold edges, residency edge counties, insured and catastrophic
(medical-hardship) cases, FPG-year boundaries, deadline dates and field-status
scenarios (provisional or abstained fields). The remainder are seeded random
households. Expected results come from :mod:`charitycare.engine.oracle`, which
is independent of the engine.

``build_households()`` is deterministic; ``write_households()`` writes
``data/demo/households.json``.
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from charitycare.engine import oracle

HOUSEHOLDS_VERSION = "households@v1"
TARGET = 300
TODAY = date(2026, 10, 24)
SEED = 20261024

# Neighbouring counties that are deliberately *outside* each policy's list.
EDGE_COUNTIES: dict[str, list[str]] = {
    "example-methodist": ["Wilson", "Kendall County", "bexar", "Kerr"],
    "hill-country-community": ["Kendall", "Gillespie County", "kerr", "Mason"],
    "brazos-valley": ["Leon", "Brazos County", "WASHINGTON", "Milam"],
    "pineywoods-regional": ["San Augustine", "Angelina County", "polk", "Tyler"],
}

_BASE_DATES = {"service_date": "2026-09-01", "first_statement_date": "2026-09-15", "application_date": "2026-10-01"}


def _money(x: Fraction) -> str:
    """Exact 2-decimal string for a whole-cent Fraction."""
    cents = x * 100
    assert cents.denominator == 1, x
    n = int(cents)
    return f"{n // 100}.{n % 100:02d}"


def _cent_floor(x: Fraction) -> Fraction:
    return Fraction(math.floor(x * 100), 100)


def _cent_ceil(x: Fraction) -> Fraction:
    return Fraction(math.ceil(x * 100), 100)


def load_gold(demo_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((demo_dir / "gold").glob("*.json"))]


class _Builder:
    def __init__(self, golds: list[dict[str, Any]]):
        self.golds = {g["hospital_id"]: g for g in golds}
        self.cases: list[dict[str, Any]] = []
        self.rng = random.Random(SEED)

    def add(self, hid: str, category: str, hh: dict[str, Any], scenario: str = "verified", fac_index: int = 0) -> None:
        g = self.golds[hid]
        fac = g["facility_ids"][fac_index % len(g["facility_ids"])]
        hh = {k: v for k, v in hh.items() if v is not None}
        hh.setdefault("state", "TX")
        self.cases.append({"hospital_id": hid, "facility_id": fac, "category": category, "scenario": scenario,
                           "today": TODAY.isoformat(), "household": hh})

    def base(self, hid: str, size: int, income: Fraction | str, **kw: Any) -> dict[str, Any]:
        """A household that passes every non-income condition of ``hid`` unless overridden."""
        pol = self.golds[hid]["policy"]
        hh: dict[str, Any] = {"household_size": size, "annual_income": income if isinstance(income, str) else _money(income),
                              **_BASE_DATES}
        if pol["residency"]["value"]["area_type"] == "counties":
            hh["county"] = pol["residency"]["value"]["counties"][0]
        if pol["asset_test"]["value"]["applies"]:
            hh["liquid_assets"] = "1000.00"
        hh["insured"] = False
        hh.update(kw)
        return hh

    def year_for(self, hid: str, hh: dict[str, Any]) -> int:
        return oracle.expected(self.golds[hid]["policy"], hh, TODAY)["fpg_year"]

    def cutoff(self, pct: float, year: int, size: int) -> Fraction:
        return oracle.frac(pct) * oracle.poverty_line(year, size) / 100

    def bounds(self, hid: str) -> list[float]:
        tiers = self.golds[hid]["policy"]["fpg_tiers"]["value"]
        return sorted({t["max_pct_inclusive"] for t in tiers} | {t["min_pct_exclusive"] for t in tiers if t["min_pct_exclusive"]})

    # ---- categories -------------------------------------------------------

    def boundaries(self) -> None:
        sizes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        k = 0
        for hid in self.golds:
            for b in self.bounds(hid):
                size = sizes[k % len(sizes)]
                k += 1
                year = self.year_for(hid, self.base(hid, size, "1.00"))
                c = self.cutoff(b, year, size)
                # exactly at the cutoff (or the cents either side), one dollar below/above, one cent above
                incomes = {_cent_floor(c), _cent_ceil(c), _cent_floor(c - 1), _cent_ceil(c) + 1, _cent_floor(c) + Fraction(1, 100)}
                for inc in sorted(incomes):
                    self.add(hid, "tier_boundary", self.base(hid, size, inc), fac_index=k)

    def size_changes(self) -> None:
        for hid in self.golds:
            free = min(self.bounds(hid))
            year = self.year_for(hid, self.base(hid, 3, "1.00"))
            inc = _cent_floor(self.cutoff(free, year, 3))
            for size in (2, 3, 4):
                self.add(hid, "household_size_change", self.base(hid, size, inc))

    def assets(self) -> None:
        for hid, g in self.golds.items():
            at = g["policy"]["asset_test"]["value"]
            if not at["applies"]:
                continue
            thr = oracle.frac(at["threshold_usd"])
            year = self.year_for(hid, self.base(hid, 2, "1.00"))
            for pct in (100, 250):  # a free-tier and a discount-tier income
                inc = _cent_floor(self.cutoff(pct, year, 2))
                for a in (thr, thr - 1, thr + 1, thr + Fraction(1, 100), Fraction(0)):
                    self.add(hid, "asset_edge", self.base(hid, 2, inc, liquid_assets=_money(a)))
                self.add(hid, "asset_edge", self.base(hid, 2, inc, liquid_assets=None))

    def residency(self) -> None:
        for hid, g in self.golds.items():
            r = g["policy"]["residency"]["value"]
            year = self.year_for(hid, self.base(hid, 4, "1.00"))
            inc = _cent_floor(self.cutoff(120, year, 4))
            if r["area_type"] == "counties":
                for county in [r["counties"][-1], *EDGE_COUNTIES[hid]]:
                    self.add(hid, "residency_edge", self.base(hid, 4, inc, county=county))
                self.add(hid, "residency_edge", self.base(hid, 4, inc, county=None))
                self.add(hid, "residency_edge", self.base(hid, 4, inc, county=EDGE_COUNTIES[hid][0], emergency_care=True))
            elif r["area_type"] == "state":
                for st, er in (("TX", False), ("NM", False), ("OK", True), ("tx", False)):
                    self.add(hid, "residency_edge", self.base(hid, 4, inc, state=st, emergency_care=er or None))

    def insured(self) -> None:
        for hid in self.golds:
            year = self.year_for(hid, self.base(hid, 3, "1.00"))
            inc = _cent_floor(self.cutoff(110, year, 3))
            rule = self.golds[hid]["policy"]["insured_patients"]["value"]
            for ins in ((True, False, None) if not rule["insured_eligible"] else (True,)):
                self.add(hid, "insured", self.base(hid, 3, inc, insured=ins))

    def catastrophic(self) -> None:
        for hid, g in self.golds.items():
            thr = g["policy"]["insured_patients"]["value"]["catastrophic_threshold_pct_income"]
            if thr is None:
                continue
            year = self.year_for(hid, self.base(hid, 2, "1.00"))
            top = max(t["max_pct_inclusive"] for t in g["policy"]["fpg_tiers"]["value"])
            for pct in (top + 1, 600):
                inc = _cent_floor(self.cutoff(pct, year, 2))
                edge = inc * oracle.frac(thr) / 100
                for oop in (_cent_floor(edge), _cent_ceil(edge) + Fraction(1, 100), edge - 1, edge + 1, Fraction(0)):
                    self.add(hid, "insured_catastrophic", self.base(hid, 2, inc, insured=True, out_of_pocket_medical_12m=_money(_cent_floor(oop))))
                self.add(hid, "insured_catastrophic", self.base(hid, 2, inc, insured=True, out_of_pocket_medical_12m=None))
            # inside the tiers the hardship rule is not needed
            self.add(hid, "insured_catastrophic", self.base(hid, 2, _cent_floor(self.cutoff(150, year, 2)), insured=True, out_of_pocket_medical_12m="50000.00"))

    def fpg_years(self) -> None:
        for hid, g in self.golds.items():
            rule = g["policy"]["fpg_year_rule"]["value"]
            free = min(self.bounds(hid))
            # an income above the 2025 cutoff but within the 2026 one
            inc = _cent_floor((self.cutoff(free, 2025, 3) + self.cutoff(free, 2026, 3)) / 2)
            if rule == "current_at_service":
                for sd in ("2026-01-14", "2026-01-15", None):
                    self.add(hid, "fpg_year", self.base(hid, 3, inc, service_date=sd))
            elif rule in ("current_at_application", "unspecified"):
                for ad in ("2026-01-14", "2026-01-15", "2026-10-01", None):
                    self.add(hid, "fpg_year", self.base(hid, 3, inc, application_date=ad))
            elif rule == "stated_year":
                for ad in ("2025-06-01", "2026-10-01"):
                    self.add(hid, "fpg_year", self.base(hid, 3, inc, application_date=ad))

    def deadlines(self) -> None:
        hids = list(self.golds)
        dates = [None, "2026-02-28", "2027-02-28", "2027-12-31", "2026-03-01", "2025-11-01"]
        for i, fs in enumerate(dates):
            hid = hids[i % len(hids)]
            self.add(hid, "deadline", self.base(hid, 2, "20000.00", first_statement_date=fs))
        for fs in ("2026-10-10", None):
            self.add("brazos-valley", "deadline", self.base("brazos-valley", 2, "20000.00", first_statement_date=fs))

    def scenarios(self) -> None:
        for hid, scen in [("example-methodist", "provisional:fpg_tiers"), ("lone-star-health", "provisional:residency"),
                          ("hill-country-community", "provisional:asset_test"), ("valley-childrens", "provisional:application_window"),
                          ("gulf-coast-baptist", "provisional:insured_patients"), ("panhandle-university", "provisional:fpg_year_rule"),
                          ("brazos-valley", "provisional:income_definition"), ("pineywoods-regional", "provisional:required_documents"),
                          ("lone-star-health", "abstained:fpg_tiers"), ("example-methodist", "abstained:fpg_year_rule"),
                          ("example-methodist", "abstained:asset_test"), ("pineywoods-regional", "abstained:residency"),
                          ("gulf-coast-baptist", "abstained:insured_patients"), ("brazos-valley", "abstained:application_window")]:
            for inc in ("18000.00", "45000.00"):
                self.add(hid, "field_status", self.base(hid, 3, inc), scenario=scen)

    def random_fill(self) -> None:
        hids = list(self.golds)
        counties = sorted({c for g in self.golds.values() for c in g["policy"]["residency"]["value"]["counties"]}
                          | {c for cs in EDGE_COUNTIES.values() for c in cs} | {"Harris", "Travis", "Dallas"})
        rng = self.rng
        while len(self.cases) < TARGET:
            hid = rng.choice(hids)
            size = rng.randint(1, 10)
            line = oracle.poverty_line(2026, size)
            inc = Fraction(rng.randint(0, line * 6 * 100), 100) if rng.random() > 0.03 else Fraction(0)
            d0 = date(2026, 1, 1) + timedelta(days=rng.randint(0, 290))
            hh = {
                "household_size": size, "annual_income": _money(inc),
                "liquid_assets": rng.choice([None, "0.00", "4999.99", "5000.00", "9999.00", "10000.00", "25000.00"]),
                "county": rng.choice([None, *counties]), "state": rng.choice(["TX"] * 9 + ["LA"]),
                "insured": rng.choice([None, True, False]),
                "out_of_pocket_medical_12m": rng.choice([None, "0.00", "2500.00", "15000.00"]),
                "emergency_care": rng.random() < 0.2,
                "service_date": rng.choice([None, d0.isoformat()]),
                "first_statement_date": rng.choice([None, (d0 + timedelta(days=rng.randint(5, 40))).isoformat()]),
                "application_date": rng.choice([None, (d0 + timedelta(days=rng.randint(10, 90))).isoformat()]),
            }
            if not hh["emergency_care"]:
                hh.pop("emergency_care")
            self.add(hid, "random", hh, fac_index=rng.randint(0, 2))


def build_households(demo_dir: str | Path | None = None) -> dict[str, Any]:
    """Generate the household set with oracle expectations (deterministic)."""
    from charitycare.config import get_settings

    root = Path(demo_dir) if demo_dir else get_settings().demo_dir
    golds = load_gold(root)
    b = _Builder(golds)
    for step in (b.boundaries, b.size_changes, b.assets, b.residency, b.insured, b.catastrophic,
                 b.fpg_years, b.deadlines, b.scenarios):
        step()
    if len(b.cases) > TARGET:
        raise RuntimeError(f"{len(b.cases)} edge cases exceed the target of {TARGET}")
    b.random_fill()
    for i, c in enumerate(b.cases, 1):
        c["id"] = f"hh-{i:03d}"
        c["expected"] = oracle.expected(b.golds[c["hospital_id"]]["policy"], c["household"],
                                        date.fromisoformat(c["today"]), c["scenario"])
    cases = [{k: c[k] for k in ("id", "hospital_id", "facility_id", "category", "scenario", "today", "household", "expected")}
             for c in b.cases]
    return {"version": HOUSEHOLDS_VERSION, "synthetic": True, "seed": SEED, "count": len(cases),
            "note": "Synthetic households for engine evaluation. Expected values come from charitycare.engine.oracle.",
            "households": cases}


def households_path(demo_dir: str | Path | None = None) -> Path:
    from charitycare.config import get_settings

    return (Path(demo_dir) if demo_dir else get_settings().demo_dir) / "households.json"


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def write_households(demo_dir: str | Path | None = None) -> Path:
    out = households_path(demo_dir)
    out.write_text(dumps(build_households(demo_dir)), encoding="utf-8")
    return out


def load_households(demo_dir: str | Path | None = None) -> dict[str, Any]:
    return json.loads(households_path(demo_dir).read_text(encoding="utf-8"))

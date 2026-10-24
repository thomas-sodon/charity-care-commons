"""Round-trip tests (design section 5 mechanism 2, step S5).

When a policy prints its own income table ("household of 3: $53,300 = 200%"),
the compiled tiers plus the HHS guideline for the policy's year must reproduce
it. Two things are checked, in code only:

* every percentage column is 100% or a compiled tier boundary (a column the
  tiers do not explain means a misread tier);
* every printed dollar amount equals ``pct% x FPG(year, size)`` within rounding.

When the amounts do not match the stated year, the other loaded guideline
years are tried so the reviewer sees a diagnosis such as "table matches the
2025 guidelines" (a stale table relabelled as the new year).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from charitycare import fpg as fpglib
from charitycare.compile.textlayer import DocText
from charitycare.engine.core import guideline_year_in
from charitycare.schemas import CompiledPolicy, FPGTier

_HEADER = re.compile(r"(household|family)\s+size|tama[ñn]o", re.IGNORECASE)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{2})?)")
_YEAR = re.compile(r"\b(20\d\d)\b")


@dataclass(frozen=True)
class PrintedTable:
    """An income table printed in the policy."""

    page: int
    stated_year: int | None
    pcts: tuple[float, ...]
    rows: tuple[tuple[int, tuple[Decimal, ...]], ...]  # (household size, amounts per pct column)
    caption: str = ""


@dataclass
class RoundTripResult:
    """Outcome of re-deriving one printed table from the compiled rule."""

    page: int
    result: Literal["match", "mismatch", "not_applicable"]
    year_used: int | None
    stated_year: int | None
    mismatched_cells: list[dict[str, Any]] = field(default_factory=list)
    unexplained_columns: list[float] = field(default_factory=list)
    boundaries_not_in_table: list[float] = field(default_factory=list)
    best_matching_year: int | None = None
    note: str = ""


def parse_tables(doc: DocText) -> list[PrintedTable]:
    """Find printed FPG income tables (a header with % columns, then size + $ rows)."""
    tables: list[PrintedTable] = []
    for p in doc.pages:
        lines = p.text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            pcts = [float(x) for x in _PCT.findall(line)]
            if _HEADER.search(line) and len(pcts) >= 2:
                caption = next((lines[j] for j in range(i - 1, max(-1, i - 3), -1) if _YEAR.search(lines[j])), "")
                cap_year = guideline_year_in(caption, table_only=False)
                rows: list[tuple[int, tuple[Decimal, ...]]] = []
                j = i + 1
                while j < len(lines):
                    m = re.match(r"\s*(\d{1,2})\s+(.*)$", lines[j])
                    amounts = _MONEY.findall(lines[j])
                    if not m or len(amounts) != len(pcts):
                        break
                    rows.append((int(m.group(1)), tuple(Decimal(a.replace(",", "")) for a in amounts)))
                    j += 1
                if rows:
                    tables.append(PrintedTable(p.page, cap_year, tuple(pcts), tuple(rows), caption.strip()))
                i = j
                continue
            i += 1
    return tables


def tier_boundaries(tiers: list[FPGTier]) -> set[float]:
    out: set[float] = set()
    for t in tiers:
        out.add(float(t.max_pct_inclusive))
        if t.min_pct_exclusive is not None:
            out.add(float(t.min_pct_exclusive))
    return out


def _cells_off(table: PrintedTable, year: int, tolerance: Decimal) -> list[dict[str, Any]]:
    bad = []
    for size, amounts in table.rows:
        for pct, amount in zip(table.pcts, amounts):
            expected = fpglib.income_at_pct(pct, size, year)
            if abs(expected - amount) > tolerance:
                bad.append({"household_size": size, "pct": pct, "printed": str(amount), "expected": str(expected.quantize(Decimal("1")))})
    return bad


def policy_year(policy: CompiledPolicy, table: PrintedTable) -> int | None:
    """Guideline year the table should use: the stated year, else the year current at the effective date."""
    if table.stated_year is not None:
        return table.stated_year
    eff = policy.effective_or_revision_date.value
    return fpglib.guideline_year_for(eff) if isinstance(eff, date) else None


def round_trip(policy: CompiledPolicy, doc: DocText, *, tolerance: Decimal = Decimal("1")) -> list[RoundTripResult]:
    """Run the round-trip test for every printed table in the policy document."""
    tiers = policy.fpg_tiers.value or []
    results = []
    for table in parse_tables(doc):
        year = policy_year(policy, table)
        if not tiers or year is None or year not in fpglib.years():
            results.append(RoundTripResult(table.page, "not_applicable", year, table.stated_year, note="no compiled tiers or unknown guideline year"))
            continue
        bounds = tier_boundaries(tiers)
        unexplained = [p for p in table.pcts if p != 100.0 and p not in bounds]
        missing = sorted(b for b in bounds if b not in table.pcts)
        bad = _cells_off(table, year, tolerance)
        best = None
        if bad:
            fits = [y for y in fpglib.years() if y != year and not _cells_off(table, y, tolerance)]
            best = fits[0] if fits else None
        ok = not bad and not unexplained
        notes = []
        if unexplained:
            notes.append(f"table columns {unexplained} are not compiled tier boundaries {sorted(bounds)}")
        if bad:
            notes.append(f"{len(bad)} of {len(table.rows) * len(table.pcts)} amounts differ from {year} guidelines")
            if best:
                notes.append(f"table matches the {best} guidelines")
        if missing:
            notes.append(f"tier boundaries {missing} are not printed in the table")
        results.append(
            RoundTripResult(
                table.page,
                "match" if ok else "mismatch",
                year,
                table.stated_year,
                mismatched_cells=bad,
                unexplained_columns=unexplained,
                boundaries_not_in_table=missing,
                best_matching_year=best,
                note="; ".join(notes) or f"all amounts reproduce the {year} guidelines",
            )
        )
    return results

"""``ccc screen`` and ``ccc households`` subcommands.

The integrator registers :data:`COMMANDS` in :data:`charitycare.cli.COMMANDS`;
until then they run as ``python -m charitycare.engine ...``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path


def _cfg_screen(p: argparse.ArgumentParser) -> None:
    p.add_argument("facility_id", help='e.g. "tx-459901"')
    p.add_argument("--size", type=int, required=True, help="household size")
    p.add_argument("--income", type=Decimal, required=True, help="annual household income in dollars")
    p.add_argument("--assets", type=Decimal, default=None, help="liquid assets")
    p.add_argument("--county", default=None)
    p.add_argument("--state", default="TX")
    p.add_argument("--insured", choices=["yes", "no"], default=None)
    p.add_argument("--oop", type=Decimal, default=None, help="out-of-pocket medical bills, prior 12 months")
    p.add_argument("--emergency", action="store_true")
    p.add_argument("--service-date", type=date.fromisoformat, default=None)
    p.add_argument("--statement-date", type=date.fromisoformat, default=None, help="first post-discharge statement")
    p.add_argument("--application-date", type=date.fromisoformat, default=None)
    p.add_argument("--db", type=Path, default=None, help="registry path (default: demo gold policies)")
    p.add_argument("--json", action="store_true", help="print the Determination as JSON")


def _run_screen(a: argparse.Namespace) -> int:
    from charitycare.engine.render import render_text
    from charitycare.engine.screen import UnknownFacility, screen
    from charitycare.schemas import Household
    from charitycare.store import Registry

    hh = Household(household_size=a.size, annual_income=a.income, liquid_assets=a.assets, county=a.county,
                   state=a.state, insured=None if a.insured is None else a.insured == "yes",
                   out_of_pocket_medical_12m=a.oop, emergency_care=a.emergency, service_date=a.service_date,
                   first_statement_date=a.statement_date, application_date=a.application_date)
    reg = Registry(a.db) if a.db else None
    try:
        det = screen(a.facility_id, hh, registry=reg)
    except UnknownFacility:
        print(f"unknown facility: {a.facility_id}", file=sys.stderr)
        return 2
    finally:
        if reg is not None:
            reg.close()
    print(det.model_dump_json(indent=2) if a.json else render_text(det))
    return 0


def _cfg_households(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="hh_cmd", required=True)
    sub.add_parser("build", help="write data/demo/households.json").add_argument("--demo-dir", type=Path, default=None)
    sub.add_parser("score", help="engine vs oracle agreement").add_argument("--demo-dir", type=Path, default=None)


def _run_households(a: argparse.Namespace) -> int:
    from charitycare.engine.agreement import score
    from charitycare.engine.households import load_households, write_households

    if a.hh_cmd == "build":
        print(write_households(a.demo_dir))
        return 0
    res = score(load_households(a.demo_dir)["households"], a.demo_dir)
    print(json.dumps({"total": res.total, "agree": res.agree, "rate": res.rate, "mismatches": res.mismatches[:20]}, indent=2))
    return 0 if res.agree == res.total else 1


COMMANDS = [
    ("screen", "deterministic screening of a household at a facility", _cfg_screen, _run_screen),
    ("households", "synthetic household set (build, score)", _cfg_households, _run_households),
]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m charitycare.engine")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_, configure, run in COMMANDS:
        sp = sub.add_parser(name, help=help_)
        configure(sp)
        sp.set_defaults(_run=run)
    args = parser.parse_args(argv)
    return int(args._run(args))

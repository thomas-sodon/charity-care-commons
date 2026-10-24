"""``ccc compile`` subcommands. The integrator registers :data:`COMMAND` in ``cli.COMMANDS``.

* ``ccc compile facility <facility_id> [--force]``: compile the governing policy
* ``ccc compile all [--force]``: every facility in the registry
* ``ccc compile demo [--db PATH]``: load the synthetic corpus (seed/discover stand-in) and compile it
* ``ccc compile queue [--limit N]``: open review tasks by priority and impact
* ``ccc compile fixtures``: regenerate the adjudicator replay fixtures
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=None, help="registry path (default CCC_DB_PATH)")
    sub = p.add_subparsers(dest="compile_cmd", required=True)
    f = sub.add_parser("facility", help="compile the policy governing one facility")
    f.add_argument("facility_id")
    f.add_argument("--force", action="store_true")
    a = sub.add_parser("all", help="compile every facility in the registry")
    a.add_argument("--force", action="store_true")
    d = sub.add_parser("demo", help="load the synthetic corpus and compile it")
    d.add_argument("--demo-dir", type=Path, default=None)
    q = sub.add_parser("queue", help="list open review tasks")
    q.add_argument("--limit", type=int, default=25)
    sub.add_parser("fixtures", help="regenerate adjudicate@v1 replay fixtures from the demo corpus")


def run(a: argparse.Namespace) -> int:
    from charitycare.compile.demo import load_demo_registry
    from charitycare.compile.pipeline import compile_facility
    from charitycare.compile.review import review_queue
    from charitycare.store import Registry

    if a.compile_cmd == "fixtures":
        from charitycare.compile.fixtures import write_adjudication_fixtures

        print(f"wrote {len(write_adjudication_fixtures())} adjudication fixtures")
        return 0
    with Registry(a.db) as reg:
        if a.compile_cmd == "queue":
            for t in review_queue(reg)[: a.limit]:
                print(f"{t.priority:<6} {t.kind:<24} {(t.notes or '')[:150]}")
            return 0
        if a.compile_cmd == "demo":
            load_demo_registry(reg, a.demo_dir)
        ids = [a.facility_id] if a.compile_cmd == "facility" else [f.id for f in reg.list_facilities(state=None)]
        for fid in ids:
            report = compile_facility(reg, fid, force=getattr(a, "force", False))
            print(json.dumps({"facility_id": fid, **report.summary()}))
    return 0


COMMAND = ("compile", "compile policies: quote gate, round trip, Schedule H cross-check, review queue", configure, run)

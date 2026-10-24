"""``ccc seed`` subcommands (also runnable as ``python -m charitycare.seed``)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


def configure(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="seed_cmd", required=True)
    c = sub.add_parser("cms", help="load the CMS facility universe (live API with --live, else cache/demo CSV)")
    c.add_argument("--live", action="store_true", help="fetch from data.cms.gov and refresh data/seed/cms_tx_hospitals.csv")
    c.add_argument("--csv", type=Path, default=None)
    d = sub.add_parser("demo", help="seed the registry from the synthetic demo corpus")
    d.add_argument("--db", type=Path, default=None)
    d.add_argument("--external", type=Path, default=None, help="optional external facility list CSV")
    r = sub.add_parser("run", help="seed from CMS + IRS XML files / a batch ZIP")
    r.add_argument("--db", type=Path, default=None)
    r.add_argument("--live-cms", action="store_true")
    r.add_argument("--cms-csv", type=Path, default=None)
    r.add_argument("--xml", type=Path, nargs="*", default=[])
    r.add_argument("--zip", type=Path, default=None)
    r.add_argument("--index", type=Path, default=None)
    r.add_argument("--external", type=Path, default=None)
    s = sub.add_parser("schedule-h", help="parse one Schedule H XML file and print the Part V sections")
    s.add_argument("xml", type=Path)


def run(a: argparse.Namespace) -> int:
    from charitycare.store import Registry

    if a.seed_cmd == "cms":
        from charitycare.seed.cms import load_cms

        load = load_cms(live=a.live, csv_path=a.csv)
        print(json.dumps({"origin": load.origin, "in_scope": len(load.rows), "excluded": load.excluded}, indent=2))
        return 0
    if a.seed_cmd == "schedule-h":
        from charitycare.seed.schedule_h import parse_file

        ret = parse_file(str(a.xml))
        print(json.dumps([s.model_dump(mode="json") for s in ret.sections], indent=2))
        return 0
    from charitycare.seed.pipeline import seed_demo, seed_from_paths

    with Registry(a.db) as reg:
        if a.seed_cmd == "demo":
            report = seed_demo(reg, external_csv=a.external)
        else:
            report = seed_from_paths(
                reg, live_cms=a.live_cms, cms_csv=a.cms_csv, xml_files=a.xml, batch_zip=a.zip,
                index_csv=a.index, external_csv=a.external,
            )
    print(json.dumps(report.as_dict(), indent=2, default=str))
    return 0


CLI_COMMAND = ("seed", "facility universe and Schedule H seed (S1)", configure, run)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m charitycare.seed")
    configure(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

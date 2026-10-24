"""``ccc site build`` / ``python -m charitycare.site build``: render the static site.

The integrator registers :data:`COMMAND` in ``charitycare.cli.COMMANDS``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path


def configure(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="site_cmd", required=True)
    b = sub.add_parser("build", help="render site/dist/ from the registry")
    b.add_argument("--db", type=Path, default=None, help="registry path (default CCC_DB_PATH)")
    b.add_argument("--demo", action="store_true", help="build from an in-memory registry seeded from data/demo")
    b.add_argument("--out", type=Path, default=None, help="output directory (default site/dist)")
    b.add_argument("--evals", type=Path, action="append", default=[], help="EvalRun JSON file or directory (repeatable)")
    b.add_argument("--release", type=Path, default=None, help="release snapshot directory to publish on the data page")
    b.add_argument("--api", default=None, help="public API base URL to link to")
    b.add_argument("--today", type=date.fromisoformat, default=None, help="as-of date (default today)")


def run(a: argparse.Namespace) -> int:
    from charitycare.site.build import build_site
    from charitycare.store import Registry

    reg = Registry(":memory:" if a.demo else a.db)
    if a.demo:
        from charitycare.api.demo import seed_demo

        seed_demo(reg)
    rep = build_site(reg, a.out, today=a.today, api_base_url=a.api, eval_paths=a.evals, release_dir=a.release)
    print(f"wrote {len(rep.pages)} pages and {len(rep.data_files)} data files to {rep.out_dir}")
    return 0


COMMAND = ("site", "static public site", configure, run)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m charitycare.site")
    configure(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

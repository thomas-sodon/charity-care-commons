"""``ccc export`` / ``python -m charitycare.export``: write a CC BY 4.0 release snapshot.

The integrator registers :data:`COMMAND` in ``charitycare.cli.COMMANDS``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=None, help="registry path (default CCC_DB_PATH)")
    p.add_argument("--demo", action="store_true", help="export an in-memory registry seeded from data/demo")
    p.add_argument("--out", type=Path, default=None, help="output directory (default data/release/<version>)")
    p.add_argument("--version", dest="release_version", default=None, help="release version label (default vYYYY.MM.DD)")
    p.add_argument("--verified-only", action="store_true", help="only reviewer-verified fields and confirmed abstentions")


def run(a: argparse.Namespace) -> int:
    from charitycare.export.snapshot import export_release
    from charitycare.store import Registry

    reg = Registry(":memory:" if a.demo else a.db)
    if a.demo:
        from charitycare.api.demo import seed_demo

        seed_demo(reg)
    m = export_release(reg, a.out, version=a.release_version, verified_only=a.verified_only)
    print(f"release {m.version}: {m.rows} -> {m.out_dir}")
    return 0


COMMAND = ("export", "CSV/Parquet release snapshot (CC BY 4.0)", configure, run)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m charitycare.export")
    configure(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

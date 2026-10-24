"""``ccc serve`` / ``python -m charitycare.api``: run the API with uvicorn.

The integrator registers :data:`COMMAND` in ``charitycare.cli.COMMANDS``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def configure(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=None, help="registry path (default CCC_DB_PATH / data/registry.sqlite)")
    p.add_argument("--demo", action="store_true", help="serve an in-memory registry seeded from data/demo")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--console-open", action="store_true", help="open the review console without a token (local only)")


def build(a: argparse.Namespace):  # type: ignore[no-untyped-def]
    from charitycare.api import ApiSettings, create_app
    from charitycare.store import Registry

    if a.demo:
        from charitycare.api.demo import seed_demo

        reg = Registry(":memory:")
        seed_demo(reg)
    else:
        reg = Registry(a.db)
    overrides = {"console_open": True} if a.console_open else {}
    return create_app(reg, settings=ApiSettings.from_env(**overrides))


def run(a: argparse.Namespace) -> int:
    import uvicorn

    # access_log=False: uvicorn's access log would record raw paths and query strings.
    uvicorn.run(build(a), host=a.host, port=a.port, access_log=False)
    return 0


COMMAND = ("serve", "run the read API, review console and navigator page", configure, run)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m charitycare.api")
    sub = p.add_subparsers(dest="cmd", required=True)
    configure(sub.add_parser("serve", help=COMMAND[1]))
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

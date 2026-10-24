"""``ccc`` command-line interface.

Each stage exposes a ``(name, help, configure, run)`` entry; :data:`COMMANDS` wires
them into one tool. ``ccc demo`` runs the whole pipeline offline on the synthetic
corpus (see :mod:`charitycare.demo.pipeline`); ``ccc demo build`` only regenerates
the corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

Configure = Callable[[argparse.ArgumentParser], None]
Run = Callable[[argparse.Namespace], int]


def _cfg_demo(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workdir", type=Path, default=None, help="intermediate outputs (default data/demo-run)")
    p.add_argument("--site-dir", type=Path, default=None, help="static site output (default site/dist)")
    p.add_argument("--no-evals", action="store_true", help="skip the offline eval suites")
    p.add_argument("--json", action="store_true", help="print the full stage report as JSON")
    sub = p.add_subparsers(dest="demo_cmd", required=False)
    b = sub.add_parser("build", help="only generate the synthetic corpus under data/demo/ and replay fixtures")
    b.add_argument("--out", type=Path, default=None, help="output directory (default data/demo)")
    b.add_argument("--fixtures", type=Path, default=None, help="fixtures directory (default fixtures/llm)")


def _run_demo(a: argparse.Namespace) -> int:
    if a.demo_cmd == "build":
        from charitycare.demo.build import build_corpus

        m = build_corpus(a.out, a.fixtures)
        print(f"built {len(m['hospitals'])} synthetic hospitals, {m['files']} files (checksum {m['checksum'][:16]})")
        return 0
    from charitycare.demo.pipeline import run_demo

    print("Charity Care Commons demo: offline, replay fixtures, synthetic corpus (fictional hospitals)")
    rep = run_demo(a.workdir, site_dir=a.site_dir, run_evals=not a.no_evals)
    if a.json:
        print(json.dumps(rep.stages, indent=2, default=str))
    scr = rep.stages["screen"]
    print(f"\nMaria at {scr['facility']}: {scr['outcome']} | {scr['arithmetic']} | federal deadline {scr['federal_deadline']}")
    print(f"Review queue left for a human: {rep.stages['review']['left_open']}")
    print("\nOutputs:")
    for name, path in rep.outputs.items():
        print(f"  {name:<13} {'ok ' if path.exists() else 'MISSING'} {path}")
    return 0 if rep.ok() else 1


def _cfg_schema(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="schema_cmd", required=True)
    e = sub.add_parser("export", help="write the Open FAP Schema JSON")
    e.add_argument("--out", type=Path, default=None)


def _run_schema(a: argparse.Namespace) -> int:
    from charitycare.schemas import export_json_schema

    print(export_json_schema(a.out))
    return 0


def _cfg_db(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="db_cmd", required=True)
    i = sub.add_parser("init", help="create the SQLite registry tables")
    i.add_argument("--path", type=Path, default=None)
    sub.add_parser("stats", help="row counts per table").add_argument("--path", type=Path, default=None)


def _run_db(a: argparse.Namespace) -> int:
    from charitycare.store import TABLES, Registry

    with Registry(a.path) as reg:
        if a.db_cmd == "init":
            print(f"registry ready at {reg.path}")
        else:
            print(json.dumps({spec.name: reg.count(model) for model, spec in TABLES.items()}, indent=2))
    return 0


def _cfg_fpg(p: argparse.ArgumentParser) -> None:
    p.add_argument("household_size", type=int)
    p.add_argument("income", type=Decimal, nargs="?")
    p.add_argument("--year", type=int, default=None)


def _run_fpg(a: argparse.Namespace) -> int:
    from charitycare import fpg

    year = a.year or fpg.latest_year()
    line = f"{year} HHS poverty guideline, household of {a.household_size}: {fpg.format_usd(fpg.fpg(year, a.household_size))}"
    if a.income is not None:
        line += f"; {fpg.format_usd(a.income)} = {fpg.pct_fpl(a.income, a.household_size, year)}% FPL"
    print(line)
    return 0


def _cfg_llm(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mode", choices=["live", "replay", "record"], default=None)


def _run_llm(a: argparse.Namespace) -> int:
    from charitycare.config import get_settings

    s = get_settings()
    print(json.dumps({"mode": a.mode or s.llm_mode, "models": s.models, "fixtures": str(s.llm_fixtures_dir)}, indent=2))
    return 0


def _cfg_review(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=None, help="registry path (default CCC_DB_PATH)")
    sub = p.add_subparsers(dest="review_cmd", required=True)
    q = sub.add_parser("queue", help="open review tasks, highest impact first")
    q.add_argument("--limit", type=int, default=25)
    d = sub.add_parser("decide", help="apply one reviewer decision (same path as the console)")
    d.add_argument("task_id")
    d.add_argument("--action", choices=["approve", "edit", "abstain", "ask_hospital"], required=True)
    d.add_argument("--reviewer", required=True)
    d.add_argument("--value", default=None, help="edited value (JSON)")
    d.add_argument("--quote", default=None)
    d.add_argument("--page", type=int, default=None)
    d.add_argument("--reason", default=None, help="abstain reason")
    d.add_argument("--note", default=None)
    au = sub.add_parser("demo-auto", help="DEMO ONLY: approve gold-matching tasks as 'demo:gold-reviewer'")
    au.add_argument("--demo-dir", type=Path, default=None)
    au.add_argument("--i-understand-this-is-a-demo", dest="demo_flag", action="store_true", required=True)


def _run_review(a: argparse.Namespace) -> int:
    from charitycare.api import review
    from charitycare.schemas import ReviewDecision
    from charitycare.store import Registry

    with Registry(a.db) as reg:
        if a.review_cmd == "queue":
            for t in review.queue(reg)[: a.limit]:
                print(f"{review.task_impact(t):>4} {t.priority:<6} {t.kind:<24} {t.id}\n      {review.task_note(t)[:160]}")
            return 0
        if a.review_cmd == "decide":
            d = ReviewDecision(task_id=a.task_id, action=a.action, reviewer=a.reviewer, note=a.note,
                               value=review.parse_value(a.value), quote=a.quote, page=a.page, abstain_reason=a.reason)
            try:
                task, _, ev = review.apply_decision(reg, d)
            except review.ReviewError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"{task.id}: {task.state}\n{ev.diff_summary.split(chr(10) + '---' + chr(10))[0]}")
            return 0
        from charitycare.config import get_settings
        from charitycare.demo.reviewer import auto_review, verify_templates

        demo_dir = a.demo_dir or get_settings().demo_dir
        summary = auto_review(reg, demo_dir, demo=a.demo_flag)
        summary.templates_verified, summary.templates_open = verify_templates(reg, demo_dir, demo=a.demo_flag)
        print(json.dumps(summary.as_dict(), indent=2))
    return 0


def _cfg_mcp(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=None, help="registry path (default CCC_DB_PATH)")
    p.add_argument("--demo", action="store_true", help="serve an in-memory registry seeded from data/demo")


def _run_mcp(a: argparse.Namespace) -> int:
    from charitycare.mcp_server import main as mcp_main

    return mcp_main((["--db", a.db] if a.db else []) + (["--demo"] if a.demo else []))


def _cfg_eval(p: argparse.ArgumentParser) -> None:
    p.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the eval harness, e.g. run --suite smoke")


def _run_eval(a: argparse.Namespace) -> int:
    try:
        from evals.cli import main as evals_main
    except ImportError:  # pragma: no cover - evals ships in the wheel; this guards odd installs
        print("error: the evals harness is not importable; run from a repository checkout", file=sys.stderr)
        return 2
    return evals_main(a.args or ["list"])


def _stage_commands() -> list[tuple[str, str, Configure, Run]]:
    from charitycare.api.cli import COMMAND as SERVE
    from charitycare.compile.cli import COMMAND as COMPILE
    from charitycare.discover.cli import CLI_COMMAND as DISCOVER
    from charitycare.engine.cli import COMMANDS as ENGINE
    from charitycare.export.cli import COMMAND as EXPORT
    from charitycare.packet.cli import COMMAND as PACKET
    from charitycare.seed.cli import CLI_COMMAND as SEED
    from charitycare.site.cli import COMMAND as SITE

    review = ("review", "review queue and reviewer decisions", _cfg_review, _run_review)
    mcp = ("mcp", "MCP server over stdio (read-only tools)", _cfg_mcp, _run_mcp)
    ev = ("eval", "evaluation harness (python -m evals)", _cfg_eval, _run_eval)
    return [SEED, DISCOVER, COMPILE, review, *ENGINE, PACKET, EXPORT, SITE, SERVE, mcp, ev]


COMMANDS: list[tuple[str, str, Configure, Run]] = [
    ("demo", "run the whole pipeline offline on the synthetic corpus (or: demo build)", _cfg_demo, _run_demo),
    *_stage_commands(),
    ("schema", "Open FAP Schema", _cfg_schema, _run_schema),
    ("db", "registry database", _cfg_db, _run_db),
    ("fpg", "poverty guideline lookup and %FPL", _cfg_fpg, _run_fpg),
    ("llm", "show LLM backend configuration", _cfg_llm, _run_llm),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccc", description="Charity Care Commons (Texas edition)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_, configure, run in COMMANDS:
        sp = sub.add_parser(name, help=help_.replace("%", "%%"))  # argparse %-formats help strings
        configure(sp)
        sp.set_defaults(_run=run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args._run(args))


if __name__ == "__main__":
    sys.exit(main())

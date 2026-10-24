"""``python -m evals`` command line.

    python -m evals list
    python -m evals run --suite smoke [--mode replay|live|record] [--no-store] [--db PATH] [--registry PATH]
    python -m evals gate --suite smoke [--run FILE] [--baseline FILE] [--enforce-targets] [--update-baseline]
    python -m evals report
    python -m evals agreement --a DIR --b DIR [--out FILE]
    python -m evals schema-compat [--committed FILE] [--base FILE]
    python -m evals baseline-fixtures
    python -m evals support-sample --suite full [--n 100] --out FILE
    python -m evals support-score FILE

Exit codes: 0 pass, 1 gate or check failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


def _cmd_list(a: argparse.Namespace) -> int:
    from evals.scorers import METRICS
    from evals.suites import load_suite, suite_names

    for name in suite_names():
        s = load_suite(name)
        print(f"{name:<8} {s.kind:<8} split={s.split or '-':<6} {s.description}")
    print()
    for m in METRICS.values():
        print(f"{m.name:<52} {m.kind:<8} {m.direction:<6} {m.description}")
    return 0


def _cmd_run(a: argparse.Namespace) -> int:
    from evals.report import REPORTS_DIR, latest_runs, write_report
    from evals.runner import RUNS_DIR, run_suite
    from evals.suites import load_suite

    suite = load_suite(a.suite)
    out_dir = Path(a.out_dir) if a.out_dir else RUNS_DIR
    try:
        r = run_suite(suite, mode=a.mode, store=not a.no_store, db_path=a.db, registry_path=a.registry, out_dir=out_dir)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{r.id}  suite={r.suite} mode={r.mode} units={len(r.units)} cost=${r.cost_usd:.4f}{' (simulated)' if r.simulated_cost else ''}")
    for m in r.metrics.values():
        ci = f" [{m.ci_low}, {m.ci_high}]" if m.ci_low is not None else ""
        tgt = f"  target {m.target}: {'ok' if m.met else 'MISS' if m.met is False else 'n/a'}" if m.target else ""
        print(f"  {m.name:<52} {m.value!s:<10}{ci} n={m.n}{tgt}")
    for n in r.notes:
        print(f"  note: {n}")
    if not a.no_report:
        write_report(latest_runs(out_dir), Path(a.report_dir) if a.report_dir else REPORTS_DIR)
    hard = [m for m in r.metrics.values() if m.hard and m.met is False]
    return 1 if hard else 0


def _cmd_gate(a: argparse.Namespace) -> int:
    from evals.gates import baseline_path, check, format_failures, load_baseline, write_baseline
    from evals.runner import RUNS_DIR, load_run
    from evals.suites import load_suite

    suite = load_suite(a.suite)
    try:
        run = load_run(a.run or a.suite, Path(a.runs_dir) if a.runs_dir else RUNS_DIR)
    except FileNotFoundError as exc:
        print(f"error: no run to gate ({exc.filename}); run `python -m evals run --suite {suite.name}` first", file=sys.stderr)
        return 2
    if run.suite != suite.name:
        print(f"error: run {run.id} is for suite {run.suite}, not {suite.name}", file=sys.stderr)
        return 2
    bpath = Path(a.baseline) if a.baseline else baseline_path(suite.name, run.mode, for_write=a.update_baseline)
    if a.update_baseline:
        print(f"baseline written: {write_baseline(run, bpath)}")
        return 0
    if not bpath.exists():
        print(f"error: no baseline at {bpath}; create one with --update-baseline", file=sys.stderr)
        return 2
    failures = check(run, load_baseline(bpath), suite=suite, enforce_targets=a.enforce_targets)
    print(f"{run.id} vs {bpath.name}")
    print(format_failures(failures))
    return 1 if failures else 0


def _cmd_report(a: argparse.Namespace) -> int:
    from evals.report import REPORTS_DIR, latest_runs, write_report
    from evals.runner import RUNS_DIR

    runs = latest_runs(Path(a.runs_dir) if a.runs_dir else RUNS_DIR)
    if not runs:
        print("no runs yet; run `python -m evals run --suite smoke` first", file=sys.stderr)
        return 2
    j, md = write_report(runs, Path(a.out_dir) if a.out_dir else REPORTS_DIR)
    print(f"wrote {j} and {md}")
    return 0


def _cmd_agreement(a: argparse.Namespace) -> int:
    from evals.agreement import interrater, load_coder_dir

    rep = interrater(load_coder_dir(Path(a.a)), load_coder_dir(Path(a.b)))
    text = json.dumps(rep.to_json(), indent=2, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(f"{len(rep.hospitals)} double-coded hospitals; agreement {rep.overall_agreement}; Cohen's kappa {rep.overall_kappa}")
    for f in rep.fields.values():
        print(f"  {f.key:<38} agreement={f.agreement} kappa={f.kappa}")
    print(f"{len(rep.disagreements)} disagreements to adjudicate")
    return 0


def _cmd_schema_compat(a: argparse.Namespace) -> int:
    from charitycare.config import get_settings
    from evals.schema_compat import check_fresh, compare

    committed = Path(a.committed) if a.committed else get_settings().schemas_dir / "open-fap-schema.json"
    ok = True
    if check_fresh(committed):
        print(f"fresh: {committed} matches the generator")
    else:
        ok = False
        print(f"STALE: {committed} differs from charitycare.schemas.open_fap_schema(); run `ccc schema export`")
    if a.base:
        base = Path(a.base)
        if not base.is_file() or not base.read_text(encoding="utf-8").strip():
            print(f"no base schema at {base}; skipping the compatibility comparison")
        else:
            r = compare(json.loads(base.read_text(encoding="utf-8")), json.loads(committed.read_text(encoding="utf-8")))
            for x in r.additive:
                print(f"  additive: {x}")
            for x in r.breaking:
                print(f"  BREAKING: {x}")
            if not r.ok:
                ok = False
                print(f"incompatible: {len(r.breaking)} breaking change(s) without a version bump ({r.old_version})")
            else:
                print(f"compatible ({r.old_version} -> {r.new_version})")
    return 0 if ok else 1


def _cmd_baseline_fixtures(a: argparse.Namespace) -> int:
    from evals.baseline import write_demo_fixtures
    from evals.cases import load_dataset

    paths = write_demo_fixtures(load_dataset(a.dataset), Path(a.fixtures) if a.fixtures else None)
    print(f"wrote {len(paths)} synthetic naive-baseline fixtures")
    return 0


def _cmd_support_sample(a: argparse.Namespace) -> int:
    from charitycare.llm import get_backend
    from evals.cases import load_dataset
    from evals.predict import compile_cases
    from evals.suites import load_suite
    from evals.support import sample_rows, write_sample

    suite = load_suite(a.suite)
    ds = load_dataset(suite.dataset)
    cases = compile_cases(ds, ds.hospitals(suite.split), backend=get_backend(a.mode))
    print(write_sample(sample_rows(cases, a.n), Path(a.out)))
    return 0


def _cmd_support_score(a: argparse.Namespace) -> int:
    from evals.support import score_judged

    v, n = score_judged(Path(a.file))
    print(f"citation support: {v} over {n} judged fields (target >= 0.95)")
    return 0 if v is None or v >= 0.95 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evals", description="Charity Care Commons evaluation harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="suites and metrics").set_defaults(fn=_cmd_list)

    r = sub.add_parser("run", help="run a suite and record an eval_run")
    r.add_argument("--suite", required=True)
    r.add_argument("--mode", choices=["replay", "live", "record"], default=None, help="default: replay unless ANTHROPIC_API_KEY is set")
    r.add_argument("--no-store", action="store_true", help="do not write the EvalRun row to the registry")
    r.add_argument("--db", default=None, help="registry for the EvalRun row (default CCC_DB_PATH)")
    r.add_argument("--registry", default=None, help="score an existing registry instead of compiling the corpus")
    r.add_argument("--out-dir", default=None, help="runs directory (default evals/runs)")
    r.add_argument("--report-dir", default=None)
    r.add_argument("--no-report", action="store_true")
    r.set_defaults(fn=_cmd_run)

    g = sub.add_parser("gate", help="compare a run with the baseline metrics file")
    g.add_argument("--suite", required=True)
    g.add_argument("--run", default=None, help="run JSON (default evals/runs/latest-<suite>.json)")
    g.add_argument("--runs-dir", default=None)
    g.add_argument("--baseline", default=None, help="default evals/baselines/<suite>.json")
    g.add_argument("--enforce-targets", action="store_true", help="also fail on any missed section 12 target (new states)")
    g.add_argument("--update-baseline", action="store_true")
    g.set_defaults(fn=_cmd_gate)

    rp = sub.add_parser("report", help="write evals/reports/latest.{json,md}")
    rp.add_argument("--runs-dir", default=None)
    rp.add_argument("--out-dir", default=None)
    rp.set_defaults(fn=_cmd_report)

    ag = sub.add_parser("agreement", help="inter-rater agreement and Cohen's kappa for double-coded gold")
    ag.add_argument("--a", required=True)
    ag.add_argument("--b", required=True)
    ag.add_argument("--out", default=None)
    ag.set_defaults(fn=_cmd_agreement)

    sc = sub.add_parser("schema-compat", help="Open FAP Schema freshness and backward compatibility")
    sc.add_argument("--committed", default=None)
    sc.add_argument("--base", default=None, help="previous published schema (e.g. from main)")
    sc.set_defaults(fn=_cmd_schema_compat)

    bf = sub.add_parser("baseline-fixtures", help="write the synthetic naive-baseline replay fixtures")
    bf.add_argument("--dataset", default="demo")
    bf.add_argument("--fixtures", default=None)
    bf.set_defaults(fn=_cmd_baseline_fixtures)

    ss = sub.add_parser("support-sample", help="sample published fields for human citation-support judging")
    ss.add_argument("--suite", default="full")
    ss.add_argument("--n", type=int, default=100)
    ss.add_argument("--mode", choices=["replay", "live", "record"], default=None)
    ss.add_argument("--out", required=True)
    ss.set_defaults(fn=_cmd_support_sample)

    sj = sub.add_parser("support-score", help="score a judged citation-support CSV")
    sj.add_argument("file")
    sj.set_defaults(fn=_cmd_support_score)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a))

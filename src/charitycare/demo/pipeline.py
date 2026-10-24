"""``ccc demo``: the whole pipeline, offline, on the synthetic corpus (zero cost, no API key).

Stages, in order (design section 8):

1. generate the synthetic corpus (deterministic; checked against the committed manifest)
2. S1 seed: CMS facility universe + Schedule H Part V (from the corpus's CMS CSV and IRS XML)
3. S2-S3 discover: crawl the offline demo web, store and classify documents, map policies
4. S4-S9 compile with the replay backend: quote gate, re-ask, round trip, Schedule H cross-check, adjudication
5. review: the explicit *demo* reviewer approves gold-matching tasks; deliberate errors stay queued
6. engine: screen persona Maria's household (HH 3, $49,700, $9,400 ER bill, statement 2026-10-10)
7. packet: extract the synthetic bill and pay stubs, fill the hospital's form, read it back
8. evals: score the registry this run built, plus the engine and packet suites
9. export the CC BY 4.0 snapshot (CSV + Parquet) and 10. build the static site

Every intermediate artifact goes under ``workdir`` (default ``data/demo-run/``, gitignored);
the site goes to ``site/dist/`` unless ``site_dir`` says otherwise.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from charitycare.config import get_settings
from charitycare.schemas import Household
from charitycare.store import Registry

DEMO_TODAY = date(2026, 10, 24)
MARIA_FACILITY = "tx-459901"  # Example Methodist Hospital, Bexar County (fictional)
MARIA_APPLICANT = {"patient_name": "Maria Example (synthetic)", "address": "123 Example Lane", "city": "San Antonio",
                   "zip": "78229", "phone": "(210) 555-0142"}


def maria_household(statement_date: date = date(2026, 10, 10)) -> Household:
    """Persona A's household (design section 3): family of 3, $49,700, uninsured, Bexar County, ER visit."""
    return Household(household_size=3, annual_income=Decimal("49700"), insured=False, county="Bexar", state="TX",
                     emergency_care=True, out_of_pocket_medical_12m=Decimal("9400"), service_date=date(2026, 9, 28),
                     first_statement_date=statement_date, application_date=DEMO_TODAY)


@dataclass
class DemoReport:
    """Per-stage summaries and output paths."""

    workdir: Path
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    outputs: dict[str, Path] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)

    def ok(self) -> bool:
        return all(p.exists() for p in self.outputs.values())


Step = Callable[[], dict[str, Any]]


def run_demo(workdir: Path | None = None, *, site_dir: Path | None = None, run_evals: bool = True,
             echo: Callable[[str], None] = print) -> DemoReport:
    """Run every stage; ``echo`` receives one progress line per stage."""
    s = get_settings()
    work = Path(workdir) if workdir else s.data_dir / "demo-run"
    _reset_workdir(work)
    corpus = work / "corpus"
    site_out = Path(site_dir) if site_dir else s.home / "site" / "dist"
    rep = DemoReport(workdir=work)
    reg = Registry(work / "registry.sqlite")

    def stage(name: str, fn: Step) -> dict[str, Any]:
        t0 = time.monotonic()
        out = fn()
        rep.timings[name] = round(time.monotonic() - t0, 2)
        rep.stages[name] = out
        echo(f"[{len(rep.stages):>2}] {name:<9} {_one_line(out)}  ({rep.timings[name]}s)")
        return out

    try:
        stage("corpus", lambda: _corpus(corpus, work / "corpus-fixtures", s.demo_dir))
        stage("seed", lambda: _seed(reg, corpus))
        stage("discover", lambda: _discover(reg, corpus, work))
        stage("compile", lambda: _compile(reg))
        stage("review", lambda: _review(reg, corpus))
        det = None

        def _screen_stage() -> dict[str, Any]:
            nonlocal det
            out, det = _screen(reg, work)
            return out

        stage("screen", _screen_stage)
        stage("packet", lambda: _packet(reg, det, work))
        if run_evals:
            stage("evals", lambda: _evals(reg, work))
        stage("export", lambda: _export(reg, work))
        stage("site", lambda: _site(reg, site_out, work, run_evals))
    finally:
        reg.close()
    rep.outputs = {
        "registry": work / "registry.sqlite",
        "packet_pdf": work / "packet" / "packet.pdf",
        "deadline_ics": work / "packet" / "deadline.ics",
        "parquet": work / "release" / "fields.parquet",
        "csv": work / "release" / "fields.csv",
        "site_index": site_out / "index.html",
    }
    if run_evals:
        rep.outputs["eval_report"] = work / "evals" / "report" / "latest.md"
    return rep


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _corpus(out: Path, fixtures: Path, committed: Path) -> dict[str, Any]:
    from charitycare.demo.build import build_corpus

    m = build_corpus(out, fixtures)
    want = json.loads((committed / "MANIFEST.json").read_text(encoding="utf-8")).get("checksum")
    return {"hospitals": len(m["hospitals"]), "files": m["files"], "checksum": m["checksum"][:16],
            "matches_committed": m["checksum"] == want}


def _seed(reg: Registry, corpus: Path) -> dict[str, Any]:
    from charitycare.seed.pipeline import seed_demo

    r = seed_demo(reg, corpus).as_dict()
    cov = r.get("coverage", r)
    return {"facilities": cov.get("facilities_total"), "schedule_h_coverage": cov.get("cms_schedule_h_coverage"),
            "needs_review": len(cov.get("needs_review") or [])}


def _discover(reg: Registry, corpus: Path, work: Path) -> dict[str, Any]:
    from charitycare.discover.pipeline import discover_demo

    r = discover_demo(reg, corpus, store_root=work / "docstore", queue_path=work / "records_requests.json").summary()
    return {"policies_found": f"{r['policies_found']}/{r['targets']}", "policy_versions": r["policy_versions"],
            "documents": reg.count(_model("DocumentVersion")), "agent_tool_calls": r["tool_calls"],
            "methods": r["methods"], "records_requests": r["records_requests"], "llm_cost_usd_simulated": r["llm_cost_usd"]}


def _compile(reg: Registry) -> dict[str, Any]:
    from collections import Counter

    from charitycare.compile.pipeline import compile_facility
    from charitycare.llm import get_backend

    backend = get_backend("replay")
    status: Counter[str] = Counter()
    checks: Counter[str] = Counter()
    reasked, forced, round_trips, adjudications = [], [], [], []
    cost = 0.0
    for fac in reg.list_facilities(state=None):
        r = compile_facility(reg, fac.id, backend=backend).summary()
        cost += r["cost_usd"]
        if r.get("cached"):
            continue
        status.update(r["status"])
        for v in r["crosschecks"].values():
            checks.update(v)
        reasked += [f"{fac.id}:{k}" for k in r["reasked"]]
        forced += [f"{fac.id}:{k}" for k in r["forced_abstentions"]]
        round_trips += [f"{fac.id}:{x}" for x in r["round_trips"]]
        adjudications += r["adjudications"]
    return {"fields": dict(status), "crosschecks": dict(checks), "reasked": reasked, "quote_gate_abstained": forced,
            "round_trips": round_trips, "adjudications": adjudications, "cost_usd_simulated": round(cost, 4),
            "open_review_tasks": len(reg.open_review_tasks())}


def _review(reg: Registry, corpus: Path) -> dict[str, Any]:
    from charitycare.demo.reviewer import auto_review, verify_templates

    summary = auto_review(reg, corpus, demo=True)
    summary.templates_verified, summary.templates_open = verify_templates(reg, corpus, demo=True)
    out = summary.as_dict()
    out["queue_after"] = len(reg.open_review_tasks())
    return out


def _screen(reg: Registry, work: Path) -> tuple[dict[str, Any], Any]:
    from charitycare.engine.render import render_text
    from charitycare.engine.screen import screen

    det = screen(MARIA_FACILITY, maria_household(), registry=reg, today=DEMO_TODAY)
    pv = reg.get_policy_version(det.policy_version_id)
    text = render_text(det, policy_as_of=pv.as_of if pv else None)
    (work / "screen").mkdir(exist_ok=True)
    (work / "screen" / "determination.txt").write_text(text + "\n", encoding="utf-8")
    return {"facility": MARIA_FACILITY, "outcome": det.outcome, "arithmetic": det.arithmetic,
            "federal_deadline": str(det.federal_deadline), "provisional_fields_used": det.provisional_fields_used}, det


def _packet(reg: Registry, det: Any, work: Path) -> dict[str, Any]:
    from charitycare.api.navigator import build_packet
    from charitycare.packet import annualize_income, extract_bill, extract_pay_stub
    from charitycare.packet.synthetic import demo_documents

    docs = demo_documents()
    bill = extract_bill(docs[0].image)
    stubs = [extract_pay_stub(d.image) for d in docs[1:]]
    pending = {k: v for k, v in {"bill": bill.needs_confirmation(),
                                 **{f"stub{i}": s.needs_confirmation() for i, s in enumerate(stubs, 1)}}.items() if v}
    # Synthetic demo documents only: these fields are accepted unconfirmed and reported in the stage output.
    # A real run goes through the navigator page, which blocks until each one is confirmed or edited.
    income = annualize_income([x.fields for x in stubs])
    stmt = bill.fields.statement_date.value
    hh = maria_household(stmt) if stmt else maria_household()
    res = build_packet(reg, MARIA_FACILITY, hh, MARIA_APPLICANT, det, DEMO_TODAY)
    out = work / "packet"
    out.mkdir(exist_ok=True)
    (out / "packet.pdf").write_bytes(res.pdf)
    (out / "deadline.ics").write_bytes(res.ics)
    filled = [f for f in res.readback.fields if not f.write_by_hand]
    return {"bill_amount": bill.fields.amount_due.value, "statement_date": str(stmt),
            "unconfirmed_fields_accepted_for_demo": pending,
            "income_from_stubs": income.amount, "readback": f"{sum(f.match for f in filled)}/{len(filled)} fields match",
            "verified": res.verified, "pages": res.page_count, "write_by_hand": res.write_by_hand}


def _evals(reg: Registry, work: Path) -> dict[str, Any]:
    from evals.report import latest_runs, write_report
    from evals.runner import run_suite
    from evals.suites import load_suite

    runs_dir, report_dir = work / "evals" / "runs", work / "evals" / "report"
    out: dict[str, Any] = {}
    misses: list[str] = []
    for name in ("full", "engine", "packet"):
        # "full" scores the registry this run built (seed -> discover -> compile -> review), not a fresh compile.
        r = run_suite(load_suite(name), mode="replay", registry_path=reg.path if name == "full" else None,
                      db_path=reg.path, out_dir=runs_dir)
        hard = [m.name for m in r.metrics.values() if m.hard and m.met is False]
        misses += [f"{name}:{m.name}" for m in r.metrics.values() if m.target and m.met is False]
        out[name] = "ok" if not hard else f"HARD MISS {hard}"
    write_report(latest_runs(runs_dir), report_dir)
    out["targets_missed"] = misses
    return out


def _export(reg: Registry, work: Path) -> dict[str, Any]:
    from charitycare.export.snapshot import export_release

    m = export_release(reg, work / "release", version="demo", today=DEMO_TODAY)
    return {"rows": m.rows, "out": str(m.out_dir)}


def _site(reg: Registry, out: Path, work: Path, with_evals: bool) -> dict[str, Any]:
    from charitycare.site.build import build_site

    evals = [work / "evals" / "runs"] if with_evals else []
    r = build_site(reg, out, today=DEMO_TODAY, eval_paths=evals, release_dir=work / "release")
    return {"pages": len(r.pages), "data_files": len(r.data_files), "out": str(r.out_dir)}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


MARKER = ".ccc-demo-run"


def _reset_workdir(work: Path) -> None:
    """Empty ``work`` for a fresh run, refusing to delete a non-empty directory a demo run did not create."""
    if work.exists() and any(work.iterdir()):
        if not (work / MARKER).is_file():
            raise FileExistsError(f"{work} is not empty and was not created by `ccc demo`; choose another --workdir")
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / MARKER).write_text("created by ccc demo; safe to delete\n", encoding="utf-8")


def _model(name: str) -> Any:
    from charitycare import schemas

    return getattr(schemas, name)


def _one_line(d: dict[str, Any], limit: int = 150) -> str:
    text = ", ".join(f"{k}={v}" for k, v in d.items() if not isinstance(v, (dict, list)) or v)
    return text if len(text) <= limit else text[: limit - 3] + "..."

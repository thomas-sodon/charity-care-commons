"""``ccc discover`` subcommands (also runnable as ``python -m charitycare.discover``)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


def configure(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="discover_cmd", required=True)
    d = sub.add_parser("demo", help="discover the synthetic corpus offline (seeds the registry first if empty)")
    d.add_argument("--db", type=Path, default=None)
    d.add_argument("--store", type=Path, default=None, help="document store root (default data/cache/docstore)")
    d.add_argument("--no-agent", action="store_true", help="heuristic crawler only")
    r = sub.add_parser("run", help="discover live hospital sites for seeded facilities (polite, robots-aware)")
    r.add_argument("--db", type=Path, default=None)
    r.add_argument("--store", type=Path, default=None)
    r.add_argument("--facility", action="append", default=None, help="limit to facility id (repeatable)")
    r.add_argument("--interval", type=float, default=None, help="seconds between requests per host (default 5)")
    r.add_argument("--no-agent", action="store_true")
    lh = sub.add_parser("link-health", help="re-check every known source URL")
    lh.add_argument("--db", type=Path, default=None)
    lh.add_argument("--demo", action="store_true", help="check against the offline demo web")
    lh.add_argument("--markdown", action="store_true")
    q = sub.add_parser("records", help="show the records-request queue (and the batched request letter)")
    q.add_argument("--letter", action="store_true")


def run(a: argparse.Namespace) -> int:
    from charitycare.discover.docstore import DocumentStore
    from charitycare.discover.fetcher import DomainAllowlist, PoliteFetcher
    from charitycare.discover.pipeline import corpus_fetcher, discover_demo, run_discovery, targets_from_registry
    from charitycare.discover.records import RecordsRequestQueue, render_batch_request
    from charitycare.store import Registry

    if a.discover_cmd == "records":
        q = RecordsRequestQueue()
        if a.letter:
            print(render_batch_request(q.pending()))
        else:
            print(json.dumps([r.__dict__ for r in q.all()], indent=2))
        return 0
    with Registry(a.db) as reg:
        if a.discover_cmd == "demo":
            rep = discover_demo(reg, store_root=a.store, use_agent=not a.no_agent)
            print(json.dumps(rep.summary(), indent=2, default=str))
        elif a.discover_cmd == "run":
            store = DocumentStore(a.store, reg)
            with PoliteFetcher(DomainAllowlist(), min_interval=a.interval) as f:
                rep = run_discovery(reg, f, store, targets=targets_from_registry(reg, a.facility), use_agent=not a.no_agent)
            print(json.dumps(rep.summary(), indent=2, default=str))
        else:
            from charitycare.discover.linkhealth import check_sources, summarize, to_markdown

            fetcher = corpus_fetcher() if a.demo else PoliteFetcher(DomainAllowlist())
            with fetcher:
                rows = check_sources(reg, fetcher, store=DocumentStore(None, reg))
            print(to_markdown(rows) if a.markdown else json.dumps(summarize(rows), indent=2))
    return 0


CLI_COMMAND = ("discover", "policy discovery agent, document store, link health (S2-S3)", configure, run)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m charitycare.discover")
    configure(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

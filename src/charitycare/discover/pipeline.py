"""Discovery stage orchestration: registry seeds -> targets -> agent/heuristic -> registry rows.

One target per organization (EIN): a system policy is usually shared by its
facilities, and the facility -> policy mapping step (``policy.map_facility``)
then decides per facility. Facilities without a usable seed or a reachable
policy go to the records-request queue.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from charitycare.config import get_settings
from charitycare.discover.agent import DEFAULT_BUDGET, Target, discover_target
from charitycare.discover.docstore import DocumentStore
from charitycare.discover.fetcher import DomainAllowlist, PoliteFetcher
from charitycare.discover.models import DiscoveryResult
from charitycare.discover.policy import ingest
from charitycare.discover.records import RecordsRequestQueue
from charitycare.discover.transports import CorpusTransport
from charitycare.llm import LLMBackend
from charitycare.schemas import Facility, Source, SourceKind
from charitycare.store import Registry

SEED_VIAS = ("schedule_h_16a", "schedule_h_16b", "schedule_h_16c", "schedule_h_narrative", "external_list", "manual")


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}/"


def targets_from_registry(registry: Registry, facility_ids: list[str] | None = None) -> list[Target]:
    """Group in-scope facilities by EIN and attach their seed URLs."""
    facs = [f for f in registry.list_facilities(in_scope=True) if facility_ids is None or f.id in facility_ids]
    sources = [s for s in registry.select(Source) if (s.discovered_via or "") in SEED_VIAS]
    groups: dict[str, list[Facility]] = defaultdict(list)
    for f in facs:
        groups[f"ein:{f.ein}" if f.ein else f"facility:{f.id}"].append(f)
    targets: list[Target] = []
    for tid, members in sorted(groups.items()):
        ids = {f.id for f in members}
        seeds: list[tuple[str, SourceKind, str]] = sorted(
            {(s.url, s.kind, s.discovered_via or "manual") for s in sources if ids & set(s.facility_ids)}
        )
        homepages = sorted({_origin(u) for u, _, _ in seeds})
        members = sorted(members, key=lambda f: f.id)
        targets.append(Target(tid, members[0].ein, [f.id for f in members], [f.name for f in members], seeds, homepages))
    return targets


@dataclass
class DiscoveryReport:
    results: list[DiscoveryResult] = field(default_factory=list)
    policy_versions: list[str] = field(default_factory=list)
    records_requests: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "targets": len(self.results),
            "policies_found": sum(r.reachable for r in self.results),
            "policy_versions": self.policy_versions,
            "tool_calls": sum(r.tool_calls for r in self.results),
            "max_tool_calls": max((r.tool_calls for r in self.results), default=0),
            "methods": sorted({r.method for r in self.results}),
            "blocked_urls": sorted({u for r in self.results for u in r.blocked_urls}),
            "records_requests": self.records_requests,
            "by_target": {r.target_id: {"status": r.status_label, "documents": sorted(f"{c.kind}:{c.language}" for c in r.candidates if c.sha256), "tool_calls": r.tool_calls, "method": r.method} for r in self.results},
            "llm_cost_usd": round(sum(r.llm_cost_usd for r in self.results), 6),
        }


def run_discovery(
    registry: Registry,
    fetcher: PoliteFetcher,
    store: DocumentStore,
    *,
    targets: list[Target] | None = None,
    queue: RecordsRequestQueue | None = None,
    budget: int = DEFAULT_BUDGET,
    use_agent: bool = True,
    backend: LLMBackend | None = None,
    use_llm_classifier: bool = True,
) -> DiscoveryReport:
    """Discover, ingest, and queue records requests for unreachable facilities."""
    report = DiscoveryReport()
    queue = queue or RecordsRequestQueue()
    for t in targets if targets is not None else targets_from_registry(registry):
        if not t.seeds and not t.homepages:
            for fid, name in zip(t.facility_ids, t.facility_names, strict=True):
                queue.enqueue(fid, name, t.ein, "not_found", "no seed URL (no Schedule H line 16 URL or website)")
                report.records_requests.append(fid)
            report.results.append(DiscoveryResult(target_id=t.target_id, ein=t.ein, facility_ids=t.facility_ids, notes=["no seed URL"]))
            continue
        res = discover_target(t, fetcher, store, registry, budget=budget, use_agent=use_agent, backend=backend, use_llm_classifier=use_llm_classifier)
        pv = ingest(res, registry, store)
        if pv is not None:
            report.policy_versions.append(pv.id)
        report.results.append(res)
        for fid, name in zip(t.facility_ids, t.facility_names, strict=True):
            if res.reachable:
                queue.resolve(fid)
                continue
            reason = "archived_only" if pv is not None else ("blocked" if res.blocked_urls else ("dead_link" if res.dead_urls else "not_found"))
            queue.enqueue(fid, name, t.ein, reason, res.status_label)  # type: ignore[arg-type]
            report.records_requests.append(fid)
    return report


def corpus_fetcher(corpus_root: Path | None = None, *, min_interval: float = 0.0) -> PoliteFetcher:
    """A fetcher wired to the offline demo web (no network)."""
    root = corpus_root or get_settings().demo_dir
    return PoliteFetcher(DomainAllowlist(), min_interval=min_interval, transport=CorpusTransport(root))


def discover_demo(
    registry: Registry,
    corpus_root: Path | None = None,
    *,
    store_root: Path | None = None,
    queue_path: Path | None = None,
    use_agent: bool = True,
    backend: LLMBackend | None = None,
) -> DiscoveryReport:
    """Seed (if needed) and discover the synthetic corpus end to end, offline."""
    from charitycare.seed.pipeline import seed_demo

    root = corpus_root or get_settings().demo_dir
    if registry.count(Facility) == 0:
        seed_demo(registry, root)
    store = DocumentStore(store_root, registry)
    with corpus_fetcher(root) as fetcher:
        return run_discovery(registry, fetcher, store, queue=RecordsRequestQueue(queue_path), use_agent=use_agent, backend=backend)

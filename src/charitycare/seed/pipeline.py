"""Seed stage orchestration: CMS + Schedule H (+ external list) -> registry rows and URL seeds.

Writes ``Organization``, ``Facility``, ``ScheduleHFacility`` and ``Source`` rows
(lines 16a-c and narrative-recovered URLs, ``discovered_via`` =
``schedule_h_16a|16b|16c|narrative`` or ``external_list``) and opens a
``facility_mapping`` review task for every low-confidence match.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from charitycare.config import get_settings
from charitycare.schemas import ReviewTask, Source, SourceKind, utcnow
from charitycare.seed.cms import CMSLoad, load_cms
from charitycare.seed.irs_batch import iter_index, iter_schedule_h
from charitycare.seed.schedule_h import ParsedReturn, parse_file, url_seeds
from charitycare.seed.universe import Candidate, Universe, read_external_list, reconcile
from charitycare.store import Registry


def source_id_for(url: str) -> str:
    """Stable ``Source.id`` for a URL."""
    return "src-" + hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]


@dataclass
class SeedReport:
    cms_origin: str
    cms_in_scope: int
    cms_excluded: dict[str, int]
    returns: int
    sections: int
    sources: int
    review_tasks: int
    coverage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def section_facility_ids(u: Universe, sec_object_id: str, facility_numbers: Iterable[int]) -> list[str]:
    refs = {f"{sec_object_id}:{n}" for n in facility_numbers}
    return sorted({m.facility_id for m in u.matches if m.source == "schedule_h" and m.ref in refs})


def persist(reg: Registry, u: Universe, returns: list[ParsedReturn]) -> tuple[int, int]:
    """Write the universe to the registry. Returns (sources written, review tasks opened)."""
    now = utcnow()
    sources: dict[str, Source] = {}
    tasks = 0
    with reg.transaction():
        for ret in returns:
            reg.upsert_organization(ret.organization)
        for f in u.facilities.values():
            reg.upsert_facility(f)
        for sec in u.sections:
            reg.add_schedule_h(sec)
            fids = section_facility_ids(u, sec.object_id, sec.facility_numbers)
            for url, kind, via in url_seeds(sec):
                _add_source(sources, url, kind, via, fids, now)  # type: ignore[arg-type]
        for fid, urls in u.external_urls.items():
            for url in urls:
                _add_source(sources, url, "other", "external_list", [fid], now)
        for s in sources.values():
            existing = reg.get(Source, id=s.id)
            if existing is not None:  # keep crawl history; merge facility ids
                s = existing.model_copy(update={"facility_ids": sorted(set(existing.facility_ids) | set(s.facility_ids))})
            reg.upsert_source(s)
        for m in u.matches:
            if m.needs_review:
                reg.add_review_task(
                    ReviewTask(
                        id=f"fmap-{m.source}-{m.ref}",
                        kind="facility_mapping",
                        facility_id=m.facility_id,
                        priority="medium",
                        notes=f"{m.source} {m.ref} matched to {m.matched_name!r} by {m.method} (confidence {m.confidence:.2f}); confirm.",
                    )
                )
                tasks += 1
    return len(sources), tasks


def _add_source(acc: dict[str, Source], url: str, kind: SourceKind, via: str, fids: list[str], now: Any) -> None:
    sid = source_id_for(url)
    if sid in acc:
        acc[sid] = acc[sid].model_copy(update={"facility_ids": sorted(set(acc[sid].facility_ids) | set(fids))})
    else:
        acc[sid] = Source(id=sid, url=url, kind=kind, discovered_via=via, first_seen=now, facility_ids=sorted(fids))


def run_seed(
    reg: Registry,
    cms: CMSLoad,
    returns: Iterable[ParsedReturn],
    external: Iterable[Candidate] = (),
) -> SeedReport:
    """Reconcile and persist; returns a report with the coverage numbers."""
    rets = [r for r in returns if (r.state or "TX") == "TX"]
    u = reconcile(cms.facilities, rets, external)
    n_sources, n_tasks = persist(reg, u, rets)
    return SeedReport(
        cms_origin=cms.origin,
        cms_in_scope=len(cms.rows),
        cms_excluded=cms.excluded,
        returns=len(rets),
        sections=len(u.sections),
        sources=n_sources,
        review_tasks=n_tasks,
        coverage=u.coverage(),
    )


def demo_returns(demo_root: Path | None = None) -> list[ParsedReturn]:
    """Schedule H returns of the demo corpus, read the production way (index CSV + streamed ZIP)."""
    root = demo_root or get_settings().demo_dir
    index = list(iter_index(root / "irs" / "index_2026_demo.csv"))
    return list(iter_schedule_h(root / "irs" / "2026_TEOS_XML_DEMO.zip", index=index))


def seed_demo(reg: Registry, demo_root: Path | None = None, external_csv: Path | None = None) -> SeedReport:
    """Seed the registry from the synthetic corpus (no network)."""
    root = demo_root or get_settings().demo_dir
    cms = load_cms(csv_path=root / "cms" / "Hospital_General_Information.csv", cache_path=root / "_no_cache.csv")
    ext = read_external_list(external_csv) if external_csv else []
    return run_seed(reg, cms, demo_returns(root), ext)


def seed_from_paths(
    reg: Registry,
    *,
    live_cms: bool = False,
    cms_csv: Path | None = None,
    xml_files: Iterable[Path] = (),
    batch_zip: Path | None = None,
    index_csv: Path | None = None,
    external_csv: Path | None = None,
) -> SeedReport:
    """General entry point: any mix of loose XML files and an IRS batch ZIP (optionally index-filtered)."""
    cms = load_cms(live=live_cms, csv_path=cms_csv)
    rets = [parse_file(str(p)) for p in xml_files]
    if batch_zip:
        index = list(iter_index(index_csv)) if index_csv else None
        rets.extend(iter_schedule_h(batch_zip, index=index))
    ext = read_external_list(external_csv) if external_csv else []
    return run_seed(reg, cms, rets, ext)

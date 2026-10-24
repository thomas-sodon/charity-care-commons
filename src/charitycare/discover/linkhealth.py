"""Link-health report: re-check every known policy URL politely and record its state.

Each ``Source`` is re-fetched through the :class:`PoliteFetcher` (so robots,
the allowlist and the rate limit still apply). The registry row gets
``last_checked``, ``http_status`` and ``blocked``; the report says whether the
document changed (new sha256 = new version for the change feed). Wording
follows section 11: "policy not currently reachable", never accusatory.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from charitycare.discover.docstore import DocumentStore
from charitycare.discover.fetcher import PoliteFetcher, registrable_domain
from charitycare.pdftext import sha256_bytes
from charitycare.schemas import DocumentVersion, Source, utcnow
from charitycare.store import Registry
from charitycare.wording import assert_safe_wording

LABELS = {
    "ok": "reachable",
    "changed": "reachable (document changed: new version stored)",
    "blocked": "policy not currently reachable (automated access blocked by the site)",
    "dead": "policy not currently reachable (link no longer works)",
    "robots_disallowed": "not checked (the site's robots.txt asks crawlers not to fetch it)",
    "domain_not_allowed": "not checked (domain not on the allowlist)",
    "http_error": "policy not currently reachable (server error)",
    "error": "policy not currently reachable (connection error)",
}


@dataclass(frozen=True)
class LinkHealth:
    source_id: str
    url: str
    kind: str
    status: str
    http_status: int | None
    final_url: str | None
    sha256: str | None
    previous_sha256: str | None
    checked_at: str

    @property
    def label(self) -> str:
        return LABELS.get(self.status, self.status)


def check_sources(
    registry: Registry,
    fetcher: PoliteFetcher,
    sources: Iterable[Source] | None = None,
    *,
    store: DocumentStore | None = None,
) -> list[LinkHealth]:
    """Re-check sources (default: all) and update their registry rows."""
    rows: list[LinkHealth] = []
    for src in sorted(sources if sources is not None else registry.select(Source), key=lambda s: s.url):
        fetcher.allowlist.add(registrable_domain(src.url))
        res = fetcher.fetch(src.url)
        prev = registry.select(DocumentVersion, source_id=src.id, order_by="-fetched_at", limit=1)
        prev_sha = prev[0].sha256 if prev else None
        sha = sha256_bytes(res.content) if res.ok and res.content else None
        status = res.outcome
        if status == "ok" and prev_sha and sha != prev_sha:
            status = "changed"
            if store is not None:
                store.put(res.content, source_id=src.id, kind=src.kind, mime=res.content_type, fetched_at=res.fetched_at)
        now = utcnow()
        registry.upsert_source(src.model_copy(update={"last_checked": now, "http_status": res.status, "blocked": res.blocked}))
        rows.append(LinkHealth(src.id, src.url, src.kind, status, res.status, res.final_url if res.redirects else None, sha, prev_sha, now.isoformat()))
    return rows


def summarize(rows: list[LinkHealth]) -> dict[str, object]:
    counts = Counter(r.status for r in rows)
    reachable = counts["ok"] + counts["changed"]
    return {
        "checked": len(rows),
        "reachable": reachable,
        "reachable_share": round(reachable / len(rows), 4) if rows else 0.0,
        "by_status": dict(sorted(counts.items())),
        "rows": [asdict(r) | {"label": r.label} for r in rows],
    }


def to_markdown(rows: list[LinkHealth]) -> str:
    """Human-readable link-health table (passes the wording guard)."""
    s = summarize(rows)
    out = [
        "# Link health",
        "",
        f"{s['reachable']} of {s['checked']} policy links reachable.",
        "",
        "| URL | Kind | Status | HTTP |",
        "|---|---|---|---|",
    ]
    out += [f"| {r.url} | {r.kind} | {r.label} | {r.http_status if r.http_status is not None else '-'} |" for r in rows]
    text = "\n".join(out) + "\n"
    assert_safe_wording(text)
    return text

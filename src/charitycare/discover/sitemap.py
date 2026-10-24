"""Sitemap lookup: robots.txt ``Sitemap:`` directives, then ``/sitemap.xml``; index files recurse."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from lxml import etree

from charitycare.discover.fetcher import PoliteFetcher
from charitycare.discover.links import score_link


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: str | None
    score: float


def parse_sitemap(xml: bytes) -> tuple[list[tuple[str, str | None]], list[str]]:
    """(page entries [(loc, lastmod)], child sitemap URLs) of a urlset or sitemapindex."""
    try:
        root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return [], []
    local = etree.QName(root).localname
    pages: list[tuple[str, str | None]] = []
    children: list[str] = []
    for el in root:
        if not isinstance(el.tag, str):
            continue
        loc = next((c.text.strip() for c in el if isinstance(c.tag, str) and etree.QName(c).localname == "loc" and c.text), None)
        lastmod = next((c.text.strip() for c in el if isinstance(c.tag, str) and etree.QName(c).localname == "lastmod" and c.text), None)
        if not loc:
            continue
        (children.append(loc) if local == "sitemapindex" else pages.append((loc, lastmod)))
    return pages, children


def sitemap_lookup(fetcher: PoliteFetcher, site_url: str, *, max_sitemaps: int = 4, min_score: float = 1.0) -> list[SitemapEntry]:
    """Financial-assistance-looking URLs from a site's sitemaps, best first."""
    p = urlsplit(site_url)
    origin = f"{p.scheme}://{p.netloc}"
    queue = fetcher.sitemaps_for(origin + "/") or [origin + "/sitemap.xml"]
    seen: set[str] = set()
    out: dict[str, SitemapEntry] = {}
    while queue and len(seen) < max_sitemaps:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        res = fetcher.fetch(sm)
        if not res.ok:
            continue
        pages, children = parse_sitemap(res.content)
        queue.extend(c for c in children if c not in seen)
        for loc, lastmod in pages:
            s = score_link("", loc)
            if s >= min_score:
                out[loc] = SitemapEntry(loc, lastmod, s)
    return sorted(out.values(), key=lambda e: (-e.score, e.url))

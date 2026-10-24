"""Internet Archive Wayback lookup (CDX API) for dead or blocked policy links.

Archived copies are always **provisional**: they are labelled with the capture
date and never replace a live copy until a live fetch confirms it (section 9).
The raw archived bytes are fetched through the ``id_`` URL form so the
document hash matches the original file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from charitycare.discover.fetcher import FetchResult, PoliteFetcher

CDX_URL = "https://web.archive.org/cdx/search/cdx"


@dataclass(frozen=True)
class WaybackCapture:
    timestamp: str  # YYYYMMDDhhmmss
    original: str
    status: str | None
    mime: str | None
    digest: str | None

    provisional: bool = True

    @property
    def captured_at(self) -> datetime:
        return datetime.strptime(self.timestamp[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)

    @property
    def archived_url(self) -> str:
        """Raw-bytes replay URL (``id_`` suppresses the Wayback toolbar rewrite)."""
        return f"https://web.archive.org/web/{self.timestamp}id_/{self.original}"

    @property
    def label(self) -> str:
        return f"Archived copy captured {self.captured_at.date().isoformat()} (provisional until a live copy is confirmed)"


def cdx_query_url(url: str, limit: int = 5) -> str:
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": "statuscode:200",
        "collapse": "digest",
        "limit": str(-abs(limit)),  # negative limit = most recent N captures
    }
    return f"{CDX_URL}?{urlencode(params)}"


def parse_cdx(body: bytes) -> list[WaybackCapture]:
    """CDX JSON (header row + rows) -> captures, newest first."""
    try:
        rows = json.loads(body or b"[]")
    except ValueError:
        return []
    if not rows or not isinstance(rows, list):
        return []
    header, data = rows[0], rows[1:]
    idx = {k: i for i, k in enumerate(header)}

    def col(r: list[str], k: str) -> str | None:
        return r[idx[k]] if k in idx and idx[k] < len(r) else None

    caps = [
        WaybackCapture(col(r, "timestamp") or "", col(r, "original") or "", col(r, "statuscode"), col(r, "mimetype"), col(r, "digest"))
        for r in data
        if col(r, "timestamp")
    ]
    return sorted(caps, key=lambda c: c.timestamp, reverse=True)


def wayback_lookup(fetcher: PoliteFetcher, url: str, limit: int = 5) -> list[WaybackCapture]:
    """Recent successful captures of ``url`` (empty on any CDX failure)."""
    res = fetcher.fetch(cdx_query_url(url, limit))
    return parse_cdx(res.content) if res.ok else []


def fetch_archived(fetcher: PoliteFetcher, capture: WaybackCapture) -> FetchResult:
    return fetcher.fetch(capture.archived_url)

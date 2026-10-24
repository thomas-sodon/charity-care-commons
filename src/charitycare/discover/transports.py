"""httpx transports that let the discovery stage run against the synthetic corpus.

* :class:`CorpusTransport` serves ``data/demo/web/url_map.json`` straight from
  disk (no sockets): the offline demo and CLI use it.
* :class:`MirrorTransport` sends every request to one local base URL (e.g. an
  ``http.server`` on 127.0.0.1) while preserving the logical URL in the
  ``X-Original-Host`` / ``X-Original-Scheme`` headers, so tests exercise real
  HTTP without touching any hospital site.

Both are test/demo plumbing: the politeness rules in :mod:`fetcher` apply on top unchanged.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def load_url_map(corpus_root: Path) -> dict[str, Path]:
    raw = json.loads((corpus_root / "web" / "url_map.json").read_text(encoding="utf-8"))
    return {url: corpus_root / rel for url, rel in raw.items()}


def content_type_for(path: Path) -> str:
    if path.suffix == ".pdf":
        return "application/pdf"
    if path.suffix in (".html", ".htm") or path.name == "index.html":
        return "text/html; charset=utf-8"
    if path.name == "robots.txt" or path.suffix == ".txt":
        return "text/plain; charset=utf-8"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def lookup(url_map: dict[str, Path], url: str) -> Path | None:
    """Resolve a URL against the map, tolerating a missing trailing slash and query strings."""
    base = url.split("#")[0].split("?")[0]
    for cand in (base, base + "/" if not base.endswith("/") else base.rstrip("/")):
        if cand in url_map:
            return url_map[cand]
    p = urlsplit(base)
    if p.path == "":
        return url_map.get(f"{p.scheme}://{p.netloc}/")
    return None


class CorpusTransport(httpx.BaseTransport):
    """Serve the demo corpus' offline web; unknown URLs are 404 (robots.txt included)."""

    def __init__(self, corpus_root: Path, extra: dict[str, tuple[int, str, bytes]] | None = None):
        self.url_map = load_url_map(Path(corpus_root))
        self.extra = extra or {}  # url -> (status, content_type, body) overrides
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.extra:
            status, ctype, body = self.extra[url]
            return httpx.Response(status, headers={"content-type": ctype}, content=body, request=request)
        path = lookup(self.url_map, url)
        if path is None or not path.exists():
            return httpx.Response(404, headers={"content-type": "text/plain"}, content=b"not found", request=request)
        return httpx.Response(200, headers={"content-type": content_type_for(path)}, content=path.read_bytes(), request=request)


class MirrorTransport(httpx.BaseTransport):
    """Route every request to ``base_url`` (scheme+host+port), keeping the logical URL in headers."""

    def __init__(self, base_url: str, inner: httpx.BaseTransport | None = None):
        self.base = urlsplit(base_url)
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        orig = request.url
        target = orig.copy_with(scheme=self.base.scheme, host=self.base.hostname, port=self.base.port)
        headers = dict(request.headers)
        headers["X-Original-Host"] = orig.host
        headers["X-Original-Scheme"] = orig.scheme
        headers.pop("host", None)
        fwd = httpx.Request(request.method, target, headers=headers, content=request.content)
        resp = self.inner.handle_request(fwd)
        resp.request = request
        return resp

    def close(self) -> None:
        self.inner.close()

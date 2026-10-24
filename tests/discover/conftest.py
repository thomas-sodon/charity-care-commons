"""Local HTTP server serving the demo corpus' offline web plus edge-case hosts.

Requests reach it through ``MirrorTransport``; the logical host arrives in
``X-Original-Host``. No test touches a real hospital site.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from charitycare.discover.fetcher import DomainAllowlist, PoliteFetcher
from charitycare.discover.transports import MirrorTransport, content_type_for, load_url_map, lookup

CHALLENGE = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'></div><script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script></body></html>"
BLOCK_403 = b"<html><head><title>Attention Required! | Cloudflare</title></head><body>Sorry, you have been blocked</body></html>"


@dataclass
class LocalWeb:
    base_url: str
    corpus_root: Path
    log: list[dict[str, str]] = field(default_factory=list)
    wayback_rows: dict[str, list[list[str]]] = field(default_factory=dict)  # original url -> cdx rows
    overrides: dict[str, tuple[int, str, bytes]] = field(default_factory=dict)  # logical url -> response

    def fetcher(self, *allow: str, min_interval: float = 0.0, **kw) -> PoliteFetcher:
        return PoliteFetcher(DomainAllowlist(allow), min_interval=min_interval, transport=MirrorTransport(self.base_url), **kw)

    def requested(self, url_part: str) -> int:
        return sum(1 for r in self.log if url_part in r["url"])


def _make_handler(web: LocalWeb, url_map: dict[str, Path], fap_pdf: bytes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # silence
            pass

        def _send(self, status: int, ctype: str, body: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            host = self.headers.get("X-Original-Host") or self.headers.get("Host", "")
            scheme = self.headers.get("X-Original-Scheme", "https")
            url = f"{scheme}://{host}{self.path}"
            web.log.append({"url": url, "ua": self.headers.get("User-Agent", ""), "from": self.headers.get("From", "")})
            path = self.path.split("?")[0]
            if url in web.overrides:
                return self._send(*web.overrides[url])
            if path == "/robots.txt" and host in ("blocked.example", "challenge.example", "dead.example", "redirect.example"):
                return self._send(200, "text/plain", b"User-agent: *\nAllow: /\n")
            if host == "blocked.example":
                return self._send(403, "text/html", BLOCK_403)
            if host == "challenge.example":
                return self._send(200, "text/html; charset=utf-8", CHALLENGE)
            if host == "dead.example":
                return self._send(404, "text/html", b"<html>not found</html>")
            if host == "norobots.example":
                if path == "/robots.txt":
                    return self._send(200, "text/plain", b"User-agent: *\nDisallow: /private/\n")
                return self._send(200, "application/pdf", fap_pdf)
            if host == "brokenrobots.example":
                return self._send(500, "text/plain", b"oops")
            if host == "redirect.example":
                if path == "/old":
                    return self._send(301, "text/html", b"", {"Location": "/new"})
                if path == "/offsite":
                    return self._send(302, "text/html", b"", {"Location": "https://evil.example/x"})
                return self._send(200, "text/html", b"<html><title>New</title><a href='/fa/fap.pdf'>Financial Assistance Policy</a></html>")
            if host == "sitemap.example":
                if path == "/robots.txt":
                    return self._send(200, "text/plain", b"User-agent: *\nAllow: /\nSitemap: https://sitemap.example/sitemap_index.xml\n")
                if path == "/sitemap_index.xml":
                    return self._send(200, "application/xml", b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://sitemap.example/sitemap1.xml</loc></sitemap></sitemapindex>')
                if path == "/sitemap1.xml":
                    return self._send(200, "application/xml", b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://sitemap.example/patients/financial-assistance/policy.pdf</loc><lastmod>2026-01-02</lastmod></url><url><loc>https://sitemap.example/careers/</loc></url><url><loc>https://sitemap.example/about/</loc></url></urlset>')
                if path.endswith(".pdf"):
                    return self._send(200, "application/pdf", fap_pdf)
                return self._send(200, "text/html", b"<html><title>Home</title><p>No links here.</p></html>")
            if host == "web.archive.org":
                if path == "/robots.txt":
                    return self._send(200, "text/plain", b"User-agent: *\nAllow: /\n")
                if path == "/cdx/search/cdx":
                    from urllib.parse import parse_qs, urlsplit

                    q = parse_qs(urlsplit(self.path).query)
                    rows = web.wayback_rows.get(q["url"][0], [])
                    body = json.dumps([["timestamp", "original", "statuscode", "mimetype", "digest"], *rows]).encode() if rows else b"[]"
                    return self._send(200, "application/json", body)
                if path.startswith("/web/"):
                    return self._send(200, "application/pdf", fap_pdf)
            p = lookup(url_map, url)
            if p is not None and p.exists():
                return self._send(200, content_type_for(p), p.read_bytes())
            return self._send(404, "text/plain", b"not found")

    return Handler


@pytest.fixture(scope="session")
def local_web(demo_corpus) -> Iterator[LocalWeb]:
    url_map = load_url_map(demo_corpus.root)
    fap = demo_corpus.path("docs/example-methodist/fap.pdf").read_bytes()
    web = LocalWeb("", demo_corpus.root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(web, url_map, fap))
    web.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    yield web
    server.shutdown()
    server.server_close()


@pytest.fixture
def web(local_web: LocalWeb) -> Iterator[LocalWeb]:
    local_web.log.clear()
    local_web.wayback_rows.clear()
    local_web.overrides.clear()
    yield local_web


@pytest.fixture(scope="session")
def discovery_fixtures(demo_corpus, tmp_path_factory) -> Path:
    """Foundation fixtures plus the synthetic discovery fixtures, in a temp dir."""
    import shutil

    from charitycare.discover.demo_fixtures import write_all

    root = tmp_path_factory.mktemp("dfx") / "llm"
    shutil.copytree(demo_corpus.fixtures, root)
    write_all(demo_corpus.root, root, demo_corpus.manifest)
    return root

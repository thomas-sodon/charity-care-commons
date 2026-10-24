"""Polite, read-only HTTP fetcher for hospital sites (design sections 8 S2 and 9).

Rules enforced here, not left to callers:

* **Domain allowlist.** Only facility/system domains and ``archive.org`` are
  fetched; every redirect hop is re-checked.
* **robots.txt respected** (RFC 9309 semantics: 4xx means no restrictions,
  5xx or unreachable means disallow all), including ``Crawl-delay``.
* **Rate limit:** at most one request per host every ``min_interval`` seconds
  (default 5 s, configurable via ``CCC_FETCH_INTERVAL_S``).
* **Honest User-Agent** with a contact address (``Settings.user_agent``).
* **Never circumvent bot protection.** A 401/403/429, or a challenge page
  (Cloudflare, Akamai, Imperva, PerimeterX, DataDome, CAPTCHA), is recorded as
  ``blocked=True`` and not retried. No header spoofing, no proxies, no solving.
"""

from __future__ import annotations

import functools
import os
import re
import threading
import time
import urllib.robotparser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from charitycare.config import get_settings
from charitycare.schemas import utcnow

DEFAULT_INTERVAL_S = 5.0
MAX_BYTES = 25 * 1024 * 1024
ARCHIVE_DOMAINS = ("archive.org",)

BLOCK_STATUSES = frozenset({401, 403, 429})
CHALLENGE_SIGNATURES = (
    "cf-chl", "challenge-platform", "cf_chl_opt", "attention required! | cloudflare", "just a moment...",
    "checking your browser", "_incapsula_resource", "incapsula incident", "px-captcha", "perimeterx",
    "datadome", "captcha-delivery", "access denied</h1>", "you don't have permission to access",
    "request unsuccessful. incapsula",
)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# Multi-tenant hosting platforms: a document hosted here allowlists its exact host only,
# never the platform (``attacker.blob.core.windows.net`` must not ride on a hospital's blob).
SHARED_HOST_SUFFIXES = (
    "windows.net", "azureedge.net", "azurewebsites.net", "amazonaws.com", "cloudfront.net", "sharepoint.com",
    "googleusercontent.com", "googleapis.com", "appspot.com", "github.io", "netlify.app", "herokuapp.com",
    "squarespace.com", "wixsite.com", "wordpress.com", "blogspot.com", "box.com", "dropbox.com", "akamaihd.net",
    "fastly.net", "firebaseapp.com", "web.app", "vercel.app", "pages.dev", "s3.amazonaws.com",
)
_IP_RX = re.compile(r"^[\d.]+$|:")


@functools.lru_cache(maxsize=1)
def _psl() -> Callable[[str], Any] | None:
    """Public-suffix-aware splitter from ``tldextract``'s bundled snapshot (no network); None if not installed."""
    try:
        import tldextract
    except ImportError:  # pragma: no cover - optional dependency
        return None
    return tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(), include_psl_private_domains=True)


def is_shared_host(host: str) -> bool:
    host = host.lower()
    return any(host == s or host.endswith("." + s) for s in SHARED_HOST_SUFFIXES)


def registrable_domain(host_or_url: str) -> str:
    """Registrable domain of a host (public-suffix aware; strips ``www.``).

    ``www.example.tx.us`` -> ``example.tx.us``; a shared hosting platform or an IP
    address yields the exact host. Without ``tldextract`` (or for names outside the
    public suffix list, such as ``.example``) the last two labels are used.
    """
    host = host_of(host_or_url) if "://" in host_or_url else host_or_url.lower()
    host = host.strip(".").removeprefix("www.")
    if not host or _IP_RX.search(host) or is_shared_host(host):
        return host
    ext = _psl()
    if ext is not None:
        r = ext(host)
        reg = r.top_domain_under_public_suffix if hasattr(r, "top_domain_under_public_suffix") else r.registered_domain
        if reg:
            return reg.lower()
    labels = [x for x in host.split(".") if x]
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


class DomainAllowlist:
    """Host suffix allowlist: ``example.org`` allows ``www.example.org`` and ``fa.example.org``.

    Entries on shared hosting platforms (and IPs) are exact hosts: they allow that host only.
    """

    def __init__(self, domains: Iterable[str] = (), *, include_archive: bool = True):
        self.domains: set[str] = set()
        self.hosts: set[str] = set()
        for d in domains:
            if d:
                self.add(d)
        if include_archive:
            self.domains.update(ARCHIVE_DOMAINS)

    def add(self, domain_or_url: str) -> None:
        d = registrable_domain(domain_or_url) if "://" in domain_or_url else domain_or_url.lower().strip(".").removeprefix("www.")
        if not d:
            return
        if is_shared_host(d) or _IP_RX.search(d):
            self.hosts.add(d)
        else:
            self.domains.add(d)

    def copy(self) -> "DomainAllowlist":
        new = DomainAllowlist(include_archive=False)
        new.domains, new.hosts = set(self.domains), set(self.hosts)
        return new

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return False
        host = (parts.hostname or "").lower()
        if host in self.hosts or host.removeprefix("www.") in self.hosts:
            return True
        if is_shared_host(host) or _IP_RX.search(host):
            return False  # never via a suffix entry
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def __repr__(self) -> str:
        return f"DomainAllowlist({sorted(self.domains)!r})"


def looks_blocked(status: int | None, body: bytes, content_type: str | None) -> bool:
    """True for bot-protection responses (block status, or a challenge page in an HTML body)."""
    if status in BLOCK_STATUSES:
        return True
    if content_type and "html" not in content_type.lower():
        return False
    head = body[:20000].decode("utf-8", "replace").lower()
    return any(sig in head for sig in CHALLENGE_SIGNATURES)


@dataclass
class FetchResult:
    """Outcome of one polite fetch. ``content`` is empty unless the fetch succeeded."""

    url: str
    final_url: str
    status: int | None = None
    content: bytes = b""
    content_type: str | None = None
    blocked: bool = False
    robots_disallowed: bool = False
    domain_not_allowed: bool = False
    error: str | None = None
    redirects: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=utcnow)

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and not self.blocked and self.error is None

    @property
    def is_pdf(self) -> bool:
        return (self.content_type or "").lower().startswith("application/pdf") or self.content[:5] == b"%PDF-"

    @property
    def is_html(self) -> bool:
        return "html" in (self.content_type or "").lower() and not self.is_pdf

    @property
    def text(self) -> str:
        m = re.search(r"charset=([\w-]+)", self.content_type or "")
        return self.content.decode(m.group(1) if m else "utf-8", "replace")

    @property
    def outcome(self) -> str:
        """Short label: ok, blocked, robots_disallowed, domain_not_allowed, dead, http_error or error."""
        if self.domain_not_allowed:
            return "domain_not_allowed"
        if self.robots_disallowed:
            return "robots_disallowed"
        if self.blocked:
            return "blocked"
        if self.error:
            return "error"
        if self.status in (404, 410):
            return "dead"
        return "ok" if self.ok else "http_error"


class PoliteFetcher:
    """Rate-limited, robots-aware, allowlisted GET-only fetcher."""

    def __init__(
        self,
        allowlist: DomainAllowlist | None = None,
        *,
        min_interval: float | None = None,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_bytes: int = MAX_BYTES,
        max_redirects: int = 5,
        respect_robots: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.allowlist = allowlist or DomainAllowlist()
        env = os.environ.get("CCC_FETCH_INTERVAL_S")
        self.min_interval = float(min_interval if min_interval is not None else (env if env else DEFAULT_INTERVAL_S))
        self.user_agent = user_agent or get_settings().user_agent
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.respect_robots = respect_robots
        self._clock, self._sleep = clock, sleep
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.requests_made = 0
        self.client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": self.user_agent, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5", "From": _contact(self.user_agent)},
        )

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "PoliteFetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- politeness ------------------------------------------------------------

    def _wait_turn(self, host: str, delay: float) -> None:
        with self._lock:
            now = self._clock()
            last = self._last.get(host)
            if last is not None and (wait := delay - (now - last)) > 0:
                self._sleep(wait)
                now = self._clock()
            self._last[host] = now

    def _host_delay(self, url: str) -> float:
        rp = self._robots.get(_origin(url))
        cd = rp.crawl_delay(self.user_agent) if rp is not None else None
        return max(self.min_interval, float(cd or 0))

    def robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        """Parsed robots.txt for the URL's origin (``None`` = no restrictions). Cached per origin."""
        origin = _origin(url)
        if not self.allowlist.allows(origin + "/"):
            deny = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
            deny.disallow_all = True  # never request anything, robots.txt included, off the allowlist
            return deny
        if origin in self._robots:
            return self._robots[origin]
        rp = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
        self._wait_turn(host_of(url), self.min_interval)
        try:
            resp = self.client.get(origin + "/robots.txt")
            self.requests_made += 1
            status = resp.status_code
            body = resp.text if status == 200 else ""
        except httpx.HTTPError:
            status, body = 599, ""
        if status == 200:
            rp.parse(body.splitlines())
            parsed: urllib.robotparser.RobotFileParser | None = rp
        elif 400 <= status < 500:
            parsed = None  # RFC 9309: unavailable -> no restrictions
        else:
            rp.disallow_all = True  # unreachable -> assume complete disallow
            parsed = rp
        self._robots[origin] = parsed
        return parsed

    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self.robots_for(url)
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def sitemaps_for(self, url: str) -> list[str]:
        rp = self.robots_for(url)
        return list(rp.site_maps() or []) if rp is not None else []

    # -- fetching -------------------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """GET ``url`` politely, following up to ``max_redirects`` allowlisted, robots-allowed hops."""
        result = FetchResult(url=url, final_url=url)
        current = url
        for _ in range(self.max_redirects + 1):
            if not self.allowlist.allows(current):
                result.domain_not_allowed, result.error, result.final_url = True, f"domain not on allowlist: {host_of(current)}", current
                return result
            if not self.allowed_by_robots(current):
                result.robots_disallowed, result.error, result.final_url = True, "disallowed by robots.txt", current
                return result
            self._wait_turn(host_of(current), self._host_delay(current))
            try:
                with self.client.stream("GET", current) as resp:
                    self.requests_made += 1
                    result.status = resp.status_code
                    result.content_type = resp.headers.get("content-type")
                    result.final_url = current
                    if resp.is_redirect and "location" in resp.headers:
                        nxt = urljoin(current, resp.headers["location"])
                        result.redirects.append(nxt)
                        current = nxt
                        continue
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            result.error = f"response larger than {self.max_bytes} bytes"
                            return result
            except httpx.HTTPError as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                return result
            content = bytes(body)
            result.fetched_at = utcnow()
            if looks_blocked(result.status, content, result.content_type):
                result.blocked = True  # recorded, never retried or circumvented
                return result
            if 200 <= result.status < 300:
                result.content = content
            return result
        result.error = f"more than {self.max_redirects} redirects"
        return result


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _contact(ua: str) -> str:
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", ua)
    return m.group(0) if m else ""

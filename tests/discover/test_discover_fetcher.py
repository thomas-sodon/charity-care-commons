"""Polite fetcher over a real local HTTP server: robots, allowlist, blocks, redirects, rate limit."""

from __future__ import annotations

import httpx

from charitycare.discover.fetcher import DomainAllowlist, PoliteFetcher, looks_blocked, registrable_domain
from charitycare.discover.sitemap import parse_sitemap, sitemap_lookup
from charitycare.discover.transports import CorpusTransport
from charitycare.discover.wayback import cdx_query_url, fetch_archived, parse_cdx, wayback_lookup

METHODIST = "https://www.examplemethodist.example"


def test_fetch_demo_page_with_honest_ua(web) -> None:
    with web.fetcher("examplemethodist.example") as f:
        res = f.fetch(METHODIST + "/financial-assistance/")
    assert res.ok and res.is_html and b"Financial Assistance Policy" in res.content
    page = [r for r in web.log if r["url"].endswith("/financial-assistance/")][0]
    assert "CharityCareCommons" in page["ua"] and "@" in page["ua"] and page["from"].count("@") == 1
    assert web.requested("/robots.txt") == 1


def test_pdf_fetch_and_robots_cached(web) -> None:
    with web.fetcher("examplemethodist.example") as f:
        a = f.fetch(METHODIST + "/financial-assistance/fap.pdf")
        b = f.fetch(METHODIST + "/financial-assistance/summary.pdf")
    assert a.ok and a.is_pdf and b.ok
    assert web.requested("examplemethodist.example/robots.txt") == 1


def test_robots_disallow_is_respected(web) -> None:
    with web.fetcher("norobots.example") as f:
        res = f.fetch("https://norobots.example/private/fap.pdf")
        ok = f.fetch("https://norobots.example/public/fap.pdf")
    assert res.robots_disallowed and not res.ok and res.outcome == "robots_disallowed"
    assert web.requested("/private/") == 0
    assert ok.ok


def test_robots_server_error_means_disallow_all(web) -> None:
    with web.fetcher("brokenrobots.example") as f:
        res = f.fetch("https://brokenrobots.example/fap.pdf")
    assert res.robots_disallowed and web.requested("brokenrobots.example/fap.pdf") == 0


def test_403_is_recorded_as_blocked_and_not_retried(web) -> None:
    with web.fetcher("blocked.example") as f:
        res = f.fetch("https://blocked.example/fap.pdf")
    assert res.blocked and res.status == 403 and res.content == b"" and res.outcome == "blocked"
    assert web.requested("blocked.example/fap.pdf") == 1


def test_challenge_page_with_200_is_blocked(web) -> None:
    with web.fetcher("challenge.example") as f:
        res = f.fetch("https://challenge.example/financial-assistance/")
    assert res.status == 200 and res.blocked and not res.ok


def test_allowlist_and_redirects(web) -> None:
    with web.fetcher("redirect.example") as f:
        outside = f.fetch("https://not-allowed.example/x")
        moved = f.fetch("https://redirect.example/old")
        off = f.fetch("https://redirect.example/offsite")
    assert outside.domain_not_allowed and web.requested("not-allowed.example") == 0
    assert moved.ok and moved.final_url == "https://redirect.example/new" and moved.redirects == ["https://redirect.example/new"]
    assert off.domain_not_allowed and off.final_url == "https://evil.example/x" and web.requested("evil.example") == 0


def test_rate_limit_per_host(demo_corpus) -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    f = PoliteFetcher(DomainAllowlist(["examplemethodist.example", "examplehillcountry.example"]), min_interval=5.0,
                      transport=CorpusTransport(demo_corpus.root), clock=lambda: now[0], sleep=sleep)
    f.fetch(METHODIST + "/")  # t=0 robots, waits 5 s, t=5 page
    f.fetch("https://www.examplehillcountry.example/")  # other host: robots at once (t=5), page waits (t=10)
    f.fetch(METHODIST + "/financial-assistance/")  # 5 s since the last Methodist request: no wait
    assert sleeps == [5.0, 5.0]
    f.fetch(METHODIST + "/financial-assistance/fap.pdf")  # immediately after: waits the full interval
    assert sleeps == [5.0, 5.0, 5.0] and now[0] == 15.0
    f.close()


def test_crawl_delay_raises_interval(demo_corpus) -> None:
    robots = b"User-agent: *\nCrawl-delay: 12\nAllow: /\n"
    now, sleeps = [0.0], []
    t = CorpusTransport(demo_corpus.root, extra={METHODIST + "/robots.txt": (200, "text/plain", robots)})
    f = PoliteFetcher(DomainAllowlist(["examplemethodist.example"]), min_interval=1.0, transport=t, clock=lambda: now[0],
                      sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)))
    f.fetch(METHODIST + "/")
    f.fetch(METHODIST + "/financial-assistance/")
    assert sleeps and max(sleeps) == 12.0


def test_env_interval_default(monkeypatch) -> None:
    monkeypatch.setenv("CCC_FETCH_INTERVAL_S", "2.5")
    assert PoliteFetcher().min_interval == 2.5
    monkeypatch.delenv("CCC_FETCH_INTERVAL_S")
    assert PoliteFetcher().min_interval == 5.0


def test_allowlist_rules() -> None:
    al = DomainAllowlist(["https://www.Example.org/fa", "hospital.example"])
    assert al.allows("https://fa.example.org/x") and al.allows("http://hospital.example/")
    assert al.allows("https://web.archive.org/web/1/x")
    assert not al.allows("https://example.org.evil.com/") and not al.allows("ftp://example.org/x")
    assert not DomainAllowlist(include_archive=False).allows("https://web.archive.org/")
    assert registrable_domain("https://www.a.b.example/x") == "b.example"


def test_block_signatures() -> None:
    assert looks_blocked(429, b"", "text/html")
    assert looks_blocked(503, b"<title>Just a moment...</title>", "text/html")
    assert not looks_blocked(200, b"%PDF-1.7 just a moment...", "application/pdf")
    assert not looks_blocked(200, b"<html><form><div class='g-recaptcha'></div></form></html>", "text/html")


def test_size_cap(demo_corpus) -> None:
    f = PoliteFetcher(DomainAllowlist(["examplemethodist.example"]), min_interval=0, transport=CorpusTransport(demo_corpus.root), max_bytes=1000)
    res = f.fetch(METHODIST + "/financial-assistance/fap.pdf")
    assert res.error and not res.ok and res.content == b""


def test_transport_error_is_reported() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    f = PoliteFetcher(DomainAllowlist(["x.example"]), min_interval=0, transport=httpx.MockTransport(boom))
    res = f.fetch("https://x.example/fap.pdf")
    assert res.robots_disallowed  # unreachable robots.txt -> assume disallow all
    f2 = PoliteFetcher(DomainAllowlist(["x.example"]), min_interval=0, transport=httpx.MockTransport(boom), respect_robots=False)
    assert f2.fetch("https://x.example/fap.pdf").outcome == "error"


# -- sitemap + wayback -------------------------------------------------------


def test_sitemap_lookup(web) -> None:
    with web.fetcher("sitemap.example") as f:
        entries = sitemap_lookup(f, "https://sitemap.example")
    assert [e.url for e in entries] == ["https://sitemap.example/patients/financial-assistance/policy.pdf"]
    assert entries[0].lastmod == "2026-01-02"


def test_parse_sitemap_bad_xml() -> None:
    assert parse_sitemap(b"<not xml") == ([], [])


def test_wayback_lookup_and_archived_fetch(web) -> None:
    orig = "https://dead.example/fap.pdf"
    web.wayback_rows[orig] = [["20240105120000", orig, "200", "application/pdf", "AAA"], ["20250610083000", orig, "200", "application/pdf", "BBB"]]
    with web.fetcher("dead.example") as f:
        caps = wayback_lookup(f, orig)
        assert [c.timestamp for c in caps] == ["20250610083000", "20240105120000"]
        assert caps[0].provisional and "2025-06-10" in caps[0].label and "provisional" in caps[0].label
        assert caps[0].archived_url == f"https://web.archive.org/web/20250610083000id_/{orig}"
        res = fetch_archived(f, caps[0])
    assert res.ok and res.is_pdf
    assert "limit=-5" in cdx_query_url(orig) and "filter=statuscode%3A200" in cdx_query_url(orig)
    assert parse_cdx(b"garbage") == [] and parse_cdx(b"[]") == []


def test_off_allowlist_hosts_get_no_requests() -> None:
    from charitycare.discover.sitemap import sitemap_lookup

    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, text="")

    f = PoliteFetcher(DomainAllowlist(["hospital.org"]), min_interval=0, transport=httpx.MockTransport(handler))
    assert sitemap_lookup(f, "http://169.254.169.254") == [] and calls == [] and f.requests_made == 0
    assert not f.allowed_by_robots("http://169.254.169.254/latest") and calls == []


def test_shared_hosts_and_public_suffixes() -> None:
    al = DomainAllowlist(["https://x.blob.core.windows.net/fap.pdf", "https://www.example.tx.us/fap.pdf"])
    assert al.allows("https://x.blob.core.windows.net/a") and not al.allows("https://attacker.blob.core.windows.net/a")
    assert al.allows("https://fa.example.tx.us/") and not al.allows("https://other.tx.us/")
    assert registrable_domain("https://www.example.tx.us/x") == "example.tx.us"

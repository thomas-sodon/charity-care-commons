"""HTML link extraction and anchor scoring for financial-assistance pages.

Scores are additive keyword weights on the anchor text (English and Spanish)
plus smaller weights on the URL path. They drive the deterministic heuristic
crawler and are shown to the LLM agent as hints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from lxml import html as lhtml

ANCHOR_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("financial assistance", 10), ("charity care", 10), ("asistencia financiera", 10), ("ayuda financiera", 8),
    ("plain language summary", 8), ("resumen en lenguaje sencillo", 8), ("solicitud de asistencia", 8),
    ("financial aid", 7), ("help paying", 6), ("paying for care", 6), ("pay for your care", 6),
    ("uninsured", 4), ("billing and collection", 5), ("billing", 4), ("pay my bill", 3), ("pay your bill", 3),
    ("discount", 2), ("patients", 1), ("insurance", 1), ("application", 2), ("policy", 2),
)
PATH_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("financial-assistance", 8), ("financialassistance", 8), ("financial_assistance", 8), ("charity", 8),
    ("asistencia", 7), ("fap", 5), ("billing", 3), ("summary", 2), ("application", 2), ("patients", 1),
)
NEGATIVE: tuple[str, ...] = ("careers", "jobs", "donate", "giving", "foundation", "volunteer", "news", "login", "mychart")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Link:
    url: str
    anchor_text: str
    found_on: str
    score: float

    @property
    def is_pdf(self) -> bool:
        return urlsplit(self.url).path.lower().endswith(".pdf")


def score_link(anchor_text: str, url: str) -> float:
    """Keyword score of a link; negative-keyword links (careers, donate ...) score 0."""
    a = _WS.sub(" ", anchor_text.lower()).strip()
    path = urlsplit(url).path.lower()
    if any(n in path or n in a for n in NEGATIVE):
        return 0.0
    s = sum(w for k, w in ANCHOR_WEIGHTS if k in a)
    s += sum(w for k, w in PATH_WEIGHTS if k in path)
    if s > 0 and path.endswith(".pdf"):
        s += 2
    return float(s)


def extract_links(page_html: str | bytes, base_url: str) -> list[Link]:
    """All http(s) links on a page with anchor text (or title/aria-label), resolved and de-duplicated."""
    if not page_html:
        return []
    try:
        doc = lhtml.fromstring(page_html)
    except (ValueError, lhtml.etree.ParserError):  # type: ignore[attr-defined]
        return []
    base = doc.base_url or base_url
    for b in doc.xpath("//base[@href]"):
        base = urljoin(base_url, b.get("href"))
    seen: dict[str, Link] = {}
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(base, href))[0]
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        text = _WS.sub(" ", a.text_content() or "").strip() or (a.get("title") or a.get("aria-label") or "").strip()
        link = Link(url, text, base_url, score_link(text, url))
        if url not in seen or link.score > seen[url].score:
            seen[url] = link
    return list(seen.values())


def page_title(page_html: str | bytes) -> str:
    try:
        doc = lhtml.fromstring(page_html)
    except (ValueError, lhtml.etree.ParserError):  # type: ignore[attr-defined]
        return ""
    t = doc.findtext(".//title") or ""
    return _WS.sub(" ", t).strip()


def visible_text(page_html: str | bytes, limit: int = 2000) -> str:
    try:
        doc = lhtml.fromstring(page_html)
    except (ValueError, lhtml.etree.ParserError):  # type: ignore[attr-defined]
        return ""
    for bad in doc.xpath("//script|//style|//noscript"):
        bad.drop_tree()
    return _WS.sub(" ", doc.text_content()).strip()[:limit]

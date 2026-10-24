"""The independent text layer of a hashed document, and cost routing for the compile call.

The quote verifier never trusts text the model saw; it re-reads the hashed PDF
with PyMuPDF (and Tesseract OCR when installed). Routing (section 8, S4):
born-digital pages go to the model as text (~2k tokens/page); pages without a
text layer, OCR'd pages and table-heavy pages go as page images (~1.6k tokens).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

import pymupdf

from charitycare.llm import DocumentPart
from charitycare.pdftext import PageText, page_texts, sha256_bytes

_MONEY = re.compile(r"\$\s?\d[\d,]*")


class DocumentHashMismatch(ValueError):
    """The bytes do not hash to the expected sha256 (identity is the hash)."""


@dataclass(frozen=True)
class DocText:
    """Per-page text of one hashed PDF."""

    sha256: str
    pages: tuple[PageText, ...]
    pdf: bytes = b""

    @classmethod
    def from_pdf(cls, pdf: bytes, expected_sha256: str | None = None, *, ocr: bool | None = None) -> DocText:
        sha = sha256_bytes(pdf)
        if expected_sha256 and sha != expected_sha256:
            raise DocumentHashMismatch(f"expected {expected_sha256[:12]}, got {sha[:12]}")
        return cls(sha, tuple(page_texts(pdf, ocr=ocr)), pdf)

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    def page(self, n: int) -> PageText | None:
        return self.pages[n - 1] if 1 <= n <= len(self.pages) else None

    @cached_property
    def unreadable_pages(self) -> tuple[int, ...]:
        """Pages with no text layer and no OCR text: quotes on them cannot be verified."""
        return tuple(p.page for p in self.pages if not p.text.strip())

    @cached_property
    def table_pages(self) -> tuple[int, ...]:
        """Pages with a printed money table (>= 4 lines carrying >= 3 dollar amounts)."""
        out = []
        for p in self.pages:
            lines = [ln for ln in p.text.splitlines() if len(_MONEY.findall(ln)) >= 3]
            if len(lines) >= 4:
                out.append(p.page)
        return tuple(out)


@dataclass(frozen=True)
class RoutingPlan:
    text_pages: tuple[int, ...]
    image_pages: tuple[int, ...]

    @property
    def est_input_tokens(self) -> int:
        """Section 13 estimate: 2,000 tokens per text page, 1,600 per image page."""
        return 2_000 * len(self.text_pages) + 1_600 * len(self.image_pages)


def plan_routing(doc: DocText) -> RoutingPlan:
    """Text for born-digital pages; images for unreadable, OCR'd or table-heavy pages."""
    image = {p.page for p in doc.pages if not p.has_text_layer} | set(doc.table_pages)
    text = tuple(p.page for p in doc.pages if p.text.strip())
    return RoutingPlan(text_pages=text, image_pages=tuple(sorted(image)))


def render_page_png(pdf: bytes, page: int, dpi: int = 110) -> bytes:
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        return d[page - 1].get_pixmap(dpi=dpi).tobytes("png")


def page_text_xml(doc: DocText, pages: tuple[int, ...]) -> str:
    """Page-tagged text block so the model can cite page numbers."""
    parts = [f'<document sha256="{doc.sha256}" pages="{doc.n_pages}">']
    for n in pages:
        p = doc.page(n)
        parts.append(f'<page number="{n}">\n{p.text.strip() if p else ""}\n</page>')
    parts.append("</document>")
    return "\n".join(parts)


def build_documents(doc: DocText, plan: RoutingPlan | None = None) -> tuple[DocumentPart, ...]:
    """Document blocks for the compile call (identical for compile and re-ask, so they cache).

    Table pages that have a text layer are sent both ways: the text keeps quotes exact,
    the image keeps the table's layout.
    """
    plan = plan or plan_routing(doc)
    parts: list[DocumentPart] = [DocumentPart("text", page_text_xml(doc, plan.text_pages), title=f"policy {doc.sha256[:12]} text")]
    if doc.pdf:
        for n in plan.image_pages:
            parts.append(DocumentPart("image", render_page_png(doc.pdf, n), title=f"page {n}", media_type="image/png"))
    return tuple(parts)

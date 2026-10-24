"""Deterministic PDF rendering helpers for the demo corpus (PyMuPDF).

Pages are lists of :class:`Block` laid out top to bottom. Determinism comes
from fixed metadata, ``no_new_id=True`` and garbage-collected, deflated saves,
so the same input always produces byte-identical PDFs (and stable sha256s).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pymupdf

PAGE_W, PAGE_H = 612.0, 792.0
MARGIN = 60.0
FIXED_DATE = "D:20261001000000Z"

Style = Literal["title", "subtitle", "h", "p", "mono", "small"]
_STYLE = {
    "title": ("hebo", 16.0, 8.0),
    "subtitle": ("helv", 12.0, 10.0),
    "h": ("hebo", 12.0, 4.0),
    "p": ("helv", 10.5, 6.0),
    "mono": ("cour", 9.0, 1.0),
    "small": ("helv", 8.0, 4.0),
}


class LayoutOverflow(RuntimeError):
    """A page's blocks do not fit; the corpus spec must move text to another page."""


@dataclass
class Block:
    style: Style
    text: str


@dataclass
class PageSpec:
    blocks: list[Block] = field(default_factory=list)
    image_only: bool = False


def new_document(title: str) -> pymupdf.Document:
    doc = pymupdf.open()
    doc.set_metadata(
        {
            "title": title,
            "author": "Charity Care Commons synthetic demo corpus (fictional)",
            "subject": "Synthetic document - fictional hospital",
            "creator": "charitycare.demo",
            "producer": "charitycare.demo",
            "creationDate": FIXED_DATE,
            "modDate": FIXED_DATE,
            "keywords": "synthetic, fictional",
        }
    )
    return doc


def write_blocks(page: pymupdf.Page, blocks: list[Block], top: float = MARGIN) -> float:
    """Lay out blocks on ``page``; returns the y coordinate after the last block."""
    y = top
    bottom = PAGE_H - MARGIN - 14
    for b in blocks:
        font, size, gap = _STYLE[b.style]
        rect = pymupdf.Rect(MARGIN, y, PAGE_W - MARGIN, bottom)
        rc = page.insert_textbox(rect, b.text, fontname=font, fontsize=size, lineheight=1.25)
        if rc < 0:
            raise LayoutOverflow(f"block does not fit on page {page.number + 1}: {b.text[:60]!r}")
        y += (bottom - y) - rc + gap
    return y


def footer(page: pymupdf.Page, text: str) -> None:
    page.insert_textbox(
        pymupdf.Rect(MARGIN, PAGE_H - MARGIN + 6, PAGE_W - MARGIN, PAGE_H - 20), text, fontname="helv", fontsize=7.5, align=1
    )


def rasterize_into(page: pymupdf.Page, draw: "callable[[pymupdf.Page], None]", dpi: int = 110) -> None:  # type: ignore[valid-type]
    """Draw onto a scratch page, render it to a grayscale image and place only the image
    on ``page`` (an image-only, "scanned" page with no text layer)."""
    scratch = pymupdf.open()
    sp = scratch.new_page(width=PAGE_W, height=PAGE_H)
    draw(sp)
    pix = sp.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    page.insert_image(page.rect, stream=pix.tobytes("png"))
    scratch.close()


def render_pages(title: str, pages: list[PageSpec], footer_text: str) -> bytes:
    doc = new_document(title)
    n = len(pages)
    for i, spec in enumerate(pages, start=1):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        foot = f"{footer_text} - Page {i} of {n}"

        def draw(p: pymupdf.Page, spec: PageSpec = spec, foot: str = foot) -> None:
            write_blocks(p, spec.blocks)
            footer(p, foot)

        if spec.image_only:
            rasterize_into(page, draw)
        else:
            draw(page)
    return save(doc)


def save(doc: pymupdf.Document) -> bytes:
    data = doc.tobytes(garbage=4, deflate=True, no_new_id=True)
    doc.close()
    return data

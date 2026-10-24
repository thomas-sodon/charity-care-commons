"""Shared PDF text-layer utilities: hashing, per-page text, optional OCR, normalization.

The quote verifier (``compile/``), readback (``packet/``) and the document store
(``discover/``) all need the *same* independent text layer, so it lives here.

OCR is optional: if ``tesseract`` is on PATH, pages without a text layer are
OCR'd through PyMuPDF's Tesseract integration; otherwise they yield ``""`` and
the page is reported in ``PageText.ocr_unavailable`` so callers can abstain
with reason ``illegible`` instead of guessing.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import pymupdf

_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_to_path(uri: str | None) -> Path | None:
    """Local path for a stored document URI: ``file://`` URIs (document store) or plain paths.

    Returns None for empty or non-local URIs (e.g. ``s3://``); the caller decides on a fallback.
    """
    if not uri:
        return None
    if uri.startswith("file:"):
        return Path(url2pathname(urlparse(uri).path))
    if "://" in uri:
        return None
    return Path(uri)


def normalize_text(text: str) -> str:
    """Normalization used for quote matching: NFKC, straight quotes and dashes,
    collapsed whitespace (so line wraps do not matter), case-folded."""
    t = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return _WS.sub(" ", t).strip().casefold()


@lru_cache(maxsize=1)
def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


@dataclass(frozen=True)
class PageText:
    page: int  # 1-indexed
    text: str
    has_text_layer: bool
    ocr_used: bool = False
    ocr_unavailable: bool = False


def page_texts(pdf: bytes, *, ocr: bool | None = None, dpi: int = 300) -> list[PageText]:
    """Text of every page. ``ocr=None`` means "OCR image-only pages if Tesseract exists"."""
    use_ocr = tesseract_available() if ocr is None else (ocr and tesseract_available())
    out: list[PageText] = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text")
            if text.strip():
                out.append(PageText(i, text, True))
                continue
            if use_ocr:
                tp = page.get_textpage_ocr(dpi=dpi, full=True)
                out.append(PageText(i, page.get_text("text", textpage=tp), False, ocr_used=True))
            else:
                out.append(PageText(i, "", False, ocr_unavailable=True))
    return out


def has_text_layer(pdf: bytes) -> bool:
    """True when every page has extractable text."""
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return all(page.get_text("text").strip() for page in doc)


def find_quote(page_text: str, quote: str) -> tuple[int, int] | None:
    """Exact normalized substring match; returns (start, end) offsets in the normalized page text."""
    hay, needle = normalize_text(page_text), normalize_text(quote)
    if not needle:
        return None
    i = find_bounded(hay, needle)
    return (i, i + len(needle)) if i >= 0 else None


def find_bounded(hay: str, needle: str, start: int = 0) -> int:
    """First index of ``needle`` in ``hay`` that does not begin or end mid-word (-1 if none).

    Where the needle starts (ends) with an alphanumeric character, the character just
    before (after) the match must be non-alphanumeric or the string edge, so that
    "insured" is not found inside "uninsured" and "0% FPG" not inside "200% FPG".
    """
    if not needle:
        return -1
    check_l, check_r = needle[0].isalnum(), needle[-1].isalnum()
    i = hay.find(needle, start)
    while i >= 0:
        j = i + len(needle)
        left_ok = not check_l or i == 0 or not hay[i - 1].isalnum()
        right_ok = not check_r or j == len(hay) or not hay[j].isalnum()
        if left_ok and right_ok:
            return i
        i = hay.find(needle, i + 1)
    return -1

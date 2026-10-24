"""PII minimization (design section 11): masking filter, EXIF stripping, image hashing.

The packet compiler never needs SSNs, full account numbers or dates of birth.
:func:`mask_pii` replaces SSN-like and account-number-like patterns in any text
a navigator types or an extractor returns; :func:`strip_exif` re-encodes an
uploaded image from its pixels only, so no camera, GPS or timestamp metadata
survives into memory, prompts or fixtures.
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Literal

from PIL import Image, ImageOps

SSN_RE = re.compile(r"(?<!\d)\d{3}[- .]\d{2}[- .]\d{4}(?!\d)|(?<![\d-])\d{9}(?![\d-])")
# Labelled account / member / MRN numbers, and any bare run of 8+ digits.
ACCOUNT_LABELLED_RE = re.compile(
    r"(?i)\b(acct|account|member|mrn|routing|n[uú]mero de cuenta)(\s*(no\.?|number|num|#|id))?\s*[:#]?\s*"
    r"((?=(?:[A-Z0-9-]*\d){4})[A-Z0-9][A-Z0-9-]{3,})"
)
LONG_DIGITS_RE = re.compile(r"(?<![\d-])\d{8,}(?![\d-])")

SSN_MASK = "[SSN removed]"
# OCR and word processors turn "-" into Unicode dashes and " " into no-break spaces.
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"}
                        | {c: " " for c in "\u00a0\u2007\u202f"})
_PHONE_RE = re.compile(r"^\+?1?[\s.-]*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}$")
_ZIP_RE = re.compile(r"^\d{5}(?:-?\d{4})?$")

PiiKind = Literal["text", "phone", "zip"]


def _last4(s: str) -> str:
    digits = re.sub(r"\D", "", s)
    return "****" + digits[-4:] if len(digits) >= 4 else "****"


def mask_pii(text: str, kind: PiiKind = "text") -> str:
    """Mask SSN-like values entirely and account-like numbers to their last four digits.

    Unicode dashes and spaces are normalized first, so "123–45–6789" is caught. For a
    ``phone`` or ``zip`` form value that is a well-formed phone number or ZIP(+4), the
    value is returned as is (a bare 10-digit phone or 9-digit ZIP+4 is not an account
    number or SSN).
    """
    if not text:
        return text
    text = text.translate(_DASHES)
    if (kind == "phone" and _PHONE_RE.match(text.strip())) or (kind == "zip" and _ZIP_RE.match(text.strip())):
        return text
    out = SSN_RE.sub(SSN_MASK, text)
    out = ACCOUNT_LABELLED_RE.sub(lambda m: m.group(0)[: m.start(4) - m.start(0)] + _last4(m.group(4)), out)
    return LONG_DIGITS_RE.sub(lambda m: _last4(m.group(0)), out)


def contains_pii(text: str) -> bool:
    """True when :func:`mask_pii` would change ``text``."""
    return bool(text) and mask_pii(text) != text


def last4(value: str | None) -> str | None:
    """Reduce an account number to ``****1234`` (None stays None)."""
    return None if value is None else _last4(value)


def strip_exif(data: bytes, *, max_side: int = 2400) -> bytes:
    """Re-encode an image as PNG from its pixels only.

    EXIF orientation is applied first (so the pixels look right), then all
    metadata is dropped. Large phone photos are downscaled to ``max_side``.
    """
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        clean = Image.frombytes("RGB", im.size, im.tobytes())  # fresh image: no info dict
    buf = io.BytesIO()
    clean.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def image_sha256(data: bytes) -> str:
    """Content hash of an image's decoded RGB pixels (encoder-independent fixture key)."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        h = hashlib.sha256(f"{im.width}x{im.height}:RGB:".encode())
        h.update(im.tobytes())
    return h.hexdigest()


def has_metadata(data: bytes) -> bool:
    """True when the image carries EXIF or text metadata chunks."""
    with Image.open(io.BytesIO(data)) as im:
        return bool(im.getexif()) or any(k for k in im.info if k not in {"dpi", "gamma", "srgb"})

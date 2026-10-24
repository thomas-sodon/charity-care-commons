"""Render-and-reread verification of a filled form (design section 5 mechanism 5, S12).

Each field region of the filled PDF is read back by independent readers and
diffed against the intended value:

1. **Text layer**: the words whose centre lies inside the field rectangle
   (PyMuPDF ``get_text("words")``), so a label above a blank is never read as
   its value.
2. **Pixels**: the field region is rendered and compared with the blank form;
   a filled value must leave ink, a hand-written or unchecked field must not.
   Check boxes are read from pixels only.
3. **OCR** of the rendered crop when Tesseract is installed, and optionally a
   Haiku-class vision read of crops whose OCR is missing or disagrees.

A mismatching field is retried once (re-fitted font size); if it still does
not read back exactly it is withheld and marked "write by hand". A packet is
verified only when every filled field matches.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import numpy as np
import pymupdf
from pydantic import BaseModel, ConfigDict

from charitycare import pdftext
from charitycare.config import PROMPT_VERSIONS
from charitycare.llm import DocumentPart, LLMBackend, LLMError, LLMRequest, get_backend
from charitycare.packet.fill import CHECK_MARK, MIN_FONTSIZE, FillPlan, fill, fit_fontsize, is_hand
from charitycare.schemas import FormFieldSpec, FormTemplate, ReadbackField, ReadbackResult

READBACK_PROMPT_VERSION = PROMPT_VERSIONS["readback_crop"]
RENDER_DPI = 144
INK_DELTA = 60  # grey levels darker than the blank form that count as ink
TEXT_INK_MIN_PX = 12  # one small glyph at RENDER_DPI leaves well over this many dark pixels
BOX_INK_MIN = 0.04


class CropReading(BaseModel):
    """Haiku-class vision read of one rendered field crop."""

    model_config = ConfigDict(extra="forbid")
    text: str
    confidence: float = 0.0


CROP_SYSTEM = (
    "You read one cropped field from a filled paper form. Return exactly the characters written in the crop, "
    "preserving case, punctuation and spacing, or an empty string if it is blank. For a check box return 'X' "
    "if it is marked and '' otherwise. Do not guess or correct anything."
)


def norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text or "").split())


@dataclass
class _Readers:
    vision: bool
    backend: LLMBackend | None
    cost_usd: float = 0.0


def _words_in(page: pymupdf.Page, rect: pymupdf.Rect) -> str:
    grow = pymupdf.Rect(rect.x0 - 1.5, rect.y0 - 1.5, rect.x1 + 1.5, rect.y1 + 1.5)
    words = [w for w in page.get_text("words") if grow.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))]
    words.sort(key=lambda w: (w[5], w[6], w[7]))
    return " ".join(w[4] for w in words)


def _gray(page: pymupdf.Page, rect: pymupdf.Rect) -> np.ndarray:
    pix = page.get_pixmap(clip=rect, dpi=RENDER_DPI, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.int16)


def _ink(filled: pymupdf.Page, blank: pymupdf.Page, rect: pymupdf.Rect) -> np.ndarray:
    a, b = _gray(filled, rect), _gray(blank, rect)
    h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    return (b[:h, :w] - a[:h, :w]) > INK_DELTA


def ink_ratio(filled: pymupdf.Page, blank: pymupdf.Page, rect: pymupdf.Rect) -> float:
    """Share of pixels in ``rect`` that are clearly darker on the filled page than on the blank one."""
    ink = _ink(filled, blank, rect)
    return float(ink.mean()) if ink.size else 0.0


def ink_pixels(filled: pymupdf.Page, blank: pymupdf.Page, rect: pymupdf.Rect) -> int:
    """Number of pixels in ``rect`` that are clearly darker on the filled page than on the blank one."""
    return int(_ink(filled, blank, rect).sum())


def _ocr(page: pymupdf.Page, rect: pymupdf.Rect) -> str | None:
    if not pdftext.tesseract_available():
        return None
    try:
        pix = page.get_pixmap(clip=rect, dpi=300)
        with pymupdf.open(stream=pix.pdfocr_tobytes(), filetype="pdf") as d:
            return norm(d[0].get_text("text"))
    except (RuntimeError, ValueError):
        return None


def _vision(page: pymupdf.Page, rect: pymupdf.Rect, key: str, readers: _Readers) -> str | None:
    png = page.get_pixmap(clip=rect, dpi=RENDER_DPI).tobytes("png")
    req = LLMRequest(
        prompt_version=READBACK_PROMPT_VERSION,
        model_tier="haiku",
        doc_sha256=pdftext.sha256_bytes(png),
        system=CROP_SYSTEM,
        documents=(DocumentPart("image", png, media_type="image/png"),),
        user="Read the field.",
        output_schema=CropReading,
        variant=key,
        max_tokens=200,
    )
    try:
        res = (readers.backend or get_backend()).complete(req)
    except LLMError:
        return None
    readers.cost_usd += res.cost_usd
    return norm(res.output.text)


def read_field(filled: pymupdf.Page, blank: pymupdf.Page, spec: FormFieldSpec, intended: str, readers: _Readers) -> tuple[str, bool]:
    """(what was read, match) for one field."""
    rect = pymupdf.Rect(spec.rect)
    want = norm(intended)
    if spec.field_type == "checkbox":
        inner = pymupdf.Rect(rect.x0 + 2.5, rect.y0 + 2.5, rect.x1 - 2.5, rect.y1 - 2.5)
        read = CHECK_MARK if ink_ratio(filled, blank, inner) >= BOX_INK_MIN else ""
        return read, read == want
    text = norm(_words_in(filled, rect))
    inked = ink_pixels(filled, blank, rect) >= TEXT_INK_MIN_PX
    if text != want:
        return text, False
    if bool(want) != inked:  # text layer says one thing, pixels another
        return (text if inked else ""), False
    if not want:
        return "", True
    visual = _ocr(filled, rect)
    if readers.vision and visual != want:
        visual = _vision(filled, rect, spec.key, readers) or visual
    if visual is not None and visual != want:
        return visual, False
    return text, True


def readback(filled_pdf: bytes, blank_pdf: bytes, template: FormTemplate, plan: FillPlan, readers: _Readers) -> list[ReadbackField]:
    """Read every templated field of a filled PDF and diff it against the plan."""
    out: list[ReadbackField] = []
    with pymupdf.open(stream=filled_pdf, filetype="pdf") as fd, pymupdf.open(stream=blank_pdf, filetype="pdf") as bd:
        for spec in template.field_map:
            intended = plan.intended(spec)
            read, match = read_field(fd[spec.page - 1], bd[spec.page - 1], spec, intended, readers)
            out.append(ReadbackField(key=spec.key, intended=intended, read=read, match=match, write_by_hand=is_hand(spec)))
    return out


@dataclass
class FillOutcome:
    pdf: bytes
    result: ReadbackResult
    withheld: list[str]  # keys that failed readback twice and were left blank
    cost_usd: float = 0.0

    @property
    def all_filled_match(self) -> bool:
        return self.result.all_match and not self.withheld


def fill_and_verify(
    pdf: bytes,
    template: FormTemplate,
    plan: FillPlan,
    *,
    template_id: str | None = None,
    vision: bool = False,
    backend: LLMBackend | None = None,
) -> FillOutcome:
    """Fill, read back, retry mismatches once, then withhold what still mismatches."""
    readers = _Readers(vision=vision, backend=backend)
    specs = {s.key: s for s in template.field_map}
    filled = fill(pdf, template, plan)
    fields = readback(filled, pdf, template, plan, readers)
    retries = 0
    bad = [f.key for f in fields if not f.match and not f.write_by_hand]
    if bad:
        retries = 1
        for k in bad:
            rect = pymupdf.Rect(specs[k].rect)
            plan.fontsize[k] = max(MIN_FONTSIZE, fit_fontsize(plan.values.get(k, ""), rect) - 0.5)
        filled = fill(pdf, template, plan)
        fields = readback(filled, pdf, template, plan, readers)
    withheld = [f.key for f in fields if not f.match and not f.write_by_hand]
    if withheld:
        failed = {f.key: f for f in fields}
        plan.skip.update(withheld)
        filled = fill(pdf, template, plan)
        fields = readback(filled, pdf, template, plan, readers)
        fields = [
            ReadbackField(key=f.key, intended=failed[f.key].intended, read=failed[f.key].read, match=False, write_by_hand=True)
            if f.key in withheld
            else f
            for f in fields
        ]
    result = ReadbackResult(template_id=template_id or template.doc_sha256, fields=fields, retries=retries)
    return FillOutcome(filled, result, withheld, readers.cost_usd)

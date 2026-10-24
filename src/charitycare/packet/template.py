"""Form templates (design section 8 S11): map a hospital's application form onto canonical keys.

A template is built once per form sha256 and then verified by a human (H3)
before packets that use it may be labelled verified.

* **Fillable AcroForm**: each widget is matched to a canonical key from its
  field name and label (:func:`charitycare.packet.keys.assign_keys`).
* **Flat**: candidate blanks are found from vector drawings
  (``page.get_drawings()``: underlines and small boxes) or, for scanned pages
  without vectors, from long dark pixel runs in a rendered image. Each blank
  gets the nearby text spans as context. An Opus-class vision call labels the
  blanks with canonical keys (``form_template`` prompt, replayable); when that
  call is unavailable, the heuristic label matcher is used instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pymupdf
from pydantic import BaseModel, ConfigDict
from pydantic import Field as PField

from charitycare import pdftext
from charitycare.config import PROMPT_VERSIONS
from charitycare.llm import DocumentPart, LLMBackend, LLMError, LLMRequest, get_backend
from charitycare.packet.keys import CANONICAL_KEYS, HAND_KEYS, assign_keys, field_type
from charitycare.schemas import FormFieldSpec, FormTemplate, ReviewTask

TEMPLATE_PROMPT_VERSION = PROMPT_VERSIONS["form_template"]

# Geometry of a blank above an underline, in PDF points.
BLANK_HEIGHT = 16.0
MIN_LINE_LEN = 30.0
BOX_MIN, BOX_MAX = 8.0, 18.0


@dataclass
class Candidate:
    """One detected blank on a flat form."""

    id: int
    page: int  # 1-indexed
    rect: pymupdf.Rect
    kind: Literal["line", "box"]
    context: str = ""
    source: Literal["vector", "raster"] = "vector"

    def as_prompt(self) -> dict[str, object]:
        return {
            "candidate_id": self.id,
            "page": self.page,
            "rect": [round(v, 1) for v in self.rect],
            "kind": self.kind,
            "nearby_text": self.context,
        }


# --------------------------------------------------------------------------
# LLM output schema (form_template prompt)
# --------------------------------------------------------------------------


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateLabel(_M):
    candidate_id: int
    key: str = PField(description="One canonical key from the list, or 'other' when none fits")
    label: str = PField(description="The label printed next to the blank, verbatim")
    field_type: Literal["text", "checkbox", "date", "money", "signature"] = "text"
    write_by_hand: bool = PField(default=False, description="True for SSN, date of birth, account number, signature")


class TemplateLabeling(_M):
    labels: list[CandidateLabel] = PField(default_factory=list)


TEMPLATE_SYSTEM = (
    "You label blanks on a hospital financial-assistance application form. You receive rendered page images "
    "and a JSON list of candidate blanks (page, rectangle in PDF points with a top-left origin, kind, nearby "
    "text). For each candidate return the canonical key it should hold, the label printed next to it copied "
    "verbatim, its field type and whether it must be written by hand. Use 'other' when no canonical key fits. "
    "Never invent a candidate. Each canonical key is used at most once. Canonical keys: "
    + ", ".join(CANONICAL_KEYS)
    + ". Fields for a Social Security number, date of birth, account number or signature are write_by_hand."
)


# --------------------------------------------------------------------------
# Candidate detection
# --------------------------------------------------------------------------


def _vector_candidates(page: pymupdf.Page, page_no: int) -> list[tuple[pymupdf.Rect, str]]:
    out: list[tuple[pymupdf.Rect, str]] = []
    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 1.0 and abs(p2.x - p1.x) >= MIN_LINE_LEN:
                    x0, x1, y = min(p1.x, p2.x), max(p1.x, p2.x), (p1.y + p2.y) / 2
                    out.append((pymupdf.Rect(x0, y - BLANK_HEIGHT, x1, y), "line"))
            elif item[0] == "re":
                r = pymupdf.Rect(item[1])
                if BOX_MIN <= r.width <= BOX_MAX and BOX_MIN <= r.height <= BOX_MAX and abs(r.width - r.height) < 3:
                    out.append((r, "box"))
                elif r.width >= MIN_LINE_LEN and r.height <= 30:  # an outlined entry box
                    out.append((r, "line"))
    return out


def _runs(mask_row: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """(start, end) of runs of True of at least ``min_len`` in a boolean row."""
    padded = np.concatenate(([False], mask_row, [False]))
    diff = np.diff(padded.astype(np.int8))
    starts, ends = np.where(diff == 1)[0], np.where(diff == -1)[0]
    return [(int(s), int(e)) for s, e in zip(starts, ends) if e - s >= min_len]


def _raster_candidates(page: pymupdf.Page, dpi: int = 144) -> list[tuple[pymupdf.Rect, str]]:
    """Underlines and checkboxes found in the rendered pixels of an image-only page."""
    scale = dpi / 72.0
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    dark = img < 170
    box_lo, box_hi = int(BOX_MIN * scale), int(BOX_MAX * scale)
    segs: list[list[int]] = []  # [x0, x1, y0, y1] in px, merged across adjacent rows
    for y in range(dark.shape[0]):
        for s, e in _runs(dark[y], box_lo):
            for seg in segs:
                if seg[3] >= y - 2 and abs(seg[0] - s) <= 2 and abs(seg[1] - e) <= 2:
                    seg[3] = y
                    break
            else:
                segs.append([s, e, y, y])
    lines: list[tuple[pymupdf.Rect, str]] = []
    short = [s for s in segs if box_lo <= s[1] - s[0] <= box_hi and s[3] - s[2] <= 3]
    used: set[int] = set()
    for i, top in enumerate(short):  # a box = two short runs with matching x and a square gap
        for j, bot in enumerate(short):
            if j <= i or j in used or i in used:
                continue
            w, h = top[1] - top[0], bot[2] - top[2]
            if abs(top[0] - bot[0]) <= 2 and abs(top[1] - bot[1]) <= 2 and abs(h - w) <= 3 * scale:
                side = dark[top[2] : bot[3] + 1, max(top[0], 0) : top[0] + 2]
                if side.any(axis=1).mean() > 0.8:
                    used.update({i, j})
                    r = pymupdf.Rect(top[0], top[2], top[1], bot[3]) * (1 / scale)
                    lines.append((r, "box"))
    for seg in segs:
        if (seg[1] - seg[0]) / scale >= MIN_LINE_LEN and seg[3] - seg[2] <= 3:
            x0, x1, ly = seg[0] / scale, seg[1] / scale, (seg[2] + seg[3] + 1) / 2 / scale
            lines.append((pymupdf.Rect(x0, ly - BLANK_HEIGHT, x1, ly), "line"))
    return lines


def _context(page: pymupdf.Page, rect: pymupdf.Rect, kind: str, words: list[tuple]) -> str:
    """Text printed just above (blanks) or just right of (boxes) a candidate."""
    picked: list[tuple] = []
    for w in words:
        wr = pymupdf.Rect(w[:4])
        if kind == "line":
            if rect.y0 - 14 <= wr.y1 <= rect.y0 + 1.5 and rect.x0 - 2 <= wr.x0 < rect.x1:
                picked.append(w)
        else:
            cy = (wr.y0 + wr.y1) / 2
            if rect.y0 - 3 <= cy <= rect.y1 + 3 and rect.x1 <= wr.x0 <= rect.x1 + 320:
                picked.append(w)
    picked.sort(key=lambda w: (w[5], w[6], w[7]))
    return " ".join(w[4] for w in picked)


def detect_candidates(pdf: bytes) -> list[Candidate]:
    """All candidate blanks of a flat form, with their nearby text."""
    cands: list[Candidate] = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, start=1):
            found = _vector_candidates(page, page_no)
            source: Literal["vector", "raster"] = "vector"
            if not found and not page.get_text("text").strip():
                found, source = _raster_candidates(page), "raster"
            words = page.get_text("words")
            if not words and source == "raster" and pdftext.tesseract_available():
                words = page.get_text("words", textpage=page.get_textpage_ocr(dpi=300, full=True))
            found.sort(key=lambda rk: (round(rk[0].y1), rk[0].x0))
            for rect, kind in found:
                cands.append(Candidate(len(cands), page_no, rect, kind, _context(page, rect, kind, words), source))  # type: ignore[arg-type]
    return cands


# --------------------------------------------------------------------------
# Labelling
# --------------------------------------------------------------------------


def heuristic_labels(cands: list[Candidate]) -> TemplateLabeling:
    """Label candidates by matching their nearby text against canonical synonyms."""
    assigned = assign_keys({c.id: c.context for c in cands if c.context})
    labels = []
    for c in cands:
        key = assigned.get(c.id)
        if key is None:
            continue
        if (field_type(key) == "checkbox") != (c.kind == "box"):
            continue
        labels.append(CandidateLabel(candidate_id=c.id, key=key, label=c.context, field_type=field_type(key), write_by_hand=key in HAND_KEYS - {"signature_date"}))
    return TemplateLabeling(labels=labels)


def page_images(pdf: bytes, dpi: int = 110) -> list[bytes]:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return [p.get_pixmap(dpi=dpi).tobytes("png") for p in doc]


def labeling_request(pdf: bytes, cands: list[Candidate], sha: str | None = None) -> LLMRequest[TemplateLabeling]:
    """The Opus-class vision request that labels candidate blanks."""
    docs = tuple(DocumentPart("image", png, media_type="image/png") for png in page_images(pdf))
    cand_json = json.dumps([c.as_prompt() for c in cands], ensure_ascii=False)
    return LLMRequest(
        prompt_version=TEMPLATE_PROMPT_VERSION,
        model_tier="opus",
        doc_sha256=sha or pdftext.sha256_bytes(pdf),
        system=TEMPLATE_SYSTEM,
        documents=docs + (DocumentPart("text", cand_json, title="candidate blanks"),),
        user="Label every candidate blank.",
        output_schema=TemplateLabeling,
        effort="high",
    )


def _specs_from_labels(cands: list[Candidate], labeling: TemplateLabeling) -> list[FormFieldSpec]:
    by_id = {c.id: c for c in cands}
    specs: list[FormFieldSpec] = []
    used: set[str] = set()
    for lab in labeling.labels:
        c = by_id.get(lab.candidate_id)
        if c is None or lab.key not in CANONICAL_KEYS or lab.key in used:
            continue  # never trust a candidate id or key the detector did not produce
        used.add(lab.key)
        specs.append(
            FormFieldSpec(
                key=lab.key,
                label=lab.label or c.context or lab.key,
                page=c.page,
                rect=[round(v, 2) for v in c.rect],
                field_type=field_type(lab.key),
                write_by_hand=lab.write_by_hand or lab.key in (HAND_KEYS - {"signature_date"}),
            )
        )
    specs.sort(key=lambda s: (s.page, s.rect[1], s.rect[0]))
    return specs


@dataclass
class TemplateBuild:
    """A freshly built template plus how it was made. ``template.verified_by`` stays None until H3."""

    template: FormTemplate
    method: Literal["widgets", "vision", "heuristic", "none"]
    candidates: list[Candidate] = field(default_factory=list)
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    def review_task(self) -> ReviewTask:
        """Human verification task (H3) for this template."""
        sha = self.template.doc_sha256
        return ReviewTask(
            id=f"form_template:{sha[:16]}",
            kind="form_template",
            template_id=sha,
            priority="high" if self.method in {"heuristic", "none"} else "medium",
            notes=f"Verify {len(self.template.field_map)} field placements ({self.method}).",
        )


def fillable_template(pdf: bytes, language: str = "en") -> FormTemplate:
    """Map AcroForm widgets onto canonical keys by field name and label."""
    sha = pdftext.sha256_bytes(pdf)
    widgets: list[tuple[int, pymupdf.Widget]] = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, start=1):
            widgets.extend((page_no, w) for w in page.widgets())
        labels = {i: f"{w.field_name or ''} {w.field_label or ''}" for i, (_, w) in enumerate(widgets)}
        assigned = assign_keys(labels)
        specs = []
        for i, (page_no, w) in enumerate(widgets):
            key = assigned.get(i)
            if key is None:
                continue
            is_box = w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON)
            if is_box != (field_type(key) == "checkbox"):
                continue
            specs.append(
                FormFieldSpec(
                    key=key,
                    label=w.field_label or w.field_name,
                    page=page_no,
                    rect=[round(v, 2) for v in w.rect],
                    field_type=field_type(key),
                    widget_name=w.field_name,
                    write_by_hand=key in (HAND_KEYS - {"signature_date"}),
                )
            )
    return FormTemplate(doc_sha256=sha, kind="fillable", language=language, field_map=specs)


def is_fillable(pdf: bytes) -> bool:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return any(True for page in doc for _ in page.widgets())


def build_template(pdf: bytes, *, language: str = "en", backend: LLMBackend | None = None, use_llm: bool = True) -> TemplateBuild:
    """Build an (unverified) template for a form: widgets, vision labels, or heuristics."""
    sha = pdftext.sha256_bytes(pdf)
    if is_fillable(pdf):
        return TemplateBuild(fillable_template(pdf, language), "widgets")
    cands = detect_candidates(pdf)
    notes: list[str] = []
    labeling: TemplateLabeling | None = None
    method: Literal["vision", "heuristic", "none"] = "heuristic"
    cost = 0.0
    if use_llm and cands:
        try:
            res = (backend or get_backend()).complete(labeling_request(pdf, cands, sha))
            labeling, method, cost = res.output, "vision", res.cost_usd
        except LLMError as e:
            notes.append(f"vision labelling unavailable ({type(e).__name__}); used heuristic labels")
    if labeling is None:
        labeling = heuristic_labels(cands)
    specs = _specs_from_labels(cands, labeling)
    if not specs:
        method = "none"
        notes.append("no fields could be placed; packets will use the answer sheet")
    tpl = FormTemplate(doc_sha256=sha, kind="flat", language=language, field_map=specs)
    return TemplateBuild(tpl, method, cands, cost, notes)


def gold_labeling(cands: list[Candidate], gold: FormTemplate, min_iou: float = 0.5) -> TemplateLabeling:
    """What a correct labeller would answer for these candidates (used to write replay fixtures)."""
    labels = []
    for spec in gold.field_map:
        g = pymupdf.Rect(spec.rect)
        best = max(cands, key=lambda c: iou(c.rect, g) if c.page == spec.page else -1.0, default=None)
        if best is not None and best.page == spec.page and iou(best.rect, g) >= min_iou:
            labels.append(
                CandidateLabel(candidate_id=best.id, key=spec.key, label=spec.label, field_type=spec.field_type, write_by_hand=spec.write_by_hand)
            )
    return TemplateLabeling(labels=labels)


def iou(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = pymupdf.Rect(a) & pymupdf.Rect(b)
    if inter.is_empty:
        return 0.0
    ia = inter.width * inter.height
    return ia / (a.width * a.height + b.width * b.height - ia)


def score_template(pred: FormTemplate, gold: FormTemplate, min_iou: float = 0.5) -> dict[str, float]:
    """Key precision/recall of a predicted template against a gold one (rects must overlap)."""
    gold_by_key = {s.key: s for s in gold.field_map}
    tp = sum(
        1
        for s in pred.field_map
        if s.key in gold_by_key
        and s.page == gold_by_key[s.key].page
        and iou(pymupdf.Rect(s.rect), pymupdf.Rect(gold_by_key[s.key].rect)) >= min_iou
    )
    p = tp / len(pred.field_map) if pred.field_map else 0.0
    r = tp / len(gold.field_map) if gold.field_map else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0}

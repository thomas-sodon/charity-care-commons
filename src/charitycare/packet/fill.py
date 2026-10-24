"""Form filling (design section 8 S12).

* Fillable forms: ``Widget.field_value`` + ``Widget.update()``, then
  ``Document.bake()`` so the values become ordinary page content that cannot
  be edited by accident and that the readback reads like any other text.
* Flat forms: ``page.insert_textbox`` inside the template rectangle; a check
  box gets an "X".

Fields in :data:`~charitycare.packet.keys.HAND_KEYS` or marked
``write_by_hand`` are never filled. A text that does not fit its box is not
written at all (``insert_textbox`` returns a negative remainder), which the
readback then reports as a mismatch rather than shipping a clipped value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pymupdf

from charitycare.packet.keys import HAND_KEYS
from charitycare.packet.models import Applicant
from charitycare.packet.pii import mask_pii
from charitycare.schemas import FormFieldSpec, FormTemplate, Household

DEFAULT_FONTSIZE = 10.0
MIN_FONTSIZE = 6.0
FONT = "helv"
CHECK_MARK = "X"


@dataclass
class FillPlan:
    """What to write where. ``fontsize`` overrides the default per key (used on retry)."""

    values: dict[str, str]
    fontsize: dict[str, float] = field(default_factory=dict)
    skip: set[str] = field(default_factory=set)  # keys withheld after a failed readback

    def intended(self, spec: FormFieldSpec) -> str:
        """The value the readback should find for this field ("" for hand-written ones)."""
        if is_hand(spec) or spec.key in self.skip:
            return ""
        v = self.values.get(spec.key, "")
        if spec.field_type == "checkbox":
            return CHECK_MARK if v else ""
        return v


def is_hand(spec: FormFieldSpec) -> bool:
    return spec.write_by_hand or spec.key in HAND_KEYS or spec.field_type == "signature"


def format_money(amount: Decimal) -> str:
    """``49700`` -> ``49,700``; cents are kept only when non-zero."""
    q = amount.quantize(Decimal("0.01"))
    return f"{q:,.0f}" if q == q.to_integral_value() else f"{q:,.2f}"


def canonical_values(applicant: Applicant, household: Household) -> dict[str, str]:
    """Canonical key -> text for one household. Checkbox values are "X" or ""."""
    vals = {
        "patient_name": applicant.patient_name,
        "address": applicant.address or "",
        "city": applicant.city or "",
        "state": applicant.state or household.state or "",
        "zip": applicant.zip or "",
        "county": applicant.county or household.county or "",
        "phone": applicant.phone or "",
        "employer": applicant.employer or "",
        "household_size": str(household.household_size),
        "annual_household_income": format_money(household.annual_income),
        "insured_yes": CHECK_MARK if household.insured is True else "",
        "insured_no": CHECK_MARK if household.insured is False else "",
        "applied_medicaid": CHECK_MARK if applicant.applied_medicaid else "",
    }
    return {k: mask_pii(v.strip(), k if k in ("phone", "zip") else "text") for k, v in vals.items()}  # type: ignore[arg-type]


def fit_fontsize(text: str, rect: pymupdf.Rect, start: float = DEFAULT_FONTSIZE) -> float:
    """Largest font size <= ``start`` at which ``text`` fits on one line of ``rect``."""
    width = pymupdf.get_text_length(text, fontname=FONT, fontsize=1.0)
    if width <= 0:
        return start
    return max(MIN_FONTSIZE, min(start, (rect.width - 4) / width, rect.height - 3))


def textbox_for(rect: pymupdf.Rect, fontsize: float) -> tuple[pymupdf.Rect, float]:
    """Box for ``insert_textbox`` that centres one line of text vertically in ``rect``.

    PyMuPDF needs about 1.7x the font size of box height for one line, and the
    glyphs occupy about 1.37x from the box top, so the box overhangs ``rect``.
    """
    fs = max(1.0, min(fontsize, (rect.height - 1) / 1.37))
    top = (rect.y0 + rect.y1) / 2 - 0.69 * fs
    return pymupdf.Rect(rect.x0 + 2, top, rect.x1 - 1, top + 1.75 * fs), fs


def fill_fillable(pdf: bytes, template: FormTemplate, plan: FillPlan) -> bytes:
    """Fill AcroForm widgets, update their appearances, and bake the result."""
    by_widget = {s.widget_name: s for s in template.field_map if s.widget_name}
    is_box = (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON)
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for page in doc:
            # Many forms inherit /DA from the AcroForm dictionary, where ``text_fontsize``
            # does not stick, so the per-widget default appearance is written directly.
            for w in page.widgets():
                spec = by_widget.get(w.field_name)
                if spec is not None and w.field_type not in is_box:
                    fs = plan.fontsize.get(spec.key, DEFAULT_FONTSIZE)
                    doc.xref_set_key(w.xref, "DA", f"(0 g /Helv {fs:.2f} Tf)")
            for w in page.widgets():
                spec = by_widget.get(w.field_name)
                if spec is None:
                    continue
                value = plan.intended(spec)
                w.field_value = (w.on_state() if value else "Off") if w.field_type in is_box else value
                w.update()
        doc.bake()
        return doc.tobytes(garbage=3, deflate=True)


def fill_flat(pdf: bytes, template: FormTemplate, plan: FillPlan) -> bytes:
    """Write values onto a flat (or scanned) form with ``insert_textbox``."""
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for spec in template.field_map:
            value = plan.intended(spec)
            if not value:
                continue
            page = doc[spec.page - 1]
            rect = pymupdf.Rect(spec.rect)
            fs = plan.fontsize.get(spec.key, DEFAULT_FONTSIZE)
            box, fs = textbox_for(rect, fs)
            align = pymupdf.TEXT_ALIGN_CENTER if spec.field_type == "checkbox" else pymupdf.TEXT_ALIGN_LEFT
            page.insert_textbox(box, value, fontname=FONT, fontsize=fs, align=align)
        return doc.tobytes(garbage=3, deflate=True)


def fill(pdf: bytes, template: FormTemplate, plan: FillPlan) -> bytes:
    """Dispatch on the template kind."""
    return fill_fillable(pdf, template, plan) if template.kind == "fillable" else fill_flat(pdf, template, plan)

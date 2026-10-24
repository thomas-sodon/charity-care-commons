"""Answer sheet for forms whose template is missing or stale (form hash mismatch).

When the hospital's form no longer matches a verified template, the packet
ships the blank official form plus this generated answer sheet. The sheet has
its own exact template, so it is filled and read back like any other form.
"""

from __future__ import annotations

import pymupdf

from charitycare import pdftext
from charitycare.packet.keys import CANONICAL_FIELDS, CANONICAL_KEYS, HAND_KEYS
from charitycare.schemas import FormFieldSpec, FormTemplate

DEFAULT_LABELS: dict[str, str] = {
    "patient_name": "Patient name",
    "date_of_birth": "Date of birth",
    "ssn": "Social Security number",
    "address": "Street address",
    "city": "City",
    "state": "State",
    "zip": "ZIP code",
    "county": "County",
    "phone": "Phone number",
    "account_number": "Hospital account number",
    "household_size": "Number of people in household",
    "annual_household_income": "Total annual household income ($)",
    "employer": "Employer name",
    "insured_yes": "I have health insurance",
    "insured_no": "I do not have health insurance",
    "applied_medicaid": "I have applied for Medicaid",
    "signature": "Signature of patient or guarantor",
    "signature_date": "Date",
}

_W, _H, _M = 612.0, 792.0, 60.0


def build_answer_sheet(hospital_name: str, stale: FormTemplate | None = None) -> tuple[bytes, FormTemplate]:
    """Blank answer-sheet PDF and its exact template, labelled from ``stale`` when available."""
    labels = dict(DEFAULT_LABELS)
    if stale is not None:
        labels.update({s.key: s.label for s in stale.field_map if s.key in labels})
    doc = pymupdf.open()
    page = doc.new_page(width=_W, height=_H)
    page.insert_text((_M, 64), "Answer sheet: financial assistance application", fontname="hebo", fontsize=14)
    note = (
        f"{hospital_name}'s application form has changed since our template was checked, so the answers below are "
        "printed on this sheet instead of on the form. Copy them onto the enclosed official form, or attach this sheet "
        "to it. Fields marked 'write by hand' are left blank on purpose."
    )
    if page.insert_textbox(pymupdf.Rect(_M, 74, _W - _M, 124), note, fontname="helv", fontsize=9) < 0:
        raise RuntimeError("answer-sheet note does not fit")
    y = 128.0
    specs: list[FormFieldSpec] = []
    for key in CANONICAL_KEYS:
        ftype = CANONICAL_FIELDS[key][0]
        hand = key in HAND_KEYS
        label = labels[key] + (" (write by hand)" if hand else "")
        if ftype == "checkbox":
            rect = pymupdf.Rect(_M, y, _M + 12, y + 12)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((_M + 20, y + 10), label, fontname="helv", fontsize=10)
            y += 22
        else:
            page.insert_text((_M, y + 9), label, fontname="helv", fontsize=8.5)
            rect = pymupdf.Rect(_M, y + 12, _W - _M, y + 28)
            page.draw_line(rect.bl, rect.br, color=(0, 0, 0), width=0.6)
            y += 34
        specs.append(
            FormFieldSpec(key=key, label=labels[key], page=1, rect=[round(v, 2) for v in rect], field_type=ftype, write_by_hand=hand)
        )
    pdf = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    tpl = FormTemplate(doc_sha256=pdftext.sha256_bytes(pdf), kind="flat", language="en", field_map=specs, verified_by="generated:answer-sheet")
    return pdf, tpl

"""Demo application forms (fillable AcroForm, flat, scanned) and plain-language summaries.

Each form is returned together with its ground-truth :class:`FormTemplate`
field map (rects in PDF points, top-left origin), which the packet builder's
template tool and readback tests can be scored against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

from charitycare.demo.fap import join_list, pct
from charitycare.demo.render import MARGIN, PAGE_H, PAGE_W, Block, new_document, rasterize_into, save, write_blocks
from charitycare.demo.spec import FormSpec, HospitalSpec
from charitycare.schemas import FormFieldSpec, FormTemplate


@dataclass(frozen=True)
class Cell:
    key: str
    label_en: str
    label_es: str
    x0: float
    x1: float
    ftype: str = "text"
    write_by_hand: bool = False


R = PAGE_W - MARGIN  # right edge

PAGE1_ROWS: list[list[Cell]] = [
    [Cell("patient_name", "Patient name (first, middle, last)", "Nombre del paciente (nombre, segundo nombre, apellido)", MARGIN, R)],
    [
        Cell("date_of_birth", "Date of birth", "Fecha de nacimiento", MARGIN, 250, "date", True),
        Cell("ssn", "Social Security number", "Número de Seguro Social", 270, R, "text", True),
    ],
    [Cell("address", "Street address", "Dirección", MARGIN, R)],
    [
        Cell("city", "City", "Ciudad", MARGIN, 300),
        Cell("state", "State", "Estado", 320, 400),
        Cell("zip", "ZIP code", "Código postal", 420, R),
    ],
    [Cell("county", "County", "Condado", MARGIN, 300), Cell("phone", "Phone number", "Número de teléfono", 320, R)],
    [Cell("account_number", "Hospital account number", "Número de cuenta del hospital", MARGIN, 300, "text", True)],
]
PAGE1_HOUSEHOLD: list[list[Cell]] = [
    [Cell("household_size", "Number of people in household", "Número de personas en el hogar", MARGIN, 300)],
    [Cell("annual_household_income", "Total annual household income ($)", "Ingreso anual total del hogar ($)", MARGIN, 300, "money")],
    [Cell("employer", "Employer name", "Nombre del empleador", MARGIN, R)],
]
CHECKBOXES: list[Cell] = [
    Cell("insured_yes", "I have health insurance", "Tengo seguro médico", MARGIN, MARGIN + 12, "checkbox"),
    Cell("insured_no", "I do not have health insurance", "No tengo seguro médico", MARGIN, MARGIN + 12, "checkbox"),
    Cell("applied_medicaid", "I have applied for Medicaid", "He solicitado Medicaid", MARGIN, MARGIN + 12, "checkbox"),
]
SIGNATURE_ROW: list[Cell] = [
    Cell("signature", "Signature of patient or guarantor", "Firma del paciente o garante", MARGIN, 350, "signature", True),
    Cell("signature_date", "Date", "Fecha", 370, R, "date"),
]

TEXT = {
    "en": {
        "title": "Financial Assistance Application",
        "intro": "Please complete all sections, sign page 2, and attach the documents listed in Section D. Leave a field blank if it does not apply.",
        "A": "Section A. Patient information",
        "B": "Section B. Household and income",
        "C": "Section C. Insurance",
        "D": "Section D. Documents to attach",
        "E": "Section E. Certification",
        "cert": "I certify that the information in this application is true and complete to the best of my knowledge.",
        "mail": "Return this form to",
    },
    "es": {
        "title": "Solicitud de Asistencia Financiera",
        "intro": "Complete todas las secciones, firme la página 2 y adjunte los documentos de la Sección D. Deje en blanco los campos que no correspondan.",
        "A": "Sección A. Información del paciente",
        "B": "Sección B. Hogar e ingresos",
        "C": "Sección C. Seguro médico",
        "D": "Sección D. Documentos que debe adjuntar",
        "E": "Sección E. Certificación",
        "cert": "Certifico que la información de esta solicitud es verdadera y completa según mi leal saber y entender.",
        "mail": "Envíe este formulario a",
    },
}


def widget_name(cell: Cell, style: str) -> str:
    words = re.findall(r"[A-Za-z]+", cell.label_en.split("(")[0])
    if style == "spaced":
        return " ".join(words)
    camel = words[0].lower() + "".join(x.capitalize() for x in words[1:])
    if style == "camel":
        return camel
    prefix = "chk" if cell.ftype == "checkbox" else "txt"
    return prefix + "".join(x.capitalize() for x in words)


class _FormPainter:
    """Draws one form page and records field specs."""

    def __init__(self, page: pymupdf.Page, lang: str, form: FormSpec, specs: list[FormFieldSpec], page_no: int):
        self.page, self.lang, self.form, self.specs, self.page_no = page, lang, form, specs, page_no
        self.y = MARGIN

    def text(self, text: str, style: str = "p") -> None:
        self.y = write_blocks(self.page, [Block(style, text)], top=self.y)  # type: ignore[arg-type]

    def row(self, cells: list[Cell]) -> None:
        for c in cells:
            label = c.label_en if self.lang == "en" else c.label_es
            self.page.insert_text((c.x0, self.y + 9), label, fontname="helv", fontsize=8.5)
            rect = pymupdf.Rect(c.x0, self.y + 12, c.x1, self.y + 28)
            self._field(c, label, rect)
        self.y += 38

    def checkbox(self, c: Cell) -> None:
        label = c.label_en if self.lang == "en" else c.label_es
        rect = pymupdf.Rect(c.x0, self.y, c.x0 + 12, self.y + 12)
        self.page.insert_text((c.x0 + 20, self.y + 10), label, fontname="helv", fontsize=10)
        self._field(c, label, rect)
        self.y += 22

    def _field(self, c: Cell, label: str, rect: pymupdf.Rect) -> None:
        name = None
        if self.form.kind == "fillable" and not self.form.scanned:
            name = widget_name(c, self.form.widget_style)
            w = pymupdf.Widget()
            w.field_name = name
            w.field_label = label
            w.rect = rect
            if c.ftype == "checkbox":
                w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
            else:
                w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
                w.text_fontsize = 10
                w.text_font = "Helv"
            w.border_color = (0.4, 0.4, 0.4)
            w.border_width = 0.6
            self.page.add_widget(w)
        elif c.ftype == "checkbox":
            self.page.draw_rect(rect, color=(0, 0, 0), width=0.8)
        else:
            self.page.draw_line(rect.bl, rect.br, color=(0, 0, 0), width=0.6)
        self.specs.append(
            FormFieldSpec(
                key=c.key,
                label=label,
                page=self.page_no,
                rect=[round(v, 2) for v in (rect.x0, rect.y0, rect.x1, rect.y1)],
                field_type=c.ftype,  # type: ignore[arg-type]
                widget_name=name,
                write_by_hand=c.write_by_hand,
            )
        )


def _doc_lines(h: HospitalSpec) -> list[str]:
    return [f"[ ] {d.text[0].upper()}{d.text[1:]}" for d in h.required_docs]


def build_application(h: HospitalSpec, form: FormSpec) -> tuple[bytes, list[FormFieldSpec]]:
    """Render one application form; returns PDF bytes and the ground-truth field map."""
    t = TEXT[form.language]
    doc = new_document(f"{h.name} - {t['title']}")
    specs: list[FormFieldSpec] = []

    def page1(p: pymupdf.Page) -> None:
        fp = _FormPainter(p, form.language, form, specs, 1)
        fp.text(h.system_name or h.name, "title")
        fp.text(t["title"], "subtitle")
        fp.text(t["intro"])
        fp.text(t["A"], "h")
        for row in PAGE1_ROWS:
            fp.row(row)
        fp.text(t["B"], "h")
        for row in PAGE1_HOUSEHOLD:
            fp.row(row)

    def page2(p: pymupdf.Page) -> None:
        fp = _FormPainter(p, form.language, form, specs, 2)
        fp.text(t["C"], "h")
        for c in CHECKBOXES:
            fp.checkbox(c)
        fp.text(t["D"], "h")
        fp.text("\n".join(_doc_lines(h)))
        fp.text(t["E"], "h")
        fp.text(t["cert"])
        fp.row(SIGNATURE_ROW)
        fp.text(f"{t['mail']}: {h.mail_address}. Fax: {h.fax}.", "small")

    for draw in (page1, page2):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        if form.scanned:
            rasterize_into(page, draw, dpi=120)
        else:
            draw(page)
    return save(doc), specs


def application_template(sha: str, form: FormSpec, specs: list[FormFieldSpec]) -> FormTemplate:
    return FormTemplate(
        doc_sha256=sha,
        kind="fillable" if (form.kind == "fillable" and not form.scanned) else "flat",
        language=form.language,
        field_map=specs,
        verified_by="gold:synthetic",
    )


def build_pls(h: HospitalSpec, lang: str) -> bytes:
    """One-page plain-language summary."""
    free = next(t for t in h.tiers if t.benefit == "free")
    top = max(t.hi for t in h.tiers)
    if lang == "en":
        blocks = [
            Block("title", h.system_name or h.name),
            Block("subtitle", "Plain Language Summary of our Financial Assistance Policy"),
            Block("p", f"You may get free care if your household income is at or below {pct(free.hi)} of the Federal Poverty Guidelines, and discounted care up to {pct(top)}."),
            Block("p", f"Apply within {h.window_days} days after your first bill after you leave the hospital. Applying is free."),
            Block("p", "To apply, send the application with: " + join_list([d.text for d in h.required_docs]) + "."),
            Block("p", f"Get a free copy of the policy, application and this summary at {h.base_url}/financial-assistance, by calling {h.phone}, or by mail."),
            Block("p", "If you qualify, you will not be charged more than amounts generally billed to insured patients for emergency or other medically necessary care."),
        ]
        title = "Plain Language Summary"
    else:
        blocks = [
            Block("title", h.system_name or h.name),
            Block("subtitle", "Resumen en lenguaje sencillo de nuestra Política de Asistencia Financiera"),
            Block("p", f"Puede recibir atención gratuita si el ingreso de su hogar es igual o menor al {pct(free.hi)} de las Pautas Federales de Pobreza, y atención con descuento hasta el {pct(top)}."),
            Block("p", f"Presente su solicitud dentro de los {h.window_days} días posteriores a su primera factura después de salir del hospital. La solicitud es gratuita."),
            Block("p", f"Obtenga una copia gratuita de la política, la solicitud y este resumen en {h.base_url}/financial-assistance o llamando al {h.phone}."),
        ]
        title = "Resumen en lenguaje sencillo"
    doc = new_document(f"{h.name} - {title}")
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    write_blocks(page, blocks)
    return save(doc)


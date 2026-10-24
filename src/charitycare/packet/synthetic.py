"""Synthetic bills and pay stubs for the public demo, plus the packet stage's replay fixtures.

Each document is laid out with PyMuPDF (so every value's bounding box is
known exactly), rasterized, then given phone-photo augmentation (slight skew,
blur, glare, noise) and fake camera EXIF so the stripping path is exercised.
Everything is seeded and deterministic. The ground truth doubles as the
synthetic replay fixture for ``extract_bill`` / ``extract_income``.

Also writes ``form_template`` fixtures for the demo corpus's flat forms: the
labels a correct vision model would give the detected candidate blanks.
"""

from __future__ import annotations

import io
import json
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pymupdf
from PIL import Image, ImageFilter

from charitycare.config import DEFAULT_MODELS, PROMPT_VERSIONS
from charitycare.llm import Usage, fixture_path, write_fixture
from charitycare.packet.extract import BillFields, IncomeDoc, PayFrequency, XField
from charitycare.packet.pii import image_sha256, strip_exif
from charitycare.packet.template import TEMPLATE_PROMPT_VERSION, detect_candidates, gold_labeling
from charitycare.schemas import FormTemplate

DPI = 110


@dataclass(frozen=True)
class BillSpec:
    provider: str = "Example Methodist Hospital"
    statement_date: date = date(2026, 10, 10)
    service_date: date = date(2026, 9, 18)
    amount_due: str = "9,400.00"
    account: str = "HA-2026-118842"
    patient: str = "Maria Example"


@dataclass(frozen=True)
class StubSpec:
    employer: str = "Example Grocery Co."
    employee: str = "Maria Example"
    period_start: date = date(2026, 9, 14)
    period_end: date = date(2026, 9, 27)
    pay_date: date = date(2026, 10, 2)
    frequency: str = "biweekly"
    gross: str = "1,911.54"
    ytd: str = "36,319.26"
    net: str = "1,602.17"


@dataclass
class SyntheticDoc:
    name: str
    kind: str  # bill | pay_stub
    image: bytes  # JPEG with fake EXIF, as a phone would upload it
    truth: BillFields | IncomeDoc


def _layout(lines: list[tuple[str, str | None, str]], title: str) -> tuple[pymupdf.Document, dict[str, pymupdf.Rect]]:
    """Draw label/value rows; returns the document and each field's value rect."""
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=300)
    page.insert_text((24, 36), title, fontname="hebo", fontsize=14)
    rects: dict[str, pymupdf.Rect] = {}
    y = 64.0
    for label, key, value in lines:
        page.insert_text((24, y), label, fontname="helv", fontsize=9)
        page.insert_text((200, y), value, fontname="cour" if key else "helv", fontsize=10)
        if key:
            w = pymupdf.get_text_length(value, fontname="cour", fontsize=10)
            rects[key] = pymupdf.Rect(198, y - 10, 202 + w, y + 3)
        y += 20
    page.draw_rect(pymupdf.Rect(16, 16, 404, y), color=(0.5, 0.5, 0.5), width=0.5)
    return doc, rects


def _photo(doc: pymupdf.Document, seed: int) -> tuple[bytes, tuple[int, int]]:
    """Rasterize with phone-photo augmentation; returns JPEG bytes with fake EXIF and the size."""
    rnd = random.Random(seed)
    pix = doc[0].get_pixmap(dpi=DPI)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    im = im.rotate(rnd.uniform(-0.8, 0.8), resample=Image.BICUBIC, fillcolor=(255, 255, 255))
    im = im.filter(ImageFilter.GaussianBlur(rnd.uniform(0.3, 0.7)))
    arr = np.asarray(im).astype(np.float32)
    w = arr.shape[1]
    gx = np.linspace(0, 1, w)[None, :, None]
    arr = arr * (0.9 + 0.1 * gx) + 18 * np.exp(-(((np.arange(w)[None, :, None] - w * 0.7) / (w * 0.2)) ** 2))  # glare
    arr += np.random.default_rng(seed).normal(0, 4, arr.shape)
    im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    exif = Image.Exif()
    exif[0x010F] = "ExamplePhone"  # Make
    exif[0x0110] = "Model X"  # Model
    exif[0x0132] = "2026:10:20 09:15:00"  # DateTime
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88, exif=exif.tobytes())
    return buf.getvalue(), im.size


def _bbox(rect: pymupdf.Rect, page_rect: pymupdf.Rect) -> list[float]:
    return [round(rect.x0 / page_rect.width, 4), round(rect.y0 / page_rect.height, 4), round(rect.x1 / page_rect.width, 4), round(rect.y1 / page_rect.height, 4)]


def make_bill(spec: BillSpec | None = None, seed: int = 7, name: str = "bill") -> SyntheticDoc:
    spec = spec or BillSpec()
    lines = [
        ("Provider", "provider_name", spec.provider),
        ("Patient", None, spec.patient),
        ("Account number", "account_last4", spec.account),
        ("Statement date", "statement_date", spec.statement_date.strftime("%m/%d/%Y")),
        ("Date of service", "service_date", spec.service_date.strftime("%m/%d/%Y")),
        ("Amount due", "amount_due", "$" + spec.amount_due),
        ("Financial assistance may be available. Ask us.", None, ""),
    ]
    doc, rects = _layout(lines, "Patient Statement")
    pr = doc[0].rect
    jpeg, _ = _photo(doc, seed)
    truth = BillFields(
        provider_name=XField[str](value=spec.provider, confidence=0.97, bbox=_bbox(rects["provider_name"], pr)),
        statement_date=XField[date](value=spec.statement_date, confidence=0.95, bbox=_bbox(rects["statement_date"], pr)),
        service_date=XField[date](value=spec.service_date, confidence=0.93, bbox=_bbox(rects["service_date"], pr)),
        amount_due=XField[str](value=spec.amount_due, confidence=0.94, bbox=_bbox(rects["amount_due"], pr)),
        account_last4=XField[str](value=spec.account, confidence=0.88, bbox=_bbox(rects["account_last4"], pr)),
    )
    return SyntheticDoc(name, "bill", jpeg, truth)


def make_pay_stub(spec: StubSpec | None = None, seed: int = 11, name: str = "pay_stub") -> SyntheticDoc:
    spec = spec or StubSpec()
    lines = [
        ("Employer", "employer_name", spec.employer),
        ("Employee", None, spec.employee),
        ("SSN", None, "XXX-XX-1234"),
        ("Pay period", "period", f"{spec.period_start:%m/%d/%Y} - {spec.period_end:%m/%d/%Y}"),
        ("Pay date", "pay_date", spec.pay_date.strftime("%m/%d/%Y")),
        ("Pay frequency", "pay_frequency", spec.frequency.capitalize()),
        ("Gross pay (current)", "gross_pay_current", spec.gross),
        ("Gross pay (year to date)", "gross_pay_ytd", spec.ytd),
        ("Net pay", "net_pay_current", spec.net),
    ]
    doc, rects = _layout(lines, "Earnings Statement")
    pr = doc[0].rect
    jpeg, _ = _photo(doc, seed)
    period = rects["period"]
    truth = IncomeDoc(
        employer_name=XField[str](value=spec.employer, confidence=0.96, bbox=_bbox(rects["employer_name"], pr)),
        pay_date=XField[date](value=spec.pay_date, confidence=0.95, bbox=_bbox(rects["pay_date"], pr)),
        period_start=XField[date](value=spec.period_start, confidence=0.92, bbox=_bbox(period, pr)),
        period_end=XField[date](value=spec.period_end, confidence=0.92, bbox=_bbox(period, pr)),
        pay_frequency=XField[PayFrequency](value=spec.frequency, confidence=0.9, bbox=_bbox(rects["pay_frequency"], pr)),  # type: ignore[arg-type]
        gross_pay_current=XField[str](value=spec.gross, confidence=0.95, bbox=_bbox(rects["gross_pay_current"], pr)),
        gross_pay_ytd=XField[str](value=spec.ytd, confidence=0.93, bbox=_bbox(rects["gross_pay_ytd"], pr)),
        net_pay_current=XField[str](value=spec.net, confidence=0.94, bbox=_bbox(rects["net_pay_current"], pr)),
    )
    return SyntheticDoc(name, "pay_stub", jpeg, truth)


def demo_documents() -> list[SyntheticDoc]:
    """One bill and two biweekly pay stubs: $1,911.54 x 26 = $49,700.04 a year (Screen 3 demo)."""
    stub2 = StubSpec(period_start=date(2026, 9, 28), period_end=date(2026, 10, 11), pay_date=date(2026, 10, 16), ytd="38,230.80")
    return [make_bill(), make_pay_stub(), make_pay_stub(stub2, seed=12, name="pay_stub_2")]


DEMO_HARDSHIP = "My hours at work were cut in August and I now have a $9,400 hospital bill."


def synthetic_input_shas() -> list[str]:
    """Hashes of the synthetic demo inputs to PII-bearing prompts (stripped images, demo hardship notes)."""
    from charitycare import pdftext
    from charitycare.packet.pii import mask_pii

    shas = [image_sha256(strip_exif(d.image)) for d in demo_documents()]
    shas.append(pdftext.sha256_bytes(mask_pii(DEMO_HARDSHIP).strip().encode("utf-8")))
    return shas


def _fixture_output(doc: SyntheticDoc) -> dict[str, Any]:
    out = doc.truth.model_dump(mode="json")
    if doc.kind == "bill":
        out["account_last4"]["value"] = out["account_last4"]["value"][-4:]  # what the prompt asks the model for
    return out


def write_extraction_fixtures(root: Path, docs: list[SyntheticDoc], tier: str = "sonnet") -> list[Path]:
    """Synthetic replay fixtures for the documents, keyed by the stripped image's pixel hash."""
    paths = []
    for doc in docs:
        pv = PROMPT_VERSIONS["extract_bill" if doc.kind == "bill" else "extract_income"]
        key = {"prompt_version": pv, "doc_sha256": image_sha256(strip_exif(doc.image)), "model_tier": tier, "variant": ""}
        paths.append(
            write_fixture(
                root, key, _fixture_output(doc), model=DEFAULT_MODELS[tier], usage=Usage(input_tokens=2400, output_tokens=600),  # type: ignore[index]
                synthetic=True, notes=f"synthetic {doc.kind} '{doc.name}' ground truth (packet/synthetic.py)",
            )
        )
    return paths


def write_template_fixtures(demo_root: Path, fixtures_root: Path) -> list[Path]:
    """``form_template`` replay fixtures for every flat demo form, derived from the gold templates."""
    paths: list[Path] = []
    for gold_file in sorted((demo_root / "gold").glob("*.json")):
        gold = json.loads(gold_file.read_text(encoding="utf-8"))
        manifest_docs = {k: v for k, v in gold["documents"].items() if "form_template" in v}
        for name, d in manifest_docs.items():
            tpl = FormTemplate.model_validate(d["form_template"])
            if tpl.kind != "flat":
                continue
            pdf = (demo_root / "docs" / gold["hospital_id"] / f"{name}.pdf").read_bytes()
            cands = detect_candidates(pdf)
            labeling = gold_labeling(cands, tpl)
            key = {"prompt_version": TEMPLATE_PROMPT_VERSION, "doc_sha256": tpl.doc_sha256, "model_tier": "opus", "variant": ""}
            paths.append(
                write_fixture(
                    fixtures_root, key, labeling.model_dump(mode="json"), model=DEFAULT_MODELS["opus"],
                    usage=Usage(input_tokens=13000, output_tokens=4000), synthetic=True,
                    notes=f"gold labels for {gold['hospital_id']}/{name} candidates (packet/synthetic.py)",
                )
            )
    return paths


def save_demo_documents(out_dir: Path, docs: list[SyntheticDoc]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for d in docs:
        p = out_dir / f"{d.name}.jpg"
        p.write_bytes(d.image)
        paths.append(p)
    return paths


__all__ = [
    "BillSpec",
    "StubSpec",
    "SyntheticDoc",
    "demo_documents",
    "fixture_path",
    "make_bill",
    "make_pay_stub",
    "save_demo_documents",
    "write_extraction_fixtures",
    "write_template_fixtures",
]

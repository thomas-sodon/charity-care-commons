"""Bill and pay-stub extraction (design section 8 S10).

Images are EXIF-stripped and kept in memory. A Sonnet-class vision call (a
Haiku-class tier can be passed for pay stubs) returns each field with a
confidence and a bounding box that the navigator confirms. Under replay, the
fixture key is the image's pixel hash. If no backend can answer, the result is
an empty extraction marked for manual entry: values are never guessed.

Code, not the model, annualizes income (:func:`annualize_income`).
"""

from __future__ import annotations

import calendar
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Generic, Literal, TypeVar

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict
from pydantic import Field as PField

from charitycare.config import PROMPT_VERSIONS
from charitycare.llm import DocumentPart, LLMBackend, LLMError, LLMRequest, get_backend
from charitycare.packet.pii import image_sha256, last4, mask_pii, strip_exif
from charitycare.schemas import ModelTier

T = TypeVar("T")
CONFIRM_BELOW = 0.9  # fields under this confidence are highlighted for confirmation


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class XField(_M, Generic[T]):
    """One extracted value with confidence and a normalized bounding box [x0, y0, x1, y1] in 0..1."""

    value: T | None = None
    confidence: float = PField(default=0.0, description="0..1")
    bbox: list[float] | None = PField(default=None, description="[x0, y0, x1, y1] as fractions of image width/height")

    @classmethod
    def model_parametrized_name(cls, params: tuple[type[Any], ...]) -> str:
        return "XField_" + "_".join(getattr(p, "__name__", "Literal") for p in params)


PayFrequency = Literal["weekly", "biweekly", "semimonthly", "monthly", "unknown"]


class BillFields(_M):
    """Fields read from a hospital bill. Amounts are strings as printed."""

    provider_name: XField[str] = PField(default_factory=XField[str])
    statement_date: XField[date] = PField(default_factory=XField[date])
    service_date: XField[date] = PField(default_factory=XField[date])
    amount_due: XField[str] = PField(default_factory=XField[str], description='As printed, e.g. "9,400.00"')
    account_last4: XField[str] = PField(default_factory=XField[str], description="Last four digits only")


class IncomeDoc(_M):
    """Fields read from one pay stub. Amounts are strings as printed."""

    employer_name: XField[str] = PField(default_factory=XField[str])
    pay_date: XField[date] = PField(default_factory=XField[date])
    period_start: XField[date] = PField(default_factory=XField[date])
    period_end: XField[date] = PField(default_factory=XField[date])
    pay_frequency: XField[PayFrequency] = PField(default_factory=XField[PayFrequency])
    gross_pay_current: XField[str] = PField(default_factory=XField[str], description="Gross pay this period, as printed")
    gross_pay_ytd: XField[str] = PField(default_factory=XField[str], description="Year-to-date gross, as printed")
    net_pay_current: XField[str] = PField(default_factory=XField[str])


BILL_SYSTEM = (
    "You read a photo or scan of a US hospital billing statement and return the requested fields. For each field "
    "give the value exactly as printed, a confidence between 0 and 1, and a bounding box as fractions of the image "
    "width and height. Leave a field null when it is not visible; never guess. Return only the last four digits of "
    "any account number. Ignore Social Security numbers and dates of birth."
)
INCOME_SYSTEM = (
    "You read a photo or scan of a US pay stub and return the requested fields. For each field give the value exactly "
    "as printed, a confidence between 0 and 1, and a bounding box as fractions of the image width and height. Leave a "
    "field null when it is not visible; never guess or compute. Pay frequency is 'unknown' unless printed. Ignore "
    "Social Security numbers, employee IDs and bank account numbers."
)


@dataclass
class Extraction:
    """Result of one document extraction; ``fields`` is BillFields or IncomeDoc."""

    kind: Literal["bill", "pay_stub"]
    fields: BillFields | IncomeDoc
    image: bytes = field(repr=False)  # EXIF-stripped PNG, in memory only
    image_sha256: str = ""
    source: Literal["live", "replay", "record", "manual"] = "manual"
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)
    confirmed: set[str] = field(default_factory=set)  # fields a navigator confirmed or edited

    def needs_confirmation(self) -> list[str]:
        """Field names the navigator must still confirm or fill in (low confidence or missing, not yet confirmed)."""
        out = []
        for name in type(self.fields).model_fields:
            f: XField[Any] = getattr(self.fields, name)
            if name not in self.confirmed and (f.value is None or f.confidence < CONFIRM_BELOW):
                out.append(name)
        return out

    def confirm(self, values: dict[str, str | None], confirmed: set[str] | frozenset[str] = frozenset()) -> "Extraction":
        """Copy with navigator input applied.

        ``values`` maps field names to the text the navigator left in the box (empty
        means "not on the document"); a value that differs from the extraction counts as
        an edit. Names in ``confirmed`` were ticked as checked against the image. Edited
        and ticked fields get confidence 1.0 and leave :meth:`needs_confirmation`.
        Raises ``ValueError`` for an unparseable date.
        """
        data = self.fields.model_dump()
        done = set(self.confirmed)
        for name, raw in values.items():
            if name not in data:
                continue
            text = (raw or "").strip()
            new: Any = None
            if text:
                new = date.fromisoformat(text) if _is_date_field(type(self.fields), name) else mask_pii(text)
            old = data[name]["value"]
            if new != old:
                data[name] = {"value": new, "confidence": 1.0, "bbox": data[name]["bbox"]}
                done.add(name)
        for name in confirmed:
            if name in data:
                data[name]["confidence"] = 1.0
                done.add(name)
        fields = _sanitize(type(self.fields).model_validate(data))
        return Extraction(self.kind, fields, self.image, self.image_sha256, self.source, self.cost_usd, list(self.notes), done)


class ConfirmationRequired(ValueError):
    """Extracted fields still need a navigator's confirmation before they can be used."""

    def __init__(self, pending: dict[str, list[str]]):
        self.pending = pending
        super().__init__("confirm these extracted fields first: "
                         + "; ".join(f"{doc}: {', '.join(names)}" for doc, names in pending.items()))


def require_confirmed(extractions: dict[str, Extraction], *, accept_extracted: bool = False) -> None:
    """Raise :class:`ConfirmationRequired` while any extraction has unconfirmed low-confidence or missing fields.

    ``accept_extracted`` is an explicit opt-out for synthetic demo documents only.
    """
    if accept_extracted:
        return
    pending = {k: ex.needs_confirmation() for k, ex in extractions.items() if ex.needs_confirmation()}
    if pending:
        raise ConfirmationRequired(pending)


def _is_date_field(model: type[BaseModel], name: str) -> bool:
    ann = model.model_fields[name].annotation
    return "date" in getattr(ann, "__name__", "").lower()


def parse_money(text: str | None) -> Decimal | None:
    """'$9,400.00' -> Decimal('9400.00'); None when unparseable."""
    if not text:
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", text.replace(" ", ""))
    if not m:
        return None
    try:
        return Decimal(m.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def _sanitize(fields: BillFields | IncomeDoc) -> BillFields | IncomeDoc:
    """Apply the PII filter to every string value; reduce account numbers to the last four digits."""
    data = fields.model_dump()
    for f in data.values():
        if isinstance(f.get("value"), str):
            f["value"] = mask_pii(f["value"])
    if isinstance(fields, BillFields) and data["account_last4"]["value"]:
        data["account_last4"]["value"] = (last4(data["account_last4"]["value"]) or "").lstrip("*") or None
    return type(fields).model_validate(data)


def _request(kind: str, png: bytes, sha: str, tier: ModelTier) -> LLMRequest[Any]:
    if kind == "bill":
        return LLMRequest(
            prompt_version=PROMPT_VERSIONS["extract_bill"],
            model_tier=tier,
            doc_sha256=sha,
            system=BILL_SYSTEM,
            documents=(DocumentPart("image", png, media_type="image/png"),),
            user="Extract the billing statement fields.",
            output_schema=BillFields,
            max_tokens=2000,
        )
    return LLMRequest(
        prompt_version=PROMPT_VERSIONS["extract_income"],
        model_tier=tier,
        doc_sha256=sha,
        system=INCOME_SYSTEM,
        documents=(DocumentPart("image", png, media_type="image/png"),),
        user="Extract the pay stub fields.",
        output_schema=IncomeDoc,
        max_tokens=2000,
    )


def _extract(kind: Literal["bill", "pay_stub"], image: bytes, tier: ModelTier, backend: LLMBackend | None) -> Extraction:
    png = strip_exif(image)
    sha = image_sha256(png)
    empty: BillFields | IncomeDoc = BillFields() if kind == "bill" else IncomeDoc()
    try:
        res = (backend or get_backend()).complete(_request(kind, png, sha, tier))
    except LLMError as e:
        return Extraction(kind, empty, png, sha, "manual", 0.0, [f"automatic extraction unavailable ({type(e).__name__}); enter the values by hand"])
    return Extraction(kind, _sanitize(res.output), png, sha, res.backend, res.cost_usd)


def extract_bill(image: bytes, *, tier: ModelTier = "sonnet", backend: LLMBackend | None = None) -> Extraction:
    """Read a hospital bill image (JPEG/PNG bytes)."""
    return _extract("bill", image, tier, backend)


def extract_pay_stub(image: bytes, *, tier: ModelTier = "sonnet", backend: LLMBackend | None = None) -> Extraction:
    """Read a pay-stub image (JPEG/PNG bytes)."""
    return _extract("pay_stub", image, tier, backend)


# --------------------------------------------------------------------------
# Income arithmetic (code, not model)
# --------------------------------------------------------------------------

PERIODS_PER_YEAR: dict[str, int] = {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}


def infer_frequency(stub: IncomeDoc) -> str:
    """Printed frequency, else inferred from the pay-period length."""
    if stub.pay_frequency.value and stub.pay_frequency.value != "unknown":
        return stub.pay_frequency.value
    s, e = stub.period_start.value, stub.period_end.value
    if s and e:
        days = (e - s).days + 1
        if 6 <= days <= 8:
            return "weekly"
        last = calendar.monthrange(e.year, e.month)[1]
        if (s.day == 1 and e.day == 15 and s.month == e.month) or (s.day == 16 and e.day == last and s.month == e.month):
            return "semimonthly"
        if 13 <= days <= 15:
            return "biweekly"
        if 28 <= days <= 31:
            return "monthly"
    return "unknown"


@dataclass
class AnnualIncome:
    amount: Decimal | None
    arithmetic: list[str]
    unresolved: list[str]


def annualize_income(stubs: list[IncomeDoc]) -> AnnualIncome:
    """Annual gross income from pay stubs: per employer, mean gross per period x periods per year."""
    by_employer: dict[str, list[tuple[Decimal, str]]] = {}
    unresolved: list[str] = []
    for i, stub in enumerate(stubs, start=1):
        gross = parse_money(stub.gross_pay_current.value)
        freq = infer_frequency(stub)
        if gross is None or freq == "unknown":
            unresolved.append(f"pay stub {i}: {'gross pay' if gross is None else 'pay frequency'} missing")
            continue
        by_employer.setdefault((stub.employer_name.value or f"employer {i}").strip(), []).append((gross, freq))
    total = Decimal(0)
    lines: list[str] = []
    for employer, rows in by_employer.items():
        freq = rows[0][1]
        if any(f != freq for _, f in rows):
            unresolved.append(f"{employer}: pay stubs disagree on pay frequency")
            continue
        mean = sum((g for g, _ in rows), Decimal(0)) / len(rows)
        annual = (mean * PERIODS_PER_YEAR[freq]).quantize(Decimal("0.01"))
        total += annual
        lines.append(f"{employer}: ${mean:,.2f} {freq} x {PERIODS_PER_YEAR[freq]} = ${annual:,.2f}")
    return AnnualIncome(total if lines else None, lines, unresolved)


def confirmation_overlay(ex: Extraction) -> bytes:
    """PNG of the document with each field's bounding box and value drawn, for navigator confirmation."""
    with Image.open(io.BytesIO(ex.image)) as im:
        im = im.convert("RGB")
        draw = ImageDraw.Draw(im)
        w, h = im.size
        for name in type(ex.fields).model_fields:
            f: XField[Any] = getattr(ex.fields, name)
            if not f.bbox or len(f.bbox) != 4:
                continue
            x0, y0, x1, y1 = f.bbox
            low = f.confidence < CONFIRM_BELOW
            color = (220, 120, 0) if low else (0, 140, 60)
            draw.rectangle([x0 * w, y0 * h, x1 * w, y1 * h], outline=color, width=3)
            draw.text((x0 * w, max(0, y0 * h - 12)), f"{name} {f.confidence:.2f}", fill=color)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

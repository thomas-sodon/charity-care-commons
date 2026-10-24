"""Bill and pay-stub extraction (replay), PII filtering, income arithmetic, synthetic documents."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from PIL import Image

from charitycare.packet.extract import (
    BillFields,
    IncomeDoc,
    XField,
    annualize_income,
    confirmation_overlay,
    extract_bill,
    extract_pay_stub,
    infer_frequency,
    parse_money,
)
from charitycare.packet.pii import has_metadata
from charitycare.packet.synthetic import StubSpec, demo_documents, make_pay_stub


def test_synthetic_documents_are_deterministic() -> None:
    a, b = demo_documents(), demo_documents()
    assert [d.image for d in a] == [d.image for d in b]
    assert all(has_metadata(d.image) for d in a)  # they arrive like phone photos


def test_extract_bill_from_replay(replay) -> None:
    bill = demo_documents()[0]
    ex = extract_bill(bill.image, backend=replay)
    assert ex.source == "replay" and ex.cost_usd > 0
    f = ex.fields
    assert isinstance(f, BillFields)
    assert f.amount_due.value == "9,400.00" and parse_money(f.amount_due.value) == Decimal("9400.00")
    assert f.statement_date.value == date(2026, 10, 10)
    assert f.account_last4.value == "8842"
    assert ex.needs_confirmation() == ["account_last4"]  # confidence 0.88 < 0.9
    assert not has_metadata(ex.image)
    assert all(0 <= v <= 1 for v in f.amount_due.bbox or [])


def test_extract_pay_stubs_and_annualize(replay) -> None:
    stubs = [extract_pay_stub(d.image, backend=replay) for d in demo_documents()[1:]]
    assert all(s.source == "replay" for s in stubs)
    income = annualize_income([s.fields for s in stubs])  # type: ignore[misc]
    assert income.amount == Decimal("49700.04")
    assert income.arithmetic == ["Example Grocery Co.: $1,911.54 biweekly x 26 = $49,700.04"]


def test_missing_fixture_means_manual_entry(empty_replay) -> None:
    ex = extract_pay_stub(make_pay_stub(StubSpec(gross="9.99"), seed=99).image, backend=empty_replay)
    assert ex.source == "manual" and ex.fields == IncomeDoc()
    assert "enter the values by hand" in ex.notes[0]
    assert len(ex.needs_confirmation()) == len(IncomeDoc.model_fields)


def test_model_output_is_pii_filtered(kit) -> None:
    def answer(req):
        return BillFields(
            provider_name=XField[str](value="Example Hospital SSN 123-45-6789", confidence=0.9),
            account_last4=XField[str](value="HA-2026-118842", confidence=0.9),
        )

    ex = extract_bill(demo_documents()[0].image, backend=kit.FakeBackend(answer))
    assert ex.fields.provider_name.value == "Example Hospital SSN [SSN removed]"
    assert ex.fields.account_last4.value == "8842"


def test_request_shape(kit) -> None:
    backend = kit.FakeBackend(lambda req: IncomeDoc())
    extract_pay_stub(demo_documents()[1].image, tier="haiku", backend=backend)
    req = backend.requests[0]
    assert req.prompt_version == "extract_income@v1" and req.model_tier == "haiku"
    assert req.documents[0].kind == "image" and req.documents[0].media_type == "image/png"
    assert b"ExamplePhone" not in req.documents[0].data  # EXIF never reaches the model


def _stub(gross: str | None, freq: str | None = None, start: date | None = None, end: date | None = None, employer: str = "A") -> IncomeDoc:
    return IncomeDoc(
        employer_name=XField[str](value=employer),
        gross_pay_current=XField[str](value=gross),
        pay_frequency=XField(value=freq),  # type: ignore[arg-type]
        period_start=XField[date](value=start),
        period_end=XField[date](value=end),
    )


def test_infer_frequency() -> None:
    assert infer_frequency(_stub("1", start=date(2026, 9, 1), end=date(2026, 9, 7))) == "weekly"
    assert infer_frequency(_stub("1", start=date(2026, 9, 14), end=date(2026, 9, 27))) == "biweekly"
    assert infer_frequency(_stub("1", start=date(2026, 9, 1), end=date(2026, 9, 15))) == "semimonthly"
    assert infer_frequency(_stub("1", start=date(2026, 9, 16), end=date(2026, 9, 30))) == "semimonthly"
    assert infer_frequency(_stub("1", start=date(2026, 9, 1), end=date(2026, 9, 30))) == "monthly"
    assert infer_frequency(_stub("1")) == "unknown"


def test_annualize_multiple_employers_and_problems() -> None:
    res = annualize_income([_stub("1,000.00", "monthly", employer="A"), _stub("$500", "weekly", employer="B")])
    assert res.amount == Decimal("38000.00") and len(res.arithmetic) == 2
    res = annualize_income([_stub(None, "monthly"), _stub("100", None)])
    assert res.amount is None and len(res.unresolved) == 2
    res = annualize_income([_stub("100", "weekly"), _stub("100", "monthly")])
    assert res.amount is None and "disagree" in res.unresolved[0]


def test_parse_money() -> None:
    assert parse_money("$1,911.54") == Decimal("1911.54")
    assert parse_money("n/a") is None and parse_money(None) is None


def test_confirmation_overlay(replay) -> None:
    ex = extract_bill(demo_documents()[0].image, backend=replay)
    png = confirmation_overlay(ex)
    with Image.open(io.BytesIO(png)) as im, Image.open(io.BytesIO(ex.image)) as base:
        assert im.size == base.size
        assert im.tobytes() != base.convert("RGB").tobytes()

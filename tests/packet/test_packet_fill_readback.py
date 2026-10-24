"""Fill + render + readback: 100% match on the demo forms, retry, withholding, tamper detection."""

from __future__ import annotations

from decimal import Decimal

import pymupdf
import pytest

from charitycare.packet.fill import FillPlan, canonical_values, fill, fit_fontsize, format_money
from charitycare.packet.readback import CropReading, _Readers, fill_and_verify, readback


def _plan(kit, **overrides) -> FillPlan:
    vals = canonical_values(kit.APPLICANT, kit.household())
    vals.update(overrides)
    return FillPlan(vals)


def test_canonical_values(kit) -> None:
    v = canonical_values(kit.APPLICANT, kit.household(insured=True))
    assert v["annual_household_income"] == "49,700"
    assert v["household_size"] == "3"
    assert v["insured_yes"] == "X" and v["insured_no"] == ""
    assert v["applied_medicaid"] == "X"
    assert not {"ssn", "date_of_birth", "account_number"} & set(v)


def test_format_money() -> None:
    assert format_money(Decimal("49700")) == "49,700"
    assert format_money(Decimal("49700.04")) == "49,700.04"


def test_every_demo_form_reads_back_exactly(demo_forms, kit) -> None:
    for f in demo_forms:
        out = fill_and_verify(f.pdf, f.gold, _plan(kit))
        assert out.all_filled_match, (f.hospital, f.doc, [x for x in out.result.fields if not x.match])
        assert out.result.retries == 0
        filled = {x.key: x for x in out.result.fields}
        assert filled["patient_name"].read == "María José Example-Gómez"
        assert filled["insured_no"].read == "X" and filled["insured_yes"].read == ""
        for key in ("ssn", "date_of_birth", "account_number", "signature", "signature_date"):
            assert filled[key].write_by_hand and filled[key].intended == "" and filled[key].read == ""


def test_fillable_form_is_baked(form_by_id, kit) -> None:
    f = form_by_id("example-methodist")
    out = fill_and_verify(f.pdf, f.gold, _plan(kit))
    with pymupdf.open(stream=out.pdf, filetype="pdf") as d:
        assert not any(True for p in d for _ in p.widgets())
        assert "Example Grocery Co." in d[0].get_text()


@pytest.mark.parametrize("hospital", ["lone-star-health", "example-methodist"])
def test_overflow_is_retried_with_a_smaller_font(form_by_id, kit, hospital) -> None:
    f = form_by_id(hospital)
    out = fill_and_verify(f.pdf, f.gold, _plan(kit, state="Texas, United States"))  # does not fit 80pt at 10pt
    assert out.result.retries == 1
    assert out.all_filled_match
    assert {x.key: x.read for x in out.result.fields}["state"] == "Texas, United States"


def test_unfittable_value_is_withheld_and_marked_write_by_hand(form_by_id, kit) -> None:
    f = form_by_id("lone-star-health")
    huge = "Example County Regional Water District Number Seven Annex Building"
    out = fill_and_verify(f.pdf, f.gold, _plan(kit, state=huge))
    assert out.withheld == ["state"]
    assert not out.all_filled_match
    st = {x.key: x for x in out.result.fields}["state"]
    assert st.write_by_hand and not st.match and st.intended == huge
    with pymupdf.open(stream=out.pdf, filetype="pdf") as d:  # the failed value is not shipped
        assert "Water District" not in d[0].get_text()
    # the schema's all_match ignores write-by-hand fields; the packet must use all_filled_match
    assert out.result.all_match is True


def test_readback_catches_a_wrong_value(form_by_id, kit) -> None:
    f = form_by_id("hill-country-community")
    filled = fill(f.pdf, f.gold, _plan(kit, zip="78228"))
    fields = readback(filled, f.pdf, f.gold, _plan(kit), _Readers(vision=False, backend=None))
    bad = [x for x in fields if not x.match]
    assert [(x.key, x.intended, x.read) for x in bad] == [("zip", "78229", "78228")]


def test_readback_catches_invisible_text(form_by_id, kit) -> None:
    """The text layer says the value is there but nothing is visible: pixels disagree, so no match."""
    f = form_by_id("lone-star-health")
    spec = next(s for s in f.gold.field_map if s.key == "city")
    with pymupdf.open(stream=fill(f.pdf, f.gold, _plan(kit, city="")), filetype="pdf") as d:
        r = pymupdf.Rect(spec.rect)
        d[0].insert_textbox(pymupdf.Rect(r.x0 + 2, r.y0 - 1, r.x1, r.y1 + 6), "San Antonio", fontsize=10, render_mode=3)
        tampered = d.tobytes()
    fields = {x.key: x for x in readback(tampered, f.pdf, f.gold, _plan(kit), _Readers(False, None))}
    assert not fields["city"].match


def test_readback_detects_unchecked_box(form_by_id, kit) -> None:
    f = form_by_id("valley-childrens", "application_es")
    filled = fill(f.pdf, f.gold, _plan(kit, insured_no=""))
    fields = {x.key: x for x in readback(filled, f.pdf, f.gold, _plan(kit), _Readers(False, None))}
    assert not fields["insured_no"].match and fields["insured_no"].read == ""


def test_vision_crop_reader(form_by_id, kit) -> None:
    f = form_by_id("lone-star-health")
    wrong = kit.FakeBackend(lambda req: CropReading(text="Sam Antonio", confidence=0.6))
    out = fill_and_verify(f.pdf, f.gold, _plan(kit), vision=True, backend=wrong)
    assert "city" in out.withheld  # an independent visual read disagreed, twice
    assert all(r.model_tier == "haiku" and r.prompt_version == "readback_crop@v1" for r in wrong.requests)
    assert out.cost_usd > 0

    def echo(req):
        key = req.variant
        return CropReading(text=_plan(kit).values[key], confidence=0.99)

    right = kit.FakeBackend(echo)
    assert fill_and_verify(f.pdf, f.gold, _plan(kit), vision=True, backend=right).all_filled_match


def test_vision_unavailable_falls_back_to_other_readers(form_by_id, kit, empty_replay) -> None:
    f = form_by_id("lone-star-health")
    assert fill_and_verify(f.pdf, f.gold, _plan(kit), vision=True, backend=empty_replay).all_filled_match


def test_fit_fontsize() -> None:
    r = pymupdf.Rect(0, 0, 80, 16)
    assert fit_fontsize("TX", r) == 10
    assert 6 <= fit_fontsize("Texas, United States", r) < 10
    assert fit_fontsize("x" * 200, r) == 6

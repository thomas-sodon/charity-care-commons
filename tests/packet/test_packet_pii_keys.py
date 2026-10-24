"""PII masking, EXIF stripping, image hashing and canonical-key matching."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from charitycare.packet.keys import CANONICAL_KEYS, assign_keys, best_key, normalize_label
from charitycare.packet.models import Applicant
from charitycare.packet.pii import contains_pii, has_metadata, image_sha256, last4, mask_pii, strip_exif


@pytest.mark.parametrize(
    "text, expected",
    [
        ("SSN 123-45-6789", "SSN [SSN removed]"),
        ("ssn 123 45 6789.", "ssn [SSN removed]."),
        ("id 123456789", "id [SSN removed]"),
        ("acct # HA-2026-118842 due", "acct # ****8842 due"),
        ("Account No.: 00991234", "Account No.: ****1234"),
        ("card 12345678901", "card ****8901"),
    ],
)
def test_mask_pii_masks(text: str, expected: str) -> None:
    assert mask_pii(text) == expected
    assert contains_pii(text)


@pytest.mark.parametrize(
    "text",
    ["call (210) 555-0142", "ZIP 78229-1234", "Policy rev 2026", "Account number on your statement", "$49,700.00", "2026-10-24"],
)
def test_mask_pii_leaves_ordinary_text(text: str) -> None:
    assert mask_pii(text) == text
    assert not contains_pii(text)


def test_last4() -> None:
    assert last4("HA-2026-118842") == "****8842"
    assert last4(None) is None


def test_applicant_masks_and_has_no_ssn_field() -> None:
    a = Applicant(patient_name="Ann Example", employer="Acme 123-45-6789")
    assert a.employer == "Acme [SSN removed]"
    with pytest.raises(ValueError):
        Applicant(patient_name="x", ssn="123-45-6789")  # type: ignore[call-arg]


def _jpeg_with_exif(orientation: int | None = None) -> bytes:
    im = Image.new("RGB", (40, 20), (255, 255, 255))
    im.putpixel((0, 0), (0, 0, 0))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[0x0132] = "2026:10:20 09:15:00"
    if orientation:
        exif[0x0112] = orientation
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes(), quality=95)
    return buf.getvalue()


def test_strip_exif_removes_metadata_and_applies_orientation() -> None:
    raw = _jpeg_with_exif(orientation=6)  # rotate 90 degrees on display
    assert has_metadata(raw)
    clean = strip_exif(raw)
    assert not has_metadata(clean)
    with Image.open(io.BytesIO(clean)) as im:
        assert im.format == "PNG"
        assert im.size == (20, 40)  # orientation applied before the tag was dropped
    assert b"PhoneMaker" not in clean


def test_strip_exif_downscales_large_images() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (5000, 1000), (200, 200, 200)).save(buf, format="PNG")
    with Image.open(io.BytesIO(strip_exif(buf.getvalue(), max_side=1000))) as im:
        assert max(im.size) == 1000


def test_image_sha256_is_encoder_independent() -> None:
    im = Image.new("RGB", (30, 30), (10, 120, 200))
    a, b = io.BytesIO(), io.BytesIO()
    im.save(a, format="PNG", compress_level=1)
    im.save(b, format="PNG", compress_level=9)
    assert a.getvalue() != b.getvalue()
    assert image_sha256(a.getvalue()) == image_sha256(b.getvalue())


@pytest.mark.parametrize(
    "label, key",
    [
        ("Patient name (first, middle, last)", "patient_name"),
        ("patientName", "patient_name"),
        ("txtPatientName", "patient_name"),
        ("Date of birth", "date_of_birth"),
        ("Fecha de nacimiento", "date_of_birth"),
        ("Número de Seguro Social", "ssn"),
        ("chkIDoNotHaveHealthInsurance", "insured_no"),
        ("No tengo seguro médico", "insured_no"),
        ("Tengo seguro médico", "insured_yes"),
        ("Total annual household income ($)", "annual_household_income"),
        ("Número de personas en el hogar", "household_size"),
        ("Date", "signature_date"),
        ("Código postal", "zip"),
    ],
)
def test_best_key(label: str, key: str) -> None:
    assert best_key(label)[0] == key


def test_assign_keys_uses_each_key_once() -> None:
    labels = {0: "Date of birth", 1: "Date", 2: "Signature date", 3: "Favourite colour"}
    out = assign_keys(labels)
    assert out[0] == "date_of_birth"
    assert sorted(out.values()) == sorted(set(out.values()))
    assert 3 not in out


def test_normalize_label() -> None:
    assert normalize_label("chkIHaveHealthInsurance") == "i have health insurance"
    assert normalize_label("Dirección") == "direccion"
    assert set(CANONICAL_KEYS) >= {"patient_name", "ssn", "signature"}


def test_mask_pii_unicode_dashes_and_form_fields() -> None:
    from charitycare.packet.models import Applicant
    from charitycare.packet.pii import SSN_MASK, mask_pii

    assert mask_pii("SSN 123–45–6789") == f"SSN {SSN_MASK}"
    assert mask_pii("123 45 6789") == SSN_MASK
    assert mask_pii("5125551234", "phone") == "5125551234" and mask_pii("787011234", "zip") == "787011234"
    assert mask_pii("123-45-6789", "phone") == SSN_MASK  # not a phone shape: still masked
    a = Applicant(patient_name="Maria Example", phone="(512) 555-1234", zip="78701-1234")
    assert a.phone == "(512) 555-1234" and a.zip == "78701-1234"

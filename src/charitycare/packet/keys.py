"""Canonical application-form keys and the heuristic label matcher.

Every hospital form is mapped onto the same small vocabulary of canonical keys
(``patient_name``, ``household_size`` ...). The matcher scores a printed label or
an AcroForm widget name against English and Spanish synonyms; the longest
matching synonym wins, so "Date of birth" maps to ``date_of_birth`` rather than
``signature_date``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

FieldType = Literal["text", "checkbox", "date", "money", "signature"]

# key -> (field type, synonyms). Synonyms are accent-free and lower case.
CANONICAL_FIELDS: dict[str, tuple[FieldType, tuple[str, ...]]] = {
    "patient_name": ("text", ("patient name", "name of patient", "applicant name", "full name", "nombre del paciente")),
    "date_of_birth": ("date", ("date of birth", "birth date", "dob", "birthdate", "fecha de nacimiento")),
    "ssn": ("text", ("social security", "ssn", "seguro social")),
    "address": ("text", ("street address", "mailing address", "home address", "address", "direccion", "domicilio")),
    "city": ("text", ("city", "ciudad")),
    "state": ("text", ("state", "estado")),
    "zip": ("text", ("zip code", "zip", "postal code", "codigo postal")),
    "county": ("text", ("county", "condado")),
    "phone": ("text", ("phone number", "telephone", "phone", "numero de telefono", "telefono")),
    "account_number": ("text", ("account number", "account no", "acct", "numero de cuenta")),
    "household_size": (
        "text",
        ("number of people in household", "people in household", "household size", "family size", "personas en el hogar"),
    ),
    "annual_household_income": (
        "money",
        ("annual household income", "annual income", "household income", "total income", "ingreso anual"),
    ),
    "employer": ("text", ("employer name", "employer", "nombre del empleador", "empleador")),
    "insured_yes": ("checkbox", ("i have health insurance", "tengo seguro medico", "insured yes")),
    "insured_no": (
        "checkbox",
        ("i do not have health insurance", "i have no health insurance", "no tengo seguro medico", "insured no"),
    ),
    "applied_medicaid": ("checkbox", ("applied for medicaid", "solicitado medicaid", "medicaid applied")),
    "signature": ("signature", ("signature", "firma")),
    "signature_date": ("date", ("date", "fecha")),
}

CANONICAL_KEYS: tuple[str, ...] = tuple(CANONICAL_FIELDS)

# Never filled by the compiler (section 11): the patient writes these by hand.
# The signature date is written at signing so it matches the signature.
HAND_KEYS: frozenset[str] = frozenset({"ssn", "date_of_birth", "account_number", "signature", "signature_date"})

_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_PREFIX = re.compile(r"^(txt|chk|cb|fld|tb|field)\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_label(text: str) -> str:
    """Lower-case, accent-free, camelCase split, widget prefixes stripped."""
    t = _CAMEL.sub(" ", text or "")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = _NON_ALNUM.sub(" ", t).strip()
    return _PREFIX.sub("", t)


def score_label(text: str, key: str) -> int:
    """Length of the longest synonym of ``key`` found as whole words in ``text`` (0 if none)."""
    hay = f" {normalize_label(text)} "
    return max((len(s) for s in CANONICAL_FIELDS[key][1] if f" {s} " in hay), default=0)


def best_key(text: str) -> tuple[str | None, int]:
    """Best canonical key for one label, with its score."""
    scored = sorted(((score_label(text, k), k) for k in CANONICAL_KEYS), reverse=True)
    score, key = scored[0]
    return (key, score) if score > 0 else (None, 0)


def assign_keys(labels: dict[int, str]) -> dict[int, str]:
    """Assign each canonical key to at most one label id, highest score first."""
    pairs = sorted(
        ((score_label(text, k), i, k) for i, text in labels.items() for k in CANONICAL_KEYS),
        key=lambda p: (-p[0], p[1]),
    )
    out: dict[int, str] = {}
    used: set[str] = set()
    for score, i, k in pairs:
        if score == 0 or i in out or k in used:
            continue
        out[i] = k
        used.add(k)
    return out


def field_type(key: str) -> FieldType:
    return CANONICAL_FIELDS[key][0]

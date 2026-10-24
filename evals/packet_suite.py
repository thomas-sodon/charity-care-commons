"""Packet suite units: form readback per demo application form, plus document extraction.

Uses a fictional applicant only (no real household data). Form units carry
field counts so scorers report field-level readback; a form counts as a
*verified* packet only when every filled field read back exactly.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

SYNTHETIC_APPLICANT = {
    "patient_name": "Maria Example",
    "address": "123 Example Lane",
    "city": "San Antonio",
    "state": "TX",
    "zip": "78229",
    "county": "Bexar",
    "phone": "(210) 555-0142",
    "employer": "Example Grocery Co.",
    "applied_medicaid": True,
}


def _forms(demo_dir: Path) -> list[tuple[str, str, bytes, dict[str, Any]]]:
    out = []
    for gf in sorted((demo_dir / "gold").glob("*.json")):
        g = json.loads(gf.read_text(encoding="utf-8"))
        for name, d in sorted(g["documents"].items()):
            if "form_template" in d:
                out.append((g["hospital_id"], name, (demo_dir / "docs" / g["hospital_id"] / f"{name}.pdf").read_bytes(), d["form_template"]))
    return out


def form_units(demo_dir: Path, *, backend: Any = None, build_templates: bool = True) -> list[dict[str, Any]]:
    from charitycare.packet import Applicant
    from charitycare.packet.fill import FillPlan, canonical_values
    from charitycare.packet.readback import fill_and_verify
    from charitycare.schemas import FormTemplate, Household

    applicant = Applicant(**SYNTHETIC_APPLICANT)
    household = Household(household_size=3, annual_income=Decimal("49700"), insured=False, county="Bexar",
                          first_statement_date=date(2026, 10, 10), application_date=date(2026, 10, 24))
    units = []
    for hospital, name, pdf, tpl_json in _forms(demo_dir):
        gold = FormTemplate.model_validate(tpl_json)
        out = fill_and_verify(pdf, gold, FillPlan(canonical_values(applicant, household)), backend=backend)
        filled = [f for f in out.result.fields if not f.write_by_hand or f.key in out.withheld]
        unit: dict[str, Any] = {
            "type": "form",
            "id": f"{hospital}/{name}",
            "form_kind": gold.kind,
            "n_fields": len(filled),
            "n_match": sum(1 for f in filled if f.match),
            "retries": out.result.retries,
            "withheld": list(out.withheld),
            "verified": bool(out.all_filled_match),
            "cost_usd": float(out.cost_usd),
            "template_f1": None,
        }
        if build_templates:
            from charitycare.packet.template import build_template, score_template

            try:
                built = build_template(pdf, language=gold.language, backend=backend).template
                unit["template_f1"] = float(score_template(built, gold)["f1"])
            except Exception:  # noqa: BLE001 - no template fixture: report the gap, not a crash
                unit["template_f1"] = 0.0
        units.append(unit)
    return units


def _same(a: Any, b: Any) -> bool:
    return (str(a).strip().casefold() if a is not None else None) == (str(b).strip().casefold() if b is not None else None)


def extraction_units(*, backend: Any = None) -> list[dict[str, Any]]:
    """Bill and pay-stub extraction vs the synthetic documents' ground truth (account: last 4 only)."""
    from charitycare.llm import LLMError
    from charitycare.packet import extract_bill, extract_pay_stub
    from charitycare.packet.synthetic import demo_documents

    units = []
    for doc in demo_documents():
        fn = extract_bill if doc.kind == "bill" else extract_pay_stub
        try:
            ex = fn(doc.image, backend=backend)
        except LLMError:
            ex = None
        names = list(type(doc.truth).model_fields)
        n_match = 0
        for nm in names:
            want = getattr(doc.truth, nm).value
            if nm == "account_last4" and want is not None:
                want = str(want)[-4:]
            got = getattr(ex.fields, nm).value if ex is not None else None
            n_match += int(_same(want, got))
        units.append({"type": "extraction", "id": doc.name, "kind": doc.kind, "n_fields": len(names), "n_match": n_match,
                      "cost_usd": float(ex.cost_usd) if ex else 0.0, "doc_set": 0})
    return units


def packet_units(demo_dir: Path, *, backend: Any = None) -> list[dict[str, Any]]:
    return form_units(demo_dir, backend=backend) + extraction_units(backend=backend)

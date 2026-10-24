"""Shared fixtures for the packet compiler tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from charitycare.llm import LLMRequest, LLMResult, ReplayBackend, Usage
from charitycare.packet import Applicant, PacketRequest
from charitycare.packet.synthetic import demo_documents, write_extraction_fixtures, write_template_fixtures
from charitycare.schemas import CompiledPolicy, Determination, FormTemplate, Household


@dataclass(frozen=True)
class DemoForm:
    hospital: str
    doc: str
    pdf: bytes
    gold: FormTemplate
    gold_json: dict[str, Any]

    @property
    def kind(self) -> str:
        return self.gold.kind

    @property
    def policy(self) -> CompiledPolicy:
        return CompiledPolicy.model_validate(self.gold_json["policy"])


def load_forms(root: Path) -> list[DemoForm]:
    out = []
    for gf in sorted((root / "gold").glob("*.json")):
        g = json.loads(gf.read_text(encoding="utf-8"))
        for name, d in g["documents"].items():
            if "form_template" in d:
                pdf = (root / "docs" / g["hospital_id"] / f"{name}.pdf").read_bytes()
                out.append(DemoForm(g["hospital_id"], name, pdf, FormTemplate.model_validate(d["form_template"]), g))
    return out


@pytest.fixture(scope="session")
def demo_forms(demo_corpus) -> list[DemoForm]:
    return load_forms(demo_corpus.root)


@pytest.fixture(scope="session")
def form_by_id(demo_forms) -> Callable[[str, str], DemoForm]:
    idx = {(f.hospital, f.doc): f for f in demo_forms}
    return lambda hospital, doc="application_en": idx[(hospital, doc)]


@pytest.fixture(scope="session")
def packet_fixtures(demo_corpus, tmp_path_factory) -> Path:
    """The packet stage's replay fixtures, generated into a private directory."""
    root = tmp_path_factory.mktemp("packet-fixtures")
    write_template_fixtures(demo_corpus.root, root)
    write_extraction_fixtures(root, demo_documents())
    return root


@pytest.fixture(scope="session")
def replay(packet_fixtures) -> ReplayBackend:
    return ReplayBackend(packet_fixtures)


@pytest.fixture
def empty_replay(tmp_path) -> ReplayBackend:
    return ReplayBackend(tmp_path / "no-fixtures")


class FakeBackend:
    """Answers every request with ``answer(request)`` (a model instance or dict)."""

    name = "fake"

    def __init__(self, answer: Callable[[LLMRequest[Any]], Any]):
        from charitycare.llm import CostLedger

        self.answer = answer
        self.requests: list[LLMRequest[Any]] = []
        self.ledger = CostLedger(10**9, 1e9)

    def complete(self, request: LLMRequest[Any]) -> LLMResult[Any]:
        self.requests.append(request)
        out = self.answer(request)
        model = out if not isinstance(out, dict) else request.output_schema.model_validate(out)
        return LLMResult(model, model.model_dump(mode="json"), Usage(100, 10), 0.001, "fake-model", "live")


@pytest.fixture
def fake_backend() -> type[FakeBackend]:
    return FakeBackend


APPLICANT = Applicant(
    patient_name="María José Example-Gómez",
    address="123 Example Lane Apt 4",
    city="San Antonio",
    state="TX",
    zip="78229",
    county="Bexar",
    phone="(210) 555-0142",
    employer="Example Grocery Co.",
    applied_medicaid=True,
)


def household(**kw: Any) -> Household:
    base: dict[str, Any] = dict(
        household_size=3,
        annual_income=Decimal("49700"),
        insured=False,
        county="Bexar",
        first_statement_date=date(2026, 10, 10),
        application_date=date(2026, 10, 24),
    )
    base.update(kw)
    return Household(**base)


def determination(**kw: Any) -> Determination:
    base: dict[str, Any] = dict(
        facility_id="tx-459901",
        policy_version_id="pv-example-methodist-2026",
        fpg_year=2026,
        household_size=3,
        annual_income=Decimal("49700"),
        pct_fpl=Decimal("181.92"),
        outcome="likely_free",
        arithmetic="$49,700 / $27,320 = 181.9% FPL",
        unevaluated_conditions=["asset test: needs 2 months of bank statements"],
        federal_deadline=date(2027, 6, 7),
        disclaimer_version="disclaimer@2026-10-24",
    )
    base.update(kw)
    return Determination(**base)


def packet_request(form: DemoForm, *, template: FormTemplate | None | str = "gold", **kw: Any) -> PacketRequest:
    tpl = form.gold.model_copy(update={"verified_by": "reviewer@example.org"}) if template == "gold" else template
    base: dict[str, Any] = dict(
        applicant=APPLICANT,
        household=household(),
        determination=determination(),
        hospital_name=form.gold_json["name"],
        policy=form.policy,
        policy_as_of=date(2026, 10, 1),
        form_pdf=form.pdf,
        template=tpl,
    )
    base.update(kw)
    return PacketRequest(**base)


class Kit:
    """Builders for tests (conftest helpers cannot be imported across test dirs)."""

    APPLICANT = APPLICANT
    FakeBackend = FakeBackend
    household = staticmethod(household)
    determination = staticmethod(determination)
    packet_request = staticmethod(packet_request)


@pytest.fixture(scope="session")
def kit() -> type[Kit]:
    return Kit

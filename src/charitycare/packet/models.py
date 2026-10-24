"""Packet-local, in-memory models. None of these is ever persisted (section 11).

The shared contracts (``Determination``, ``Household``, ``FormTemplate``,
``ReadbackResult``, ``PacketMetric``) come from :mod:`charitycare.schemas`.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator
from pydantic import Field as PField

from charitycare.packet.pii import mask_pii
from charitycare.schemas import CompiledPolicy, Determination, FormTemplate, Household, PacketMetric, ReadbackResult, ReviewTask


class Applicant(BaseModel):
    """Contact facts for the form. Deliberately has no SSN, DOB or account-number field."""

    model_config = ConfigDict(extra="forbid")

    patient_name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    county: str | None = None
    phone: str | None = None
    employer: str | None = None
    applied_medicaid: bool = False

    @field_validator("*", mode="before")
    @classmethod
    def _mask(cls, v: object, info: ValidationInfo) -> object:
        kind = info.field_name if info.field_name in ("phone", "zip") else "text"
        return mask_pii(v, kind) if isinstance(v, str) else v  # type: ignore[arg-type]


class PacketRequest(BaseModel):
    """Everything one packet needs. Lives in request memory only."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    applicant: Applicant
    household: Household
    determination: Determination
    hospital_name: str
    facility_name: str | None = None
    policy: CompiledPolicy | None = None
    policy_as_of: date | None = None
    form_pdf: bytes = PField(repr=False)
    template: FormTemplate | None = None
    hardship_circumstances: str | None = PField(default=None, repr=False, description="Navigator's notes for the optional hardship letter")
    language: str = "en"

    @field_validator("hardship_circumstances", mode="before")
    @classmethod
    def _mask(cls, v: object) -> object:
        return mask_pii(v) if isinstance(v, str) else v


class ChecklistItem(BaseModel):
    text: str
    citation: str | None = None  # e.g. '§6.2, p. 5: "two months of bank statements ..."'
    kind: str = "document"  # document | action | condition


class PacketResult(BaseModel):
    """The compiled packet, returned to the navigator and then discarded."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    pdf: bytes = PField(repr=False)
    ics: bytes = PField(repr=False)
    readback: ReadbackResult
    verified: bool = PField(description="True only when the template is human-verified and every filled field read back exactly")
    template_verified: bool
    form_mode: str = PField(description='"filled" or "answer_sheet" (form hash mismatch or no template)')
    write_by_hand: list[str] = PField(default_factory=list)
    checklist: list[ChecklistItem] = PField(default_factory=list)
    cover_letter: str = PField(default="", repr=False)
    hardship_letter: str | None = PField(default=None, repr=False)
    review_tasks: list[ReviewTask] = PField(default_factory=list)
    notes: list[str] = PField(default_factory=list)
    page_count: int = 0
    metric: PacketMetric

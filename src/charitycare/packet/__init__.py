"""Packet compiler: extraction, form templates, fill + readback, letters, checklist, ``.ics``.

Typical use::

    from charitycare.packet import Applicant, PacketRequest, compile_packet
    result = compile_packet(PacketRequest(...))
    result.pdf, result.ics, result.readback, result.verified

Nothing here persists household data; see :mod:`charitycare.packet.compiler`.
"""

from charitycare.packet.compiler import compile_packet, purge_stale
from charitycare.packet.extract import (
    AnnualIncome,
    BillFields,
    ConfirmationRequired,
    Extraction,
    IncomeDoc,
    XField,
    annualize_income,
    confirmation_overlay,
    extract_bill,
    extract_pay_stub,
    require_confirmed,
)
from charitycare.packet.models import Applicant, ChecklistItem, PacketRequest, PacketResult
from charitycare.packet.pii import mask_pii, strip_exif
from charitycare.packet.readback import fill_and_verify
from charitycare.packet.template import TemplateBuild, build_template, score_template

__all__ = [
    "AnnualIncome",
    "Applicant",
    "BillFields",
    "ChecklistItem",
    "ConfirmationRequired",
    "Extraction",
    "IncomeDoc",
    "PacketRequest",
    "PacketResult",
    "TemplateBuild",
    "XField",
    "annualize_income",
    "build_template",
    "compile_packet",
    "confirmation_overlay",
    "extract_bill",
    "extract_pay_stub",
    "require_confirmed",
    "fill_and_verify",
    "mask_pii",
    "purge_stale",
    "score_template",
    "strip_exif",
]

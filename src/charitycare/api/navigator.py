"""Navigator packet compiler page (design section 7, Screen 3) and ``POST /packet``.

Optional step 0 reads a bill and pay stubs (``POST /navigator/extract``): images are
EXIF-stripped and held in memory, extracted, and shown with a bounding-box overlay
and editable fields. ``POST /navigator/confirm`` refuses until every low-confidence
or missing field is ticked as checked or edited, then annualizes income in code and
pre-fills step 1. Step 1 collects household facts, step 2 shows the deterministic determination
from :mod:`charitycare.engine`, step 3 returns the packet PDF built by
:mod:`charitycare.packet` (imported lazily: both are separate stages). Facts
live only in the request; responses are ``Cache-Control: no-store`` and the
only thing the packet stage may persist is a ``PacketMetric``.
"""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from charitycare import pdftext
from charitycare.api import queries
from charitycare.api.app import determination_payload, run_screen
from charitycare.config import DISCLAIMER_TEXT, DISCLAIMER_VERSION
from charitycare.schemas import CompiledPolicy, Determination, Household, Organization
from charitycare.store import Registry

HOUSEHOLD_FIELDS = ("facility_id", "household_size", "annual_income", "liquid_assets", "county", "insured",
                    "first_statement_date", "service_date", "emergency_care")
APPLICANT_FIELDS = ("patient_name", "address", "city", "zip", "phone")
NO_STORE = {"Cache-Control": "no-store"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_STUBS = 6


class PacketUnavailable(RuntimeError):
    """The packet stage is not installed or cannot build a packet for this facility."""


def load_packet() -> tuple[Any, Any, Any]:
    """``(compile_packet, Applicant, PacketRequest)`` from :mod:`charitycare.packet` (lazy)."""
    from charitycare.packet.compiler import compile_packet
    from charitycare.packet.models import Applicant, PacketRequest

    return compile_packet, Applicant, PacketRequest


def _dec(v: str | None, name: str, required: bool = False) -> Decimal | None:
    if v is None or not str(v).strip():
        if required:
            raise ValueError(f"{name} is required")
        return None
    try:
        d = Decimal(str(v).replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a number") from exc
    if d < 0:
        raise ValueError(f"{name} cannot be negative")
    return d


def _date(v: str | None) -> date | None:
    return date.fromisoformat(v) if v and v.strip() else None


def household_from_form(form: dict[str, str]) -> Household:
    """Build a :class:`Household` from the step 1 form (raises ValueError on bad input)."""
    size = form.get("household_size", "").strip()
    if not size.isdigit() or int(size) < 1:
        raise ValueError("household size must be a whole number of at least 1")
    insured = {"yes": True, "no": False}.get(form.get("insured", ""), None)
    return Household(
        household_size=int(size),
        annual_income=_dec(form.get("annual_income"), "annual income", required=True),  # type: ignore[arg-type]
        liquid_assets=_dec(form.get("liquid_assets"), "liquid assets"),
        county=(form.get("county") or "").strip() or None,
        insured=insured,
        emergency_care=bool(form.get("emergency_care")),
        service_date=_date(form.get("service_date")),
        first_statement_date=_date(form.get("first_statement_date")),
    )


def _policy(reg: Registry, version_id: str) -> CompiledPolicy | None:
    data = {r.key: r.field.model_dump(mode="json") for r in reg.fields_for_policy(version_id)}
    try:
        return CompiledPolicy.model_validate(data)
    except ValueError:
        return None


def application_form(reg: Registry, version_id: str, language: str = "en") -> tuple[bytes, Any, str]:
    """The hospital's application PDF, its form template (or None) and its language."""
    pv = reg.get_policy_version(version_id)
    if pv is None:
        raise PacketUnavailable("no policy on file")
    names = sorted(n for n in pv.doc_shas if n.startswith("application"))
    # doc_shas keys are "application_en" (demo seed) or "application:en" (discover stage).
    name = next((n for n in names if n.endswith((f"_{language}", f":{language}"))), names[0] if names else None)
    if name is None:
        raise PacketUnavailable("no application form on file for this policy")
    doc = reg.get_document_version(pv.doc_shas[name])
    path = pdftext.uri_to_path(doc.pdf_uri) if doc is not None else None
    if doc is None or path is None or not path.is_file():
        raise PacketUnavailable("the application form PDF is not on file")
    return path.read_bytes(), reg.get_form_template(doc.sha256), doc.language or language


def build_packet(reg: Registry, facility_id: str, household: Household, applicant: dict[str, str | None],
                 det: Determination, today: date) -> Any:
    """Compile a packet with the packet stage; returns its ``PacketResult``."""
    try:
        compile_packet, Applicant, PacketRequest = load_packet()
    except ImportError as exc:
        raise PacketUnavailable("the packet compiler is not installed") from exc
    fac = reg.get_facility(facility_id)
    pv = reg.get_policy_version(det.policy_version_id) if det.policy_version_id else None
    if fac is None or pv is None:
        raise PacketUnavailable("no governing policy on file for this facility")
    org = reg.get(Organization, ein=fac.ein) if fac.ein else None
    form_pdf, template, lang = application_form(reg, pv.id)
    req = PacketRequest(
        applicant=Applicant(patient_name=applicant.get("patient_name") or "", address=applicant.get("address"),
                            city=applicant.get("city"), state="TX", zip=applicant.get("zip"),
                            county=household.county, phone=applicant.get("phone")),
        household=household, determination=det, hospital_name=(org.name if org else fac.name), facility_name=fac.name,
        policy=_policy(reg, pv.id), policy_as_of=pv.as_of, form_pdf=form_pdf, template=template, language=lang,
    )
    return compile_packet(req, today=today, registry=reg)


def _ctx(request: Request, reg: Registry, form: dict[str, str], result: dict[str, Any] | None) -> dict[str, Any]:
    return {"request": request, "section": "navigator", "facilities": queries.list_facilities(reg, None),
            "form": form, "result": result, "disclaimer": DISCLAIMER_TEXT.format(as_of="the date shown"),
            "disclaimer_version": DISCLAIMER_VERSION}


def _reg(request: Request):  # type: ignore[no-untyped-def]
    with request.app.state.lock:
        yield request.app.state.registry


async def _form(request: Request) -> dict[str, str]:
    data = await request.form()
    return {k: str(v) for k, v in data.items() if k in HOUSEHOLD_FIELDS + APPLICANT_FIELDS}


# --------------------------------------------------------------------------
# Step 0: bill and pay-stub extraction with confirmation (nothing stored)
# --------------------------------------------------------------------------


def _doc_view(key: str, label: str, ex: Any) -> dict[str, Any]:
    from charitycare.packet.extract import CONFIRM_BELOW, confirmation_overlay

    pending = set(ex.needs_confirmation())
    rows = []
    for name in type(ex.fields).model_fields:
        f = getattr(ex.fields, name)
        rows.append({"name": name, "input": f"{key}__{name}", "value": "" if f.value is None else str(f.value),
                     "confidence": f.confidence, "low": f.confidence < CONFIRM_BELOW, "pending": name in pending,
                     "label": name.replace("_", " ")})
    overlay = base64.b64encode(confirmation_overlay(ex)).decode()
    return {"key": key, "label": label, "rows": rows, "overlay": overlay, "notes": ex.notes, "source": ex.source,
            "image": base64.b64encode(ex.image).decode()}


def extraction_view(bill: Any, stubs: list[Any]) -> list[dict[str, Any]]:
    docs = [_doc_view("bill", "Hospital bill", bill)] if bill is not None else []
    docs += [_doc_view(f"stub{i}", f"Pay stub {i}", s) for i, s in enumerate(stubs, 1)]
    return docs


def confirm_extractions(form: dict[str, str], docs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply the confirmation form to fresh extractions; returns (confirmed extractions, step 1 prefill).

    Raises :class:`~charitycare.packet.extract.ConfirmationRequired` while any flagged
    field is neither ticked nor edited, and ``ValueError`` for a bad date.
    """
    from charitycare.packet.extract import annualize_income, require_confirmed

    out: dict[str, Any] = {}
    for key, ex in docs.items():
        values = {n: form[f"{key}__{n}"] for n in type(ex.fields).model_fields if f"{key}__{n}" in form}
        ticked = {n for n in type(ex.fields).model_fields if form.get(f"ok__{key}__{n}")}
        out[key] = ex.confirm(values, ticked)
    require_confirmed(out)
    prefill: dict[str, str] = {}
    bill = out.get("bill")
    if bill is not None:
        if bill.fields.statement_date.value:
            prefill["first_statement_date"] = bill.fields.statement_date.value.isoformat()
        if bill.fields.service_date.value:
            prefill["service_date"] = bill.fields.service_date.value.isoformat()
    stubs = [ex.fields for k, ex in sorted(out.items()) if k.startswith("stub")]
    if stubs:
        income = annualize_income(stubs)
        if income.amount is not None and not income.unresolved:
            prefill["annual_income"] = f"{income.amount:.2f}"
    return out, prefill


async def _uploads(request: Request) -> tuple[dict[str, str], bytes | None, list[bytes]]:
    data = await request.form()
    bill: bytes | None = None
    stubs: list[bytes] = []
    for key, v in data.multi_items():
        if not hasattr(v, "read"):
            continue
        raw = await v.read()  # type: ignore[union-attr]
        if not raw:
            continue
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError("each image must be 10 MB or smaller")
        if key == "bill":
            bill = raw
        elif key == "stubs" and len(stubs) < MAX_STUBS:
            stubs.append(raw)
    fields = {k: str(v) for k, v in data.multi_items() if not hasattr(v, "read")}
    # The confirm step carries the EXIF-stripped images back in the form (never stored server-side).
    if bill is None and fields.get("img__bill"):
        bill = _b64(fields["img__bill"])
    if not stubs:
        stubs = [_b64(fields[k]) for k in sorted(fields) if k.startswith("img__stub")][:MAX_STUBS]
    return fields, bill, stubs


def _b64(text: str) -> bytes:
    raw = base64.b64decode(text.encode(), validate=True)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("each image must be 10 MB or smaller")
    return raw


def _extract_all(bill: bytes | None, stubs: list[bytes]) -> dict[str, Any]:
    from charitycare.packet.extract import extract_bill, extract_pay_stub

    docs: dict[str, Any] = {}
    if bill is not None:
        docs["bill"] = extract_bill(bill)
    for i, s in enumerate(stubs, 1):
        docs[f"stub{i}"] = extract_pay_stub(s)
    return docs


def navigator_router() -> APIRouter:
    r = APIRouter(include_in_schema=False)

    @r.get("/navigator", response_class=HTMLResponse)
    def page(request: Request, facility_id: str | None = None) -> Response:
        with request.app.state.lock:
            ctx = _ctx(request, request.app.state.registry, {"facility_id": facility_id or ""}, None)
            return request.app.state.templates.TemplateResponse(request, "navigator.html", ctx, headers=NO_STORE)

    @r.post("/navigator/extract", response_class=HTMLResponse)
    async def extract(request: Request) -> Response:
        """Step 0: read the uploaded bill and pay stubs and show them for confirmation."""
        tpl = request.app.state.templates
        try:
            fields, bill, stubs = await _uploads(request)
            docs = _extract_all(bill, stubs)
        except (ValueError, OSError) as exc:
            return tpl.TemplateResponse(request, "_extract.html", {"request": request, "error": str(exc).splitlines()[0],
                                                                   "docs": [], "facility_id": ""}, headers=NO_STORE)
        view = extraction_view(docs.get("bill"), [v for k, v in sorted(docs.items()) if k.startswith("stub")])
        ctx = {"request": request, "docs": view, "facility_id": fields.get("facility_id", ""), "error": None}
        return tpl.TemplateResponse(request, "_extract.html", ctx, headers=NO_STORE)

    @r.post("/navigator/confirm", response_class=HTMLResponse)
    async def confirm(request: Request) -> Response:
        """Re-read the same images, apply the navigator's confirmations, and pre-fill step 1."""
        from charitycare.packet.extract import ConfirmationRequired

        tpl = request.app.state.templates
        try:
            fields, bill, stubs = await _uploads(request)
            docs = _extract_all(bill, stubs)
            _, prefill = confirm_extractions(fields, docs)
        except ConfirmationRequired as exc:
            view = extraction_view(docs.get("bill"), [v for k, v in sorted(docs.items()) if k.startswith("stub")])
            return tpl.TemplateResponse(request, "_extract.html", {"request": request, "docs": view, "error": str(exc),
                                                                   "facility_id": fields.get("facility_id", "")},
                                        status_code=422, headers=NO_STORE)
        except (ValueError, OSError) as exc:
            return tpl.TemplateResponse(request, "_extract.html", {"request": request, "error": str(exc).splitlines()[0],
                                                                   "docs": [], "facility_id": ""}, status_code=422,
                                        headers=NO_STORE)
        form = {"facility_id": fields.get("facility_id", ""), **prefill}
        with request.app.state.lock:
            ctx = _ctx(request, request.app.state.registry, form, None)
            ctx["confirmed_from_documents"] = sorted(prefill)
            return tpl.TemplateResponse(request, "navigator.html", ctx, headers=NO_STORE)

    @r.post("/navigator/screen", response_class=HTMLResponse)
    async def screen(request: Request) -> Response:
        form = await _form(request)
        today = request.app.state.settings.today()
        with request.app.state.lock:
            reg: Registry = request.app.state.registry
            try:
                hh = household_from_form(form)
                det = run_screen(reg, form.get("facility_id", ""), hh, today)
                pv = reg.get_policy_version(det.policy_version_id) if det.policy_version_id else None
                result: dict[str, Any] = determination_payload(det, pv.as_of if pv else None)
            except ValueError as exc:
                result = {"error": str(exc).splitlines()[0]}
            except HTTPException as exc:
                result = {"error": str(exc.detail)}
            ctx = _ctx(request, reg, form, result)
            tpl = "_determination.html" if request.headers.get("hx-request") else "navigator.html"
            return request.app.state.templates.TemplateResponse(request, tpl, ctx, headers=NO_STORE)

    @r.post("/packet")
    async def packet(request: Request) -> Response:
        form = await _form(request)
        today = request.app.state.settings.today()
        with request.app.state.lock:
            reg: Registry = request.app.state.registry
            try:
                hh = household_from_form(form)
            except ValueError as exc:
                raise HTTPException(422, str(exc).splitlines()[0]) from exc
            fid = form.get("facility_id", "")
            det = run_screen(reg, fid, hh, today)
            try:
                res = build_packet(reg, fid, hh, {k: form.get(k) for k in APPLICANT_FIELDS}, det, today)
            except PacketUnavailable as exc:
                raise HTTPException(503, str(exc)) from exc
        headers = NO_STORE | {
            "Content-Disposition": 'attachment; filename="financial-assistance-packet.pdf"',
            "X-CCC-Packet-Verified": "true" if getattr(res, "verified", False) else "false",
        }
        return Response(res.pdf, media_type="application/pdf", headers=headers)

    return r

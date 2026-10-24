"""FastAPI application: the public read API (design section 2b), ``/v1/screen``,
hospital corrections, the maintainer review console and the navigator packet page.

``create_app(registry)`` wires everything over one :class:`~charitycare.store.Registry`.
Every ``GET /v1`` response is an envelope ``{"data": ..., "meta": {...}}`` whose
``meta`` carries the as-of date, the last verification date and the sunset flag
``stale`` (section 17: if maintenance stops, the API says so), the disclaimer
version and the data licence.

Privacy (section 11): request bodies are never logged (see
:mod:`charitycare.api.middleware`), validation errors never echo submitted
values, and nothing about a household is written anywhere.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time
from decimal import InvalidOperation
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic import Field as PField

from charitycare import __version__ as ccc_version
from charitycare.api import feeds, queries
from charitycare.api.middleware import AccessLogMiddleware, RateLimitMiddleware
from charitycare.api.settings import ApiSettings
from charitycare.config import DISCLAIMER_TEXT, DISCLAIMER_VERSION
from charitycare.schemas import (
    SCHEMA_VERSION,
    CompiledPolicy,
    Determination,
    FieldStatus,
    HospitalResponse,
    Household,
    ReviewTask,
    utcnow,
)
from charitycare.store import Registry
from charitycare.wording import OUTCOME_LABELS

TEMPLATES_DIR = Path(__file__).parent / "templates"
ASSETS_DIR = Path(__file__).parent.parent / "site" / "static"
DATA_LICENSE = "CC-BY-4.0"
_SSN = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
_LONG_DIGITS = re.compile(r"\b\d{9,}\b")


def mask_identifiers(text: str) -> str:
    """Mask SSN-like and account-number-like digit runs before anything is stored."""
    return _LONG_DIGITS.sub("[number removed]", _SSN.sub("[number removed]", text))


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class ScreenRequest(BaseModel):
    """``POST /v1/screen`` body. Held in memory for the request only."""

    model_config = ConfigDict(extra="forbid")

    facility_id: str = PField(max_length=64)
    household: Household


class CorrectionRequest(BaseModel):
    """``POST /v1/corrections``: a hospital (or anyone) reports an error or replies to a discrepancy."""

    model_config = ConfigDict(extra="forbid")

    facility_id: str = PField(max_length=64)
    field_key: str | None = PField(default=None, description="A CompiledPolicy field key, if the correction is about one field")
    statement: str = PField(min_length=10, max_length=4000)
    submitter_role: Literal["hospital", "navigator", "researcher", "other"] = "hospital"
    publish_as_response: bool = PField(default=True, description="Hospitals: may this statement be shown as the hospital's response?")

    @field_validator("field_key")
    @classmethod
    def _known_key(cls, v: str | None) -> str | None:
        if v is not None and v not in CompiledPolicy.model_fields:
            raise ValueError("unknown field_key")
        return v


# --------------------------------------------------------------------------
# Engine / packet hooks (lazy imports; built by other stages)
# --------------------------------------------------------------------------


def load_screen() -> Callable[..., Determination]:
    """``charitycare.engine.screen``, imported lazily so the read API runs without the engine."""
    from charitycare.engine import screen

    return screen


def unknown_facility_error() -> type[Exception]:
    try:
        from charitycare.engine import UnknownFacility

        return UnknownFacility
    except ImportError:  # pragma: no cover - engine absent
        return KeyError


def run_screen(reg: Registry, facility_id: str, household: Household, today: date) -> Determination:
    """Screen a household with the deterministic engine; raises 404/503 as HTTP errors."""
    if reg.get_facility(facility_id) is None:
        raise HTTPException(404, "unknown facility")
    try:
        screen = load_screen()
    except ImportError as exc:
        raise HTTPException(503, "screening engine not available") from exc
    try:
        return screen(facility_id, household, registry=reg, today=today)
    except unknown_facility_error() as exc:
        raise HTTPException(404, "unknown facility") from exc
    except (InvalidOperation, OverflowError) as exc:
        raise HTTPException(422, "household values are out of range") from exc


def determination_payload(det: Determination, as_of: date | None) -> dict[str, Any]:
    return {
        "determination": det.model_dump(mode="json"),
        "label": OUTCOME_LABELS.get(det.outcome, det.outcome),
        "disclaimer": DISCLAIMER_TEXT.format(as_of=as_of.isoformat() if as_of else "the date shown"),
        "disclaimer_version": DISCLAIMER_VERSION,
        "decided_by": "deterministic code from the hospital's published policy; the hospital makes the final decision",
    }


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------


def _parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    try:
        if len(since) == 10:
            return datetime.combine(date.fromisoformat(since), time.min, UTC)
        dt = datetime.fromisoformat(since)
    except ValueError as exc:
        raise HTTPException(422, "since must be an ISO-8601 date or datetime") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def create_app(registry: Registry | None = None, *, settings: ApiSettings | None = None) -> FastAPI:
    """Build the application over ``registry`` (default: the configured SQLite registry)."""
    settings = settings or ApiSettings.from_env()
    reg = registry if registry is not None else Registry()
    lock = threading.RLock()
    app = FastAPI(
        title="Charity Care Commons API (Texas edition)",
        version=ccc_version,
        description="Cited, verified hospital financial-assistance rules. Legal information, not legal advice.",
    )
    app.state.registry = reg
    app.state.settings = settings
    app.state.lock = lock
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(AccessLogMiddleware)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted values (household facts) back or into logs.
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
    app.include_router(v1_router())
    from charitycare.api.console import console_router
    from charitycare.api.navigator import navigator_router

    app.include_router(console_router())
    app.include_router(navigator_router())

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, Any]:
        return {"name": "Charity Care Commons API", "docs": "/docs", "v1": "/v1/facilities?state=TX",
                "console": "/console", "navigator": "/navigator", "license": DATA_LICENSE}

    return app


def get_registry(request: Request) -> Iterator[Registry]:
    """Serialize registry access: one SQLite connection is shared by the worker threads."""
    with request.app.state.lock:
        yield request.app.state.registry


def get_settings_dep(request: Request) -> ApiSettings:
    return request.app.state.settings


def today_for(request: Request) -> date:
    return request.app.state.settings.today()


def meta(reg: Registry, settings: ApiSettings, **extra: Any) -> dict[str, Any]:
    today = settings.today()
    fr = queries.freshness(reg, today, settings.stale_after_days, settings.sunset)
    return {
        "as_of": today.isoformat(),
        **fr.as_meta(),
        "disclaimer_version": DISCLAIMER_VERSION,
        "license": DATA_LICENSE,
        "schema_version": SCHEMA_VERSION,
        **extra,
    }


def envelope(reg: Registry, settings: ApiSettings, data: Any, response: Response | None = None, **extra: Any) -> dict[str, Any]:
    m = meta(reg, settings, **extra)
    if response is not None and m["stale"]:
        response.headers["X-CCC-Stale"] = "true"
    return {"data": data, "meta": m}


def v1_router() -> APIRouter:
    r = APIRouter(prefix="/v1", tags=["v1"])
    Reg = Depends(get_registry)
    Cfg = Depends(get_settings_dep)

    @r.get("/facilities")
    def facilities(response: Response, state: str = Query("TX", max_length=2), coverage: str | None = None,
                   q: str | None = Query(None, max_length=100), reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        rows = queries.search_facilities(reg, q, limit=10_000) if q else queries.list_facilities(reg, state.upper())
        rows = [f for f in rows if f["state"] == state.upper() and (coverage is None or f["coverage"] == coverage)]
        return envelope(reg, cfg, rows, response, count=len(rows))

    @r.get("/facilities/{facility_id}")
    def facility(facility_id: str, response: Response, reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        d = queries.facility_detail(reg, facility_id)
        if d is None:
            raise HTTPException(404, "unknown facility")
        return envelope(reg, cfg, d, response)

    @r.get("/facilities/{facility_id}/policy")
    def facility_policy(facility_id: str, response: Response, as_of: date | None = None,
                        reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        if reg.get_facility(facility_id) is None:
            raise HTTPException(404, "unknown facility")
        pv = reg.policy_for_facility(facility_id, as_of)
        if pv is None:
            raise HTTPException(404, "no governing policy on file for this facility" + (f" as of {as_of}" if as_of else ""))
        return envelope(reg, cfg, queries.policy_bundle(reg, pv, facility_id), response,
                        policy_as_of=pv.as_of.isoformat(), requested_as_of=as_of.isoformat() if as_of else None)

    @r.get("/policies/{version_id}/fields")
    def policy_fields(version_id: str, response: Response, status: FieldStatus | None = None,
                      reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        if reg.get_policy_version(version_id) is None:
            raise HTTPException(404, "unknown policy version")
        rows = [queries.field_json(f, reg) for f in queries.fields_for_policy(reg, version_id) if status is None or f.status == status]
        return envelope(reg, cfg, rows, response, count=len(rows))

    @r.get("/policies/{version_id}/crosschecks")
    def policy_crosschecks(version_id: str, response: Response, reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        if reg.get_policy_version(version_id) is None:
            raise HTTPException(404, "unknown policy version")
        rows = queries.crosschecks_for_policy(reg, version_id)
        return envelope(reg, cfg, rows, response, count=len(rows))

    @r.get("/discrepancies")
    def discrepancies(response: Response, reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        rows = queries.discrepancies(reg)
        return envelope(reg, cfg, rows, response, count=len(rows))

    @r.get("/changes")
    def changes(response: Response, since: str | None = None, reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        rows = queries.changes(reg, _parse_since(since))
        return envelope(reg, cfg, rows, response, count=len(rows), since=since)

    @r.get("/changes.atom")
    def changes_atom(since: str | None = None, reg: Registry = Reg, cfg: ApiSettings = Cfg) -> Response:
        body = feeds.atom_feed(queries.changes(reg, _parse_since(since)), base_url=cfg.base_url)
        return Response(body, media_type="application/atom+xml")

    @r.get("/link-health")
    def link_health(response: Response, reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        return envelope(reg, cfg, queries.link_health(reg), response)

    @r.post("/screen")
    def screen(body: ScreenRequest, response: Response, reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        det = run_screen(reg, body.facility_id, body.household, cfg.today())
        pv = reg.get_policy_version(det.policy_version_id) if det.policy_version_id else None
        return envelope(reg, cfg, determination_payload(det, pv.as_of if pv else None), response)

    @r.post("/corrections", status_code=202)
    def corrections(body: CorrectionRequest, reg: Registry = Reg, cfg: ApiSettings = Cfg) -> dict[str, Any]:
        fac = reg.get_facility(body.facility_id)
        if fac is None:
            raise HTTPException(404, "unknown facility")
        now = utcnow()
        stamp = now.strftime("%Y%m%dT%H%M%S%f")
        rid = f"hr:{fac.id}:{stamp}"
        statement = mask_identifiers(body.statement.strip())
        pv = reg.policy_for_facility(fac.id)
        field_id = f"{pv.id}:{body.field_key}" if pv and body.field_key and reg.get_field(pv.id, body.field_key) else None
        with reg.transaction():
            if body.submitter_role == "hospital" and body.publish_as_response:
                # Stored unpublished; a reviewer publishes it beside the discrepancy (right of reply).
                reg.add_hospital_response(HospitalResponse(id=rid, facility_id=fac.id, field_key=body.field_key,
                                                           statement=statement, received_at=now, published=False))
            task = ReviewTask(
                id=f"rt:hospital_correction:{rid}", kind="hospital_correction", facility_id=fac.id, field_id=None,
                priority="high" if body.submitter_role == "hospital" else "medium",
                notes=f"[impact {60 if body.submitter_role == 'hospital' else 35}] correction from {body.submitter_role}"
                      f"{' on ' + body.field_key if body.field_key else ''}"
                      f"{' (field ' + field_id + ')' if field_id else ''}: {statement[:1500]}",
                created_at=now,
            )
            reg.add_review_task(task)
        return {"data": {"status": "received", "review_task_id": task.id,
                         "message": "Thank you. A reviewer will look at this before anything is published."},
                "meta": meta(reg, cfg)}

    return r

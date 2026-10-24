"""Maintainer review console (design section 7, Screen 2): FastAPI + Jinja + HTMX.

Server-rendered; every action also works without JavaScript (plain form posts
and redirects). HTMX requests get partials: the task detail and an
out-of-band refresh of the queue.

Access: a maintainer token (``CCC_CONSOLE_TOKEN``) is required as a bearer
header or as the ``ccc_console`` cookie, which ``/console/login?token=...``
sets (HttpOnly, SameSite=Strict, so cross-site form posts carry no cookie).
Without a configured token the console is closed unless ``console_open`` is
set for local development.
"""

from __future__ import annotations

import hmac
import html
import json
import typing
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from charitycare.api import present, queries, review
from charitycare.api.settings import ApiSettings
from charitycare.config import DISCLAIMER_TEXT, DISCLAIMER_VERSION
from charitycare.schemas import AbstainReason, Facility, FieldRecord, ReviewTask
from charitycare.store import Registry

COOKIE = "ccc_console"
ABSTAIN_REASONS: tuple[str, ...] = typing.get_args(AbstainReason)


def require_maintainer(request: Request) -> str:
    """Authorize a maintainer; returns the default reviewer name."""
    s: ApiSettings = request.app.state.settings
    if s.console_token:
        auth = request.headers.get("authorization", "")
        supplied = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else request.cookies.get(COOKIE, "")
        if not supplied or not hmac.compare_digest(supplied, s.console_token):
            raise HTTPException(401, "maintainer token required", headers={"WWW-Authenticate": "Bearer"})
        return request.headers.get("x-reviewer", "maintainer")[:80]
    if s.console_open:
        return request.headers.get("x-reviewer", "local-maintainer")[:80]
    raise HTTPException(403, "review console is disabled (set CCC_CONSOLE_TOKEN)")


def _reg(request: Request):  # type: ignore[no-untyped-def]
    with request.app.state.lock:
        yield request.app.state.registry


def _base_ctx(request: Request) -> dict[str, Any]:
    return {"request": request, "disclaimer": DISCLAIMER_TEXT.format(as_of="the date shown"),
            "disclaimer_version": DISCLAIMER_VERSION}


def _subject(reg: Registry, task: ReviewTask, rec: FieldRecord | None) -> str:
    fids = queries.facilities_for_policy(reg, rec.policy_version_id) if rec else ([task.facility_id] if task.facility_id else [])
    names = [f.name for f in (reg.get(Facility, id=i) for i in fids) if f is not None]
    if not names:
        return task.facility_id or "registry"
    return names[0] + (f" +{len(names) - 1}" if len(names) > 1 else "")


def queue_rows(reg: Registry) -> list[dict[str, Any]]:
    rows = []
    for t in review.queue(reg):
        rec = reg.get(FieldRecord, id=t.field_id) if t.field_id else None
        title = present.label(rec.key) if rec else t.kind.replace("_", " ").capitalize()
        line = review.task_note(t)
        rows.append({"id": t.id, "title": title, "subject": _subject(reg, t, rec), "priority": t.priority,
                     "impact": review.task_impact(t), "line": line[:140] + ("…" if len(line) > 140 else "")})
    return rows


def task_detail(reg: Registry, task: ReviewTask, reviewer: str) -> dict[str, Any]:
    rec = reg.get(FieldRecord, id=task.field_id) if task.field_id else None
    fids = queries.facilities_for_policy(reg, rec.policy_version_id) if rec else ([task.facility_id] if task.facility_id else [])
    facilities = [f.name for f in (reg.get(Facility, id=i) for i in fids) if f is not None]
    field = queries.field_json(rec, reg) if rec else None
    sources = []
    if rec is not None:
        for ev in present.typed_field(rec.key, rec.field).evidence[:3]:
            ctx = review.source_context(reg, ev)
            ctx["quote"] = ev.quote
            sources.append(ctx)
    value_json = json.dumps(field["field"]["value"], indent=2, ensure_ascii=False) if field and field["field"]["value"] is not None else ""
    return {
        "task": task, "title": (present.label(rec.key) if rec else task.kind.replace("_", " ").capitalize())
        + (f" · {facilities[0]}" if facilities else ""),
        "impact": review.task_impact(task), "note": review.task_note(task), "adjudicator": review.adjudicator_note(task),
        "facilities": facilities, "field": field, "sources": sources, "value_json": value_json,
        "abstain_reasons": ABSTAIN_REASONS, "reviewer": reviewer,
    }


MAINTAINER = Depends(require_maintainer)
REGISTRY = Depends(_reg)


def console_router() -> APIRouter:
    r = APIRouter(prefix="/console", include_in_schema=False)

    @r.get("/login")
    def login(request: Request, token: str) -> Response:
        s: ApiSettings = request.app.state.settings
        if not s.console_token or not hmac.compare_digest(token, s.console_token):
            raise HTTPException(401, "invalid token")
        resp = RedirectResponse("/console", status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return resp

    @r.get("", response_class=HTMLResponse)
    def console(request: Request, task: str | None = None, done: str | None = None, error: str | None = None,
                reviewer: str = MAINTAINER, reg: Registry = REGISTRY) -> Response:
        rows = queue_rows(reg)
        selected = reg.get(ReviewTask, id=task) if task else None
        if selected is None and rows:
            selected = reg.get(ReviewTask, id=rows[0]["id"])
        ctx = _base_ctx(request) | {
            "section": "console", "tasks": rows,
            "detail": task_detail(reg, selected, reviewer) if selected else None,
            "flash": error or (f"Saved: {done}" if done else None), "flash_error": bool(error),
        }
        return request.app.state.templates.TemplateResponse(request, "console.html", ctx)

    @r.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_partial(request: Request, task_id: str, reviewer: str = MAINTAINER, reg: Registry = REGISTRY) -> Response:
        task = reg.get(ReviewTask, id=task_id)
        if task is None:
            raise HTTPException(404, "unknown task")
        if not request.headers.get("hx-request"):
            return RedirectResponse(f"/console?task={task_id}", status_code=303)
        ctx = _base_ctx(request) | {"detail": task_detail(reg, task, reviewer)}
        return request.app.state.templates.TemplateResponse(request, "_task_detail.html", ctx)

    @r.post("/tasks/{task_id}/decision", response_class=HTMLResponse)
    def decide(request: Request, task_id: str, action: str = Form(...), reviewer_form: str | None = Form(None, alias="reviewer"),
               note: str | None = Form(None), value: str | None = Form(None), quote: str | None = Form(None),
               page: str | None = Form(None), abstain_reason: str | None = Form(None),
               reviewer: str = MAINTAINER, reg: Registry = REGISTRY) -> Response:
        who = (reviewer_form or reviewer).strip() or reviewer
        htmx = bool(request.headers.get("hx-request"))
        error: str | None = None
        try:
            decision = review.ReviewDecision(
                task_id=task_id, action=action, reviewer=who, note=(note or None) and note.strip(),  # type: ignore[arg-type]
                value=review.parse_value(value) if action == "edit" else None,
                quote=(quote or "").strip() or None if action == "edit" else None,
                page=int(page) if action == "edit" and page and page.strip() else None,
                abstain_reason=abstain_reason if action == "abstain" else None,  # type: ignore[arg-type]
            )
            review.apply_decision(reg, decision)
        except (review.ReviewError, ValueError) as exc:
            error = str(exc).splitlines()[0][:300]
        if not htmx:
            from urllib.parse import quote as q

            target = f"/console?task={q(task_id)}&error={q(error)}" if error else f"/console?done={q(action)}"
            return RedirectResponse(target, status_code=303)
        rows = queue_rows(reg)
        nxt = reg.get(ReviewTask, id=task_id) if error else (reg.get(ReviewTask, id=rows[0]["id"]) if rows else None)
        ctx = _base_ctx(request) | {"tasks": rows, "oob_queue": True}
        if nxt is None:
            msg = html.escape(error or f"Saved: {action}. Queue is empty.")
            return HTMLResponse(f'<div class="card"><p class="flash" role="status">{msg}</p></div>')
        detail = task_detail(reg, nxt, who)
        detail["flash"], detail["flash_error"] = (error, True) if error else (f"Saved: {action} on {task_id}", False)
        ctx["detail"] = detail
        return request.app.state.templates.TemplateResponse(request, "_task_detail.html", ctx)

    return r

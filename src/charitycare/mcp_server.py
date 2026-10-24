"""MCP server: verified charity-care rules as agent tools (design section 2b).

Tools (all read-only; no write, send or submit capability):

* ``find_facility``    search Texas hospitals by name, city, county or CCN;
* ``get_policy``       the governing policy's fields with quotes, pages, sha256 and status;
* ``screen_household`` deterministic, cited screening by :mod:`charitycare.engine` (pure code);
* ``list_changes``     the policy change feed.

Household facts passed to ``screen_household`` are used for that call only and
never stored or logged. Built on the MCP Python SDK: ``MCPServer`` in mcp 2.x
(``FastMCP`` in 1.x). Run with ``python -m charitycare.mcp_server [--db PATH | --demo]``
(stdio transport).
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from charitycare.api import queries
from charitycare.config import DISCLAIMER_TEXT, DISCLAIMER_VERSION
from charitycare.schemas import Household
from charitycare.store import Registry
from charitycare.wording import OUTCOME_LABELS

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    ToolAnnotations = None  # type: ignore[assignment,misc]

INSTRUCTIONS = (
    "Cited, human-verified Texas hospital financial-assistance (charity care) rules. Use find_facility to get a "
    "facility_id, get_policy for the rules with verbatim quotes, and screen_household for a deterministic estimate. "
    "Report results as 'likely eligible' estimates with the quotes; the hospital makes the final decision. "
    "Fields marked provisional are not reviewer-verified. This is legal information, not legal advice."
)


def _annotations() -> dict[str, Any]:
    if ToolAnnotations is None:
        return {}
    return {"annotations": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)}


def _load_screen() -> Callable[..., Any]:
    from charitycare.engine import screen

    return screen


def build_server(registry: Registry, *, today: Callable[[], date] = date.today,
                 screen_fn: Callable[..., Any] | None = None) -> Any:
    """An MCP server exposing the four read-only tools over ``registry``."""
    server = _Server("charity-care-commons", instructions=INSTRUCTIONS)
    lock = threading.RLock()
    ann = _annotations()

    @server.tool(**ann)
    def find_facility(query: str = "", city: str | None = None, county: str | None = None, ccn: str | None = None,
                      limit: int = 10) -> dict[str, Any]:
        """Find Texas hospitals by name words, city, county or CMS Certification Number (CCN).

        Returns facility ids, coverage (verified / partial / not_found) and headline rules.
        """
        with lock:
            rows = queries.search_facilities(registry, query, city=city, county=county, ccn=ccn, limit=min(max(limit, 1), 50))
        keep = ("id", "name", "city", "county", "ccn", "coverage", "policy_version_id", "free_max_pct", "discount_max_pct",
                "asset_test", "irs_check")
        return {"facilities": [{k: r[k] for k in keep} for r in rows], "count": len(rows)}

    @server.tool(**ann)
    def get_policy(facility_id: str, as_of: str | None = None, include_provisional: bool = True) -> dict[str, Any]:
        """The governing financial-assistance policy for a facility, field by field.

        Each field has a display value, status (verified / provisional / abstained), verbatim quotes with page
        numbers and document sha256, and IRS Schedule H cross-checks. ``as_of`` is YYYY-MM-DD.
        """
        with lock:
            fac = registry.get_facility(facility_id)
            if fac is None:
                return {"error": f"unknown facility {facility_id!r}; use find_facility"}
            pv = registry.policy_for_facility(facility_id, date.fromisoformat(as_of) if as_of else None)
            if pv is None:
                return {"facility_id": facility_id, "coverage": "not_found", "error": "no governing policy on file"}
            bundle = queries.policy_bundle(registry, pv, facility_id)
        fields = []
        for f in bundle["fields"]:
            if not include_provisional and f["status"] == "provisional":
                continue
            fields.append({
                "key": f["key"], "label": f["label"], "display": f["display"], "status": f["status"],
                "value": f["field"]["value"], "abstain_reason": f["field"]["abstain_reason"],
                "quotes": [{"quote": e["quote"], "page": e["page"], "section": e["section_label"], "doc_sha256": e["doc_sha256"]}
                           for e in f["field"]["evidence"]],
                "irs_crosschecks": [{"result": c["result"], "label": c["public_label"], "summary": c["summary"]} for c in f["crosschecks"]],
            })
        return {"facility_id": facility_id, "facility_name": fac.name, "policy_version_id": pv.id, "title": pv.title,
                "as_of": pv.as_of.isoformat(), "documents": bundle["documents"], "fields": fields,
                "disclaimer": DISCLAIMER_TEXT.format(as_of=pv.as_of.isoformat())}

    @server.tool(**ann)
    def screen_household(facility_id: str, household_size: int, annual_income: float, liquid_assets: float | None = None,
                         county: str | None = None, insured: bool | None = None, emergency_care: bool = False,
                         first_statement_date: str | None = None, application_date: str | None = None) -> dict[str, Any]:
        """Deterministic screening of one household against the facility's verified policy (no LLM).

        Returns the outcome label (e.g. "Likely eligible for free care"), the %FPL arithmetic, cited quotes,
        conditions not evaluated and deadlines. Inputs are not stored. Dates are YYYY-MM-DD.
        """
        try:
            hh = Household(
                household_size=household_size, annual_income=Decimal(str(annual_income)),
                liquid_assets=Decimal(str(liquid_assets)) if liquid_assets is not None else None, county=county,
                insured=insured, emergency_care=emergency_care,
                first_statement_date=date.fromisoformat(first_statement_date) if first_statement_date else None,
                application_date=date.fromisoformat(application_date) if application_date else None,
            )
        except ValueError as exc:
            return {"error": "invalid household: " + str(exc).splitlines()[0]}
        with lock:
            if registry.get_facility(facility_id) is None:
                return {"error": f"unknown facility {facility_id!r}; use find_facility"}
            try:
                fn = screen_fn or _load_screen()
            except ImportError:
                return {"error": "screening engine not available"}
            det = fn(facility_id, hh, registry=registry, today=today())
            pv = registry.get_policy_version(det.policy_version_id) if det.policy_version_id else None
        return {
            "label": OUTCOME_LABELS.get(det.outcome, det.outcome), "outcome": det.outcome, "arithmetic": det.arithmetic,
            "pct_fpl": str(det.pct_fpl), "fpg_year": det.fpg_year,
            "quotes": [{"quote": c.quote, "page": c.page, "section": c.section_label, "doc_sha256": c.doc_sha256} for c in det.citations],
            "unevaluated_conditions": det.unevaluated_conditions, "provisional_fields_used": det.provisional_fields_used,
            "notes": det.notes,
            "federal_deadline": det.federal_deadline.isoformat() if det.federal_deadline else None,
            "policy_deadline": det.policy_deadline.isoformat() if det.policy_deadline else None,
            "policy_version_id": det.policy_version_id,
            "disclaimer": DISCLAIMER_TEXT.format(as_of=pv.as_of.isoformat() if pv else "the date shown"),
            "disclaimer_version": DISCLAIMER_VERSION,
        }

    @server.tool(**ann)
    def list_changes(since: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Policy changes and review decisions published since ``since`` (YYYY-MM-DD or ISO datetime), newest first."""
        dt = None
        if since:
            try:
                dt = (datetime.combine(date.fromisoformat(since), time.min, UTC) if len(since) == 10
                      else datetime.fromisoformat(since))
            except ValueError:
                return {"error": "since must be YYYY-MM-DD or an ISO datetime"}
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        with lock:
            rows = queries.changes(registry, dt)
        return {"changes": [{**r, "diff_summary": r["diff_summary"].split("\n---\n")[0]} for r in rows[: max(1, min(limit, 500))]],
                "count": len(rows)}

    return server


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m charitycare.mcp_server", description="Charity Care Commons MCP server (stdio)")
    p.add_argument("--db", default=None, help="registry path (default CCC_DB_PATH)")
    p.add_argument("--demo", action="store_true", help="serve an in-memory registry seeded from data/demo")
    a = p.parse_args(argv)
    if a.demo:
        from charitycare.api.demo import seed_demo

        reg = Registry(":memory:")
        seed_demo(reg)
    else:
        reg = Registry(a.db)
    build_server(reg).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

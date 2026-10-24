"""Shared constants and client factory for the api/ tests (imported by name; not a conftest)."""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from charitycare.api import ApiSettings, create_app
from charitycare.store import Registry

TODAY = date(2026, 10, 24)
TOKEN = "test-maintainer-token"
EM = "tx-459901"  # Example Methodist
EM_PV = "pv:example-methodist:demo"


def make_client(reg: Registry, **overrides: object) -> TestClient:
    vals: dict[str, object] = {"today": lambda: TODAY, "console_token": TOKEN, "rate_limit_per_min": 10_000}
    vals.update(overrides)
    return TestClient(create_app(reg, settings=ApiSettings(**vals)))  # type: ignore[arg-type]

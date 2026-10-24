"""Fixtures for the API, console, navigator and MCP tests: a registry seeded from the demo corpus."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_helpers import TOKEN, make_client
from fastapi.testclient import TestClient

from charitycare.api.demo import seed_demo
from charitycare.store import Registry


@pytest.fixture
def seeded(demo_corpus) -> Iterator[Registry]:  # type: ignore[no-untyped-def]
    reg = Registry(":memory:")
    seed_demo(reg, demo_corpus.root)
    yield reg
    reg.close()


@pytest.fixture
def client(seeded: Registry) -> TestClient:
    return make_client(seeded)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}

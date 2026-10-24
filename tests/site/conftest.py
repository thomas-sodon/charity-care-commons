"""Site tests: a registry seeded from the demo corpus."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from charitycare.api.demo import seed_demo
from charitycare.store import Registry


@pytest.fixture
def seeded(demo_corpus) -> Iterator[Registry]:  # type: ignore[no-untyped-def]
    reg = Registry(":memory:")
    seed_demo(reg, demo_corpus.root)
    yield reg
    reg.close()

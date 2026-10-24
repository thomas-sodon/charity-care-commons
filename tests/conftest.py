"""Shared pytest fixtures.

* ``demo_corpus``: the synthetic corpus built once per session into a temp dir,
  with its replay fixtures; returns a :class:`DemoCorpus`.
* ``registry``: an empty in-memory :class:`charitycare.store.Registry`.
* ``isolated_env``: points CCC_* paths at a temp dir and resets cached settings.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from charitycare import llm
from charitycare.config import get_settings
from charitycare.store import Registry


@dataclass(frozen=True)
class DemoCorpus:
    root: Path
    fixtures: Path
    manifest: dict[str, Any]

    def path(self, rel: str) -> Path:
        return self.root / rel


@pytest.fixture(scope="session")
def demo_corpus(tmp_path_factory: pytest.TempPathFactory) -> DemoCorpus:
    from charitycare.demo.build import build_corpus

    base = tmp_path_factory.mktemp("demo")
    root, fixtures = base / "demo", base / "fixtures" / "llm"
    manifest = build_corpus(root, fixtures)
    return DemoCorpus(root, fixtures, manifest)


@pytest.fixture
def registry() -> Iterator[Registry]:
    reg = Registry(":memory:")
    yield reg
    reg.close()


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("CCC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CCC_FIXTURES_DIR", str(tmp_path / "fixtures"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CCC_LLM", raising=False)
    get_settings.cache_clear()
    llm.reset_backend()
    yield tmp_path
    get_settings.cache_clear()
    llm.reset_backend()

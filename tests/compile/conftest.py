"""Fixtures for the compile-stage tests: the demo corpus, replay fixtures and a loaded registry."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from charitycare.compile.demo import load_demo_registry
from charitycare.compile.fixtures import write_adjudication_fixtures
from charitycare.compile.textlayer import DocText
from charitycare.llm import ReplayBackend
from charitycare.schemas import CompiledPolicy
from charitycare.store import Registry


@pytest.fixture(scope="session")
def compile_fixtures(demo_corpus: Any, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Foundation compile/re-ask fixtures plus freshly generated adjudication fixtures."""
    root = tmp_path_factory.mktemp("compile-fixtures") / "llm"
    shutil.copytree(demo_corpus.fixtures, root)
    write_adjudication_fixtures(root, demo_corpus.root)
    return root


@pytest.fixture
def backend(compile_fixtures: Path) -> ReplayBackend:
    return ReplayBackend(compile_fixtures)


@pytest.fixture
def demo_registry(demo_corpus: Any) -> Iterator[Registry]:
    reg = Registry(":memory:")
    load_demo_registry(reg, demo_corpus.root)
    yield reg
    reg.close()


@pytest.fixture(scope="session")
def gold(demo_corpus: Any):
    def load(hospital_id: str) -> dict[str, Any]:
        return json.loads(demo_corpus.path(f"gold/{hospital_id}.json").read_text(encoding="utf-8"))

    return load


@pytest.fixture(scope="session")
def fap(demo_corpus: Any, gold):
    cache: dict[str, DocText] = {}

    def load(hospital_id: str) -> DocText:
        if hospital_id not in cache:
            d = gold(hospital_id)["documents"]["fap"]
            cache[hospital_id] = DocText.from_pdf(demo_corpus.path(d["path"]).read_bytes(), d["sha256"])
        return cache[hospital_id]

    return load


@pytest.fixture(scope="session")
def gold_policy(gold):
    return lambda hospital_id: CompiledPolicy.model_validate(gold(hospital_id)["policy"])


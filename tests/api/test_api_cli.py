"""Command entry points for serve / site / export (registered in ccc by the integrator)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from charitycare.api import cli as api_cli
from charitycare.config import get_settings
from charitycare.export import cli as export_cli
from charitycare.site import cli as site_cli


@pytest.fixture
def demo_env(demo_corpus, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CCC_DATA_DIR", str(demo_corpus.root.parent))
    get_settings.cache_clear()
    yield demo_corpus
    get_settings.cache_clear()


def test_command_tuples():
    for mod, name in ((api_cli, "serve"), (site_cli, "site"), (export_cli, "export")):
        cmd, help_, configure, run = mod.COMMAND
        assert cmd == name and help_ and callable(configure) and callable(run)
        configure(argparse.ArgumentParser())


def test_serve_builds_demo_app(demo_env):
    p = argparse.ArgumentParser()
    api_cli.configure(p)
    app = api_cli.build(p.parse_args(["--demo", "--console-open"]))
    c = TestClient(app)
    assert c.get("/v1/facilities").json()["meta"]["count"] == 10
    assert c.get("/console").status_code == 200


def test_site_and_export_clis(demo_env, tmp_path: Path, capsys):
    assert export_cli.main(["--demo", "--out", str(tmp_path / "rel"), "--version", "v-test"]) == 0
    assert (tmp_path / "rel" / "fields.parquet").is_file()
    assert site_cli.main(["build", "--demo", "--out", str(tmp_path / "dist"), "--today", "2026-10-24",
                          "--release", str(tmp_path / "rel")]) == 0
    assert (tmp_path / "dist" / "data" / "release" / "fields.parquet").is_file()
    out = capsys.readouterr().out
    assert "release v-test" in out and "wrote 15 pages" in out

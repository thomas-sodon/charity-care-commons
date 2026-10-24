"""``ccc packet`` subcommands (registered by the integrator)."""

from __future__ import annotations

import argparse
import json
import shutil

from charitycare import llm
from charitycare.config import get_settings
from charitycare.packet.cli import COMMAND


def _run(*argv: str) -> int:
    name, _help, configure, run = COMMAND
    p = argparse.ArgumentParser(prog=f"ccc {name}")
    configure(p)
    return run(p.parse_args(list(argv)))


def test_fixtures_then_demo(isolated_env, demo_corpus, capsys) -> None:
    demo_dir = isolated_env / "data" / "demo"
    shutil.copytree(demo_corpus.root, demo_dir)
    assert _run("fixtures") == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["fixtures"]) == 7 and len(out["images"]) == 3
    llm.reset_backend()
    get_settings.cache_clear()
    out_dir = isolated_env / "out"
    assert _run("demo", "--hospital", "pineywoods-regional", "--out", str(out_dir)) == 2
    assert "confirm these extracted fields first: bill: account_last4" in capsys.readouterr().out
    assert _run("demo", "--hospital", "pineywoods-regional", "--out", str(out_dir), "--accept-extracted") == 0
    text = capsys.readouterr().out
    assert "readback 13/13 fields match; verified=True" in text
    assert "$1,911.54 biweekly x 26 = $49,700.04" in text
    assert (out_dir / "packet.pdf").exists() and (out_dir / "deadline.ics").exists()


def test_demo_without_fixtures_fails_softly(isolated_env, demo_corpus, capsys) -> None:
    out_dir = isolated_env / "out"
    assert _run("demo", "--demo-dir", str(demo_corpus.root), "--out", str(out_dir)) == 1
    assert "extraction unavailable" in capsys.readouterr().out


def test_template_command(demo_corpus, capsys) -> None:
    pdf = demo_corpus.root / "docs" / "lone-star-health" / "application_en.pdf"
    assert _run("template", str(pdf), "--no-llm") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["method"] == "heuristic" and len(out["template"]["field_map"]) == 18

"""Docs exist and follow the wording rules; workflows are wired as section 12 describes."""

from __future__ import annotations

from pathlib import Path

import yaml

from charitycare.config import DISCLAIMER_TEXT, DISCLAIMER_VERSION
from charitycare.wording import find_banned_wording

REPO = Path(__file__).resolve().parents[2]
DOCS = ["METHODS.md", "PRIVACY.md", "DISCLAIMER.md", "GOLD_LABELING.md", "OUTREACH.md", "SUNSET.md", "CONTRIBUTING.md"]


def _workflow(name: str) -> dict:
    d = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text())
    d["on"] = d.pop(True, d.get("on"))  # YAML 1.1 reads the bare key `on` as True
    return d


def test_docs_exist_and_use_safe_wording() -> None:
    for name in DOCS:
        text = (REPO / "docs" / name).read_text(encoding="utf-8")
        assert len(text) > 500, name
        # Quoted text (hospital quotes, and the banned words themselves when the rule is explained) is exempt.
        assert find_banned_wording(text, ignore_quoted=True) == [], name


def test_disclaimer_doc_matches_config() -> None:
    text = (REPO / "docs" / "DISCLAIMER.md").read_text(encoding="utf-8")
    assert DISCLAIMER_VERSION in text
    assert DISCLAIMER_TEXT in text.replace("> ", "")


def test_outreach_is_drafts_only() -> None:
    text = (REPO / "docs" / "OUTREACH.md").read_text(encoding="utf-8")
    assert "Drafts only" in text and "Public Information Act" in text
    assert "Dollar For" in text and "Tradeoffs" in text


def test_ci_workflow_jobs() -> None:
    ci = _workflow("ci.yml")
    assert {"push", "pull_request"} <= set(ci["on"])
    test_steps = "\n".join(s.get("run", "") for s in ci["jobs"]["test"]["steps"])
    for needle in ("pytest", "ruff", "python -m evals run", "--mode replay", "python -m evals gate", "schema-compat"):
        assert needle in test_steps, needle
    live = ci["jobs"]["smoke-live"]
    assert live["if"] == "github.event_name == 'pull_request'"
    assert "secrets.ANTHROPIC_API_KEY" in live["env"]["ANTHROPIC_API_KEY"]
    live_steps = "\n".join(s.get("run", "") for s in live["steps"])
    assert 'if [ -z "$ANTHROPIC_API_KEY" ]' in live_steps
    assert "prompts/|schemas/|src/charitycare/compile/" in live_steps
    assert "--suite smoke --mode live" in live_steps
    gated = [s for s in live["steps"] if "evals" in s.get("run", "")]
    assert gated and all(s["if"] == "steps.scope.outputs.run == 'true'" for s in gated)


def test_weekly_refresh_workflow() -> None:
    wf = _workflow("refresh.yml")
    assert wf["on"]["schedule"][0]["cron"].split()[4] == "1"  # weekly, Mondays
    steps = "\n".join(s.get("run", "") for s in wf["jobs"]["refresh"]["steps"])
    assert "python -m evals run --suite full" in steps and "python -m evals gate --suite full" in steps

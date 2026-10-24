"""Synthetic replay fixtures for the compile stage's own prompts (``adjudicate@v1``).

The compile and re-ask fixtures come from the foundation corpus builder. The
adjudicator's fixtures are generated here: the demo is compiled once with no
adjudication fixtures, so every adjudication falls back to the deterministic
code draft, and each draft is recorded as the Opus-class answer (marked
``synthetic``). One fixture is deliberately broken, so tests can show that a
draft which does not quote Schedule H exactly is discarded:

* ``lone-star-health`` / ``facilities_covered``: the Schedule H "quote" is a
  paraphrase that is not a line of the supplied context.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from charitycare.compile.adjudicate import Adjudication
from charitycare.compile.demo import load_demo_registry
from charitycare.compile.pipeline import compile_facility
from charitycare.compile.prompts import ADJUDICATE_VERSION
from charitycare.config import DEFAULT_MODELS, get_settings
from charitycare.llm import ReplayBackend, Usage, write_fixture
from charitycare.store import Registry

ADJUDICATION_ERRORS: list[dict[str, str]] = [
    {
        "hospital_id": "lone-star-health",
        "subject": "facilities_covered",
        "kind": "schedule_h_quote_not_exact",
        "description": "The draft paraphrases Schedule H instead of copying a context line; code must discard it.",
    }
]
_BAD_SH_QUOTE = "Schedule H lists the same three hospitals as one group"
ADJUDICATE_USAGE = Usage(input_tokens=25_000, output_tokens=2_000)


def _model_like(draft: Adjudication) -> dict[str, Any]:
    out = draft.model_dump(mode="json")
    out["rationale"] = out["rationale"].removeprefix("Code draft (no model output available). ")
    out["confidence"] = 0.8
    return out


def write_adjudication_fixtures(fixtures_root: Path | None = None, demo_dir: Path | None = None) -> list[Path]:
    """Regenerate ``fixtures/llm/adjudicate@v1`` from the demo corpus (deterministic)."""
    root = Path(fixtures_root) if fixtures_root else get_settings().llm_fixtures_dir
    shutil.rmtree(root / ADJUDICATE_VERSION, ignore_errors=True)
    paths: list[Path] = []
    with Registry(":memory:") as reg:
        manifest = load_demo_registry(reg, demo_dir)
        backend = ReplayBackend(root)
        for h in manifest["hospitals"]:
            for f in h["facilities"]:
                report = compile_facility(reg, f["facility_id"], backend=backend)
                for rec in report.adjudications:
                    if rec.adjudication is None or report.doc_sha256 is None:
                        continue
                    out = _model_like(rec.adjudication)
                    bad = any(e["hospital_id"] == h["id"] and e["subject"] == rec.subject for e in ADJUDICATION_ERRORS)
                    if bad:
                        out["schedule_h_quote"] = _BAD_SH_QUOTE
                    key = {"prompt_version": ADJUDICATE_VERSION, "doc_sha256": report.doc_sha256, "model_tier": "opus",
                           "variant": rec.subject}
                    paths.append(write_fixture(root, key, out, model=DEFAULT_MODELS["opus"], usage=ADJUDICATE_USAGE,
                                               synthetic=True,
                                               notes="Synthetic adjudication derived from the deterministic draft."
                                               + (" Deliberate error: schedule_h_quote_not_exact." if bad else "")))
    (root / ADJUDICATE_VERSION).mkdir(parents=True, exist_ok=True)
    (root / ADJUDICATE_VERSION / "DELIBERATE_ERRORS.json").write_text(json.dumps(ADJUDICATION_ERRORS, indent=2) + "\n", encoding="utf-8")
    return paths

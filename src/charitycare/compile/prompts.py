"""Versioned prompt texts for the compile stage (stored under ``prompts/`` at the repo root).

The prompt version string (``config.PROMPT_VERSIONS``) is part of every replay
fixture key, so a change to any file here must come with a version bump.
:func:`prompt_digest` fingerprints the texts so tests can pin them.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path

from charitycare.config import PROMPT_VERSIONS, get_settings

COMPILE_VERSION = PROMPT_VERSIONS["compile_policy"]
REASK_VERSION = PROMPT_VERSIONS["reask_field"]
ADJUDICATE_VERSION = PROMPT_VERSIONS["adjudicate"]
TRIAGE_VERSION = PROMPT_VERSIONS["change_triage"]


def prompts_dir() -> Path:
    return get_settings().home / "prompts"


@cache
def _read(rel: str) -> str:
    return (prompts_dir() / rel).read_text(encoding="utf-8")


def compile_system() -> str:
    """Static, cacheable compile prefix: field guide + hard few-shot examples."""
    return _read(f"{COMPILE_VERSION}/field_guide.md") + "\n\n" + _read(f"{COMPILE_VERSION}/few_shots.md")


def reask_user(field_key: str, doc_sha256: str, problems: list[str], previous: dict) -> str:
    """Volatile user turn for a targeted re-ask (the system prefix and documents stay cached)."""
    return _read(f"{REASK_VERSION}.md").format(
        field_key=field_key,
        doc_sha256=doc_sha256,
        problems="\n".join(f"- {p}" for p in problems) or "- (no detail)",
        previous=json.dumps(previous, indent=2, ensure_ascii=False, default=str),
    )


def adjudicate_system() -> str:
    return _read(f"{ADJUDICATE_VERSION}.md")


def triage_system() -> str:
    return _read(f"{TRIAGE_VERSION}.md")


def prompt_digest() -> str:
    """sha256 over all compile-stage prompt texts (pin in tests to force version bumps)."""
    h = hashlib.sha256()
    for text in (compile_system(), _read(f"{REASK_VERSION}.md"), adjudicate_system(), triage_system()):
        h.update(text.encode("utf-8"))
    return h.hexdigest()

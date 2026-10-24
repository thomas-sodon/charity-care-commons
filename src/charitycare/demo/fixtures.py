"""Recorded LLM replay fixtures for compiling each demo policy.

The fixture is what a Sonnet-class compile call would plausibly return: the
gold policy with model-like confidences, plus a few deliberate, realistic
mistakes so the gates have something to catch:

* ``quote_not_verbatim`` (example-methodist, ``refund_policy``): the quote is a
  paraphrase, so the quote verifier must fail it, re-ask, then abstain.
* ``wrong_page`` (lone-star-health, ``required_documents``): one quote cites
  the page before the one it is on.
* ``misread_value`` (gulf-coast-baptist, ``fpg_tiers``): free care read as
  <= 250% while the quote says 200%; Schedule H reports 200%, so the
  cross-check must flag it (and the re-ask fixture returns the correct value).

Image-only pages (pineywoods-regional page 3) carry correct quotes, because a
vision model can read them; without OCR the verifier cannot confirm them and
the fields must abstain as ``illegible``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from charitycare.config import DEFAULT_MODELS, PROMPT_VERSIONS
from charitycare.demo.spec import HospitalSpec
from charitycare.llm import Usage, write_fixture
from charitycare.schemas import CompiledPolicy

DELIBERATE_ERRORS: list[dict[str, str]] = [
    {
        "hospital_id": "example-methodist",
        "field": "refund_policy",
        "kind": "quote_not_verbatim",
        "description": "Quote paraphrases the refund clause; it does not occur verbatim on the cited page.",
    },
    {
        "hospital_id": "lone-star-health",
        "field": "required_documents",
        "kind": "wrong_page",
        "description": "Second evidence item cites page 4; the quote is on page 5.",
    },
    {
        "hospital_id": "gulf-coast-baptist",
        "field": "fpg_tiers",
        "kind": "misread_value",
        "description": "Free-care tier max read as 250 (quote says 200%); Schedule H line 13a says 200.",
    },
]


def _confidence(key: str, field: dict[str, Any], image_pages: tuple[int, ...]) -> float:
    if field.get("abstain_reason"):
        return 0.55
    pages = {e["page"] for e in field.get("evidence", [])}
    if pages & set(image_pages):
        return 0.82
    return 0.97 if key in {"policy_title", "effective_or_revision_date", "fpg_tiers"} else 0.93


def model_output(h: HospitalSpec, gold: CompiledPolicy) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The compile fixture output for ``h`` and, when an error has a corrected re-ask, its output."""
    out = gold.model_dump(mode="json")
    for key, field in out.items():
        field["confidence"] = _confidence(key, field, h.image_only_fap_pages)
    reask: dict[str, Any] | None = None
    if h.id == "example-methodist":
        f = out["refund_policy"]
        f["evidence"][0]["quote"] = "The Hospital will refund any payments over the amount the patient owes once assistance is approved."
        f["confidence"] = 0.74
    elif h.id == "lone-star-health":
        f = out["required_documents"]
        f["evidence"][1]["page"] = f["evidence"][1]["page"] - 1
        f["confidence"] = 0.88
    elif h.id == "gulf-coast-baptist":
        f = out["fpg_tiers"]
        reask = {"fpg_tiers": json.loads(json.dumps(f))}
        f["value"][0]["max_pct_inclusive"] = 250
        f["value"][1]["min_pct_exclusive"] = 250
        f["confidence"] = 0.9
    return out, reask


def compile_usage(fap_pages: int, image_pages: int) -> Usage:
    """Plausible usage for one compile call (section 13 assumptions)."""
    return Usage(
        input_tokens=2_000 * (fap_pages - image_pages) + 1_600 * image_pages + 600,
        output_tokens=5_000,
        cache_creation_input_tokens=9_000,
        cache_read_input_tokens=0,
    )


def write_compile_fixtures(
    root: Path, h: HospitalSpec, gold: CompiledPolicy, fap_sha: str, fap_pages: int
) -> list[Path]:
    """Write the compile fixture (and a re-ask fixture where one applies) for one hospital."""
    output, reask = model_output(h, gold)
    key = {"prompt_version": PROMPT_VERSIONS["compile_policy"], "doc_sha256": fap_sha, "model_tier": "sonnet", "variant": ""}
    errs = [e for e in DELIBERATE_ERRORS if e["hospital_id"] == h.id]
    paths = [
        write_fixture(
            root,
            key,
            output,
            model=DEFAULT_MODELS["sonnet"],
            usage=compile_usage(fap_pages, len(h.image_only_fap_pages)),
            synthetic=True,
            notes=("Derived from gold. Deliberate errors: " + "; ".join(f"{e['field']}: {e['kind']}" for e in errs)) if errs else "Derived from gold.",
        )
    ]
    if reask:
        for field_key, value in reask.items():
            rkey = {"prompt_version": PROMPT_VERSIONS["reask_field"], "doc_sha256": fap_sha, "model_tier": "sonnet", "variant": field_key}
            paths.append(
                write_fixture(
                    root,
                    rkey,
                    value,
                    model=DEFAULT_MODELS["sonnet"],
                    usage=Usage(input_tokens=1_200, output_tokens=600, cache_read_input_tokens=15_000),
                    synthetic=True,
                    notes="Targeted re-ask returns the corrected value (output is a Field JSON object).",
                )
            )
    return paths


def write_manifest(root: Path) -> Path:
    path = root / "DELIBERATE_ERRORS.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DELIBERATE_ERRORS, indent=2) + "\n", encoding="utf-8")
    return path

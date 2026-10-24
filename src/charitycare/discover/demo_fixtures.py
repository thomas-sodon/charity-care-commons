"""Synthetic replay fixtures for the discovery stage's LLM steps on the demo corpus.

* ``classify_doc@v1/haiku/<doc_sha>.json`` - one per demo document, labelled from the manifest.
* ``discover_agent@v1/sonnet/<target_key>.stepNN.json`` - one plausible agent trajectory per demo
  organization: read Schedule H context, walk from the home page to the financial-assistance page
  (two clicks deep for Pineywoods), record every linked PDF, finish.

They are marked ``synthetic: true``; recording real trajectories with ``CCC_LLM=record`` replaces them.
Regenerate with ``python -m charitycare.discover.demo_fixtures``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from charitycare.config import get_settings
from charitycare.discover import classify
from charitycare.discover.agent import AGENT_PROMPT_VERSION, Target
from charitycare.discover.links import extract_links
from charitycare.discover.models import AgentAction
from charitycare.discover.transports import load_url_map, lookup
from charitycare.llm import Usage, write_fixture

# Hospitals whose financial-assistance page is reached by clicking through from the home page.
CLICK_PATHS: dict[str, tuple[str, ...]] = {
    "www.examplepineywoods.example": ("/", "/patients/", "/billing/", "/financial-assistance/"),
    "www.lonestarexamplehealth.example": ("/", "/financial-assistance/"),
}


def agent_trajectory(target: Target, corpus_root: Path) -> list[AgentAction]:
    """Deterministic trajectory built from the offline web (what a good agent would do)."""
    url_map = load_url_map(corpus_root)
    home = target.homepages[0]
    host = home.split("/")[2]
    actions = [AgentAction(tool="schedule_h_context", ein=target.ein, rationale="Start from the URLs the hospital reported on Schedule H.")]
    pages = [home.rstrip("/") + p for p in CLICK_PATHS.get(host, ("/financial-assistance/",))]
    for p in pages:
        actions.append(AgentAction(tool="fetch_page", url=p, rationale="Follow the financial-assistance navigation."))
    landing = pages[-1]
    path = lookup(url_map, landing)
    links = extract_links(path.read_bytes(), landing) if path else []
    for lk in sorted((lk for lk in links if lk.is_pdf), key=lambda lk: lk.url):
        actions.append(AgentAction(tool="record_candidate", url=lk.url, anchor_text=lk.anchor_text, found_on=landing,
                                   rationale="Linked from the financial-assistance page."))
    actions.append(AgentAction(tool="finish", rationale="Policy, application and summary recorded."))
    return actions


def write_agent_fixtures(targets: list[Target], corpus_root: Path, fixtures_root: Path) -> list[Path]:
    out = []
    for t in targets:
        for n, action in enumerate(agent_trajectory(t, corpus_root)):
            key = {"prompt_version": AGENT_PROMPT_VERSION, "doc_sha256": t.key(), "model_tier": "sonnet", "variant": f"step{n:02d}"}
            out.append(write_fixture(
                fixtures_root, key, action.model_dump(mode="json", exclude_none=True), model="claude-sonnet-5-5",
                usage=Usage(input_tokens=600, output_tokens=60, cache_read_input_tokens=1500 + 900 * n), synthetic=True,
                notes=f"Synthetic agent trajectory for {t.target_id} (charitycare.discover.demo_fixtures)",
            ))
    return out


def demo_targets(corpus_root: Path) -> list[Target]:
    from charitycare.discover.pipeline import targets_from_registry
    from charitycare.seed.pipeline import seed_demo
    from charitycare.store import Registry

    with Registry(":memory:") as reg:
        seed_demo(reg, corpus_root)
        return targets_from_registry(reg)


def write_all(corpus_root: Path | None = None, fixtures_root: Path | None = None, manifest: dict[str, Any] | None = None) -> list[Path]:
    import json

    root = corpus_root or get_settings().demo_dir
    fx = fixtures_root or get_settings().llm_fixtures_dir
    manifest = manifest or json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    return classify.write_demo_fixtures(manifest, fx) + write_agent_fixtures(demo_targets(root), root, fx)


if __name__ == "__main__":
    paths = write_all()
    print(f"wrote {len(paths)} synthetic discovery fixtures")
    sys.exit(0)

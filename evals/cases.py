"""Gold datasets and the per-hospital ``Case`` that every compile scorer consumes.

Gold files use the demo corpus format (``data/demo/gold/<hospital>.json``):
``policy`` (CompiledPolicy JSON with value + quote + page per field),
``schedule_h.expected_crosschecks`` and ``schedule_h.tax_period_end``,
``facility_ids`` and ``hospital_id``. A dataset YAML names the corpus root and
its splits; see ``evals/datasets/demo.yaml`` and docs/GOLD_LABELING.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from evals.compare import FieldDict

EVALS_DIR = Path(__file__).resolve().parent
DATASETS_DIR = EVALS_DIR / "datasets"


@dataclass
class GoldPolicy:
    """One hospital's gold labels."""

    hospital_id: str
    name: str
    fields: dict[str, FieldDict]
    facility_ids: list[str] = field(default_factory=list)
    expected_crosschecks: dict[str, str] = field(default_factory=dict)
    tax_period_end: date | None = None
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> {path, sha256, ...}
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def policy_date(self) -> date | None:
        v = (self.fields.get("effective_or_revision_date") or {}).get("value")
        return date.fromisoformat(v) if isinstance(v, str) else v

    @classmethod
    def from_json(cls, g: dict[str, Any], documents: dict[str, dict[str, Any]] | None = None) -> GoldPolicy:
        sh = g.get("schedule_h") or {}
        tpe = sh.get("tax_period_end")
        return cls(
            hospital_id=g["hospital_id"],
            name=g.get("name", g["hospital_id"]),
            fields=dict(g["policy"]),
            facility_ids=list(g.get("facility_ids", [])),
            expected_crosschecks=dict(sh.get("expected_crosschecks") or {}),
            tax_period_end=date.fromisoformat(tpe) if tpe else None,
            documents=documents or {},
            raw=g,
        )


@dataclass
class Case:
    """Gold and prediction for one hospital, plus what the pipeline reported about it.

    ``pred`` maps field key to Field JSON; ``status`` to the FieldStatus value
    (missing keys default to ``provisional`` when a value is present, else
    ``abstained``). ``citations`` maps each *published* field to whether all
    its quotes verified on the cited page; ``pred_crosschecks`` holds
    CrossCheck JSON dicts; ``tasks`` ReviewTask JSON dicts.
    """

    hospital_id: str
    gold: dict[str, FieldDict]
    pred: dict[str, FieldDict]
    status: dict[str, str] = field(default_factory=dict)
    expected_crosschecks: dict[str, str] = field(default_factory=dict)
    pred_crosschecks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    citations: dict[str, bool] = field(default_factory=dict)
    policy_date: date | None = None
    tax_period_end: date | None = None
    policy_version_id: str | None = None
    cost_usd: float = 0.0
    simulated_cost: bool = False
    naive_ran: bool = False
    naive_free_cutoff: float | str | None = None  # naive-agent answer (None = no answer)
    naive_cost_usd: float = 0.0
    error: str | None = None

    def field_status(self, key: str) -> str:
        if key in self.status:
            return self.status[key]
        f = self.pred.get(key)
        return "provisional" if f and f.get("abstain_reason") is None and f.get("value") is not None else "abstained"

    def published(self, key: str) -> bool:
        return self.field_status(key) in ("provisional", "verified", "disputed")


@dataclass
class Dataset:
    name: str
    root: Path
    splits: dict[str, list[str]]
    version: str
    manifest: dict[str, Any] = field(default_factory=dict, repr=False)

    def hospitals(self, split: str | None) -> list[str]:
        if split in (None, "all"):
            return [h["id"] for h in self.manifest.get("hospitals", [])] or sorted({h for s in self.splits.values() for h in s})
        if split not in self.splits:
            raise KeyError(f"dataset {self.name} has no split {split!r}; known: {sorted(self.splits)}")
        return list(self.splits[split])

    def gold(self, hospital_id: str) -> GoldPolicy:
        entry = next((h for h in self.manifest.get("hospitals", []) if h["id"] == hospital_id), None)
        path = self.root / (entry["gold"] if entry else f"gold/{hospital_id}.json")
        g = json.loads(path.read_text(encoding="utf-8"))
        return GoldPolicy.from_json(g, (entry or {}).get("documents"))

    def documents(self) -> dict[str, Path]:
        """sha256 -> local path of every hashed document in the corpus."""
        out: dict[str, Path] = {}
        for h in self.manifest.get("hospitals", []):
            for d in h.get("documents", {}).values():
                out[d["sha256"]] = self.root / d["path"]
        return out


def load_dataset(name: str = "demo", root: Path | None = None) -> Dataset:
    """Load ``evals/datasets/<name>.yaml``; the corpus root defaults to the settings demo dir."""
    spec = yaml.safe_load((DATASETS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    if root is None:
        from charitycare.config import get_settings

        s = get_settings()
        rel = spec.get("root", "demo")
        root = Path(rel) if Path(rel).is_absolute() else s.data_dir / rel
    manifest_path = root / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    version = f"{manifest.get('corpus_version', spec.get('version', name))}+{str(manifest.get('checksum', ''))[:12]}".rstrip("+")
    return Dataset(name=name, root=root, splits={k: list(v) for k, v in (spec.get("splits") or {}).items()},
                   version=version, manifest=manifest)

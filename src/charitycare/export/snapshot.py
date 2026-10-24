"""Release snapshots: registry -> CSV + Parquet via DuckDB, with a CC BY 4.0 notice and dataset card.

``export_release(registry, out_dir)`` writes four tables (``fields``,
``crosschecks``, ``facilities``, ``policies``) as both CSV and Parquet, a
Hugging Face dataset card (``README.md`` with YAML front matter), the data
licence notice and ``MANIFEST.json`` (row counts and sha256 of every file).
Parquet files carry the licence and release version as key-value metadata.

The snapshot holds registry data only: there is no household, bill or
pay-stub data anywhere in the registry, so none can leak into a release.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from charitycare import __version__
from charitycare.api import present, queries
from charitycare.config import get_settings
from charitycare.schemas import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    CrossCheckResult,
    FieldStatus,
    PolicyVersion,
    utcnow,
)
from charitycare.store import Registry

LICENSE_ID = "CC-BY-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
ATTRIBUTION = "Charity Care Commons (Texas edition)"
NOTICE = (
    f"{ATTRIBUTION} data release. Licensed under the Creative Commons Attribution 4.0 International "
    f"licence ({LICENSE_URL}). Credit \"Charity Care Commons\" and link to the source. Quotes are excerpts of "
    "hospital financial-assistance policies, reproduced for reference. This is legal information, not legal advice; "
    "provisional fields have not been verified by a reviewer."
)
TABLES = ("fields", "crosschecks", "facilities", "policies")


@dataclass
class ReleaseManifest:
    version: str
    out_dir: Path
    rows: dict[str, int] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # relative path -> sha256

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "license": LICENSE_ID, "license_url": LICENSE_URL, "attribution": ATTRIBUTION,
            "schema_version": SCHEMA_VERSION, "schema_id": SCHEMA_ID, "generator": f"charitycare {__version__}",
            "rows": self.rows, "files": self.files,
        }


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, sort_keys=True, ensure_ascii=False)


def field_rows(reg: Registry, *, verified_only: bool = False) -> list[dict[str, Any]]:
    pvs = {p.id: p for p in reg.select(PolicyVersion)}
    rows = []
    for pv_id in sorted(pvs):
        fids = queries.facilities_for_policy(reg, pv_id)
        for rec in queries.fields_for_policy(reg, pv_id):
            if verified_only and rec.status not in (FieldStatus.verified, FieldStatus.abstained):
                continue
            f = present.typed_field(rec.key, rec.field)
            dumped = f.model_dump(mode="json")
            ev = f.evidence[0] if f.evidence else None
            results = sorted({c.result.value for c in rec.crosschecks})
            rows.append({
                "field_id": rec.id, "policy_version_id": pv_id, "organization_ein": pvs[pv_id].organization_ein,
                "facility_ids": ",".join(fids), "key": rec.key, "label": present.label(rec.key), "status": rec.status.value,
                "value_json": _j(dumped["value"]), "display": present.field_display(rec), "abstain_reason": f.abstain_reason,
                "confidence": float(f.confidence), "quote": ev.quote if ev else None, "page": ev.page if ev else None,
                "section_label": ev.section_label if ev else None, "doc_sha256": ev.doc_sha256 if ev else None,
                "n_evidence": len(f.evidence), "evidence_json": _j(dumped["evidence"]),
                "schedule_h_results": ",".join(results), "as_of": rec.as_of.isoformat(),
                "extractor_model_tier": rec.extractor_model_tier, "prompt_version": rec.prompt_version,
                "reviewed_at": rec.reviewed_at.isoformat() if rec.reviewed_at else None,
            })
    return rows


def crosscheck_rows(reg: Registry) -> list[dict[str, Any]]:
    rows = []
    for pv in sorted(reg.select(PolicyVersion), key=lambda p: p.id):
        for rec in queries.fields_for_policy(reg, pv.id):
            for cc in rec.crosschecks:
                disp = present.crosscheck_display(
                    cc, reg.discrepancy_state(cc) if cc.result is CrossCheckResult.disagree_unexplained else None)
                rows.append({
                    "field_id": rec.id, "policy_version_id": pv.id, "field_key": cc.field_key, "source": cc.source,
                    "external_ref": cc.external_ref, "external_value_json": _j(cc.model_dump(mode="json")["external_value"]),
                    "filing_period_end": cc.filing_period_end.isoformat() if cc.filing_period_end else None,
                    "policy_date": cc.policy_date.isoformat() if cc.policy_date else None, "result": cc.result.value,
                    "public_label": disp["public_label"], "note": cc.note,
                })
    return rows


def facility_rows(reg: Registry) -> list[dict[str, Any]]:
    rows = []
    for s in queries.list_facilities(reg, state=None):
        rows.append({k: s[k] for k in ("id", "ccn", "tx_license_no", "name", "address", "city", "zip", "county", "state",
                                       "type", "ownership", "ein", "schedule_h_group_letter", "coverage",
                                       "policy_version_id", "fields_total", "fields_verified", "irs_check",
                                       "free_max_pct", "discount_max_pct", "asset_test")})
    return rows


def policy_rows(reg: Registry) -> list[dict[str, Any]]:
    return [{
        "id": p.id, "organization_ein": p.organization_ein, "title": p.title,
        "effective_date": p.effective_date.isoformat() if p.effective_date else None, "status": p.status,
        "as_of": p.as_of.isoformat(), "supersedes": p.supersedes, "doc_shas_json": _j(p.doc_shas),
        "facility_ids": ",".join(queries.facilities_for_policy(reg, p.id)),
    } for p in sorted(reg.select(PolicyVersion), key=lambda p: p.id)]


SCHEMAS: dict[str, pa.Schema] = {
    "fields": pa.schema([("field_id", pa.string()), ("policy_version_id", pa.string()), ("organization_ein", pa.string()),
                         ("facility_ids", pa.string()), ("key", pa.string()), ("label", pa.string()), ("status", pa.string()),
                         ("value_json", pa.string()), ("display", pa.string()), ("abstain_reason", pa.string()),
                         ("confidence", pa.float64()), ("quote", pa.string()), ("page", pa.int32()),
                         ("section_label", pa.string()), ("doc_sha256", pa.string()), ("n_evidence", pa.int32()),
                         ("evidence_json", pa.string()), ("schedule_h_results", pa.string()), ("as_of", pa.string()),
                         ("extractor_model_tier", pa.string()), ("prompt_version", pa.string()), ("reviewed_at", pa.string())]),
    "crosschecks": pa.schema([("field_id", pa.string()), ("policy_version_id", pa.string()), ("field_key", pa.string()),
                              ("source", pa.string()), ("external_ref", pa.string()), ("external_value_json", pa.string()),
                              ("filing_period_end", pa.string()), ("policy_date", pa.string()), ("result", pa.string()),
                              ("public_label", pa.string()), ("note", pa.string())]),
    "facilities": pa.schema([("id", pa.string()), ("ccn", pa.string()), ("tx_license_no", pa.string()), ("name", pa.string()),
                             ("address", pa.string()), ("city", pa.string()), ("zip", pa.string()), ("county", pa.string()),
                             ("state", pa.string()), ("type", pa.string()), ("ownership", pa.string()), ("ein", pa.string()),
                             ("schedule_h_group_letter", pa.string()), ("coverage", pa.string()),
                             ("policy_version_id", pa.string()), ("fields_total", pa.int32()), ("fields_verified", pa.int32()),
                             ("irs_check", pa.string()), ("free_max_pct", pa.float64()), ("discount_max_pct", pa.float64()),
                             ("asset_test", pa.bool_())]),
    "policies": pa.schema([("id", pa.string()), ("organization_ein", pa.string()), ("title", pa.string()),
                           ("effective_date", pa.string()), ("status", pa.string()), ("as_of", pa.string()),
                           ("supersedes", pa.string()), ("doc_shas_json", pa.string()), ("facility_ids", pa.string())]),
}


def snapshot_tables(reg: Registry, *, verified_only: bool = False) -> dict[str, pa.Table]:
    data = {"fields": field_rows(reg, verified_only=verified_only), "crosschecks": crosscheck_rows(reg),
            "facilities": facility_rows(reg), "policies": policy_rows(reg)}
    return {name: pa.Table.from_pylist(rows, schema=SCHEMAS[name]) for name, rows in data.items()}


def _sql_path(p: Path) -> str:
    return "'" + str(p).replace("'", "''") + "'"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def export_release(reg: Registry, out_dir: str | Path | None = None, *, version: str | None = None,
                   verified_only: bool = False, today: date | None = None) -> ReleaseManifest:
    """Write the CC BY 4.0 release snapshot; returns its manifest."""
    today = today or utcnow().date()
    version = version or f"v{today:%Y.%m.%d}"
    out = Path(out_dir) if out_dir else get_settings().data_dir / "release" / version
    out.mkdir(parents=True, exist_ok=True)
    manifest = ReleaseManifest(version=version, out_dir=out)
    tables = snapshot_tables(reg, verified_only=verified_only)
    con = duckdb.connect()
    try:
        for name, tbl in tables.items():
            con.register(f"t_{name}", tbl)
            order = "1" if name != "fields" else "policy_version_id, key"
            meta = f"KV_METADATA {{license: '{LICENSE_ID}', release: '{version}', attribution: '{ATTRIBUTION}'}}"
            pq, csv = out / f"{name}.parquet", out / f"{name}.csv"
            con.execute(f"COPY (SELECT * FROM t_{name} ORDER BY {order}) TO {_sql_path(pq)} "
                        f"(FORMAT PARQUET, COMPRESSION ZSTD, {meta})")
            con.execute(f"COPY (SELECT * FROM t_{name} ORDER BY {order}) TO {_sql_path(csv)} (FORMAT CSV, HEADER)")
            manifest.rows[name] = tbl.num_rows
    finally:
        con.close()
    (out / "LICENSE.txt").write_text(_license_text(), encoding="utf-8")
    (out / "README.md").write_text(dataset_card(manifest, verified_only=verified_only, today=today), encoding="utf-8")
    for p in sorted(out.iterdir()):
        if p.is_file() and p.name != "MANIFEST.json":
            manifest.files[p.name] = _sha(p)
    (out / "MANIFEST.json").write_text(json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _license_text() -> str:
    repo_license = get_settings().home / "DATA_LICENSE"
    body = repo_license.read_text(encoding="utf-8") if repo_license.is_file() else ""
    return NOTICE + "\n\n" + (body or f"Full licence text: {LICENSE_URL}\n")


def dataset_card(m: ReleaseManifest, *, verified_only: bool, today: date) -> str:
    """Hugging Face dataset card (YAML front matter + Markdown)."""
    n_fields = m.rows.get("fields", 0)
    size = "n<1K" if n_fields < 1_000 else ("1K<n<10K" if n_fields < 10_000 else "10K<n<100K")
    configs = "\n".join(
        f"- config_name: {t}\n  data_files:\n  - split: train\n    path: {t}.parquet" for t in TABLES
    )
    rows = "\n".join(f"| `{t}` | {m.rows.get(t, 0):,} |" for t in TABLES)
    return f"""---
license: cc-by-4.0
pretty_name: Charity Care Commons (Texas)
language:
- en
- es
tags:
- healthcare
- hospital-financial-assistance
- charity-care
- irs-990-schedule-h
- legal-information
size_categories:
- {size}
configs:
{configs}
---

# Charity Care Commons: Texas hospital financial-assistance rules

Release `{m.version}` ({today.isoformat()}), Open FAP Schema {SCHEMA_VERSION} (`{SCHEMA_ID}`).

Machine-readable rules from Texas nonprofit hospitals' financial-assistance policies (IRS section 501(r)). Every
value carries a verbatim quote, the page it is on and the sha256 of the hashed source document, a verification
status, and a cross-check against the hospital's IRS Form 990 Schedule H (Part V).

## Tables

| Table | Rows |
|---|---|
{rows}

* **fields**: one row per compiled policy field. `value_json` holds the typed value (see the Open FAP Schema),
  `status` is `verified` (reviewer-approved), `provisional` (auto-extracted, not verified), `abstained` (the policy
  does not say, or says it at the hospital's discretion; see `abstain_reason`) or `disputed`. `quote`, `page` and
  `doc_sha256` cite the first piece of evidence; `evidence_json` has all of it.
* **crosschecks**: comparisons with Schedule H. `result` is `agree`, `disagree_policy_newer_than_filing` (context:
  the policy postdates the filing period), `disagree_filing_lag_possible` (context: the
  policy date is unknown, so filing lag cannot be ruled out), `disagree_unexplained` (shown publicly as a *discrepancy for
  review* only after a reviewer verified the field and the hospital's 10-business-day reply window passed or it
  replied; never as a finding) or `not_reported`.
* **facilities**: the facility universe (CMS CCN, Texas licence, EIN, Schedule H reporting group) with coverage.
* **policies**: policy versions, their documents and the facilities they govern.

{"This release includes only reviewer-verified fields and confirmed abstentions." if verified_only else
 "This release includes provisional fields; filter on `status = 'verified'` for reviewer-verified values only."}

## How it was made

The LLM extracts; code computes; humans verify. Quotes are checked against the document's own text layer before
anything is published ("no quote, no publish"); Schedule H disagreements are adjudicated with the filing period
in view; hospitals can reply before a discrepancy is shown. See the methods page of the site for details.

## Privacy

The dataset contains no information about patients or households. Screening and packet requests are processed in
memory and never stored.

## Licence and citation

Data: [CC BY 4.0]({LICENSE_URL}). Please credit "{ATTRIBUTION}" and link to the source. Code: Apache-2.0.
This is legal information, not legal advice; hospitals make the final decision on every application.

```bibtex
@misc{{charitycarecommons_{m.version.replace('.', '_')},
  title  = {{Charity Care Commons: Texas hospital financial-assistance rules, release {m.version}}},
  year   = {{{today.year}}},
  note   = {{CC BY 4.0}}
}}
```
"""

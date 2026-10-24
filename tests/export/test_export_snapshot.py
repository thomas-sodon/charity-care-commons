"""Release snapshot: DuckDB -> CSV + Parquet with CC BY 4.0 notice, manifest and dataset card."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path

import duckdb

from charitycare.export import export_release, snapshot_tables
from charitycare.schemas import CrossCheck, FieldRecord
from charitycare.store import Registry

TODAY = date(2026, 10, 24)


def test_release_files_and_counts(seeded: Registry, tmp_path: Path):
    m = export_release(seeded, tmp_path / "rel", today=TODAY)
    out = m.out_dir
    assert m.version == "v2026.10.24"
    for t in ("fields", "crosschecks", "facilities", "policies"):
        assert (out / f"{t}.csv").is_file() and (out / f"{t}.parquet").is_file()
    assert m.rows == {"fields": seeded.count(FieldRecord), "crosschecks": seeded.count(CrossCheck), "facilities": 10, "policies": 8}
    pq = str(out / "fields.parquet")
    assert duckdb.sql(f"SELECT count(*) FROM '{pq}'").fetchall()[0][0] == m.rows["fields"]
    meta = {k.decode(): v.decode() for _, k, v in duckdb.sql(f"SELECT * FROM parquet_kv_metadata('{pq}')").fetchall()}
    assert meta["license"] == "CC-BY-4.0" and meta["release"] == "v2026.10.24"
    with open(out / "crosschecks.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == m.rows["crosschecks"]
    disc = [r for r in rows if r["result"] == "disagree_unexplained"]
    assert len(disc) == 1 and disc[0]["public_label"] == "Discrepancy for review"


def test_field_rows_carry_citations(seeded: Registry):
    t = snapshot_tables(seeded)["fields"].to_pylist()
    tiers = next(r for r in t if r["field_id"] == "pv:example-methodist:demo:fpg_tiers")
    assert tiers["status"] == "verified" and tiers["page"] == 3 and len(tiers["doc_sha256"]) == 64
    assert json.loads(tiers["value_json"])[0]["max_pct_inclusive"] == 200
    assert tiers["facility_ids"] == "tx-459901" and tiers["schedule_h_results"] == "agree"
    abst = [r for r in t if r["status"] == "abstained"]
    assert abst and all(r["value_json"] is None and r["abstain_reason"] for r in abst)


def test_manifest_license_and_card(seeded: Registry, tmp_path: Path):
    m = export_release(seeded, tmp_path / "rel", today=TODAY)
    out = m.out_dir
    manifest = json.loads((out / "MANIFEST.json").read_text())
    assert manifest["license"] == "CC-BY-4.0" and manifest["rows"] == m.rows
    for name, sha in manifest["files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == sha
    assert "Creative Commons Attribution 4.0" in (out / "LICENSE.txt").read_text()
    card = (out / "README.md").read_text()
    front = card.split("---\n")[1]
    assert "license: cc-by-4.0" in front and "config_name: fields" in front and "path: fields.parquet" in front
    assert "size_categories:\n- n<1K" in front
    assert "includes provisional fields" in card and "no information about patients or households" in card


def test_verified_only_and_no_household_columns(seeded: Registry, tmp_path: Path):
    m = export_release(seeded, tmp_path / "v", verified_only=True, today=TODAY)
    statuses = {r[0] for r in duckdb.sql(f"SELECT DISTINCT status FROM '{m.out_dir / 'fields.parquet'}'").fetchall()}
    assert statuses == {"verified", "abstained"}
    assert "only reviewer-verified" in (m.out_dir / "README.md").read_text()
    for t in snapshot_tables(seeded).values():
        cols = " ".join(t.column_names).lower()
        for banned in ("income", "household", "ssn", "dob", "account_number", "patient"):
            assert banned not in cols


def test_export_deterministic(seeded: Registry, tmp_path: Path):
    a = export_release(seeded, tmp_path / "a", today=TODAY)
    b = export_release(seeded, tmp_path / "b", today=TODAY)
    for name in ("fields.csv", "crosschecks.csv", "facilities.csv", "policies.csv", "README.md"):
        assert a.files[name] == b.files[name]

"""Build the synthetic demo corpus under ``data/demo/`` (deterministic).

Layout::

    data/demo/
      MANIFEST.json                      hospitals, facilities, documents (+sha256, URL), corpus checksum
      docs/<hospital>/fap.pdf            financial assistance policy
      docs/<hospital>/application_<lang>.pdf
      docs/<hospital>/pls_<lang>.pdf     plain-language summary
      gold/<hospital>.json               ground truth: CompiledPolicy + form templates + expectations
      irs/xml/<OBJECT_ID>_public.xml     Schedule H returns (IRS e-file layout)
      irs/index_2026_demo.csv            IRS index CSV layout
      irs/2026_TEOS_XML_DEMO.zip         the same XML as an IRS-style batch zip
      cms/Hospital_General_Information.csv
      web/<domain>/...html + web/url_map.json   offline site for discovery

Replay fixtures go to ``fixtures/llm/`` (see :mod:`charitycare.demo.fixtures`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pymupdf

from charitycare.config import get_settings
from charitycare.demo import cms, forms, irs, web
from charitycare.demo.fap import build_fap, gold_policy
from charitycare.demo.fixtures import write_compile_fixtures, write_manifest
from charitycare.demo.render import render_pages
from charitycare.demo.spec import HOSPITALS, HospitalSpec
from charitycare.pdftext import sha256_bytes

CORPUS_VERSION = "demo-2026.10"


def facility_id(ccn: str) -> str:
    return f"tx-{ccn}"


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def _jdump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"


def _expected_crosschecks(h: HospitalSpec) -> dict[str, str]:
    """What a correct cross-check against the hospital's Schedule H should conclude."""
    sh = h.schedule_h
    newer = h.effective_date > sh.tax_period_end

    def res(agree: bool) -> str:
        if agree:
            return "agree"
        return "disagree_policy_newer_than_filing" if newer else "disagree_unexplained"

    free = next(t.hi for t in h.tiers if t.benefit == "free")
    top = max(t.hi for t in h.tiers)
    return {
        "fpg_tiers": res(free == sh.free_pct and top == sh.discount_pct),
        "asset_test": res((h.asset_threshold is not None) == sh.asset),
        "residency": res(bool(h.residency_counties or h.residency_state) == sh.residency),
        "agb_method": res(h.agb_method == sh.agb),
    }


def build_hospital(h: HospitalSpec, out: Path, fixtures_root: Path) -> dict[str, Any]:
    docs_dir = out / "docs" / h.id
    footer = f"{h.system_name or h.name} - FICTIONAL SYNTHETIC DOCUMENT"
    writer, table = build_fap(h)
    fap = render_pages(f"{h.name} - {h.policy_title}", writer.pages, footer)
    fap_sha = sha256_bytes(fap)
    _write(docs_dir / "fap.pdf", fap)
    base = f"{h.base_url}/financial-assistance"
    documents: dict[str, dict[str, Any]] = {
        "fap": {
            "path": f"docs/{h.id}/fap.pdf",
            "sha256": fap_sha,
            "pages": len(writer.pages),
            "image_only_pages": list(h.image_only_fap_pages),
            "language": "en",
            "url": f"{base}/fap.pdf",
            "kind": "fap",
        }
    }
    for form in h.forms:
        pdf, specs = forms.build_application(h, form)
        sha = sha256_bytes(pdf)
        name = f"application_{form.language}"
        _write(docs_dir / f"{name}.pdf", pdf)
        documents[name] = {
            "path": f"docs/{h.id}/{name}.pdf",
            "sha256": sha,
            "pages": 2,
            "image_only_pages": [1, 2] if form.scanned else [],
            "language": form.language,
            "url": f"{base}/application.pdf" if form.language == "en" else f"{base}/application-es.pdf",
            "kind": "application",
            "form_kind": "fillable" if form.kind == "fillable" and not form.scanned else "flat",
            "form_template": forms.application_template(sha, form, specs).model_dump(mode="json"),
        }
    for lang in h.pls_languages:
        pdf = forms.build_pls(h, lang)
        name = f"pls_{lang}"
        _write(docs_dir / f"{name}.pdf", pdf)
        documents[name] = {
            "path": f"docs/{h.id}/{name}.pdf",
            "sha256": sha256_bytes(pdf),
            "pages": 1,
            "image_only_pages": [],
            "language": lang,
            "url": f"{base}/summary.pdf" if lang == "en" else f"{base}/summary-es.pdf",
            "kind": "pls",
        }

    gold = gold_policy(h, writer, fap_sha)
    write_compile_fixtures(fixtures_root, h, gold, fap_sha, len(writer.pages))
    xml = irs.build_return(h)
    _write(out / "irs" / "xml" / f"{h.schedule_h.object_id}_public.xml", xml)

    gold_doc = {
        "corpus_version": CORPUS_VERSION,
        "synthetic": True,
        "hospital_id": h.id,
        "name": h.name,
        "system_name": h.system_name,
        "ein": h.ein,
        "facility_ids": [facility_id(f.ccn) for f in h.facilities],
        "documents": documents,
        "policy": gold.model_dump(mode="json"),
        "income_table": (
            {
                "stated_year": table.stated_year,
                "computed_from_year": table.computed_from_year,
                "page": table.page,
                "pcts": table.pcts,
                "rows": table.rows,
                "expected_round_trip": "match" if table.stated_year == table.computed_from_year else "mismatch",
            }
            if table
            else None
        ),
        "schedule_h": {
            "object_id": h.schedule_h.object_id,
            "tax_period_end": h.schedule_h.tax_period_end.isoformat(),
            "group_letter": "A" if len(h.facilities) > 1 else None,
            "expected_crosschecks": _expected_crosschecks(h),
        },
        "notes": list(h.notes),
    }
    _write(out / "gold" / f"{h.id}.json", _jdump(gold_doc))
    return {
        "id": h.id,
        "name": h.name,
        "system_name": h.system_name,
        "ein": h.ein,
        "domain": h.domain,
        "facilities": [
            {"facility_id": facility_id(f.ccn), **asdict(f)} for f in h.facilities
        ],
        "documents": {k: {kk: v[kk] for kk in ("path", "sha256", "url", "kind", "language")} for k, v in documents.items()},
        "schedule_h_object_id": h.schedule_h.object_id,
        "gold": f"gold/{h.id}.json",
    }


def _url_to_path(url: str) -> str:
    u = urlparse(url)
    path = u.path if not u.path.endswith("/") else u.path + "index.html"
    return f"web/{u.netloc}{path}"


def build_corpus(out_dir: Path | None = None, fixtures_dir: Path | None = None) -> dict[str, Any]:
    """Generate the full corpus. Returns the manifest (also written to ``MANIFEST.json``)."""
    s = get_settings()
    out = Path(out_dir) if out_dir else s.demo_dir
    fixtures_root = Path(fixtures_dir) if fixtures_dir else s.llm_fixtures_dir
    pymupdf.TOOLS.set_small_glyph_heights(False)
    hospitals = [build_hospital(h, out, fixtures_root) for h in HOSPITALS]
    write_manifest(fixtures_root)

    xml_files = {p.name: p.read_bytes() for p in sorted((out / "irs" / "xml").glob("*_public.xml"))}
    _write(out / "irs" / "index_2026_demo.csv", irs.index_rows(HOSPITALS))
    _write(out / "irs" / f"{irs.BATCH_ID}.zip", irs.batch_zip(xml_files))
    _write(out / "cms" / "Hospital_General_Information.csv", cms.build_csv())

    url_map: dict[str, str] = {}
    for h, entry in zip(HOSPITALS, hospitals):
        doc_urls = {k: v["url"] for k, v in entry["documents"].items()}
        for url, page in web.site_pages(h, doc_urls).items():
            rel = _url_to_path(url)
            _write(out / rel, page)
            url_map[url] = rel
        for v in entry["documents"].values():
            url_map[v["url"]] = v["path"]
    _write(out / "web" / "url_map.json", _jdump(dict(sorted(url_map.items()))))

    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "MANIFEST.json")
    digest = hashlib.sha256()
    for p in files:
        digest.update(p.relative_to(out).as_posix().encode())
        digest.update(hashlib.sha256(p.read_bytes()).digest())
    manifest = {
        "corpus_version": CORPUS_VERSION,
        "synthetic": True,
        "notice": "All hospitals, people, identifiers and URLs in this corpus are fictional.",
        "hospitals": hospitals,
        "irs": {"index": "irs/index_2026_demo.csv", "zip": f"irs/{irs.BATCH_ID}.zip", "xml_dir": "irs/xml"},
        "cms": "cms/Hospital_General_Information.csv",
        "web_url_map": "web/url_map.json",
        "fixtures": str(fixtures_root.relative_to(s.home)) if fixtures_root.is_relative_to(s.home) else str(fixtures_root),
        "files": len(files),
        "checksum": digest.hexdigest(),
    }
    _write(out / "MANIFEST.json", _jdump(manifest))
    return manifest


def load_manifest(demo_dir: Path | None = None) -> dict[str, Any]:
    d = Path(demo_dir) if demo_dir else get_settings().demo_dir
    return json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))


def load_gold(hospital_id: str, demo_dir: Path | None = None) -> dict[str, Any]:
    d = Path(demo_dir) if demo_dir else get_settings().demo_dir
    return json.loads((d / "gold" / f"{hospital_id}.json").read_text(encoding="utf-8"))

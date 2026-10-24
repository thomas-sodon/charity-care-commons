"""Offline stand-in for the seed and discover stages, for compile demos and tests.

:func:`load_demo_registry` fills a registry from the synthetic corpus the way
``seed/`` (Schedule H Part V, CMS facilities) and ``discover/`` (sources,
document versions, policy versions) would, but straight from the corpus
specs, so the compile stage can be exercised end to end without them. It does
not create facility -> policy mappings: resolving those is the compile stage's
job (:mod:`charitycare.compile.mapping`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from charitycare.config import get_settings
from charitycare.demo.build import load_manifest
from charitycare.demo.spec import HOSPITALS, HospitalSpec
from charitycare.schemas import (
    DocumentVersion,
    Facility,
    Organization,
    PolicyVersion,
    ScheduleHFacility,
    Source,
)
from charitycare.store import Registry

DEMO_FETCHED_AT = datetime(2026, 10, 1, tzinfo=UTC)
DEMO_AS_OF = date(2026, 10, 1)


def schedule_h_from_spec(h: HospitalSpec) -> ScheduleHFacility:
    """The ScheduleHFacility a Part V parser would produce for this hospital's return."""
    sh = h.schedule_h
    group = "A" if len(h.facilities) > 1 else None
    fap_url = f"{h.base_url}/financial-assistance/fap.pdf"
    return ScheduleHFacility(
        id=f"{sh.object_id}:{group or 1}",
        ein=h.ein,
        org_name=(h.system_name or h.name).upper(),
        object_id=sh.object_id,
        tax_year=sh.tax_period_begin.year,
        tax_period_begin=sh.tax_period_begin,
        tax_period_end=sh.tax_period_end,
        group_letter=group,
        facility_numbers=list(range(1, len(h.facilities) + 1)),
        facility_names=[f.name.upper() for f in h.facilities],
        line_13_criteria_explained=True,
        line_13a_fpg_used=True,
        line_13a_free_pct=float(sh.free_pct),
        line_13a_discount_pct=float(sh.discount_pct),
        line_13b_income_level=True,
        line_13c_asset_level=True if sh.asset else None,
        line_13d_medical_indigency=True if sh.medical_indigency else None,
        line_13e_insurance_status=True if sh.insurance else None,
        line_13f_underinsurance_status=True if sh.underinsurance else None,
        line_13g_residency=True if sh.residency else None,
        line_15a_described_info=True,
        line_15b_described_docs=True,
        line_16a_fap_url="SEE PART V, SECTION C" if sh.fap_url_in_narrative else fap_url,
        line_16b_app_url=f"{h.base_url}/financial-assistance/application.pdf",
        line_16c_pls_url=f"{h.base_url}/financial-assistance/summary.pdf",
        line_16i_translated=True if sh.translated else None,
        line_18_ecas_permitted=list(sh.ecas),  # type: ignore[arg-type]
        line_22_agb_method=sh.agb,  # type: ignore[arg-type]
        narrative_urls_recovered=[fap_url] if sh.fap_url_in_narrative else [],
        section_c_text=[f"THE FINANCIAL ASSISTANCE POLICY IS AVAILABLE AT {fap_url}"] if sh.fap_url_in_narrative else [],
    )


def policy_version_id(hospital_id: str) -> str:
    return f"pv:{hospital_id}:demo"


def load_demo_registry(registry: Registry, demo_dir: Path | None = None) -> dict[str, Any]:
    """Load organizations, facilities, Schedule H, sources, documents and policy versions."""
    root = Path(demo_dir) if demo_dir else get_settings().demo_dir
    manifest = load_manifest(root)
    specs = {h.id: h for h in HOSPITALS}
    with registry.transaction():
        for hm in manifest["hospitals"]:
            h = specs[hm["id"]]
            registry.upsert_organization(Organization(ein=h.ein, name=h.name, system_name=h.system_name))
            group = "A" if len(h.facilities) > 1 else None
            fac_ids = []
            for fm in hm["facilities"]:
                fac_ids.append(fm["facility_id"])
                registry.upsert_facility(Facility(
                    id=fm["facility_id"], ccn=fm["ccn"], tx_license_no=fm["license_no"], name=fm["name"],
                    address=fm["address"], city=fm["city"], zip=fm["zip"], county=fm["county"], type=fm["cms_type"],
                    ownership=h.ownership, ein=h.ein, schedule_h_group_letter=group,
                ))
            registry.add_schedule_h(schedule_h_from_spec(h))
            doc_shas: dict[str, str] = {}
            for name, d in hm["documents"].items():
                src_id = f"src:{hm['id']}:{name}"
                registry.upsert_source(Source(id=src_id, url=d["url"], kind=d["kind"], discovered_via="demo",
                                              first_seen=DEMO_FETCHED_AT, last_checked=DEMO_FETCHED_AT, http_status=200,
                                              facility_ids=fac_ids))
                registry.add_document_version(DocumentVersion(sha256=d["sha256"], source_id=src_id, kind=d["kind"],
                                                              fetched_at=DEMO_FETCHED_AT, language=d["language"],
                                                              pdf_uri=str((root / d["path"]).resolve())))
                doc_shas[name] = d["sha256"]
            registry.add_policy_version(PolicyVersion(id=policy_version_id(hm["id"]), organization_ein=h.ein,
                                                      title=h.policy_title, effective_date=None, doc_shas=doc_shas,
                                                      as_of=DEMO_AS_OF))
    return manifest

"""Seed a registry from the synthetic demo corpus, for the API/site demo and tests.

This is the read side's offline stand-in for running seed -> discover ->
compile -> review: it loads organizations, facilities, sources, document
versions, policy versions and facility mappings from ``MANIFEST.json``, and
fields from the gold policies. Gold labels play the role of reviewed fields,
with a few deliberate gaps so every public state is exercised:

* ``screening_timing`` stays *provisional* everywhere (Screen 1's "○");
* gold abstentions are stored as ``abstained``;
* Schedule H cross-checks follow ``gold.schedule_h.expected_crosschecks``, and
  each non-agreeing one opens a ``crosscheck_disagreement`` review task;
* one source (Pineywoods' plain-language summary) answers HTTP 404, so the
  link-health feed has a dead link whose hashed snapshot is still on file;
* Example Methodist carries a published hospital response to its asset-test
  discrepancy (right of reply, H6).

The real pipeline writes the same tables, so nothing downstream depends on
this module. All content is synthetic (fictional hospitals, ``.example`` domains).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from charitycare.config import get_settings
from charitycare.demo.spec import HOSPITALS, HospitalSpec
from charitycare.schemas import (
    DETERMINATION_FIELDS,
    ChangeEvent,
    CrossCheck,
    CrossCheckResult,
    DocumentVersion,
    Facility,
    Field,
    FieldRecord,
    FieldStatus,
    FormTemplate,
    HospitalResponse,
    Organization,
    PolicyFacilityMap,
    PolicyVersion,
    ReviewTask,
    ScheduleHFacility,
    Source,
)
from charitycare.store import Registry

DEMO_AS_OF = date(2026, 10, 1)
DEMO_FETCHED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DEMO_REVIEWED_AT = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
PROVISIONAL_KEYS: tuple[str, ...] = ("screening_timing",)
REVIEWER = "demo-reviewer"
# One dead link so the link-health surfaces have something to show (the snapshot is still served).
BROKEN_LINKS: dict[str, int] = {"src:pineywoods-regional:pls_en": 404}


def policy_version_id(hospital_id: str) -> str:
    """Same id scheme as ``compile.demo`` and ``engine.policy``."""
    return f"pv:{hospital_id}:demo"


def _external_value(h: HospitalSpec, key: str) -> Any:
    sh = h.schedule_h
    return {
        "fpg_tiers": {"free_pct": float(sh.free_pct), "discount_pct": float(sh.discount_pct)},
        "asset_test": sh.asset,
        "residency": sh.residency,
        "insured_patients": sh.insurance,
        "agb_method": sh.agb,
    }.get(key)


_LINE = {"fpg_tiers": "Part V line 13a", "asset_test": "Part V line 13c", "residency": "Part V line 13g",
         "insured_patients": "Part V line 13e", "agb_method": "Part V line 22"}


def _schedule_h(h: HospitalSpec) -> ScheduleHFacility:
    sh = h.schedule_h
    group = "A" if len(h.facilities) > 1 else None
    return ScheduleHFacility(
        id=f"{sh.object_id}:{group or 1}", ein=h.ein, org_name=(h.system_name or h.name).upper(), object_id=sh.object_id,
        tax_year=sh.tax_period_begin.year, tax_period_begin=sh.tax_period_begin, tax_period_end=sh.tax_period_end,
        group_letter=group, facility_numbers=list(range(1, len(h.facilities) + 1)),
        facility_names=[f.name.upper() for f in h.facilities], line_13a_fpg_used=True,
        line_13a_free_pct=float(sh.free_pct), line_13a_discount_pct=float(sh.discount_pct),
        line_13c_asset_level=True if sh.asset else None, line_13e_insurance_status=True if sh.insurance else None,
        line_13g_residency=True if sh.residency else None,
        line_16a_fap_url="SEE PART V, SECTION C" if sh.fap_url_in_narrative else f"{h.base_url}/financial-assistance/fap.pdf",
        line_22_agb_method=sh.agb,  # type: ignore[arg-type]
    )


def _impact_task(task_id: str, kind: str, priority: str, impact: int, notes: str, **kw: Any) -> ReviewTask:
    return ReviewTask(id=task_id, kind=kind, priority=priority, notes=f"[impact {impact}] {notes}",  # type: ignore[arg-type]
                      created_at=DEMO_FETCHED_AT, **kw)


def seed_demo(registry: Registry, demo_dir: str | Path | None = None) -> dict[str, Any]:
    """Load the demo corpus into ``registry``; returns the corpus manifest."""
    root = Path(demo_dir) if demo_dir else get_settings().demo_dir
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    specs = {h.id: h for h in HOSPITALS}
    with registry.transaction():
        for hm in manifest["hospitals"]:
            _seed_hospital(registry, root, hm, specs[hm["id"]])
    return manifest


def _seed_hospital(reg: Registry, root: Path, hm: dict[str, Any], h: HospitalSpec) -> None:
    gold = json.loads((root / hm["gold"]).read_text(encoding="utf-8"))
    reg.upsert_organization(Organization(ein=h.ein, name=h.name, system_name=h.system_name))
    group = "A" if len(h.facilities) > 1 else None
    fac_ids = [fm["facility_id"] for fm in hm["facilities"]]
    for fm in hm["facilities"]:
        reg.upsert_facility(Facility(
            id=fm["facility_id"], ccn=fm["ccn"], tx_license_no=fm["license_no"], name=fm["name"], address=fm["address"],
            city=fm["city"], zip=fm["zip"], county=fm["county"], type=fm["cms_type"], ownership=h.ownership, ein=h.ein,
            schedule_h_group_letter=group,
        ))
    sec = _schedule_h(h)
    reg.add_schedule_h(sec)
    doc_shas: dict[str, str] = {}
    for name, d in hm["documents"].items():
        src_id = f"src:{hm['id']}:{name}"
        status = BROKEN_LINKS.get(src_id, 200)
        reg.upsert_source(Source(id=src_id, url=d["url"], kind=d["kind"], discovered_via="demo", first_seen=DEMO_FETCHED_AT,
                                 last_checked=DEMO_FETCHED_AT, http_status=status, facility_ids=fac_ids))
        gd = gold["documents"].get(name, {})
        reg.add_document_version(DocumentVersion(
            sha256=d["sha256"], source_id=src_id, kind=d["kind"], fetched_at=DEMO_FETCHED_AT, language=d["language"],
            pages=gd.get("pages"), has_text_layer=not gd.get("image_only_pages"), pdf_uri=str((root / d["path"]).resolve()),
        ))
        doc_shas[name] = d["sha256"]
        if gd.get("form_template"):
            tpl = FormTemplate.model_validate(gd["form_template"])
            reg.upsert_form_template(tpl.model_copy(update={"verified_by": REVIEWER, "verified_at": DEMO_REVIEWED_AT}))
    pv_id = policy_version_id(hm["id"])
    reg.add_policy_version(PolicyVersion(id=pv_id, organization_ein=h.ein, title=h.policy_title,
                                         effective_date=h.effective_date, doc_shas=doc_shas, as_of=DEMO_AS_OF))
    for fid in fac_ids:
        method = "schedule_h_group" if group else "named_in_policy"
        reg.map_policy_facility(PolicyFacilityMap(policy_version_id=pv_id, facility_id=fid, method=method, confidence=0.97))

    expected: dict[str, str] = gold["schedule_h"]["expected_crosschecks"]
    for key, raw in gold["policy"].items():
        fld = Field[Any].model_validate(raw)
        status = (FieldStatus.abstained if fld.abstain_reason else
                  FieldStatus.provisional if key in PROVISIONAL_KEYS else FieldStatus.verified)
        checks = []
        if key in expected:
            checks.append(CrossCheck(
                field_key=key, source="schedule_h", external_ref=f"irs:{sec.object_id}|{_LINE.get(key, 'Part V')}|"
                + (f"group {group}" if group else "facility 1"),
                external_value=_external_value(h, key), filing_period_end=sec.tax_period_end,
                policy_date=h.effective_date, result=CrossCheckResult(expected[key]),
            ))
        rec = FieldRecord(
            id=FieldRecord.make_id(pv_id, key), policy_version_id=pv_id, key=key, field=fld, status=status,
            crosschecks=checks, as_of=DEMO_AS_OF, extractor_model_tier="sonnet", prompt_version="compile_policy@v1",
            reviewer=REVIEWER if status == FieldStatus.verified else None,
            reviewed_at=DEMO_REVIEWED_AT if status == FieldStatus.verified else None,
        )
        reg.put_field(rec)
        _tasks_for(reg, rec, h, len(fac_ids))
    reg.add_change_event(ChangeEvent(id=f"ce:{pv_id}:initial", policy_version_id=pv_id, triage="substantive",
                                     diff_summary=f"First compiled version of “{h.policy_title}” added to the registry.",
                                     published_at=DEMO_FETCHED_AT))
    if hm["id"] == "example-methodist":
        reg.add_hospital_response(HospitalResponse(
            id="hr:example-methodist:asset_test", facility_id=fac_ids[0], field_key="asset_test",
            statement="The asset test was added in the July 2025 policy revision; our next Schedule H will report it.",
            received_at=datetime(2026, 10, 6, 14, 0, tzinfo=UTC), published=True,
        ))


def _tasks_for(reg: Registry, rec: FieldRecord, h: HospitalSpec, n_fac: int) -> None:
    weight = 20 if rec.key in DETERMINATION_FIELDS else 0
    for cc in rec.crosschecks:
        if cc.result == CrossCheckResult.agree:
            continue
        unexplained = cc.result == CrossCheckResult.disagree_unexplained
        note = (f"{rec.key}: extracted vs Sched H {cc.external_ref.split('|')[1]} {cc.external_value} -> {cc.result.value}"
                f" | adjudicator: policy effective {h.effective_date.isoformat()}, filing period ended "
                f"{cc.filing_period_end.isoformat() if cc.filing_period_end else 'unknown'}"
                + (" - the filing should reflect it" if unexplained else " - the filing predates the policy"))
        reg.add_review_task(_impact_task(f"rt:crosscheck_disagreement:{rec.id}", "crosscheck_disagreement",
                                         "high" if unexplained else "medium", 50 + weight + 5 * n_fac, note, field_id=rec.id))
    if rec.status == FieldStatus.provisional:
        reg.add_review_task(_impact_task(f"rt:field_verify:{rec.id}", "field_verify", "medium", 10 + weight + 5 * n_fac,
                                         f"{rec.key}: extracted {json.dumps(rec.field.model_dump(mode='json')['value'])}"
                                         f" | conf {rec.field.confidence:.2f}", field_id=rec.id))
    elif rec.status == FieldStatus.abstained:
        reg.add_review_task(_impact_task(f"rt:field_verify:{rec.id}", "field_verify", "low", 10 + 5 * n_fac,
                                         f"confirm abstention | {rec.key}: {rec.field.abstain_reason}", field_id=rec.id))

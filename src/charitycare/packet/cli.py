"""``ccc packet`` subcommands. The integrator registers :data:`COMMAND` in ``cli.COMMANDS``.

* ``ccc packet fixtures``: write the packet stage's synthetic replay fixtures
  (``form_template`` for flat demo forms, ``extract_bill`` / ``extract_income``
  for the synthetic documents) and save the synthetic images under
  ``data/packet-demo/``.
* ``ccc packet template <form.pdf>``: build an (unverified) template and print it.
* ``ccc packet demo [--hospital ID] [--form application_en] [--out DIR]``: the Screen 3
  walk-through on synthetic data: extract a bill and two pay stubs, screen,
  compile the packet, and write ``packet.pdf`` and ``deadline.ics``.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any


def configure(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="packet_cmd", required=True)
    f = sub.add_parser("fixtures", help="write packet replay fixtures and synthetic demo images")
    f.add_argument("--demo-dir", type=Path, default=None)
    t = sub.add_parser("template", help="build a form template from a PDF")
    t.add_argument("pdf", type=Path)
    t.add_argument("--no-llm", action="store_true")
    d = sub.add_parser("demo", help="compile a synthetic demo packet")
    d.add_argument("--hospital", default="example-methodist")
    d.add_argument("--form", default="application_en")
    d.add_argument("--out", type=Path, default=Path("packet-demo-out"))
    d.add_argument("--demo-dir", type=Path, default=None)
    d.add_argument("--accept-extracted", action="store_true",
                   help="use low-confidence extracted fields without confirmation (synthetic demo documents only)")


def demo_determination(policy: Any, facility_id: str, household: Any, as_of: date) -> Any:
    """Stand-in screening for the CLI demo (the engine module owns real determinations)."""
    from charitycare import fpg
    from charitycare.schemas import Determination

    year = fpg.guideline_year_for(as_of)
    pct = fpg.pct_fpl(household.annual_income, household.household_size, year)
    tiers = sorted(policy.fpg_tiers.value or [], key=lambda t: t.max_pct_inclusive)
    tier = next((t for t in tiers if fpg.income_within_pct(household.annual_income, t.max_pct_inclusive, household.household_size, year)), None)
    outcome = "likely_not_eligible_on_income" if tier is None else ("likely_free" if tier.benefit == "free" else "likely_discount")
    first = household.first_statement_date
    return Determination(
        facility_id=facility_id,
        policy_version_id=f"demo:{facility_id}",
        fpg_year=year,
        household_size=household.household_size,
        annual_income=household.annual_income,
        pct_fpl=pct,
        outcome=outcome,
        matched_tier=tier,
        arithmetic=f"{fpg.format_usd(household.annual_income)} / {fpg.format_usd(fpg.fpg(year, household.household_size))} = {pct:.1f}% FPL",
        unevaluated_conditions=["asset test: attach the bank statements listed on the checklist"],
        citations=policy.fpg_tiers.evidence,
        federal_deadline=first + timedelta(days=240) if first else None,
        disclaimer_version="demo",
    )


def run(a: argparse.Namespace) -> int:
    from charitycare.config import get_settings

    s = get_settings()
    demo_dir = getattr(a, "demo_dir", None) or s.demo_dir
    if a.packet_cmd == "fixtures":
        from charitycare.packet.synthetic import (
            demo_documents,
            save_demo_documents,
            write_extraction_fixtures,
            write_template_fixtures,
        )

        docs = demo_documents()
        paths = write_template_fixtures(demo_dir, s.llm_fixtures_dir) + write_extraction_fixtures(s.llm_fixtures_dir, docs)
        images = save_demo_documents(s.data_dir / "packet-demo", docs)
        print(json.dumps({"fixtures": [str(p) for p in paths], "images": [str(p) for p in images]}, indent=2))
        return 0
    if a.packet_cmd == "template":
        from charitycare.packet.template import build_template

        b = build_template(a.pdf.read_bytes(), use_llm=not a.no_llm)
        print(json.dumps({"method": b.method, "notes": b.notes, "template": b.template.model_dump(mode="json")}, indent=2))
        return 0
    return _demo(a, demo_dir)


def _demo(a: argparse.Namespace, demo_dir: Path) -> int:
    from charitycare.packet import (
        Applicant,
        ConfirmationRequired,
        PacketRequest,
        annualize_income,
        compile_packet,
        extract_bill,
        extract_pay_stub,
        require_confirmed,
    )
    from charitycare.packet.letters import navigator_summary
    from charitycare.packet.synthetic import DEMO_HARDSHIP, demo_documents
    from charitycare.packet.template import build_template, score_template
    from charitycare.schemas import CompiledPolicy, FormTemplate, Household

    gold = json.loads((demo_dir / "gold" / f"{a.hospital}.json").read_text(encoding="utf-8"))
    policy = CompiledPolicy.model_validate(gold["policy"])
    form_pdf = (demo_dir / "docs" / a.hospital / f"{a.form}.pdf").read_bytes()
    docs = demo_documents()
    bill = extract_bill(docs[0].image)
    stubs = [extract_pay_stub(d.image) for d in docs[1:]]
    income = annualize_income([x.fields for x in stubs])
    if income.amount is None or bill.fields.statement_date.value is None:
        print("extraction unavailable: run `ccc packet fixtures` first or enter values by hand")
        return 1
    try:
        require_confirmed({"bill": bill, **{f"pay stub {i}": s for i, s in enumerate(stubs, 1)}},
                          accept_extracted=a.accept_extracted)
    except ConfirmationRequired as exc:
        print(f"{exc}. Confirm them in the navigator page, or pass --accept-extracted for the synthetic demo documents.")
        return 2
    today = date(2026, 10, 24)
    hh = Household(
        household_size=3,
        annual_income=Decimal(income.amount),
        insured=False,
        county="Bexar",
        first_statement_date=bill.fields.statement_date.value,
        application_date=today,
    )
    facility_id = gold["facility_ids"][0]
    det = demo_determination(policy, facility_id, hh, today)
    build = build_template(form_pdf)
    gold_tpl = FormTemplate.model_validate(gold["documents"][a.form]["form_template"])
    # Real templates are verified by a person (H3); the demo accepts one only if it matches the gold labels exactly.
    matches_gold = score_template(build.template, gold_tpl)["f1"] == 1.0
    template = build.template.model_copy(update={"verified_by": "demo:matches-gold" if matches_gold else None})
    req = PacketRequest(
        applicant=Applicant(
            patient_name="Maria Example", address="123 Example Lane", city="San Antonio", state="TX", zip="78229",
            county="Bexar", phone="(210) 555-0142", employer=stubs[0].fields.employer_name.value,
        ),
        household=hh,
        determination=det,
        hospital_name=gold["name"],
        policy=policy,
        policy_as_of=today,
        form_pdf=form_pdf,
        template=template,
        hardship_circumstances=DEMO_HARDSHIP,
    )
    extract_cost = bill.cost_usd + sum(x.cost_usd for x in stubs)
    res = compile_packet(req, today=today, extra_cost_usd=extract_cost)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "packet.pdf").write_bytes(res.pdf)
    (a.out / "deadline.ics").write_bytes(res.ics)
    ok = sum(f.match for f in res.readback.fields if not f.write_by_hand)
    n = sum(1 for f in res.readback.fields if not f.write_by_hand)
    print(navigator_summary(det, today))
    print("Income: " + "; ".join(income.arithmetic))
    print(f"Template: {build.method}; readback {ok}/{n} fields match; verified={res.verified}; pages={res.page_count}")
    print(f"Write by hand: {', '.join(res.write_by_hand)}")
    print(f"Wrote {a.out / 'packet.pdf'} and {a.out / 'deadline.ics'} (cost ${res.metric.cost_usd:.4f}, simulated)")
    return 0


COMMAND = ("packet", "packet compiler: fixtures, templates, demo packet", configure, run)

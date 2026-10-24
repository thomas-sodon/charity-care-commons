"""Static site generator: dashboard, hospital pages (Screen 1), methods, evals, data; wording and accessibility."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

from charitycare.schemas import EvalRun, Facility
from charitycare.site import build_site, check_page_wording
from charitycare.site.build import TEXAS_OUTLINE, map_dots, project
from charitycare.store import Registry

TODAY = date(2026, 10, 24)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.ids: set[str] = set()
        self.lang = None
        self.imgs_without_alt = 0

    def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        for k in ("href", "src"):
            if a.get(k):
                self.hrefs.append(a[k])
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "img" and "alt" not in a:
            self.imgs_without_alt += 1


@pytest.fixture
def site(seeded: Registry, tmp_path: Path):
    seeded.add_eval_run(EvalRun(id="run-1", git_sha="abc1234", dataset_version="demo-2026.10", suite="compile",
                                metrics={"field_f1": 0.931, "citation_validity": 1.0}, cost_usd=0.42,
                                created_at=datetime(2026, 10, 20, tzinfo=UTC)))
    extra = tmp_path / "evals.json"
    extra.write_text(json.dumps([EvalRun(id="run-2", dataset_version="demo-2026.10", suite="screening",
                                         metrics={"outcome_accuracy": 1.0}, created_at=datetime(2026, 10, 21, tzinfo=UTC)
                                         ).model_dump(mode="json")]))
    release = tmp_path / "release"
    release.mkdir()
    (release / "fields.csv").write_text("field_id\n")
    rep = build_site(seeded, tmp_path / "dist", today=TODAY, eval_paths=[extra], release_dir=release,
                     api_base_url="https://api.example.org")
    return rep


def test_pages_written(site):
    out = site.out_dir
    for rel in ("index.html", "methods.html", "evals.html", "data.html", "changes.html", "assets/style.css",
                "assets/site.js", "data/facilities.json", "data/changes.json", "data/link-health.json"):
        assert (out / rel).is_file(), rel
    assert len(site.hospital_pages) == 10
    assert (out / "data/hospitals/tx-459901.json").is_file()


def test_every_page_accessible_and_links_resolve(site):
    for page in site.pages:
        html = page.read_text(encoding="utf-8")
        p = _Links()
        p.feed(html)
        assert p.lang == "en" and 'name="viewport"' in html and "<title>" in html, page
        assert 'class="skip" href="#main"' in html and 'id="main"' in html
        assert p.imgs_without_alt == 0
        assert "not legal advice" in html
        for href in p.hrefs:
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            target, _, frag = href.partition("#")
            if not target:
                assert frag in p.ids, (page, href)
                continue
            assert (page.parent / target).resolve().exists(), (page, href)


def test_no_banned_wording_anywhere(site):
    for page in site.pages:
        assert check_page_wording(page.read_text(encoding="utf-8")) == [], page


def test_wording_guard_blocks_build(seeded: Registry, tmp_path: Path):
    seeded.upsert_facility(Facility(id="tx-459999", name="Compliant Example Hospital", county="Bexar"))
    with pytest.raises(ValueError, match="banned wording"):
        build_site(seeded, tmp_path / "dist", today=TODAY)
    assert check_page_wording("<p>“Patients are eligible if…”</p>") == []
    assert check_page_wording("<p>You are eligible</p>") == ["eligible"]


def test_hospital_page_screen1(site):
    html = (site.out_dir / "hospitals/tx-459901.html").read_text(encoding="utf-8")
    assert "Example Methodist Hospital · San Antonio, TX" in html and "CCN 459901" in html
    assert "“Financial Assistance Policy” rev. 2025-07-01" in html
    assert "029f1ec4ac87…" in html and 'title="029f1ec4ac87f3a5d64ebda5e3952bcf893889cfc43299051a71509fa29216ec"' in html
    assert "Free care up to 200% FPL" in html and "✓ verified" in html and "○ provisional" in html
    assert "IRS FY2025: free up to 200%, discount up to 400% ✓" in html
    assert "“Patients with household income at or below 200% of the Federal Poverty Guidelines" in html
    assert "p. 3 § 4.2" in html
    # Discrepancy for review, beside the filing period, the policy date and the hospital's response.
    assert "Discrepancy for review</strong>: the IRS filing for the period ending 2025-12-31 lists asset test as not checked" in html
    assert "the policy is dated 2025-07-01" in html
    assert "Hospital response (2026-10-06): “The asset test was added" in html
    assert "Version history" in html and "pv:example-methodist:demo" in html
    assert 'href="../data/hospitals/tx-459901.json" download' in html
    assert "https://api.example.org/navigator?facility_id=tx-459901" in html
    assert "POST https://api.example.org/v1/corrections" in html


def test_abstentions_displayed(site):
    gulf = (site.out_dir / "hospitals/tx-459907.html").read_text(encoding="utf-8")
    assert "Hospital discretion, see quote" in gulf
    assert "Gulf Coast" in gulf
    hill = (site.out_dir / "hospitals/tx-459908.html").read_text(encoding="utf-8")
    assert hill.count("Not addressed in policy") == 3 and "– abstained" in hill


def test_dashboard(site):
    html = (site.out_dir / "index.html").read_text(encoding="utf-8")
    assert "Texas · 10 facilities · as of 2026-10-24" in html
    assert "100.0%" in html and "discrepancies for review" in html
    assert html.count('class="dot-verified"') == 10 + 1  # ten dots plus the legend swatch
    assert 'role="img"' in html and "<desc id=\"map-desc\">10 verified" in html
    assert html.count("<tr data-free=") == 10
    assert 'data-asset="yes"' in html and 'data-screening="after_billing_before_collections"' in html
    assert '<form class="filters" id="filters" hidden' in html  # enhanced by site.js only
    assert "IRS filing: not checked · filing period ended 2025-12-31" in html


def test_map_projection_and_dots():
    xs, ys = zip(*(project(lat, lon) for lon, lat in TEXAS_OUTLINE))
    assert 0 <= min(xs) and max(xs) <= 600 and 0 <= min(ys) and max(ys) <= 560
    facs = [{"id": f"f{i}", "county": "Dallas", "coverage": "verified"} for i in range(3)] + [{"id": "x", "county": "Loving"}]
    dots, unmapped = map_dots(facs)
    assert len(dots) == 3 and [u["id"] for u in unmapped] == ["x"]
    assert len({(d["x"], d["y"]) for d in dots}) == 3  # same county, spread apart


def test_eval_page(site):
    html = (site.out_dir / "evals.html").read_text(encoding="utf-8")
    assert "Suite: compile" in html and "Suite: screening" in html
    assert "abc1234" in html and "field_f1" in html and "0.931" in html and "demo-2026.10" in html
    runs = json.loads((site.out_dir / "data/eval-runs.json").read_text())
    assert [r["id"] for r in runs] == ["run-2", "run-1"]


def test_eval_page_empty(seeded: Registry, tmp_path: Path):
    rep = build_site(seeded, tmp_path / "d", today=TODAY)
    assert "No eval runs have been recorded yet" in (rep.out_dir / "evals.html").read_text(encoding="utf-8")


def test_data_and_changes_pages(site):
    data = (site.out_dir / "data.html").read_text(encoding="utf-8")
    assert "CC BY 4.0" in data and "data/release/fields.csv" in data and "open-fap-schema.json" in data
    changes = (site.out_dir / "changes.html").read_text(encoding="utf-8")
    assert "1 broken" in changes and "https://api.example.org/v1/changes.atom" in changes
    assert changes.index("b-broken") < changes.index("b-ok")  # unreachable links listed first


def test_stale_banner(seeded: Registry, tmp_path: Path):
    stale = build_site(seeded, tmp_path / "s", today=date(2027, 6, 1))
    assert "This data may be out of date" in (stale.out_dir / "index.html").read_text(encoding="utf-8")
    fresh = build_site(seeded, tmp_path / "f", today=TODAY)
    assert "This data may be out of date" not in (fresh.out_dir / "index.html").read_text(encoding="utf-8")


def test_css_has_light_and_dark_tokens():
    css = (Path(__file__).parents[2] / "src/charitycare/site/static/style.css").read_text(encoding="utf-8")
    assert "@media (prefers-color-scheme: dark)" in css and ':root[data-theme="dark"]' in css
    assert re.search(r"body \{[^}]*background: var\(--bg\)", css)


def test_eval_page_marks_simulated_numbers_and_demo_banner(seeded: Registry, tmp_path: Path):
    run = {"id": "run-sim", "dataset_version": "demo-2026.10", "suite": "full", "created_at": "2026-10-22T00:00:00+00:00",
           "metrics": {"naive_baseline_gap_points": {"value": 40.0}, "cost_per_hospital_usd": {"value": 0.0},
                       "cost_per_policy_usd": {"value": 0.24}, "field_f1": {"value": 0.95}},
           "notes": ["Naive-agent baseline answers are SIMULATED synthetic fixtures."], "simulated_cost": True,
           "cost_usd": 1.2}
    p = tmp_path / "run.json"
    p.write_text(json.dumps(run))
    rep = build_site(seeded, tmp_path / "dist", today=TODAY, eval_paths=[p])
    html = (rep.out_dir / "evals.html").read_text(encoding="utf-8")
    assert "Naive-agent baseline answers are SIMULATED" in html and "(SIMULATED)" in html
    row = re.search(r"naive_baseline_gap_points</code></th><td>([^<]*)<strong>SIMULATED", html)
    assert row is not None and "cost_per_hospital_usd" not in html
    assert re.search(r"field_f1</code></th><td>0.95</td>", html)
    assert "Synthetic demo data" in (rep.out_dir / "index.html").read_text(encoding="utf-8")

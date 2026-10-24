"""Static public site generator: Jinja templates -> ``site/dist/`` (no JS build chain).

Pages (design section 7): the state dashboard (Screen 4: stats, an SVG map of
facility dots coloured by coverage, a filterable table), one page per hospital
(Screen 1: verification badges, quotes with page and sha256, the IRS check,
discrepancies for review beside the hospital's response, version history),
the methods page, the eval report (Screen 5, from ``EvalRun`` records or JSON
files), the change log with link health, and the data download page. JSON
copies of every page's data sit under ``data/`` so the site works without the API.

Every generated page passes :func:`charitycare.wording.find_banned_wording`
(hospital quotes are wrapped in curly quotes and exempt); the build fails otherwise.
"""

from __future__ import annotations

import html as html_lib
import json
import math
import re
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from charitycare import __version__
from charitycare.api import present, queries
from charitycare.config import DISCLAIMER_TEXT, DISCLAIMER_VERSION, get_settings
from charitycare.schemas import SCHEMA_VERSION, EvalRun, Source, utcnow
from charitycare.store import Registry
from charitycare.wording import DISCREPANCY_LABEL, find_banned_wording

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

# Approximate county centroids (lat, lon) for the map; facilities in other counties are listed as unmapped.
COUNTY_CENTROIDS: dict[str, tuple[float, float]] = {
    "Angelina": (31.25, -94.61), "Bell": (31.04, -97.48), "Bexar": (29.45, -98.52), "Brazoria": (29.17, -95.43),
    "Brazos": (30.66, -96.30), "Cameron": (26.15, -97.45), "Collin": (33.19, -96.57), "Dallas": (32.77, -96.78),
    "Denton": (33.21, -97.12), "Ector": (31.87, -102.54), "El Paso": (31.77, -106.24), "Fort Bend": (29.53, -95.77),
    "Galveston": (29.38, -94.90), "Gillespie": (30.32, -98.95), "Grayson": (33.63, -96.68), "Gregg": (32.48, -94.82),
    "Harris": (29.86, -95.39), "Hays": (30.06, -98.03), "Hidalgo": (26.40, -98.18), "Jefferson": (29.85, -94.15),
    "Lubbock": (33.61, -101.82), "McLennan": (31.55, -97.20), "Midland": (31.87, -102.03), "Montgomery": (30.30, -95.50),
    "Nueces": (27.73, -97.52), "Potter": (35.40, -101.89), "Randall": (34.97, -101.90), "Smith": (32.38, -95.27),
    "Tarrant": (32.77, -97.29), "Taylor": (32.30, -99.89), "Tom Green": (31.40, -100.46), "Travis": (30.33, -97.78),
    "Victoria": (28.80, -96.97), "Webb": (27.76, -99.33), "Wichita": (33.99, -98.70), "Williamson": (30.65, -97.60),
}

# Simplified Texas outline (lon, lat), clockwise from the north-west corner of the Panhandle.
TEXAS_OUTLINE: tuple[tuple[float, float], ...] = (
    (-103.04, 36.50), (-100.00, 36.50), (-100.00, 34.56), (-99.20, 34.36), (-98.10, 34.13), (-96.94, 33.95),
    (-95.80, 33.86), (-94.48, 33.64), (-94.04, 33.55), (-94.04, 31.99), (-93.70, 31.40), (-93.52, 30.90),
    (-93.84, 29.70), (-94.70, 29.33), (-95.60, 28.70), (-96.60, 28.10), (-97.20, 27.40), (-97.40, 26.60),
    (-97.15, 25.95), (-97.70, 26.05), (-98.60, 26.25), (-99.10, 26.60), (-99.50, 27.50), (-100.30, 28.30),
    (-100.90, 29.30), (-101.40, 29.77), (-102.40, 29.85), (-103.10, 28.98), (-103.80, 29.30), (-104.50, 29.65),
    (-105.00, 30.70), (-106.00, 31.40), (-106.62, 31.95), (-106.62, 32.00), (-103.06, 32.00),
)

MAP_W, MAP_H = 600.0, 560.0
_LON0, _LON1, _LAT0, _LAT1 = -106.8, -93.3, 25.6, 36.7


def project(lat: float, lon: float) -> tuple[float, float]:
    """Equirectangular projection of Texas into the map's viewBox."""
    x = (lon - _LON0) / (_LON1 - _LON0) * MAP_W
    y = (_LAT1 - lat) / (_LAT1 - _LAT0) * MAP_H
    return round(x, 1), round(y, 1)


def texas_path() -> str:
    pts = [project(lat, lon) for lon, lat in TEXAS_OUTLINE]
    return "M" + " L".join(f"{x},{y}" for x, y in pts) + " Z"


def map_dots(facilities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(dots, unmapped): one dot per facility, spread around its county centroid."""
    dots, unmapped, per_county = [], [], {}
    for f in facilities:
        c = COUNTY_CENTROIDS.get((f.get("county") or "").removesuffix(" County"))
        if c is None:
            unmapped.append(f)
            continue
        i = per_county.get(f["county"], 0)
        per_county[f["county"]] = i + 1
        x, y = project(*c)
        if i:
            ang = i * 2.4
            r = 7 + 3 * math.sqrt(i)
            x, y = round(x + r * math.cos(ang), 1), round(y + r * math.sin(ang), 1)
        dots.append({"x": x, "y": y, "f": f})
    return dots, unmapped


def _as_eval_run(item: dict[str, Any]) -> EvalRun:
    """Accept an ``EvalRun`` row or the harness's richer run JSON (``evals/runs/*.json``)."""
    metrics = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (item.get("metrics") or {}).items()}
    data = {k: item[k] for k in EvalRun.model_fields if k in item}
    data["metrics"] = {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))}
    return EvalRun.model_validate(data)


def simulated_metrics(run: EvalRun) -> set[str]:
    """Metric names whose numbers are simulated rather than measured (naive baseline, replayed costs)."""
    out: set[str] = set()
    if any("SIMULATED" in n for n in run.notes):
        out |= {k for k in run.metrics if k.startswith("naive_")}
    if run.simulated_cost:
        out |= {k for k in run.metrics if "cost" in k}
    return out


def shown_metrics(run: EvalRun) -> dict[str, float]:
    """Metrics to display: a zero cost-per-hospital means no compile cost was recorded, so it is left out."""
    return {k: v for k, v in run.metrics.items() if not (k == "cost_per_hospital_usd" and not v)}


def is_demo_registry(registry: Registry) -> bool:
    """True when the registry holds the synthetic demo corpus (``.example`` sources)."""
    from urllib.parse import urlparse

    for src in registry.select(Source):
        host = (urlparse(src.url).hostname or "")
        if host.endswith(".example") or host == "example":
            return True
    return False


def load_eval_runs(registry: Registry, paths: list[Path] | None = None) -> list[EvalRun]:
    """Eval runs from the registry plus any ``EvalRun`` JSON files (an object or a list)."""
    runs = {r.id: r for r in registry.eval_runs()}
    for p in paths or []:
        files = sorted(p.glob("*.json")) if p.is_dir() else [p]
        for fp in files:
            data = json.loads(fp.read_text(encoding="utf-8"))
            for item in data if isinstance(data, list) else [data]:
                run = _as_eval_run(item)
                runs[run.id] = run
    return sorted(runs.values(), key=lambda r: r.created_at, reverse=True)


def _visible_text(page_html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", page_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return html_lib.unescape(text)


def check_page_wording(page_html: str) -> list[str]:
    """Banned wording in a page's visible text (curly-quoted hospital quotes excluded)."""
    return find_banned_wording(_visible_text(page_html))


@dataclass
class SiteReport:
    out_dir: Path
    pages: list[Path] = field(default_factory=list)
    data_files: list[Path] = field(default_factory=list)

    @property
    def hospital_pages(self) -> list[Path]:
        return [p for p in self.pages if p.parent.name == "hospitals"]


class SiteBuilder:
    """Renders the site from a registry. Use :func:`build_site`."""

    def __init__(self, registry: Registry, out_dir: Path, *, today: date, stale_after_days: int = 45,
                 sunset: bool = False, api_base_url: str | None = None, eval_runs: list[EvalRun] | None = None,
                 release_dir: Path | None = None):
        self.reg = registry
        self.out = out_dir
        self.today = today
        self.api = api_base_url.rstrip("/") if api_base_url else None
        self.eval_runs = eval_runs or []
        self.release_dir = release_dir
        self.fresh = queries.freshness(registry, today, stale_after_days, sunset)
        self.demo_data = is_demo_registry(registry)
        self.env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                               trim_blocks=True, lstrip_blocks=True)
        self.env.filters["pct"] = lambda v: "—" if v is None else f"{v:g}%"
        self.env.filters["short"] = lambda s, n=12: (s[:n] + "…") if s and len(s) > n else (s or "")
        self.env.filters["usd"] = lambda v: "—" if v is None else f"${v:,.0f}"
        self.report = SiteReport(out_dir)

    # -- helpers ---------------------------------------------------------------

    def _common(self, page: str, depth: int) -> dict[str, Any]:
        root = "../" * depth
        lv = self.fresh.last_verified
        return {
            "root": root, "page": page, "today": self.today.isoformat(), "version": __version__,
            "schema_version": SCHEMA_VERSION, "fresh": self.fresh, "api": self.api,
            "disclaimer": DISCLAIMER_TEXT.format(as_of=lv.isoformat() if lv else self.today.isoformat()),
            "disclaimer_version": DISCLAIMER_VERSION, "discrepancy_label": DISCREPANCY_LABEL,
            "demo_data": self.demo_data,
        }

    def _write(self, rel: str, template: str, ctx: dict[str, Any]) -> Path:
        depth = rel.count("/")
        page_html = self.env.get_template(template).render(**self._common(rel, depth), **ctx)
        bad = check_page_wording(page_html)
        if bad:
            raise ValueError(f"banned wording on {rel}: {sorted(set(bad))}")
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page_html, encoding="utf-8")
        self.report.pages.append(path)
        return path

    def _json(self, rel: str, data: Any) -> Path:
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self.report.data_files.append(path)
        return path

    # -- pages -------------------------------------------------------------------

    def build(self) -> SiteReport:
        if self.out.exists():
            shutil.rmtree(self.out)
        (self.out / "assets").mkdir(parents=True)
        for asset in STATIC.iterdir():
            if asset.is_file():
                shutil.copy2(asset, self.out / "assets" / asset.name)
        facilities = queries.list_facilities(self.reg, "TX")
        self._json("data/facilities.json", {"as_of": self.today.isoformat(), **self.fresh.as_meta(), "facilities": facilities})
        self.dashboard(facilities)
        for f in facilities:
            self.hospital(f["id"])
        self.changes_page()
        self.methods()
        self.evals()
        self.data_page()
        return self.report

    def dashboard(self, facilities: list[dict[str, Any]]) -> Path:
        dots, unmapped = map_dots(facilities)
        stats = queries.stats(self.reg, "TX", today=self.today)
        return self._write("index.html", "dashboard.html", {
            "facilities": facilities, "stats": stats, "dots": dots, "unmapped": unmapped, "outline": texas_path(),
            "map_w": MAP_W, "map_h": MAP_H, "discrepancies": queries.discrepancies(self.reg, self.today),
        })

    def hospital(self, facility_id: str) -> Path:
        detail = queries.facility_detail(self.reg, facility_id)
        assert detail is not None
        pv = self.reg.policy_for_facility(facility_id)
        bundle = queries.policy_bundle(self.reg, pv, facility_id) if pv else None
        responses = detail["hospital_responses"]
        rules = []
        if bundle:
            for f in bundle["fields"]:
                f["responses"] = [r for r in responses if r["field_key"] in (None, f["key"])] if any(
                    c["is_discrepancy"] for c in f["crosschecks"]) else []
                rules.append(f)
        fap = next((d for d in (bundle or {}).get("documents", []) if d["name"] == "fap"), None)
        self._json(f"data/hospitals/{facility_id}.json", {"facility": detail, "policy": bundle,
                                                          "as_of": self.today.isoformat(), **self.fresh.as_meta()})
        return self._write(f"hospitals/{facility_id}.html", "hospital.html", {
            "f": detail, "bundle": bundle, "rules": rules, "fap": fap, "policy": bundle["policy"] if bundle else None,
        })

    def changes_page(self) -> Path:
        changes = queries.changes(self.reg)
        for c in changes:
            c["summary_text"] = c["diff_summary"].split("\n---\n")[0]
        health = queries.link_health(self.reg)
        self._json("data/changes.json", changes)
        self._json("data/link-health.json", health)
        self._json("data/discrepancies.json", queries.discrepancies(self.reg, self.today))
        names = {f.id: f.name for f in self.reg.list_facilities(state=None)}
        return self._write("changes.html", "changes.html", {"changes": changes, "health": health, "names": names})

    def methods(self) -> Path:
        return self._write("methods.html", "methods.html", {
            "labels": present.CROSSCHECK_LABELS, "abstain": present.ABSTAIN_TEXT, "stale_after": self.fresh.stale_after_days,
        })

    def evals(self) -> Path:
        latest: dict[str, EvalRun] = {}
        for r in self.eval_runs:
            latest.setdefault(r.suite, r)
        self._json("data/eval-runs.json", [r.model_dump(mode="json") for r in self.eval_runs])
        latest_rows = [{"run": r, "metrics": shown_metrics(r), "simulated": simulated_metrics(r)} for r in latest.values()]
        return self._write("evals.html", "evals.html", {"latest": latest_rows, "runs": self.eval_runs})

    def data_page(self) -> Path:
        downloads: list[dict[str, Any]] = []
        schema = get_settings().schemas_dir / "open-fap-schema.json"
        if schema.is_file():
            shutil.copy2(schema, self.out / "data" / "open-fap-schema.json")
            downloads.append({"href": "data/open-fap-schema.json", "name": "Open FAP Schema (JSON Schema)", "size": schema.stat().st_size})
        if self.release_dir and self.release_dir.is_dir():
            dest = self.out / "data" / "release"
            shutil.copytree(self.release_dir, dest, dirs_exist_ok=True)
            for p in sorted(dest.iterdir()):
                if p.is_file():
                    downloads.append({"href": f"data/release/{p.name}", "name": p.name, "size": p.stat().st_size})
        for rel in ("data/facilities.json", "data/changes.json", "data/discrepancies.json", "data/link-health.json"):
            p = self.out / rel
            downloads.append({"href": rel, "name": rel.split("/")[-1], "size": p.stat().st_size})
        return self._write("data.html", "data.html", {"downloads": downloads})


def build_site(registry: Registry, out_dir: str | Path | None = None, *, today: date | None = None,
               stale_after_days: int = 45, sunset: bool = False, api_base_url: str | None = None,
               eval_paths: list[Path] | None = None, release_dir: Path | None = None) -> SiteReport:
    """Render the whole site into ``out_dir`` (default ``site/dist`` under the repo root)."""
    out = Path(out_dir) if out_dir else get_settings().home / "site" / "dist"
    runs = load_eval_runs(registry, eval_paths)
    return SiteBuilder(registry, out, today=today or utcnow().date(), stale_after_days=stale_after_days, sunset=sunset,
                       api_base_url=api_base_url, eval_runs=runs, release_dir=release_dir).build()

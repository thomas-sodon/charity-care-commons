"""CMS Hospital General Information loader (Provider Data Catalog dataset ``xubh-q36u``).

The facility universe starts from CMS: every Medicare-certified hospital with
its CCN, address, type and ownership. Rows come from one of three places, in
order of preference:

1. the live PDC datastore API (``fetch_cms``, paged, filtered server-side by state);
2. a cached CSV of a previous live pull (``data/seed/cms_tx_hospitals.csv``);
3. the demo CSV in the synthetic corpus (``data/demo/cms/Hospital_General_Information.csv``).

The API returns snake_case keys (``facility_id``, ``citytown`` ...); the bulk CSV
uses the human header (``Facility ID``, ``City/Town`` ...). Both are normalized
to the CSV header so the rest of the pipeline sees one layout.

Scope (section 9): Texas, ``Hospital Ownership`` starting with ``Voluntary
non-profit``, excluding ``Psychiatric`` hospitals (501(r) applies to nonprofit
hospital organizations; government and proprietary hospitals are out of scope).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from charitycare.config import get_settings
from charitycare.schemas import Facility

DATASET_ID = "xubh-q36u"
DATASTORE_URL = f"https://data.cms.gov/provider-data/api/1/datastore/query/{DATASET_ID}/0"
PAGE_SIZE = 500  # the PDC datastore caps a page at 500 rows

# CSV header name -> datastore API property name, for the columns the pipeline uses.
API_KEYS: dict[str, str] = {
    "Facility ID": "facility_id",
    "Facility Name": "facility_name",
    "Address": "address",
    "City/Town": "citytown",
    "State": "state",
    "ZIP Code": "zip_code",
    "County/Parish": "countyparish",
    "Telephone Number": "telephone_number",
    "Hospital Type": "hospital_type",
    "Hospital Ownership": "hospital_ownership",
    "Emergency Services": "emergency_services",
}
CORE_COLUMNS = list(API_KEYS)

NONPROFIT_PREFIX = "voluntary non-profit"
EXCLUDED_TYPES = frozenset({"psychiatric"})


@dataclass
class CMSLoad:
    """Rows of one CMS load plus where they came from and what the scope filter dropped."""

    rows: list[dict[str, str]]
    origin: str  # "live", "cache:<path>" or "csv:<path>"
    excluded: dict[str, int] = field(default_factory=dict)

    @property
    def facilities(self) -> list[Facility]:
        return [to_facility(r) for r in self.rows]


def normalize_row(row: dict[str, Any]) -> dict[str, str]:
    """Map an API or CSV row onto the CSV header names (other columns are kept as-is)."""
    out: dict[str, str] = {}
    reverse = {v: k for k, v in API_KEYS.items()}
    for k, v in row.items():
        key = reverse.get(k, k)
        out[key] = "" if v is None else str(v).strip()
    for col in CORE_COLUMNS:
        out.setdefault(col, "")
    if out["Facility ID"].isdigit():
        out["Facility ID"] = out["Facility ID"].zfill(6)
    return out


def scope_reason(row: dict[str, str], state: str = "TX") -> str | None:
    """Why a row is out of scope, or None when it is in scope."""
    if row.get("State", "").upper() != state:
        return "out_of_state"
    if not row.get("Hospital Ownership", "").strip().lower().startswith(NONPROFIT_PREFIX):
        return "not_voluntary_nonprofit"
    if row.get("Hospital Type", "").strip().lower() in EXCLUDED_TYPES:
        return "psychiatric"
    return None


def filter_in_scope(rows: Iterable[dict[str, str]], state: str = "TX") -> tuple[list[dict[str, str]], dict[str, int]]:
    """Split rows into in-scope rows and a count of exclusions by reason."""
    kept: list[dict[str, str]] = []
    excluded: dict[str, int] = {}
    for r in rows:
        reason = scope_reason(r, state)
        if reason is None:
            kept.append(r)
        else:
            excluded[reason] = excluded.get(reason, 0) + 1
    kept.sort(key=lambda r: r["Facility ID"])
    return kept, excluded


def facility_id_for_ccn(ccn: str, state: str = "TX") -> str:
    return f"{state.lower()}-{ccn}"


def to_facility(row: dict[str, str]) -> Facility:
    """CMS row -> registry ``Facility`` (id ``tx-{ccn}``)."""
    state = row.get("State") or "TX"
    ccn = row["Facility ID"]
    return Facility(
        id=facility_id_for_ccn(ccn, state),
        ccn=ccn,
        name=row["Facility Name"],
        address=row.get("Address") or None,
        city=row.get("City/Town") or None,
        zip=(row.get("ZIP Code") or "")[:5] or None,
        county=row.get("County/Parish") or None,
        state=state,
        type=row.get("Hospital Type") or None,
        ownership=row.get("Hospital Ownership") or None,
        in_scope=scope_reason(row, state) is None,
    )


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def _query_params(state: str, offset: int, limit: int) -> dict[str, str]:
    return {
        "conditions[0][property]": "state",
        "conditions[0][value]": state,
        "conditions[0][operator]": "=",
        "limit": str(limit),
        "offset": str(offset),
        "count": "true",
        "results": "true",
        "schema": "false",
        "keys": "true",
    }


def iter_cms_api(state: str = "TX", client: httpx.Client | None = None, page_size: int = PAGE_SIZE) -> Iterator[dict[str, str]]:
    """Page through the PDC datastore API for one state, yielding normalized rows."""
    own = client is None
    c = client or httpx.Client(timeout=60, headers={"User-Agent": get_settings().user_agent})
    try:
        offset = 0
        while True:
            resp = c.get(DATASTORE_URL, params=_query_params(state, offset, page_size))
            resp.raise_for_status()
            payload = resp.json()
            results = payload.get("results") or []
            for r in results:
                yield normalize_row(r)
            offset += len(results)
            total = int(payload.get("count") or 0)
            if not results or offset >= total:
                break
    finally:
        if own:
            c.close()


def fetch_cms(state: str = "TX", client: httpx.Client | None = None) -> list[dict[str, str]]:
    """All CMS rows for ``state`` from the live API (no scope filter applied)."""
    return list(iter_cms_api(state, client))


def read_cms_csv(path: str | Path) -> list[dict[str, str]]:
    """Read a CMS CSV (bulk download, cache or demo) into normalized rows."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [normalize_row(r) for r in csv.DictReader(fh)]


def write_cms_csv(rows: Iterable[dict[str, str]], path: str | Path) -> Path:
    """Write rows with the core CSV columns (quality-measure columns are not needed downstream)."""
    rows = list(rows)
    cols = list(CORE_COLUMNS)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in sorted(rows, key=lambda r: r["Facility ID"]):
        w.writerow({c: r.get(c, "") for c in cols})
    out.write_text(buf.getvalue(), encoding="utf-8")
    return out


def default_cache_path() -> Path:
    return get_settings().data_dir / "seed" / "cms_tx_hospitals.csv"


def demo_csv_path() -> Path:
    return get_settings().demo_dir / "cms" / "Hospital_General_Information.csv"


def load_cms(
    *,
    state: str = "TX",
    live: bool = False,
    csv_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    client: httpx.Client | None = None,
) -> CMSLoad:
    """Load in-scope CMS rows: live API (refreshing the cache), else cache, else ``csv_path``/demo CSV.

    A live failure falls back to the cache rather than aborting the weekly seed.
    """
    cache = Path(cache_path) if cache_path else default_cache_path()
    rows: list[dict[str, str]] | None = None
    origin = ""
    if live:
        try:
            rows = fetch_cms(state, client)
            write_cms_csv(rows, cache)
            origin = "live"
        except (httpx.HTTPError, ValueError):
            rows = None
    if rows is None and csv_path is None and cache.exists():
        rows, origin = read_cms_csv(cache), f"cache:{cache}"
    if rows is None:
        path = Path(csv_path) if csv_path else demo_csv_path()
        rows, origin = read_cms_csv(path), f"csv:{path}"
    kept, excluded = filter_in_scope(rows, state)
    return CMSLoad(kept, origin, excluded)

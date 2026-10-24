"""Streaming readers for the IRS Form 990 e-file index CSV and yearly XML ZIP batches.

The IRS publishes, per processing year, an index CSV (``index_YYYY.csv``) and
several multi-GB ZIP batches of ``{OBJECT_ID}_public.xml`` files
(https://www.irs.gov/charities-non-profits/form-990-series-downloads). This
module never loads a batch into memory: members are read one at a time
through :mod:`zipfile`, cheaply screened for ``IRS990ScheduleH`` and the
filer's state before the XML is parsed.

The index has no state column, so the state filter is applied to the return
header (``Filer/USAddress/StateAbbreviationCd``) of each candidate member.
Schedule H is only filed with a full Form 990, so index rows are narrowed to
``RETURN_TYPE == "990"`` first.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import httpx

from charitycare.config import get_settings
from charitycare.seed.schedule_h import ParsedReturn, ScheduleHParseError, parse_return

INDEX_URL = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv"
SCHEDULE_H_MARKER = b"IRS990ScheduleH"
_STATE_RE = re.compile(rb"<(?:\w+:)?Filer>.*?<(?:\w+:)?StateAbbreviationCd>\s*([A-Z]{2})\s*<", re.DOTALL)
_HEADER_BYTES = 16_384  # the return header sits at the top of every file


@dataclass(frozen=True)
class IndexRow:
    """One row of the IRS yearly index CSV."""

    return_id: str
    filing_type: str
    ein: str
    tax_period: str
    sub_date: str
    taxpayer_name: str
    return_type: str
    dln: str
    object_id: str
    xml_batch_id: str | None = None


def iter_index(source: str | Path | IO[str], *, return_types: Iterable[str] = ("990",), eins: Iterable[str] | None = None) -> Iterator[IndexRow]:
    """Stream index rows, keeping the given return types (and EINs, when given)."""
    keep_types = {t.upper() for t in return_types}
    keep_eins = {e.replace("-", "") for e in eins} if eins is not None else None
    fh: IO[str] = open(source, newline="", encoding="utf-8-sig") if isinstance(source, (str, Path)) else source
    try:
        for raw in csv.DictReader(fh):
            row = {k.strip().upper(): (v or "").strip() for k, v in raw.items() if k}
            if keep_types and row.get("RETURN_TYPE", "").upper() not in keep_types:
                continue
            if keep_eins is not None and row.get("EIN", "") not in keep_eins:
                continue
            yield IndexRow(
                return_id=row.get("RETURN_ID", ""),
                filing_type=row.get("FILING_TYPE", ""),
                ein=row.get("EIN", ""),
                tax_period=row.get("TAX_PERIOD", ""),
                sub_date=row.get("SUB_DATE", ""),
                taxpayer_name=row.get("TAXPAYER_NAME", ""),
                return_type=row.get("RETURN_TYPE", ""),
                dln=row.get("DLN", ""),
                object_id=row.get("OBJECT_ID", ""),
                xml_batch_id=row.get("XML_BATCH_ID") or None,
            )
    finally:
        if isinstance(source, (str, Path)):
            fh.close()


def object_id_of(member_name: str) -> str | None:
    """``2026_TEOS_XML_01A/202601234567890123_public.xml`` -> ``202601234567890123``."""
    m = re.search(r"(\d{10,})_public\.xml$", member_name)
    return m.group(1) if m else None


def filer_state(xml_head: bytes) -> str | None:
    m = _STATE_RE.search(xml_head)
    return m.group(1).decode("ascii") if m else None


def iter_batch(
    zip_path: str | Path | IO[bytes],
    *,
    state: str | None = "TX",
    object_ids: Iterable[str] | None = None,
    chunk_size: int = 1 << 16,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(object_id, xml_bytes)`` for members filed from ``state`` that carry Schedule H.

    Each member is read in chunks and abandoned as soon as the header shows the
    wrong state; the marker scan stops at the first ``IRS990ScheduleH`` hit.
    """
    wanted = set(object_ids) if object_ids is not None else None
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            oid = object_id_of(info.filename)
            if oid is None or (wanted is not None and oid not in wanted):
                continue
            with zf.open(info) as member:
                head = member.read(_HEADER_BYTES)
                if state is not None and filer_state(head) != state:
                    continue
                parts = [head]
                while chunk := member.read(chunk_size):
                    parts.append(chunk)
            data = b"".join(parts)
            if SCHEDULE_H_MARKER in data:
                yield oid, data


def iter_schedule_h(
    zip_path: str | Path | IO[bytes],
    *,
    state: str | None = "TX",
    index: Iterable[IndexRow] | None = None,
) -> Iterator[ParsedReturn]:
    """Parsed Schedule H returns from a batch, optionally limited to object ids in ``index``."""
    oids = [r.object_id for r in index] if index is not None else None
    for oid, data in iter_batch(zip_path, state=state, object_ids=oids):
        try:
            yield parse_return(data, oid)
        except ScheduleHParseError:
            continue


def download_index(year: int, dest: str | Path | None = None, client: httpx.Client | None = None) -> Path:
    """Stream the (tens of MB) yearly index CSV to disk. Batches are never downloaded here."""
    out = Path(dest) if dest else get_settings().data_dir / "raw" / "irs" / f"index_{year}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    own = client is None
    c = client or httpx.Client(timeout=120, headers={"User-Agent": get_settings().user_agent}, follow_redirects=True)
    try:
        with c.stream("GET", INDEX_URL.format(year=year)) as resp:
            resp.raise_for_status()
            tmp = out.with_suffix(".part")
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
            tmp.replace(out)
    finally:
        if own:
            c.close()
    return out


def build_zip(files: dict[str, bytes], batch_id: str = "TEST_BATCH") -> bytes:
    """Small in-memory batch with the IRS member layout (used by tests and fixtures)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(f"{batch_id}/{name}", files[name])
    return buf.getvalue()

"""Atom 1.0 change feed (``/v1/changes.atom``), built with the stdlib XML writer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

ATOM_NS = "http://www.w3.org/2005/Atom"


def _ts(value: str | datetime | None) -> str:
    if value is None:
        return datetime(1970, 1, 1, tzinfo=UTC).isoformat()
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return datetime.fromisoformat(value).astimezone(UTC).isoformat()


def atom_feed(changes: list[dict[str, Any]], *, base_url: str, self_path: str = "/v1/changes.atom",
              title: str = "Charity Care Commons: Texas policy changes") -> bytes:
    """Render change events (as returned by ``queries.changes``) as an Atom document."""
    ET.register_namespace("", ATOM_NS)

    def el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
        e = ET.SubElement(parent, f"{{{ATOM_NS}}}{tag}", attrs)
        if text is not None:
            e.text = text
        return e

    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    el(feed, "id", f"{base_url}{self_path}")
    el(feed, "title", title)
    el(feed, "updated", _ts(changes[0]["published_at"]) if changes else _ts(None))
    el(feed, "link", rel="self", href=f"{base_url}{self_path}", type="application/atom+xml")
    el(feed, "link", rel="alternate", href=f"{base_url}/v1/changes", type="application/json")
    author = el(feed, "author")
    el(author, "name", "Charity Care Commons")
    el(feed, "rights", "Data CC BY 4.0, Charity Care Commons")
    for ch in changes:
        entry = el(feed, "entry")
        el(entry, "id", f"urn:charitycarecommons:change:{ch['id']}")
        facilities = ", ".join(ch.get("facility_ids") or []) or "unmapped facilities"
        el(entry, "title", f"{ch['triage'].replace('_', ' ').capitalize()} change: {ch['policy_version_id']} ({facilities})")
        el(entry, "updated", _ts(ch["published_at"]))
        el(entry, "link", rel="alternate", href=f"{base_url}/v1/policies/{ch['policy_version_id']}/fields",
           type="application/json")
        el(entry, "category", term=ch["triage"])
        el(entry, "summary", ch["diff_summary"])
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)

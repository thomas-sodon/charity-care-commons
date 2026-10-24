"""Schema-compatibility check for the published Open FAP Schema (``schemas/open-fap-schema.json``).

Two checks, both run in CI:

1. **Fresh**: the committed file equals what ``charitycare.schemas.open_fap_schema()`` generates.
2. **Compatible**: compared with the previous published version (e.g. the file on
   ``main``), no definition or property was removed, no property became newly
   required, no property changed shape and no enum value disappeared, unless
   the schema ``version`` was bumped. Additions are reported, never failed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SHAPE_KEYS = ("type", "$ref", "items", "anyOf", "oneOf", "allOf", "const", "format", "additionalProperties", "prefixItems")


def shape(s: Any) -> Any:
    """The structural part of a JSON schema node (descriptions, titles and defaults dropped)."""
    if isinstance(s, dict):
        out = {k: shape(v) for k, v in s.items() if k in _SHAPE_KEYS}
        if "enum" in s:
            out["enum"] = "<enum>"  # enum membership is compared separately
        return out
    if isinstance(s, list):
        return [shape(x) for x in s]
    return s


def _enums(s: Any, path: str = "") -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    if isinstance(s, dict):
        if "enum" in s:
            out[path] = {json.dumps(v) for v in s["enum"]}
        for k, v in s.items():
            if k != "enum":
                out.update(_enums(v, f"{path}/{k}"))
    elif isinstance(s, list):
        for i, v in enumerate(s):
            out.update(_enums(v, f"{path}/{i}"))
    return out


@dataclass
class CompatReport:
    breaking: list[str] = field(default_factory=list)
    additive: list[str] = field(default_factory=list)
    old_version: str | None = None
    new_version: str | None = None

    @property
    def version_bumped(self) -> bool:
        return self.old_version != self.new_version

    @property
    def ok(self) -> bool:
        return not self.breaking or self.version_bumped


def compare(old: dict[str, Any], new: dict[str, Any]) -> CompatReport:
    """Breaking and additive differences between two Open FAP Schema documents."""
    r = CompatReport(old_version=old.get("version"), new_version=new.get("version"))
    if old.get("$ref") != new.get("$ref"):
        r.breaking.append(f"root $ref changed: {old.get('$ref')} -> {new.get('$ref')}")
    od, nd = old.get("$defs", {}), new.get("$defs", {})
    for name in sorted(set(od) - set(nd)):
        r.breaking.append(f"definition removed: {name}")
    for name in sorted(set(nd) - set(od)):
        r.additive.append(f"definition added: {name}")
    for name in sorted(set(od) & set(nd)):
        o, n = od[name], nd[name]
        op, np_ = o.get("properties", {}), n.get("properties", {})
        for p in sorted(set(op) - set(np_)):
            r.breaking.append(f"{name}.{p}: property removed")
        for p in sorted(set(np_) - set(op)):
            (r.breaking if p in set(n.get("required", [])) else r.additive).append(
                f"{name}.{p}: property added{' as required' if p in set(n.get('required', [])) else ''}")
        for p in sorted(set(n.get("required", [])) - set(o.get("required", [])) - (set(np_) - set(op))):
            r.breaking.append(f"{name}.{p}: became required")
        for p in sorted(set(op) & set(np_)):
            if shape(op[p]) != shape(np_[p]):
                r.breaking.append(f"{name}.{p}: shape changed")
        if not op and shape(o) != shape(n):
            r.breaking.append(f"{name}: shape changed")
        oe, ne = _enums(o), _enums(n)
        for path in sorted(set(oe) & set(ne)):
            for v in sorted(oe[path] - ne[path]):
                r.breaking.append(f"{name}{path}: enum value removed {v}")
            for v in sorted(ne[path] - oe[path]):
                r.additive.append(f"{name}{path}: enum value added {v}")
    return r


def check_fresh(committed: Path) -> bool:
    """True when the committed schema file matches the generator byte for byte."""
    from charitycare.schemas import open_fap_schema

    expected = json.dumps(open_fap_schema(), indent=2, sort_keys=True) + "\n"
    return committed.is_file() and committed.read_text(encoding="utf-8") == expected

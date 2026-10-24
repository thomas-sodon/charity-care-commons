"""Value normalization and comparison for scoring compiled fields against gold.

Fields are handled as plain JSON dicts (``Field.model_dump(mode="json")``) so
scorers can be exercised with hand-written synthetic predictions. A missing
key, an ``abstain_reason`` or a ``None`` value all count as an abstention.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

ABSTAIN = "__abstain__"
NONE = "__none__"  # the policy addresses the rule and says there is none (e.g. no asset test)

FieldDict = Mapping[str, Any]

_PUNCT = re.compile(r"[^\w\s%$.@/:-]+")
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[\w%$]+")

# Long free-text fields: equal when normalized equal or token-set F1 >= this.
TEXT_F1_EQUAL = 0.9
FREE_TEXT_FIELDS = frozenset({"policy_title", "household_definition", "provider_list_location", "refund_policy"})

THRESHOLD_KEYS: tuple[str, ...] = ("free_cutoff_pct", "discount_cutoff_pct", "asset_threshold_usd")

# (field key, dotted path or "" for the value itself)
CATEGORICAL_ITEMS: tuple[tuple[str, str], ...] = (
    ("fpg_year_rule", ""),
    ("income_definition", ""),
    ("agb_method", ""),
    ("screening_timing", ""),
    ("medicaid_application_required_first", ""),
    ("determination_notice", ""),
    ("asset_test", "applies"),
    ("asset_test", "applies_to_tiers"),
    ("residency", "required"),
    ("residency", "area_type"),
    ("residency", "emergency_exempt"),
    ("insured_patients", "insured_eligible"),
    ("application_window", "anchor"),
    ("presumptive_eligibility", "allowed"),
    ("presumptive_eligibility", "uses_credit_data"),
)

# (name, field key, item extractor) for item-level F1.
LIST_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("required_documents", "required_documents", "doc_type"),
    ("submission_channels", "submission_channels", "kind"),
    ("presumptive_criteria", "presumptive_eligibility", "criteria"),
    ("residency_counties", "residency", "counties"),
    ("excluded_services", "excluded_services", ""),
    ("facilities_covered", "facilities_covered", ""),
)


def norm_text(s: Any) -> str:
    """Case-folded, NFKC, punctuation-light, whitespace-collapsed text."""
    t = unicodedata.normalize("NFKC", str(s)).casefold()
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip(" .")


def token_f1(a: str, b: str) -> float:
    """Token-set F1 of two strings (after :func:`norm_text`)."""
    ta, tb = set(_TOKEN.findall(norm_text(a))), set(_TOKEN.findall(norm_text(b)))
    if not ta and not tb:
        return 1.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    p, r = inter / len(tb), inter / len(ta)
    return 2 * p * r / (p + r)


def is_abstained(f: FieldDict | None) -> bool:
    return f is None or f.get("abstain_reason") is not None or f.get("value") is None


def value_of(f: FieldDict | None) -> Any:
    return None if is_abstained(f) else f["value"]  # type: ignore[index]


def canonical(x: Any) -> Any:
    """Hashable canonical form: numbers rounded, strings normalized, lists as sorted tuples."""
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, (int, float)):
        return round(float(x), 6)
    if isinstance(x, str):
        return norm_text(x)
    if isinstance(x, Mapping):
        return tuple(sorted((k, canonical(v)) for k, v in x.items()))
    if isinstance(x, (list, tuple, set)):
        return tuple(sorted((canonical(v) for v in x), key=repr))
    return norm_text(x)


def values_equal(a: Any, b: Any, *, free_text: bool = False) -> bool:
    """Structural equality with normalization; free text also matches at token F1 >= 0.9."""
    if isinstance(a, str) and isinstance(b, str):
        return norm_text(a) == norm_text(b) or (free_text and token_f1(a, b) >= TEXT_F1_EQUAL)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    return canonical(a) == canonical(b)


def field_correct(key: str, gold: FieldDict | None, pred: FieldDict | None) -> bool | None:
    """Whether a published prediction matches gold; ``None`` if the prediction abstains."""
    if is_abstained(pred):
        return None
    if is_abstained(gold):
        return False
    return values_equal(value_of(gold), value_of(pred), free_text=key in FREE_TEXT_FIELDS)


# --------------------------------------------------------------------------
# Derived thresholds (section 12 "numeric thresholds")
# --------------------------------------------------------------------------


def _eligible_tiers(tiers: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [t for t in tiers if t.get("applies_to", "all") in ("all", "uninsured")]


def thresholds(fields: Mapping[str, FieldDict]) -> dict[str, Any]:
    """Free cutoff %FPG, top discount cutoff %FPG and asset threshold $, or ABSTAIN / NONE."""
    out: dict[str, Any] = {}
    tiers = value_of(fields.get("fpg_tiers"))
    if tiers is None:
        out["free_cutoff_pct"] = out["discount_cutoff_pct"] = ABSTAIN
    else:
        el = _eligible_tiers(tiers)
        free = [float(t["max_pct_inclusive"]) for t in el if t.get("benefit") == "free"]
        disc = [float(t["max_pct_inclusive"]) for t in el if t.get("benefit") != "free"]
        out["free_cutoff_pct"] = max(free) if free else NONE
        out["discount_cutoff_pct"] = max(disc) if disc else NONE
    asset = value_of(fields.get("asset_test"))
    if asset is None:
        out["asset_threshold_usd"] = ABSTAIN
    elif not asset.get("applies"):
        out["asset_threshold_usd"] = NONE
    else:
        thr = asset.get("threshold_usd")
        out["asset_threshold_usd"] = float(thr) if thr is not None else NONE
    return out


def threshold_equal(a: Any, b: Any) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(float(a) - float(b)) < 1e-6


# --------------------------------------------------------------------------
# Categorical and list items
# --------------------------------------------------------------------------


def _get(v: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if not isinstance(v, Mapping):
            return None
        v = v.get(part)
    return v


def categorical_label(fields: Mapping[str, FieldDict], key: str, path: str) -> str:
    """Label of one categorical item: the value as text, or ABSTAIN."""
    v = value_of(fields.get(key))
    if v is None:
        return ABSTAIN
    sub = _get(v, path) if path else v
    return "null" if sub is None else norm_text(sub) if not isinstance(sub, bool) else str(sub).lower()


def list_items(fields: Mapping[str, FieldDict], key: str, extractor: str) -> set[str] | None:
    """Normalized item set of a list field, or ``None`` when the field abstains."""
    v = value_of(fields.get(key))
    if v is None:
        return None
    if extractor and isinstance(v, Mapping):
        v = v.get(extractor) or []
    items: set[str] = set()
    for it in v if isinstance(v, list) else [v]:
        if extractor and isinstance(it, Mapping):
            it = it.get(extractor)
        if it is not None:
            items.add(norm_text(it))
    return items

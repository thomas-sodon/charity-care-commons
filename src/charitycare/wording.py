"""Substantiation discipline for user-facing text (design section 11).

We never say a household *is* "eligible", that a hospital is "compliant", or
that it committed a "violation". We say "likely eligible" and "discrepancy for
review". :func:`find_banned_wording` checks our own generated strings
(templates, API messages, letters); verbatim hospital quotes are exempt and
should be stripped before checking (pass ``ignore_quoted=True``).
"""

from __future__ import annotations

import re

OUTCOME_LABELS: dict[str, str] = {
    "likely_free": "Likely eligible for free care",
    "likely_discount": "Likely eligible for discounted care",
    "likely_not_eligible_on_income": "Likely not eligible on income alone",
    "preliminary_provisional_fields": "Preliminary: based on fields not yet verified by a reviewer",
    "insufficient_information": "Not enough information to estimate",
}

DISCREPANCY_LABEL = "Discrepancy for review"

_BANNED = [
    # "eligible" not preceded by "likely" / "likely not" (and not part of "eligibility")
    re.compile(r"(?<![A-Za-z])(?<!likely )(?<!likely not )(?<!in)eligible(?![A-Za-z])", re.IGNORECASE),
    re.compile(r"\b(non-?)?complian(t|ce)\b", re.IGNORECASE),
    re.compile(r"\bviolat(ion|ions|es|ed|ing|e)\b", re.IGNORECASE),
]
_QUOTED = re.compile(r"“[^”]*”|\"[^\"]*\"")


def find_banned_wording(text: str, *, ignore_quoted: bool = True) -> list[str]:
    """Return the offending phrases found in ``text`` (empty list means clean)."""
    if ignore_quoted:
        text = _QUOTED.sub(" ", text)
    hits: list[str] = []
    for rx in _BANNED:
        hits.extend(m.group(0) for m in rx.finditer(text))
    return hits


def assert_safe_wording(text: str, *, ignore_quoted: bool = True) -> None:
    hits = find_banned_wording(text, ignore_quoted=ignore_quoted)
    if hits:
        raise ValueError(f"banned wording in user-facing text: {hits}")

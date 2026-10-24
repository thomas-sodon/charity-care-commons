"""Change detection between document versions (step S9).

``difflib`` runs on normalized page text (so re-flowed lines and smart quotes do
not count as changes). A Haiku-class model labels the diff ``substantive``,
``cosmetic`` or ``date_only``; a deterministic heuristic labels it when no
model output is available, and acts as a guard either way: a diff that changes
any number other than a date, or a negation/limiting/modal word ("not", "only",
"may", "shall"), is always ``substantive``, whatever the model says.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PField

from charitycare.compile.prompts import TRIAGE_VERSION, triage_system
from charitycare.compile.textlayer import DocText
from charitycare.compile.verify import normalize_variants
from charitycare.llm import DocumentPart, LLMBackend, LLMError, LLMRequest
from charitycare.schemas import ChangeEvent, utcnow
from charitycare.wording import assert_safe_wording

TriageLabel = Literal["substantive", "cosmetic", "date_only"]

_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE = re.compile(
    rf"\b{_MONTHS}\.? \d{{1,2}}, \d{{4}}\b|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b{_MONTHS} \d{{4}}\b",
    re.IGNORECASE,
)
_PAGE_FOOTER = re.compile(r"page \d+ of \d+", re.IGNORECASE)
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[a-z0-9$%]+")


class ChangeTriage(BaseModel):
    """LLM output schema for change triage."""

    model_config = ConfigDict(extra="forbid")

    label: TriageLabel
    rationale: str
    changed_topics: list[str] = PField(default_factory=list)


@dataclass
class TextDiff:
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.removed and not self.added

    def unified(self, limit: int = 120) -> str:
        lines = [f"- {x}" for x in self.removed] + [f"+ {x}" for x in self.added]
        return "\n".join(lines[:limit])


def _norm_lines(pages: list[str]) -> list[str]:
    """Normalized sentences: pages joined, footers dropped, re-split on sentence ends."""
    text = " ".join(_PAGE_FOOTER.sub(" ", p) for p in pages)
    flat = normalize_variants(text)["joined"]
    return [s.strip() for s in re.split(r"(?<=[.;:])\s+", flat) if s.strip()]


def diff_texts(old_pages: list[str], new_pages: list[str]) -> TextDiff:
    """Sentence-level diff of two documents' normalized text."""
    a, b = _norm_lines(old_pages), _norm_lines(new_pages)
    d = TextDiff()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            d.removed += a[i1:i2]
        if op in ("replace", "insert"):
            d.added += b[j1:j2]
    return d


def heuristic_triage(d: TextDiff) -> ChangeTriage:
    """Deterministic label: date-only, cosmetic (no word changes) or substantive."""
    if d.empty:
        return ChangeTriage(label="cosmetic", rationale="No text changes after normalization.")
    old, new = " ".join(d.removed), " ".join(d.added)
    strip_dates = lambda s: _DATE.sub("<date>", s)  # noqa: E731
    if strip_dates(old) == strip_dates(new) and _DATE.search(old + new):
        return ChangeTriage(label="date_only", rationale="Only dates changed.", changed_topics=["effective_date"])
    if _NUM.findall(strip_dates(old)) != _NUM.findall(strip_dates(new)):
        return ChangeTriage(label="substantive", rationale="Numbers changed (limits, amounts, days or contacts).")
    if _WORD.findall(strip_dates(old)) == _WORD.findall(strip_dates(new)):
        return ChangeTriage(label="cosmetic", rationale="Only punctuation or formatting changed.")
    return ChangeTriage(label="substantive", rationale="Wording changed; treated as substantive until reviewed.")


def numbers_changed(d: TextDiff) -> bool:
    old, new = (_DATE.sub(" ", " ".join(x)) for x in (d.removed, d.added))
    return _NUM.findall(old) != _NUM.findall(new)


_POLARITY_WORDS = re.compile(r"\b(?:not|no|never|none|nor|without|except|unless|only|may|shall|must|will|cannot)\b",
                             re.IGNORECASE)


def polarity_changed(d: TextDiff) -> bool:
    """A negation, limiting or modal word ("not", "only", "may", "shall") was added or removed."""
    old, new = (" ".join(x) for x in (d.removed, d.added))
    return sorted(m.lower() for m in _POLARITY_WORDS.findall(old)) != sorted(m.lower() for m in _POLARITY_WORDS.findall(new))


@dataclass
class ChangeResult:
    diff: TextDiff
    triage: ChangeTriage
    source: Literal["live", "replay", "record", "heuristic"]
    event: ChangeEvent | None = None
    cost_usd: float = 0.0


def triage(d: TextDiff, *, new_sha: str, prev_sha: str, backend: LLMBackend | None) -> tuple[ChangeTriage, str, float]:
    """Model triage with the heuristic as fallback and guard."""
    guard = heuristic_triage(d)
    if d.empty or backend is None:
        return guard, "heuristic", 0.0
    req: LLMRequest[ChangeTriage] = LLMRequest(
        prompt_version=TRIAGE_VERSION,
        model_tier="haiku",
        doc_sha256=new_sha,
        system=triage_system(),
        documents=(DocumentPart("text", d.unified(), title="normalized diff"),),
        user=f"Label this change from document {prev_sha[:12]} to {new_sha[:12]}.",
        output_schema=ChangeTriage,
        variant=f"prev-{prev_sha[:16]}",
        max_tokens=1000,
        effort="low",
    )
    try:
        res = backend.complete(req)
    except LLMError:
        return guard, "heuristic", 0.0
    out = res.output
    if out.label != "substantive" and numbers_changed(d):
        out = out.model_copy(update={"label": "substantive", "rationale": out.rationale + " (numbers changed; raised to substantive by code)"})
    elif out.label != "substantive" and polarity_changed(d):
        out = out.model_copy(update={"label": "substantive",
                                     "rationale": out.rationale + " (a negation or modal word changed; raised to substantive by code)"})
    return out, res.backend, res.cost_usd


def detect_change(
    old: DocText,
    new: DocText,
    *,
    policy_version_id: str,
    prev_version_id: str | None,
    backend: LLMBackend | None = None,
) -> ChangeResult:
    """Diff two hashed versions and build the :class:`ChangeEvent` (None when hashes are equal)."""
    d = diff_texts([p.text for p in old.pages], [p.text for p in new.pages])
    if old.sha256 == new.sha256:
        return ChangeResult(d, ChangeTriage(label="cosmetic", rationale="Identical document hash."), "heuristic")
    t, source, cost = triage(d, new_sha=new.sha256, prev_sha=old.sha256, backend=backend)
    summary = (
        f"{t.label}: {t.rationale} {len(d.removed)} passage(s) removed, {len(d.added)} added."
        + (f" Topics: {', '.join(t.changed_topics)}." if t.changed_topics else "")
    )
    if d.added:
        summary += f' First added passage: "{d.added[0][:200].replace('"', "'")}"'
    assert_safe_wording(summary)
    ev = ChangeEvent(
        id=f"chg:{policy_version_id}:{prev_version_id or old.sha256[:12]}",
        policy_version_id=policy_version_id,
        prev_version_id=prev_version_id,
        triage=t.label,
        diff_summary=summary,
        published_at=utcnow() if t.label == "substantive" else None,
    )
    return ChangeResult(d, t, source, ev, cost)

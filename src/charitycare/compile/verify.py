"""The quote verifier: "no quote, no publish" (design section 5 mechanism 1, step S4).

A field is publishable only when every evidence quote is found on its cited
page of the hashed document's independent text layer. Matching is done on
normalized text (Unicode NFKC, straight quotes and dashes, soft hyphens removed,
line-end hyphenation joined, whitespace collapsed, case-folded):

1. exact substring on the plain normalization (offsets are reported in
   ``pdftext.normalize_text(page)`` coordinates, as the schema promises);
2. exact substring after joining line-end hyphenation, both keeping the hyphen
   ("post-\\ndischarge") and dropping it ("assis-\\ntance");
3. fuzzy: best window similarity >= 0.98 (``difflib``), for OCR-grade noise,
   provided every differing token is a one-for-one, edit-distance-1 change inside a
   word of 4+ characters that is not a number, a negation or modal ("not", "may"),
   a proper noun or a polarity-affix pair ("insured"/"uninsured"), because one
   changed digit or a dropped "not" passes any similarity threshold. Exact matches
   must start and end on word boundaries.

Anything else fails. A quote found verbatim on a *different* page still fails
(the citation is wrong), but the other page is reported so the targeted re-ask
and the reviewer can see it. A separate *numeric grounding* check requires that
every number in a value is printed in one of the field's quotes, which catches
misread values whose quote is genuine.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Literal

from charitycare.compile.textlayer import DocText
from charitycare.pdftext import find_bounded, find_quote
from charitycare.schemas import Evidence, Field

FUZZY_THRESHOLD = 0.98
MIN_QUOTE_CHARS = 8
MIN_FUZZY_CHARS = 40

MatchMethod = Literal["exact", "exact_dehyphenated", "fuzzy", "none"]
FailReason = Literal[
    "unknown_document", "page_out_of_range", "no_text_layer", "empty_quote", "quote_too_short", "not_found", "wrong_page"
]

_TRANSLATE = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
        " ": " ", " ": " ", " ": " ", "­": None, "​": None, "﻿": None,
    }
)
_WS = re.compile(r"\s+")
_HYPHEN_BREAK = re.compile(r"(\w)-[ \t]*\r?\n[ \t]*(\w)")


def _base(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_TRANSLATE)


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()


def normalize_variants(text: str) -> dict[str, str]:
    """Normalized forms used for matching: ``plain``, ``joined`` (hyphen kept), ``dehyphenated``."""
    t = _base(text)
    return {
        "plain": _collapse(t),
        "joined": _collapse(_HYPHEN_BREAK.sub(r"\1-\2", t)),
        "dehyphenated": _collapse(_HYPHEN_BREAK.sub(r"\1\2", t)),
    }


def best_similarity(hay: str, needle: str, k: int = 12) -> tuple[float, int, int]:
    """Best ``SequenceMatcher`` ratio of ``needle`` against any window of ``hay``.

    Candidate windows are anchored on shared k-grams, so the search is linear-ish
    rather than quadratic. Returns (ratio, start, end) in ``hay`` coordinates.
    """
    n = len(needle)
    if not n or not hay:
        return 0.0, -1, -1
    k = min(k, max(4, n // 4))
    starts: set[int] = set()
    for off in range(0, max(1, n - k + 1), max(1, k // 2)):
        gram = needle[off : off + k]
        i = hay.find(gram)
        while i >= 0:
            starts.add(max(0, i - off))
            i = hay.find(gram, i + 1)
    slack = max(2, int(n * (1 - FUZZY_THRESHOLD)) + 2)
    best = (0.0, -1, -1)
    for s0 in sorted(starts):
        for s in range(max(0, s0 - slack), s0 + slack + 1):
            for ln in (n - slack, n, n + slack):
                window = hay[s : s + max(1, ln)]
                if not window:
                    continue
                sm = SequenceMatcher(None, window, needle, autojunk=False)
                if sm.real_quick_ratio() <= best[0] or sm.quick_ratio() <= best[0]:
                    continue
                r = sm.ratio()
                if r > best[0]:
                    best = (r, s, s + len(window))
    return best


@dataclass(frozen=True)
class QuoteCheck:
    """Outcome of verifying one evidence quote."""

    ok: bool
    method: MatchMethod
    page: int
    similarity: float = 0.0
    reason: FailReason | None = None
    found_on_pages: tuple[int, ...] = ()
    span: tuple[int, int] | None = None

    def describe(self) -> str:
        if self.ok:
            return f"p.{self.page} quote match: {self.method} (similarity {self.similarity:.3f})"
        extra = f"; found verbatim on page(s) {', '.join(map(str, self.found_on_pages))}" if self.found_on_pages else ""
        return f"p.{self.page} quote not verified: {self.reason} (best similarity {self.similarity:.3f}){extra}"


def match_on_page(page_text: str, quote: str) -> tuple[MatchMethod, float, tuple[int, int] | None]:
    """Match one quote against one page's text."""
    span = find_quote(page_text, quote)
    if span is not None:
        return "exact", 1.0, span
    hv, nv = normalize_variants(page_text), normalize_variants(quote)
    for variant in ("joined", "dehyphenated"):
        i = find_bounded(hv[variant], nv[variant])
        if nv[variant] and i >= 0:
            return "exact_dehyphenated", 1.0, (i, i + len(nv[variant]))
    needle = nv["joined"]
    if len(needle) < MIN_FUZZY_CHARS:
        return "none", 0.0, None
    hay = hv["joined"]
    ratio, s, e = best_similarity(hay, needle)
    # widen the window to whole words so a mid-word start ("[un]insured") is compared as a token
    while s > 0 and hay[s - 1].isalnum():
        s -= 1
    while 0 <= e < len(hay) and hay[e].isalnum():
        e += 1
    if ratio >= FUZZY_THRESHOLD and fuzzy_safe(hay[s:e], needle, proper_nouns(quote)):
        return "fuzzy", ratio, (s, e)
    return "none", ratio, None


_NEGATIONS = {"not", "no", "never", "without", "except", "unless", "nor", "cannot", "only",
              "may", "might", "can", "will", "shall", "must", "should", "all", "any", "none", "each"}
_POLARITY = ("un", "in", "im", "ir", "il", "non", "dis")
_TOKEN = re.compile(r"\w+")


def proper_nouns(text: str) -> set[str]:
    """Casefolded capitalized tokens that do not start a sentence (likely names, e.g. "Bexar")."""
    out: set[str] = set()
    toks = list(re.finditer(r"\w+", _base(text)))
    for k, m in enumerate(toks):
        w = m.group(0)
        if not w[0].isupper():
            continue
        prev = text[: m.start()].rstrip()
        if k == 0 or prev.endswith((".", "!", "?", ":")):
            continue
        out.add(w.casefold())
    return out


def _edit1(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ by exactly one substitution, insertion or deletion."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _polarity_pair(a: str, b: str) -> bool:
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return any(long_ == p + short for p in _POLARITY)


def fuzzy_safe(window: str, needle: str, names: set[str] | frozenset[str] = frozenset()) -> bool:
    """A fuzzy match may differ only by OCR-grade noise that cannot change meaning.

    Every differing token must be a one-for-one replacement of a word of 4+ characters
    at edit distance 1 (such as OCR's "disc0unt"); it must not be a number, a
    negation/modal/limiting word, a proper noun (``names``), or a polarity-affix pair
    ("insured"/"uninsured"). Whole-token insertions and deletions are never tolerated.
    """
    a, b = _TOKEN.findall(window), _TOKEN.findall(needle)
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op != "replace" or (i2 - i1) != (j2 - j1):
            return False
        for x, y in zip(a[i1:i2], b[j1:j2]):
            for tok in (x, y):
                if tok.isdigit() or tok in _NEGATIONS or tok in names:
                    return False
            if min(len(x), len(y)) < 4 or not _edit1(x, y) or _polarity_pair(x, y):
                return False
    return True


def verify_evidence(ev: Evidence, docs: dict[str, DocText]) -> QuoteCheck:
    """Verify one evidence item against the hashed document it cites."""
    doc = docs.get(ev.doc_sha256)
    if doc is None:
        return QuoteCheck(False, "none", ev.page, reason="unknown_document")
    if len(normalize_variants(ev.quote)["plain"]) == 0:
        return QuoteCheck(False, "none", ev.page, reason="empty_quote")
    if len(normalize_variants(ev.quote)["plain"]) < MIN_QUOTE_CHARS:
        return QuoteCheck(False, "none", ev.page, reason="quote_too_short")
    page = doc.page(ev.page)
    if page is None:
        return QuoteCheck(False, "none", ev.page, reason="page_out_of_range")
    if not page.text.strip():
        return QuoteCheck(False, "none", ev.page, reason="no_text_layer")
    method, sim, span = match_on_page(page.text, ev.quote)
    if method != "none":
        return QuoteCheck(True, method, ev.page, sim, span=span)
    elsewhere = tuple(
        p.page for p in doc.pages if p.page != ev.page and p.text.strip() and match_on_page(p.text, ev.quote)[0] != "none"
    )
    return QuoteCheck(False, "none", ev.page, sim, reason="wrong_page" if elsewhere else "not_found", found_on_pages=elsewhere)


# --------------------------------------------------------------------------
# Numeric grounding: every number in a value must be printed in a quote
# --------------------------------------------------------------------------

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "eighteen": 18, "twenty": 20, "twenty-four": 24, "thirty": 30,
    "sixty": 60, "ninety": 90, "hundred": 100,
}


def numbers_in(text: str) -> set[Decimal]:
    """Numbers printed in ``text`` (digits with thousands separators, and common number words)."""
    out: set[Decimal] = set()
    t = _base(text)
    for m in _NUM.finditer(t):
        try:
            out.add(Decimal(m.group(0).replace(",", "").rstrip(".")).normalize())
        except InvalidOperation:
            continue
    for w in re.findall(r"[a-z]+(?:-[a-z]+)?", t.casefold()):
        if w in _WORDS:
            out.add(Decimal(_WORDS[w]))
    return out


def _d(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v)).normalize()
    except InvalidOperation:
        return None


def value_numbers(key: str, value: Any) -> list[tuple[str, Decimal]]:
    """The numbers in a compiled value that must be grounded, as (path, number)."""
    if value is None:
        return []
    v = value.model_dump() if hasattr(value, "model_dump") else value
    nums: list[tuple[str, Any]] = []
    if key == "fpg_tiers":
        for i, t in enumerate(v or []):
            t = t.model_dump() if hasattr(t, "model_dump") else t
            nums += [(f"[{i}].max_pct_inclusive", t.get("max_pct_inclusive")), (f"[{i}].min_pct_exclusive", t.get("min_pct_exclusive"))]
            if t.get("benefit") in ("percent_discount", "sliding_scale"):
                nums.append((f"[{i}].discount_pct", t.get("discount_pct")))
    elif key == "agb_pct":
        nums.append(("", v))
    elif key == "award_period_months":
        nums.append(("", v))
    elif key == "asset_test":
        nums.append((".threshold_usd", v.get("threshold_usd")))
    elif key == "application_window":
        nums.append((".days", v.get("days")))
    elif key == "insured_patients":
        nums.append((".catastrophic_threshold_pct_income", v.get("catastrophic_threshold_pct_income")))
    out = []
    for path, n in nums:
        d = _d(n)
        if d is not None and d != 0:
            out.append((path, d))
    return out


def ungrounded_numbers(key: str, f: Field[Any]) -> list[str]:
    """Value numbers that appear in none of the field's quotes (empty list means grounded)."""
    printed: set[Decimal] = set()
    for ev in f.evidence:
        printed |= numbers_in(ev.quote)
    return [f"{key}{path} = {n:f}" for path, n in value_numbers(key, f.value) if n not in printed]


# --------------------------------------------------------------------------
# Field-level verification
# --------------------------------------------------------------------------


@dataclass
class FieldCheck:
    """Verification of one compiled field."""

    key: str
    quote_checks: list[QuoteCheck] = field(default_factory=list)
    ungrounded: list[str] = field(default_factory=list)
    missing_evidence: bool = False

    @property
    def quotes_ok(self) -> bool:
        return bool(self.quote_checks) and all(q.ok for q in self.quote_checks)

    @property
    def ok(self) -> bool:
        return not self.missing_evidence and self.quotes_ok and not self.ungrounded

    @property
    def illegible(self) -> bool:
        """Failure caused only by pages without a readable text layer."""
        failed = [q for q in self.quote_checks if not q.ok]
        return bool(failed) and all(q.reason == "no_text_layer" for q in failed)

    def problems(self) -> list[str]:
        out = []
        if self.missing_evidence:
            out.append("no evidence quote was given for a non-abstained value")
        out += [q.describe() for q in self.quote_checks if not q.ok]
        out += [f"number not printed in any quote: {u}" for u in self.ungrounded]
        return out


def check_field(key: str, f: Field[Any], docs: dict[str, DocText]) -> FieldCheck:
    """Verify all quotes of a field and (for non-abstained values) numeric grounding."""
    fc = FieldCheck(key, [verify_evidence(ev, docs) for ev in f.evidence])
    if f.abstain_reason is None and f.value is not None:
        fc.missing_evidence = not f.evidence
        fc.ungrounded = ungrounded_numbers(key, f)
    return fc


def with_spans(f: Field[Any], fc: FieldCheck, *, keep_failed: bool = False) -> Field[Any]:
    """Copy of the field with verified char spans set (and failed evidence dropped unless kept)."""
    evs = []
    for ev, qc in zip(f.evidence, fc.quote_checks):
        if qc.ok:
            evs.append(ev.model_copy(update={"char_start": qc.span[0] if qc.span else None, "char_end": qc.span[1] if qc.span else None}))
        elif keep_failed:
            evs.append(ev)
    return f.model_copy(update={"evidence": evs})

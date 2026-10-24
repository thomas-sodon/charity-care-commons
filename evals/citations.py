"""Citation validity: is every quote of a published field on its cited page of the hashed document?

This re-verifies independently of the compile stage's own gate, from the
document bytes. It reuses the compile quote verifier
(:func:`charitycare.compile.verify.verify_evidence`: exact, de-hyphenated and
guarded >= 0.98 fuzzy matching) when that module is importable, and otherwise
falls back to the foundation's exact normalized matcher
(:func:`charitycare.pdftext.find_quote`), which is stricter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from charitycare.pdftext import PageText, find_quote, page_texts, sha256_bytes
from evals.compare import FieldDict


@dataclass(frozen=True)
class QuoteVerdict:
    ok: bool
    reason: str | None = None


@dataclass
class PageStore:
    """sha256 -> per-page text. Built from PDFs on disk or from synthetic page strings."""

    pages: dict[str, tuple[PageText, ...]]

    @classmethod
    def from_texts(cls, texts: Mapping[str, Sequence[str]]) -> PageStore:
        return cls({sha: tuple(PageText(i, t, bool(t.strip())) for i, t in enumerate(ps, start=1)) for sha, ps in texts.items()})

    @classmethod
    def from_paths(cls, paths: Mapping[str, Path]) -> PageStore:
        out: dict[str, tuple[PageText, ...]] = {}
        for sha, p in paths.items():
            if p.is_file():
                out[sha] = _pdf_pages(str(p), sha)
        return cls(out)


@lru_cache(maxsize=256)
def _pdf_pages(path: str, expected_sha: str) -> tuple[PageText, ...]:
    data = Path(path).read_bytes()
    if sha256_bytes(data) != expected_sha:  # identity is the hash: a different file is not that document
        return ()
    return tuple(page_texts(data))


def _compile_verifier() -> Callable[[Any, Any], Any] | None:
    try:
        from charitycare.compile.verify import verify_evidence

        return verify_evidence
    except Exception:  # noqa: BLE001 - optional dependency on a concurrently built stage
        return None


def verify_quote(ev: Mapping[str, Any], store: PageStore) -> QuoteVerdict:
    """Verify one evidence dict ``{doc_sha256, page, quote}``."""
    sha, page, quote = ev.get("doc_sha256"), int(ev.get("page") or 0), str(ev.get("quote") or "")
    pages = store.pages.get(str(sha))
    if not pages:
        return QuoteVerdict(False, "unknown_document")
    verifier = _compile_verifier()
    if verifier is not None:
        from charitycare.compile.textlayer import DocText
        from charitycare.schemas import Evidence

        try:
            evidence = Evidence.model_validate({k: ev.get(k) for k in ("doc_sha256", "page", "quote", "section_label")})
        except Exception:  # noqa: BLE001 - malformed evidence is simply invalid
            return QuoteVerdict(False, "malformed_evidence")
        qc = verifier(evidence, {str(sha): DocText(str(sha), pages)})
        return QuoteVerdict(bool(qc.ok), None if qc.ok else str(qc.reason))
    if not 1 <= page <= len(pages):
        return QuoteVerdict(False, "page_out_of_range")
    if not pages[page - 1].text.strip():
        return QuoteVerdict(False, "no_text_layer")
    return QuoteVerdict(True) if find_quote(pages[page - 1].text, quote) else QuoteVerdict(False, "not_found")


def verify_field(f: FieldDict | None, store: PageStore) -> tuple[bool, list[str]]:
    """A published field is valid only with >= 1 evidence item and every quote verified."""
    evidence = list((f or {}).get("evidence") or [])
    if not evidence:
        return False, ["no_evidence"]
    reasons = [v.reason or "" for v in (verify_quote(e, store) for e in evidence) if not v.ok]
    return not reasons, reasons


def verify_published(pred: Mapping[str, FieldDict], published: Callable[[str], bool], store: PageStore) -> dict[str, bool]:
    """Citation verdict for each published field of one prediction."""
    return {k: verify_field(f, store)[0] for k, f in pred.items() if published(k)}

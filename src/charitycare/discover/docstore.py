"""Versioned, content-addressed document store (design section 8, S3).

The sha256 of the bytes is the document's identity. A changed file at the same
URL is a *new* version; earlier versions are retained, never overwritten.
Layout under the store root (default ``data/cache/docstore``, git-ignored)::

    blobs/<sha[:2]>/<sha>.pdf|.html     original bytes (private evidence copy; not re-hosted publicly)
    text/<sha[:2]>/<sha>.json           per-page text layer (PyMuPDF; Tesseract OCR only if installed)

``DocumentVersion`` rows go to the registry when one is attached.
``pdf_uri`` / ``text_uri`` are ``file://`` URIs to those files.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pymupdf

from charitycare.config import get_settings
from charitycare.discover.classify import detect_language
from charitycare.discover.links import visible_text
from charitycare.pdftext import PageText, page_texts, sha256_bytes
from charitycare.schemas import DocumentVersion, SourceKind, utcnow
from charitycare.store import Registry


@dataclass(frozen=True)
class StoredDoc:
    version: DocumentVersion
    pages: list[PageText]
    is_new: bool

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def ocr_unavailable_pages(self) -> list[int]:
        return [p.page for p in self.pages if p.ocr_unavailable]


def sniff_mime(content: bytes, declared: str | None = None) -> str:
    if content[:5] == b"%PDF-":
        return "application/pdf"
    d = (declared or "").split(";")[0].strip().lower()
    if d:
        return d
    head = content[:512].lstrip().lower()
    return "text/html" if head.startswith((b"<!doctype html", b"<html")) else "application/octet-stream"


def uri_to_path(uri: str) -> Path:
    return Path(urlparse(uri).path)


class DocumentStore:
    """Content-addressed store of fetched documents and their text layers."""

    def __init__(self, root: str | Path | None = None, registry: Registry | None = None, *, ocr: bool | None = None):
        self.root = Path(root) if root else get_settings().data_dir / "cache" / "docstore"
        self.registry = registry
        self.ocr = ocr  # None: OCR image-only pages when Tesseract is installed

    def _blob(self, sha: str, ext: str) -> Path:
        return self.root / "blobs" / sha[:2] / f"{sha}.{ext}"

    def _text(self, sha: str) -> Path:
        return self.root / "text" / sha[:2] / f"{sha}.json"

    def put(
        self,
        content: bytes,
        *,
        source_id: str | None = None,
        kind: SourceKind = "other",
        mime: str | None = None,
        fetched_at: datetime | None = None,
        language: str | None = None,
        wayback_uri: str | None = None,
    ) -> StoredDoc:
        """Store bytes (idempotent by sha256) and return the version with its text layer."""
        sha = sha256_bytes(content)
        mime = sniff_mime(content, mime)
        ext = "pdf" if mime == "application/pdf" else ("html" if "html" in mime else "bin")
        blob = self._blob(sha, ext)
        is_new = not blob.exists()
        if is_new:
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(content)
        pages = self._load_pages(sha)
        if pages is None:
            pages = self._extract(content, mime)
            tp = self._text(sha)
            tp.parent.mkdir(parents=True, exist_ok=True)
            tp.write_text(json.dumps([asdict(p) for p in pages], ensure_ascii=False, indent=1), encoding="utf-8")
        prev = self.registry.get_document_version(sha) if self.registry else None
        text = "\n".join(p.text for p in pages)
        version = DocumentVersion(
            sha256=sha,
            source_id=source_id or (prev.source_id if prev else None),
            kind=kind if kind != "other" or prev is None else prev.kind,
            fetched_at=(prev.fetched_at if prev else None) or fetched_at or utcnow(),
            mime=mime,
            pages=len(pages) if mime == "application/pdf" else None,
            has_text_layer=all(p.has_text_layer for p in pages) if pages else False,
            ocr_used=any(p.ocr_used for p in pages),
            language=language or detect_language(text),
            text_uri=self._text(sha).resolve().as_uri(),
            pdf_uri=blob.resolve().as_uri(),
            wayback_uri=wayback_uri or (prev.wayback_uri if prev else None),
        )
        if self.registry is not None:
            self.registry.add_document_version(version)
        return StoredDoc(version, pages, is_new)

    def _extract(self, content: bytes, mime: str) -> list[PageText]:
        if mime == "application/pdf":
            try:
                return page_texts(content, ocr=self.ocr)
            except (pymupdf.FileDataError, RuntimeError, ValueError):
                return []
        if "html" in mime:
            text = visible_text(content, limit=200_000)
            return [PageText(1, text, bool(text.strip()))]
        return []

    def _load_pages(self, sha: str) -> list[PageText] | None:
        tp = self._text(sha)
        if not tp.exists():
            return None
        return [PageText(**p) for p in json.loads(tp.read_text(encoding="utf-8"))]

    def get_bytes(self, sha: str) -> bytes | None:
        for ext in ("pdf", "html", "bin"):
            if (p := self._blob(sha, ext)).exists():
                return p.read_bytes()
        return None

    def page_texts(self, sha: str) -> list[PageText] | None:
        """The stored per-page text layer for a document (None if unknown)."""
        return self._load_pages(sha)

    def versions_for_source(self, source_id: str) -> list[DocumentVersion]:
        """All stored versions fetched from one source, newest first (needs a registry)."""
        if self.registry is None:
            return []
        return self.registry.select(DocumentVersion, source_id=source_id, order_by="-fetched_at")

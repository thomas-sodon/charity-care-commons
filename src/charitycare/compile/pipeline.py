"""Compile orchestration: S4 compile -> quote gate -> re-ask -> S5 round trip -> S6 cross-check
-> S7 adjudication -> S8 gating and review tasks, written to the registry.

Entry points:

* :func:`compile_policy_version` compiles one :class:`PolicyVersion` bundle
  (idempotent: a version already compiled with the current prompt is reused
  unless ``force=True``);
* :func:`compile_facility` resolves the policy that governs a facility,
  compiles it if needed and stores the facility -> policy mapping.

Invariants: every published field passed the quote gate; a field the gate
rejects is stored as ``abstained`` with no value; code never sets ``verified``
(a reviewer's earlier verdict on an unchanged value is kept); no household data
is touched here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from charitycare import pdftext
from charitycare.compile import adjudicate as adj
from charitycare.compile.compiler import compile_document, field_schema, reask_field
from charitycare.compile.crosscheck import (
    DOCUMENTS_KEY,
    cross_check,
    document_cross_check,
    select_section,
)
from charitycare.compile.mapping import PolicyCandidate, needs_review, resolve_facility
from charitycare.compile.prompts import COMPILE_VERSION, REASK_VERSION
from charitycare.compile.review import FieldContext, impact, make_task, plan_tasks
from charitycare.compile.roundtrip import RoundTripResult, round_trip
from charitycare.compile.textlayer import DocText, RoutingPlan, plan_routing
from charitycare.compile.verify import FieldCheck, check_field, with_spans
from charitycare.config import get_settings
from charitycare.llm import LLMBackend, LLMError, get_backend
from charitycare.pdftext import sha256_bytes
from charitycare.schemas import (
    DETERMINATION_FIELDS,
    CompiledPolicy,
    CrossCheck,
    CrossCheckResult,
    DocumentVersion,
    Facility,
    Field,
    FieldRecord,
    FieldStatus,
    PolicyFacilityMap,
    PolicyVersion,
    ReviewTask,
    ScheduleHFacility,
    Source,
)
from charitycare.store import Registry

PdfLoader = Callable[[DocumentVersion], bytes]


class CompileError(RuntimeError):
    """The bundle cannot be compiled (missing policy, document or hash mismatch)."""


# --------------------------------------------------------------------------
# Document loading (sha256 is the identity)
# --------------------------------------------------------------------------


def default_pdf_loader(doc: DocumentVersion) -> bytes:
    """Read a stored document from ``pdf_uri`` (``file://``, absolute, or relative to the
    data dir / repo) or ``$CCC_DATA_DIR/docs/<sha>.pdf``, and check its hash."""
    s = get_settings()
    candidates: list[Path] = []
    if doc.pdf_uri:
        p = pdftext.uri_to_path(doc.pdf_uri)
        if p is not None:
            candidates += [p] if p.is_absolute() else [s.data_dir / p, s.home / p, s.demo_dir / p]
    candidates.append(s.data_dir / "docs" / f"{doc.sha256}.pdf")
    for c in candidates:
        if c.is_file():
            data = c.read_bytes()
            if sha256_bytes(data) != doc.sha256:
                raise CompileError(f"{c} does not hash to {doc.sha256[:12]}")
            return data
    raise CompileError(f"no stored bytes for document {doc.sha256[:12]} (pdf_uri={doc.pdf_uri!r})")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass
class FieldOutcome:
    key: str
    status: FieldStatus
    check: FieldCheck | None = None
    reasked: bool = False
    reask_source: str | None = None
    forced_abstention: bool = False
    problems: list[str] = field(default_factory=list)
    candidate: Field[Any] | None = None  # the rejected model value (review only, never published)


@dataclass
class CompileReport:
    policy_version_id: str
    doc_sha256: str | None = None
    cached: bool = False
    backend: str | None = None
    routing: RoutingPlan | None = None
    fields: dict[str, FieldOutcome] = field(default_factory=dict)
    crosschecks: dict[str, list[CrossCheck]] = field(default_factory=dict)
    document_checks: list[CrossCheck] = field(default_factory=list)
    candidate_crosschecks: dict[str, list[CrossCheck]] = field(default_factory=dict)  # rejected values; review only
    round_trips: list[RoundTripResult] = field(default_factory=list)
    adjudications: list[adj.AdjudicationRecord] = field(default_factory=list)
    tasks: list[ReviewTask] = field(default_factory=list)
    mappings: list[PolicyFacilityMap] = field(default_factory=list)
    schedule_h_id: str | None = None
    cost_usd: float = 0.0
    error: str | None = None

    def status_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.fields.values():
            out[o.status.value] = out.get(o.status.value, 0) + 1
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "policy_version_id": self.policy_version_id,
            "doc_sha256": self.doc_sha256,
            "cached": self.cached,
            "backend": self.backend,
            "error": self.error,
            "status": self.status_counts(),
            "reasked": sorted(k for k, o in self.fields.items() if o.reasked),
            "forced_abstentions": sorted(k for k, o in self.fields.items() if o.forced_abstention),
            "crosschecks": {k: [c.result.value for c in v] for k, v in sorted(self.crosschecks.items())},
            "document_checks": [c.result.value for c in self.document_checks],
            "round_trips": [rt.result for rt in self.round_trips],
            "adjudications": [f"{a.subject}:{a.source}:{'valid' if a.valid else 'discarded'}" for a in self.adjudications],
            "open_tasks": len(self.tasks),
            "mappings": {m.facility_id: f"{m.method}@{m.confidence:.2f}" for m in self.mappings},
            "schedule_h": self.schedule_h_id,
            "cost_usd": round(self.cost_usd, 6),
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def typed_field(rec: FieldRecord) -> Field[Any]:
    """A stored field re-validated into its concrete ``Field[...]`` type."""
    return field_schema(rec.key).model_validate(rec.field.model_dump(mode="json"))


def policy_from_records(records: Iterable[FieldRecord]) -> CompiledPolicy:
    """Rebuild a CompiledPolicy from stored records (missing keys abstain as not addressed)."""
    by_key = {r.key: r for r in records}
    data = {}
    for k in CompiledPolicy.field_keys():
        r = by_key.get(k)
        data[k] = r.field.model_dump(mode="json") if r else {"abstain_reason": "not_addressed"}
    return CompiledPolicy.model_validate(data)


def _abstained(key: str, reason: str | None) -> Field[Any]:
    return field_schema(key)(value=None, evidence=[], confidence=0.0, abstain_reason=reason)


_DISCRETION_PHRASE = re.compile(r"\b(?:at|in)\s+(?:its|their|the\s+[\w']+)\s+(?:sole\s+)?discretion\b|\bdiscretionary\b",
                                re.IGNORECASE)
_MAY = re.compile(r"\b(\w+)\s+may\b(?!\s+not\b)", re.IGNORECASE)
# "Patients may apply ..." grants the patient a right; it is not hospital discretion.
_PATIENT_SUBJECTS = {"patient", "patients", "applicant", "applicants", "you", "individuals", "individual", "guarantor",
                     "guarantors", "families", "family", "households", "household", "they", "person", "persons"}
_MANDATORY = re.compile(r"\b(?:will|shall|must|qualif(?:y|ies|ied)|(?:is|are)\s+eligible|entitled|may\s+not)\b",
                        re.IGNORECASE)


def _discretionary_quote(q: str) -> bool:
    if _DISCRETION_PHRASE.search(q):
        return True
    return any(m.group(1).casefold() not in _PATIENT_SUBJECTS for m in _MAY.finditer(q))


def is_discretionary(f: Field[Any]) -> bool:
    """Every quote states a discretionary rule ("the hospital may", "at its discretion") and none uses mandatory wording.

    Code-side guardrail (design section 11): such clauses abstain by default instead of
    being published as a firm rule, whatever the model returned. A "may" whose subject is
    the patient ("Patients may apply within 240 days") is a right, not discretion.
    """
    quotes = [ev.quote for ev in f.evidence]
    return bool(quotes) and all(_discretionary_quote(q) for q in quotes) and not any(_MANDATORY.search(q) for q in quotes)


def _discretion_guard(key: str, f: Field[Any], o: "FieldOutcome") -> Field[Any]:
    """Force ``ambiguous_may_clause`` (quotes kept) on a determination field whose only basis is discretionary."""
    if key not in DETERMINATION_FIELDS or f.abstain_reason is not None or not is_discretionary(f):
        return f
    o.status = FieldStatus.abstained
    o.problems = [*o.problems, "discretionary wording only: abstained by code (hospital discretion, see quote)"]
    return f.model_copy(update={"value": None, "abstain_reason": "ambiguous_may_clause"})


def _same_value(a: Field[Any], b: Field[Any]) -> bool:
    return a.model_dump(mode="json")["value"] == b.model_dump(mode="json")["value"]


class _Gate:
    """Quote gate with a once-only targeted re-ask per field."""

    def __init__(self, doc: DocText, docs: dict[str, DocText], backend: LLMBackend, report: CompileReport):
        self.doc, self.docs, self.backend, self.report = doc, docs, backend, report

    def _reask(self, key: str, previous: Field[Any], problems: list[str]) -> tuple[Field[Any], FieldCheck] | None:
        o = self.report.fields[key]
        if o.reasked:
            return None
        o.reasked = True
        try:
            res = reask_field(self.doc, key, previous, problems, self.backend)
        except LLMError as exc:
            o.reask_source = f"unavailable ({type(exc).__name__})"
            return None
        self.report.cost_usd += res.cost_usd
        o.reask_source = res.backend
        return res.output, check_field(key, res.output, self.docs)

    def gate(self, key: str, f: Field[Any]) -> Field[Any]:
        """Return the publishable field (verified spans) or a forced abstention."""
        o = self.report.fields.setdefault(key, FieldOutcome(key, FieldStatus.provisional))
        fc = check_field(key, f, self.docs)
        o.check = fc
        if f.abstain_reason is not None:
            o.status = FieldStatus.abstained
            return with_spans(f, fc)  # keep only verified quotes ("hospital discretion, see quote")
        if fc.ok:
            o.status = FieldStatus.provisional
            return _discretion_guard(key, with_spans(f, fc), o)
        o.problems = fc.problems()
        again = self._reask(key, f, o.problems)
        if again is not None:
            f2, fc2 = again
            o.check = fc2
            if f2.abstain_reason is not None:
                o.status = FieldStatus.abstained
                return with_spans(f2, fc2)
            if fc2.ok:
                o.status, o.problems = FieldStatus.provisional, []
                return _discretion_guard(key, with_spans(f2, fc2), o)
            o.problems += ["after re-ask: " + p for p in fc2.problems()]
        o.status, o.forced_abstention, o.candidate = FieldStatus.abstained, True, f
        return _abstained(key, "illegible" if fc.illegible else "quote_unverified")

    def recheck_disagreement(self, key: str, f: Field[Any], checks: list[CrossCheck]) -> Field[Any] | None:
        """Re-ask a published field that Schedule H contradicts; adopt the answer only if it verifies."""
        problems = [f"Schedule H disagrees: {c.note}" for c in checks if c.result is CrossCheckResult.disagree_unexplained]
        again = self._reask(key, f, problems)
        if again is None:
            return None
        f2, fc2 = again
        if f2.abstain_reason is None and fc2.ok and not _same_value(f, f2):
            self.report.fields[key].check = fc2
            return with_spans(f2, fc2)
        return None


def _facilities_for(registry: Registry, pv: PolicyVersion, extra: Iterable[str] = ()) -> list[Facility]:
    ids = {m.facility_id for m in registry.select(PolicyFacilityMap, policy_version_id=pv.id)} | set(extra)
    facs = {f.id: f for f in registry.select(Facility, ein=pv.organization_ein)}
    for fid in ids:
        if fid not in facs and (f := registry.get_facility(fid)) is not None:
            facs[fid] = f
    return sorted(facs.values(), key=lambda f: f.id)


def _load_docs(registry: Registry, pv: PolicyVersion, loader: PdfLoader) -> tuple[DocText, dict[str, DocText], dict[str, DocumentVersion]]:
    fap_sha = pv.doc_shas.get("fap")
    if not fap_sha:
        raise CompileError(f"policy version {pv.id} has no FAP document")
    docs: dict[str, DocText] = {}
    meta: dict[str, DocumentVersion] = {}
    for kind, sha in pv.doc_shas.items():
        dv = registry.get_document_version(sha)
        if dv is None:
            if kind == "fap":
                raise CompileError(f"document version {sha[:12]} is not in the registry")
            continue
        try:
            docs[sha] = DocText.from_pdf(loader(dv), sha)
            meta[sha] = dv
        except (CompileError, ValueError):
            if kind == "fap":
                raise
    return docs[fap_sha], docs, meta


def _document_urls(registry: Registry, pv: PolicyVersion, meta: dict[str, DocumentVersion]) -> tuple[dict[str, str], set[str]]:
    urls: dict[str, str] = {}
    langs: set[str] = set()
    for kind, sha in pv.doc_shas.items():
        dv = meta.get(sha)
        if dv is None:
            continue
        if dv.language:
            langs.add(dv.language)
        src = registry.get(Source, id=dv.source_id) if dv.source_id else None
        base = kind.split("_")[0]
        if src and base in ("fap", "application", "pls") and (base not in urls or kind.endswith("_en")):
            urls[base] = src.url
    return urls, langs


def _existing_tasks(registry: Registry, ids: Iterable[str]) -> dict[str, ReviewTask]:
    out = {}
    for i in ids:
        t = registry.get(ReviewTask, id=i)
        if t is not None:
            out[i] = t
    return out


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def is_compiled(registry: Registry, policy_version_id: str) -> bool:
    recs = registry.fields_for_policy(policy_version_id)
    return bool(recs) and all(r.prompt_version in (COMPILE_VERSION, REASK_VERSION) for r in recs)


def compile_policy_version(
    registry: Registry,
    policy_version_id: str,
    *,
    backend: LLMBackend | None = None,
    pdf_loader: PdfLoader = default_pdf_loader,
    facility_ids: Iterable[str] = (),
    force: bool = False,
    now: datetime | None = None,
) -> CompileReport:
    """Compile, gate, round-trip, cross-check, adjudicate and store one policy version."""
    report = CompileReport(policy_version_id)
    pv = registry.get_policy_version(policy_version_id)
    if pv is None:
        raise CompileError(f"unknown policy version {policy_version_id}")
    report.doc_sha256 = pv.doc_shas.get("fap")
    if not force and is_compiled(registry, pv.id):
        report.cached = True
        for r in registry.fields_for_policy(pv.id):
            report.fields[r.key] = FieldOutcome(r.key, r.status)
            if r.crosschecks:
                report.crosschecks[r.key] = r.crosschecks
        report.mappings = _map_facilities(registry, pv, facility_ids, report)
        return report

    backend = backend or get_backend()
    report.backend = backend.name
    doc, docs, meta = _load_docs(registry, pv, pdf_loader)
    report.routing = plan_routing(doc)
    try:
        res = compile_document(doc, backend)
    except LLMError as exc:
        report.error = f"compile call failed: {type(exc).__name__}: {exc}"
        return report
    report.cost_usd += res.cost_usd
    model_policy = res.output

    # S4: quote gate (+ one targeted re-ask per field) ------------------------------
    gate = _Gate(doc, docs, backend, report)
    final: dict[str, Field[Any]] = {k: gate.gate(k, f) for k, f in model_policy.fields().items()}

    def published() -> set[str]:
        return {k for k, o in report.fields.items() if o.status is FieldStatus.provisional}

    # S6: Schedule H cross-check (filing-period aware) ---------------------------------
    facilities = _facilities_for(registry, pv, facility_ids)
    covered = final["facilities_covered"].value or []
    sec = select_section(registry.schedule_h_for_ein(pv.organization_ein), facilities, covered)
    report.schedule_h_id = sec.id if sec else None
    checks: dict[str, list[CrossCheck]] = {}
    if sec is not None:
        policy = CompiledPolicy.model_validate({k: f.model_dump(mode="json") for k, f in final.items()})
        checks = cross_check(policy, sec, published=published())
        for key in [k for k, cs in checks.items() if any(c.result is CrossCheckResult.disagree_unexplained for c in cs)]:
            better = gate.recheck_disagreement(key, final[key], checks[key])
            if better is not None:
                final[key] = better
        policy = CompiledPolicy.model_validate({k: f.model_dump(mode="json") for k, f in final.items()})
        checks = cross_check(policy, sec, published=published())
        forced = {k for k, o in report.fields.items() if o.forced_abstention and o.candidate is not None}
        if forced:
            cand = CompiledPolicy.model_validate(
                {k: (report.fields[k].candidate if k in forced else f).model_dump(mode="json") for k, f in final.items()}  # type: ignore[union-attr]
            )
            report.candidate_crosschecks = cross_check(cand, sec, published=forced | published())
            for k in forced:
                for c in report.candidate_crosschecks.get(k, []):
                    report.fields[k].problems.append(f"unverified candidate vs Schedule H: {c.result.value} ({c.note})")
        urls, langs = _document_urls(registry, pv, meta)
        eff = final["effective_or_revision_date"].value
        report.document_checks = document_cross_check(sec, urls, langs, eff if isinstance(eff, date) else None)
    report.crosschecks = checks
    policy = CompiledPolicy.model_validate({k: f.model_dump(mode="json") for k, f in final.items()})

    # S5: round trip against the policy's own printed income tables ----------------------
    report.round_trips = round_trip(policy, doc) if "fpg_tiers" in published() else []
    rt_bad = "; ".join(f"p.{r.page}: {r.note}" for r in report.round_trips if r.result == "mismatch") or None

    # S7: advisory adjudication -----------------------------------------------------
    multi = len(covered) > 1 or bool(sec and sec.group_letter)
    for subject in adj.subjects_for(final, checks, multi_facility=multi):
        f = final.get(subject)
        if f is None or not f.evidence:
            f = report.fields[subject].candidate if subject in report.fields else None
        rec = adj.adjudicate(subject, field_value=f, checks=checks.get(subject, []), section=sec, doc=doc, docs=docs, backend=backend)
        report.cost_usd += rec.cost_usd
        report.adjudications.append(rec)
    adj_by_subject = {a.subject: a.summary() for a in report.adjudications}

    # S8: gating, records and review tasks --------------------------------------------
    as_of = pv.as_of
    records: list[FieldRecord] = []
    contexts: list[FieldContext] = []
    for key in CompiledPolicy.field_keys():
        o = report.fields[key]
        rec = FieldRecord(
            id=FieldRecord.make_id(pv.id, key),
            policy_version_id=pv.id,
            key=key,
            field=final[key],
            status=o.status,
            crosschecks=checks.get(key, []),
            as_of=as_of,
            extractor_model_tier="sonnet",
            prompt_version=REASK_VERSION if o.reasked and o.reask_source in ("live", "replay", "record") else COMPILE_VERSION,
        )
        prev = registry.get(FieldRecord, id=rec.id)
        if prev is not None and prev.status in (FieldStatus.verified, FieldStatus.disputed) and _same_value(prev.field, rec.field):
            rec = rec.model_copy(update={"status": prev.status, "reviewer": prev.reviewer, "reviewed_at": prev.reviewed_at})
            o.status = prev.status
        records.append(rec)
        if rec.status in (FieldStatus.verified, FieldStatus.disputed):
            continue
        contexts.append(FieldContext(rec, o.forced_abstention, tuple(o.problems),
                                     rt_bad if key == "fpg_tiers" else None, adj_by_subject.get(key)))
    n_fac = max(1, len(covered), len(facilities))
    tasks = plan_tasks(contexts, n_facilities=n_fac, at=now)
    for c in report.document_checks:
        if c.result is CrossCheckResult.disagree_unexplained:
            subject = f"{pv.id}:{DOCUMENTS_KEY}:{c.external_ref.split('|')[1]}"
            tasks.append(make_task("crosscheck_disagreement", subject, "medium", c.note or "", field_id=f"{pv.id}:{DOCUMENTS_KEY}",
                                   impact_score=impact("crosscheck_disagreement", None, n_fac), at=now))
    existing = _existing_tasks(registry, [t.id for t in tasks])
    tasks = [t for t in tasks if t.id not in existing or existing[t.id].state == "open"]

    with registry.transaction():
        for rec in records:
            registry.put_field(rec)
        for c in report.document_checks:
            registry.put(c.model_copy(update={"field_id": f"{pv.id}:{DOCUMENTS_KEY}"}))
        for t in tasks:
            if t.id in existing:
                t = t.model_copy(update={"created_at": existing[t.id].created_at})
            registry.add_review_task(t)
    report.tasks = tasks
    report.mappings = _map_facilities(registry, pv, facility_ids, report)
    return report


def _map_facilities(registry: Registry, pv: PolicyVersion, facility_ids: Iterable[str], report: CompileReport) -> list[PolicyFacilityMap]:
    """Resolve and store facility -> policy mappings for the facilities this policy may govern."""
    wanted = set(facility_ids)
    facilities = [f for f in _facilities_for(registry, pv, wanted) if not wanted or f.id in wanted]
    versions = [v for v in registry.select(PolicyVersion, organization_ein=pv.organization_ein)] or [pv]
    if pv.id not in {v.id for v in versions}:
        versions.append(pv)
    cands = []
    for v in versions:
        rec = registry.get_field(v.id, "facilities_covered")
        f = typed_field(rec) if rec else None
        ok = bool(rec and rec.status in (FieldStatus.provisional, FieldStatus.verified) and f and f.evidence)
        cands.append(PolicyCandidate(v, f, ok))
    sections: list[ScheduleHFacility] = registry.schedule_h_for_ein(pv.organization_ein)
    out = []
    for fac in facilities:
        existing = registry.select(PolicyFacilityMap, facility_id=fac.id)
        m = resolve_facility(fac, cands, sections, existing)
        if m is not None and m.method != "manual":
            registry.map_policy_facility(m)
        if m is not None and m.policy_version_id != pv.id:
            continue
        if needs_review(m):
            note = (f"{fac.name}: mapped by {m.method} at confidence {m.confidence:.2f}" if m else f"{fac.name}: no governing policy found")
            t = make_task("facility_mapping", fac.id, "medium", note, facility_id=fac.id,
                          impact_score=impact("facility_mapping", None, 1))
            prev = registry.get(ReviewTask, id=t.id)
            if prev is None or prev.state == "open":
                registry.add_review_task(t)
                report.tasks.append(t)
        if m is not None:
            out.append(m)
    return out


def compile_facility(
    registry: Registry,
    facility_id: str,
    *,
    backend: LLMBackend | None = None,
    pdf_loader: PdfLoader = default_pdf_loader,
    force: bool = False,
    now: datetime | None = None,
) -> CompileReport:
    """Compile the policy governing one facility and store its FieldRecords and mapping."""
    fac = registry.get_facility(facility_id)
    if fac is None:
        raise CompileError(f"unknown facility {facility_id}")
    pv = registry.policy_for_facility(facility_id)
    if pv is None and fac.ein:
        current = registry.select(PolicyVersion, organization_ein=fac.ein, status="current")
        pv = max(current, key=lambda v: v.as_of) if current else None
    if pv is None:
        raise CompileError(f"no policy version found for facility {facility_id}")
    return compile_policy_version(registry, pv.id, backend=backend, pdf_loader=pdf_loader,
                                  facility_ids=[facility_id], force=force, now=now)

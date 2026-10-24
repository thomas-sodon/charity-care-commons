"""SQLite-backed registry implementing the section 10 data model.

One table per registry entity. Column names and types are derived from the
Pydantic models in :mod:`charitycare.schemas`, so the schema cannot drift from
the contract: scalars map to INTEGER/REAL/TEXT, dates to ISO-8601 TEXT, and
lists, dicts and nested models to JSON TEXT. ``FieldRecord.crosschecks`` is
stored in its own ``crosscheck`` table and re-attached on read.

Production target is Postgres (managed, JSONB for field values); this module
keeps to portable SQL (no SQLite-only features beyond ``INSERT ... ON CONFLICT``,
which Postgres also supports) so a port is mechanical.

Privacy: there is no household, bill or pay-stub table. ``packet_metric`` is the
only table the packet compiler writes, and its model has no household fields.
"""

from __future__ import annotations

import json
import sqlite3
import types
import typing
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from charitycare.config import get_settings
from charitycare.schemas import (
    ChangeEvent,
    CrossCheck,
    CrossCheckResult,
    DocumentVersion,
    EvalRun,
    Facility,
    FieldRecord,
    FieldStatus,
    FormTemplate,
    HospitalNotice,
    HospitalResponse,
    Organization,
    PacketMetric,
    PolicyFacilityMap,
    PolicyVersion,
    ReviewDecision,
    ReviewTask,
    ScheduleHFacility,
    Source,
)

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class TableSpec:
    name: str
    model: type[BaseModel]
    pk: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    indexes: tuple[tuple[str, ...], ...] = ()


TABLES: dict[type[BaseModel], TableSpec] = {
    spec.model: spec
    for spec in (
        TableSpec("organization", Organization, ("ein",)),
        TableSpec("facility", Facility, ("id",), indexes=(("ein",), ("ccn",))),
        TableSpec("schedule_h_section", ScheduleHFacility, ("id",), indexes=(("ein",),)),
        TableSpec("source", Source, ("id",), indexes=(("url",),)),
        TableSpec("document_version", DocumentVersion, ("sha256",), indexes=(("source_id",),)),
        TableSpec("policy_version", PolicyVersion, ("id",), indexes=(("organization_ein",),)),
        TableSpec("policy_facility_map", PolicyFacilityMap, ("policy_version_id", "facility_id"), indexes=(("facility_id",),)),
        TableSpec("field", FieldRecord, ("id",), exclude=("crosschecks",), indexes=(("policy_version_id",),)),
        TableSpec("crosscheck", CrossCheck, ("field_id", "source", "external_ref"), indexes=(("result",),)),
        TableSpec("review_task", ReviewTask, ("id",), indexes=(("state", "priority"),)),
        TableSpec("review_decision", ReviewDecision, ("task_id", "decided_at"), indexes=(("field_id",),)),
        TableSpec("hospital_response", HospitalResponse, ("id",), indexes=(("facility_id",),)),
        TableSpec("hospital_notice", HospitalNotice, ("id",), indexes=(("field_id",),)),
        TableSpec("change_event", ChangeEvent, ("id",), indexes=(("published_at",),)),
        TableSpec("form_template", FormTemplate, ("doc_sha256",)),
        TableSpec("eval_run", EvalRun, ("id",), indexes=(("suite", "created_at"),)),
        TableSpec("packet_metric", PacketMetric, ("id",)),
    )
}


def _strip_optional(tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return _strip_optional(args[0])
    return tp


def _sql_type(tp: Any) -> str:
    tp = _strip_optional(tp)
    if tp is bool or tp is int:
        return "INTEGER"
    if tp is float:
        return "REAL"
    if tp in (str, date, datetime) or (isinstance(tp, type) and issubclass(tp, Enum)):
        return "TEXT"
    if typing.get_origin(tp) is typing.Literal:
        return "TEXT"
    return "JSON"


def _columns(spec: TableSpec) -> dict[str, str]:
    return {
        name: _sql_type(f.annotation)
        for name, f in spec.model.model_fields.items()
        if name not in spec.exclude
    }


def ddl(spec: TableSpec) -> list[str]:
    """CREATE TABLE / INDEX statements for one table."""
    cols = _columns(spec)
    lines = [f'"{c}" {"TEXT" if t == "JSON" else t}' for c, t in cols.items()]
    lines.append(f"PRIMARY KEY ({', '.join(spec.pk)})")
    stmts = [f"CREATE TABLE IF NOT EXISTS {spec.name} (\n  " + ",\n  ".join(lines) + "\n)"]
    for idx in spec.indexes:
        stmts.append(f"CREATE INDEX IF NOT EXISTS ix_{spec.name}_{'_'.join(idx)} ON {spec.name} ({', '.join(idx)})")
    return stmts


def _to_row(spec: TableSpec, obj: BaseModel) -> dict[str, Any]:
    cols = _columns(spec)
    dumped = obj.model_dump(mode="json")
    row: dict[str, Any] = {}
    for c, t in cols.items():
        v = dumped.get(c)
        if t == "JSON":
            row[c] = None if v is None else json.dumps(v, sort_keys=True, ensure_ascii=False)
        elif isinstance(v, bool):
            row[c] = int(v)
        else:
            row[c] = v
    return row


def _from_row(spec: TableSpec, row: sqlite3.Row, extra: dict[str, Any] | None = None) -> BaseModel:
    cols = _columns(spec)
    data: dict[str, Any] = {}
    fields = spec.model.model_fields
    for c, t in cols.items():
        v = row[c]
        if v is None and not fields[c].is_required():
            continue  # NULL in a column added by migration: let the model default apply
        data[c] = json.loads(v) if (t == "JSON" and v is not None) else v
    if extra:
        data.update(extra)
    return spec.model.model_validate(data)


class Registry:
    """Typed repository over one SQLite file (or ``":memory:"``)."""

    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path is not None else str(get_settings().db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._depth = 0
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # -- lifecycle -----------------------------------------------------------

    def init_schema(self) -> None:
        """Create missing tables and add columns for model fields added since the file was made.

        Migrations are additive only (new nullable columns); a dropped or retyped field
        needs an explicit migration.
        """
        with self.transaction():
            for spec in TABLES.values():
                for stmt in ddl(spec):
                    self.conn.execute(stmt)
                have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({spec.name})")}
                for col, typ in _columns(spec).items():
                    if col not in have:
                        self.conn.execute(f'ALTER TABLE {spec.name} ADD COLUMN "{col}" {"TEXT" if typ == "JSON" else typ}')

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Nestable transaction: only the outermost block issues BEGIN/COMMIT."""
        outer = self._depth == 0
        if outer:
            self.conn.execute("BEGIN")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if outer:
                self.conn.execute("ROLLBACK")
            raise
        self._depth -= 1
        if outer:
            self.conn.execute("COMMIT")

    # -- generic CRUD --------------------------------------------------------

    def put(self, obj: BaseModel) -> None:
        """Insert or replace one row (upsert on the primary key)."""
        if isinstance(obj, FieldRecord):
            self.put_field(obj)
            return
        self._put(TABLES[type(obj)], obj)

    def put_many(self, objs: Iterable[BaseModel]) -> int:
        n = 0
        with self.transaction():
            for o in objs:
                self.put(o)
                n += 1
        return n

    def _put(self, spec: TableSpec, obj: BaseModel) -> None:
        row = _to_row(spec, obj)
        cols = list(row)
        updates = [c for c in cols if c not in spec.pk]
        sql = (
            f"INSERT INTO {spec.name} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT ({', '.join(spec.pk)}) DO "
            + (f"UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in updates)}" if updates else "NOTHING")
        )
        self.conn.execute(sql, [row[c] for c in cols])

    def get(self, model: type[M], **pk: Any) -> M | None:
        spec = TABLES[model]
        if set(pk) != set(spec.pk):
            raise ValueError(f"{spec.name} primary key is {spec.pk}, got {tuple(pk)}")
        rows = self.select(model, **pk)
        return rows[0] if rows else None

    def select(self, model: type[M], order_by: str | None = None, limit: int | None = None, **where: Any) -> list[M]:
        """Rows matching equality filters on columns (``None`` matches NULL)."""
        spec = TABLES[model]
        cols = _columns(spec)
        clauses, params = [], []
        for k, v in where.items():
            if k not in cols:
                raise ValueError(f"unknown column {k!r} on {spec.name}")
            if v is None:
                clauses.append(f'"{k}" IS NULL')
            else:
                clauses.append(f'"{k}" = ?')
                params.append(int(v) if isinstance(v, bool) else (v.value if isinstance(v, Enum) else v))
        sql = f"SELECT * FROM {spec.name}" + (f" WHERE {' AND '.join(clauses)}" if clauses else "")
        if order_by:
            col = order_by.lstrip("-")
            if col not in cols:
                raise ValueError(f"unknown order column {col!r}")
            sql += f' ORDER BY "{col}" {"DESC" if order_by.startswith("-") else "ASC"}'
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        out = []
        for row in self.conn.execute(sql, params).fetchall():
            if model is FieldRecord:
                out.append(self._hydrate_field(row))
            else:
                out.append(_from_row(spec, row))
        return out  # type: ignore[return-value]

    def delete(self, model: type[BaseModel], **pk: Any) -> None:
        spec = TABLES[model]
        with self.transaction():
            if model is FieldRecord:
                self.conn.execute("DELETE FROM crosscheck WHERE field_id = ?", [pk["id"]])
            self.conn.execute(f"DELETE FROM {spec.name} WHERE " + " AND ".join(f"{k} = ?" for k in pk), list(pk.values()))

    def count(self, model: type[BaseModel]) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {TABLES[model].name}").fetchone()[0])

    # -- fields + crosschecks ----------------------------------------------

    def put_field(self, rec: FieldRecord) -> None:
        """Upsert a field and replace its crosschecks."""
        with self.transaction():
            self._put(TABLES[FieldRecord], rec)
            self.conn.execute("DELETE FROM crosscheck WHERE field_id = ?", [rec.id])
            for cc in rec.crosschecks:
                self._put(TABLES[CrossCheck], cc.model_copy(update={"field_id": rec.id}))

    def _hydrate_field(self, row: sqlite3.Row) -> FieldRecord:
        ccs = self.select(CrossCheck, field_id=row["id"])
        return _from_row(TABLES[FieldRecord], row, {"crosschecks": [c.model_dump() for c in ccs]})  # type: ignore[return-value]

    def fields_for_policy(self, policy_version_id: str) -> list[FieldRecord]:
        return self.select(FieldRecord, policy_version_id=policy_version_id, order_by="key")

    def get_field(self, policy_version_id: str, key: str) -> FieldRecord | None:
        return self.get(FieldRecord, id=FieldRecord.make_id(policy_version_id, key))

    def set_field_status(self, field_id: str, status: FieldStatus, reviewer: str | None = None, at: datetime | None = None) -> FieldRecord:
        rec = self.get(FieldRecord, id=field_id)
        if rec is None:
            raise KeyError(field_id)
        rec = rec.model_copy(update={"status": status, "reviewer": reviewer, "reviewed_at": at or datetime.now().astimezone()})
        self.put_field(rec)
        return rec

    def discrepancies(self) -> list[CrossCheck]:
        """All ``disagree_unexplained`` cross-checks (internal; see :meth:`public_discrepancies`)."""
        return self.select(CrossCheck, result=CrossCheckResult.disagree_unexplained)

    def public_discrepancies(self, today: date | None = None) -> list[CrossCheck]:
        """Unexplained cross-checks that may be shown publicly as "discrepancies for review".

        Only when the field is reviewer-verified (or disputed, shown beside the
        response), its policy version is current, and the hospital has had its right
        of reply: a :class:`HospitalNotice` whose ``reply_due`` has passed, or a
        published hospital response on that field.
        """
        return [cc for cc in self.discrepancies() if self.discrepancy_state(cc, today) == "public"]

    def discrepancy_state(self, cc: CrossCheck, today: date | None = None) -> str:
        """``public``, ``awaiting_reply`` (notified, window open), ``unreviewed`` or ``withdrawn``."""
        today = today or date.today()
        rec = self.get(FieldRecord, id=cc.field_id) if cc.field_id else None
        if rec is None:
            return "withdrawn"
        pv = self.get(PolicyVersion, id=rec.policy_version_id)
        if pv is None or pv.status != "current":
            return "withdrawn"
        if rec.status not in (FieldStatus.verified, FieldStatus.disputed):
            return "unreviewed"
        facs = [m.facility_id for m in self.select(PolicyFacilityMap, policy_version_id=rec.policy_version_id)]
        if any(r.published and r.field_key in (None, cc.field_key) for f in facs for r in self.responses_for_facility(f)):
            return "public"
        notices = self.select(HospitalNotice, field_id=rec.id)
        if not notices:
            return "unreviewed"
        return "public" if min(n.reply_due for n in notices) <= today else "awaiting_reply"

    def add_hospital_notice(self, n: HospitalNotice) -> None:
        self.put(n)

    # -- typed convenience ----------------------------------------------------

    def upsert_organization(self, org: Organization) -> None:
        self.put(org)

    def upsert_facility(self, fac: Facility) -> None:
        self.put(fac)

    def get_facility(self, facility_id: str) -> Facility | None:
        return self.get(Facility, id=facility_id)

    def list_facilities(self, state: str | None = "TX", in_scope: bool | None = None) -> list[Facility]:
        where: dict[str, Any] = {}
        if state is not None:
            where["state"] = state
        if in_scope is not None:
            where["in_scope"] = in_scope
        return self.select(Facility, order_by="name", **where)

    def add_schedule_h(self, sec: ScheduleHFacility) -> None:
        self.put(sec)

    def schedule_h_for_ein(self, ein: str) -> list[ScheduleHFacility]:
        return self.select(ScheduleHFacility, ein=ein, order_by="-tax_period_end")

    def upsert_source(self, src: Source) -> None:
        self.put(src)

    def add_document_version(self, doc: DocumentVersion) -> None:
        self.put(doc)

    def get_document_version(self, sha256: str) -> DocumentVersion | None:
        return self.get(DocumentVersion, sha256=sha256)

    def add_policy_version(self, pv: PolicyVersion) -> None:
        """Insert a policy version; marks the version it supersedes as superseded."""
        with self.transaction():
            if pv.supersedes:
                prev = self.get(PolicyVersion, id=pv.supersedes)
                if prev is not None:
                    self._put(TABLES[PolicyVersion], prev.model_copy(update={"status": "superseded"}))
            self._put(TABLES[PolicyVersion], pv)

    def get_policy_version(self, version_id: str) -> PolicyVersion | None:
        return self.get(PolicyVersion, id=version_id)

    def map_policy_facility(self, m: PolicyFacilityMap) -> None:
        self.put(m)

    def policy_for_facility(self, facility_id: str, as_of: date | None = None) -> PolicyVersion | None:
        """Governing policy for a facility: the latest mapped version with ``as_of`` on/before the date
        (or the current one when no date is given). Ties break on mapping confidence."""
        rows = self.conn.execute(
            "SELECT p.*, m.confidence AS _conf FROM policy_version p "
            "JOIN policy_facility_map m ON m.policy_version_id = p.id WHERE m.facility_id = ?",
            [facility_id],
        ).fetchall()
        spec = TABLES[PolicyVersion]
        cands = [(_from_row(spec, r), r["_conf"]) for r in rows]
        if as_of is None:
            cands = [c for c in cands if c[0].status == "current"] or cands  # type: ignore[union-attr]
        else:
            cands = [c for c in cands if c[0].as_of <= as_of]  # type: ignore[union-attr]
        if not cands:
            return None
        cands.sort(key=lambda c: (c[0].as_of, c[1]), reverse=True)  # type: ignore[union-attr]
        return cands[0][0]  # type: ignore[return-value]

    def add_review_task(self, task: ReviewTask) -> None:
        self.put(task)

    def open_review_tasks(self) -> list[ReviewTask]:
        order = {"high": 0, "medium": 1, "low": 2}
        tasks = self.select(ReviewTask, state="open")
        return sorted(tasks, key=lambda t: (order[t.priority], t.created_at))

    def update_review_task(self, task_id: str, state: str, notes: str | None = None) -> ReviewTask:
        task = self.get(ReviewTask, id=task_id)
        if task is None:
            raise KeyError(task_id)
        task = task.model_copy(update={"state": state, "notes": notes if notes is not None else task.notes})
        self.put(ReviewTask.model_validate(task.model_dump()))
        return task

    def add_hospital_response(self, r: HospitalResponse) -> None:
        self.put(r)

    def responses_for_facility(self, facility_id: str) -> list[HospitalResponse]:
        return self.select(HospitalResponse, facility_id=facility_id, order_by="received_at")

    def add_change_event(self, ev: ChangeEvent) -> None:
        self.put(ev)

    def changes_since(self, since: datetime | None = None) -> list[ChangeEvent]:
        evs = [e for e in self.select(ChangeEvent, order_by="published_at") if e.published_at is not None]
        return [e for e in evs if since is None or e.published_at > since]  # type: ignore[operator]

    def upsert_form_template(self, t: FormTemplate) -> None:
        self.put(t)

    def get_form_template(self, doc_sha256: str) -> FormTemplate | None:
        return self.get(FormTemplate, doc_sha256=doc_sha256)

    def add_eval_run(self, run: EvalRun) -> None:
        self.put(run)

    def eval_runs(self, suite: str | None = None) -> list[EvalRun]:
        return self.select(EvalRun, order_by="-created_at", **({"suite": suite} if suite else {}))

    def add_packet_metric(self, m: PacketMetric) -> None:
        self.put(m)


def connect(path: str | Path | None = None) -> Registry:
    """Open (and create if needed) the registry at ``path`` or the configured DB path."""
    return Registry(path)

"""Integrator contracts: store migration, URI resolution, review decisions, CLI wiring."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from charitycare import pdftext
from charitycare.cli import COMMANDS, build_parser, main
from charitycare.config import PROMPT_VERSIONS
from charitycare.schemas import ReviewDecision, ReviewTask
from charitycare.store import Registry


def test_registry_adds_columns_to_older_files(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE review_task ("id" TEXT, "kind" TEXT, "field_id" TEXT, "facility_id" TEXT, "template_id" TEXT, '
                '"priority" TEXT, "state" TEXT, "notes" TEXT, "created_at" TEXT, PRIMARY KEY (id))')
    con.execute("INSERT INTO review_task VALUES ('t1','field_verify',NULL,NULL,NULL,'low','open','[impact 12] x','2026-10-24T00:00:00Z')")
    con.commit()
    con.close()
    with Registry(db) as reg:
        (t,) = reg.open_review_tasks()
        assert t.impact is None and t.batch == []
        from charitycare.api.review import task_impact

        assert task_impact(t) == 12  # legacy notes prefix still honoured


def test_uri_to_path(tmp_path: Path) -> None:
    f = tmp_path / "a b.pdf"
    assert pdftext.uri_to_path(f.as_uri()) == f
    assert pdftext.uri_to_path(str(f)) == f
    assert pdftext.uri_to_path("s3://bucket/x.pdf") is None
    assert pdftext.uri_to_path(None) is None


def test_review_decisions_are_persisted() -> None:
    from charitycare.api.review import apply_decision

    with Registry(":memory:") as reg:
        reg.add_review_task(ReviewTask(id="rt:x", kind="hospital_correction", impact=40))
        apply_decision(reg, ReviewDecision(task_id="rt:x", action="ask_hospital", reviewer="alex"))
        (d,) = reg.select(ReviewDecision)
        assert d.action == "ask_hospital" and d.reviewer == "alex"
        assert reg.get(ReviewTask, id="rt:x").state == "hospital_query"


def test_stage_prompt_versions_registered() -> None:
    from charitycare.discover.agent import AGENT_PROMPT_VERSION
    from charitycare.packet.letters import HARDSHIP_PROMPT_VERSION
    from charitycare.packet.readback import READBACK_PROMPT_VERSION

    assert {AGENT_PROMPT_VERSION, HARDSHIP_PROMPT_VERSION, READBACK_PROMPT_VERSION} <= set(PROMPT_VERSIONS.values())


def test_cli_has_every_stage_and_help_renders(capsys: pytest.CaptureFixture[str]) -> None:
    names = {c[0] for c in COMMANDS}
    assert {"seed", "discover", "compile", "review", "screen", "packet", "export", "site", "serve", "mcp", "eval", "demo"} <= names
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["--help"])
    assert e.value.code == 0 and "%FPL" in capsys.readouterr().out


def test_cli_screen_and_eval_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["screen", "tx-459901", "--size", "3", "--income", "49700", "--statement-date", "2026-10-10"]) == 0
    assert "LIKELY ELIGIBLE FOR FREE CARE" in capsys.readouterr().out
    assert main(["eval", "list"]) == 0
    assert "smoke" in capsys.readouterr().out


def test_cli_review_queue_and_decide(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "r.sqlite"
    with Registry(db) as reg:
        reg.add_review_task(ReviewTask(id="rt:hospital_correction:h1", kind="hospital_correction", impact=30))
    assert main(["review", "--db", str(db), "queue"]) == 0
    assert "rt:hospital_correction:h1" in capsys.readouterr().out
    assert main(["review", "--db", str(db), "decide", "rt:hospital_correction:h1", "--action", "approve", "--reviewer", "sam"]) == 0
    assert main(["review", "--db", str(db), "decide", "rt:hospital_correction:h1", "--action", "approve", "--reviewer", "sam"]) == 2

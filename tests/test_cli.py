from charitycare.cli import main


def test_fpg_command(capsys) -> None:
    assert main(["fpg", "3", "49700", "--year", "2026"]) == 0
    assert "$27,320" in capsys.readouterr().out


def test_db_commands(tmp_path, capsys) -> None:
    db = tmp_path / "r.sqlite"
    assert main(["db", "init", "--path", str(db)]) == 0
    assert main(["db", "stats", "--path", str(db)]) == 0
    assert '"facility": 0' in capsys.readouterr().out


def test_schema_and_demo(tmp_path, capsys) -> None:
    assert main(["schema", "export", "--out", str(tmp_path / "s.json")]) == 0
    assert main(["demo", "build", "--out", str(tmp_path / "d"), "--fixtures", str(tmp_path / "f")]) == 0
    assert "built 8 synthetic hospitals" in capsys.readouterr().out

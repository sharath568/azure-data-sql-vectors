"""CLI behaviour that doesn't need a database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from sqlvec import __version__, db
from sqlvec.cli import cli, parse_key_values


@pytest.fixture
def runner(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> CliRunner:
    clean_env.chdir(tmp_path)  # so no stray .env is picked up
    return CliRunner()


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"], obj={})
    assert result.exit_code == 0 and __version__ in result.output


def test_help_lists_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"], obj={})
    assert result.exit_code == 0
    for cmd in ("init", "index", "ingest", "search", "bench", "cost", "compare-pg", "doctor"):
        assert cmd in result.output


def test_search_modes_in_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["search", "--help"], obj={})
    assert "exact|ann|fts|hybrid" in result.output


def test_cost_default(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["cost"], obj={})
    assert result.exit_code == 0, result.output
    assert "100,000 vCore-seconds" in result.output
    assert "| 15 |" in result.output and "| 60 |" in result.output
    assert "verify" in result.output


def test_cost_json(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["cost", "--sessions", "10", "--autopause-delay", "15", "--json"], obj={}
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["usage"]["autopause_delay_min"] == 15
    assert data["estimate"]["free_allowance"] == 100_000
    assert set(data["by_autopause_delay"]) == {"15", "30", "60"}


def test_cost_rejects_delay_below_minimum(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["cost", "--autopause-delay", "5"], obj={})
    assert result.exit_code != 0


def test_compare_pg_text_and_markdown(runner: CliRunner) -> None:
    text = runner.invoke(cli, ["compare-pg"], obj={})
    assert text.exit_code == 0 and "pgvector   : embedding <=> :qv" in text.output
    md = runner.invoke(cli, ["compare-pg", "--format", "markdown", "--no-notes"], obj={})
    assert md.output.startswith(
        "| operation | SQL Server 2025 / Azure SQL | PostgreSQL + pgvector |\n"
    )
    assert "| notes |" not in md.output


def test_config_masks_password(runner: CliRunner) -> None:
    result = runner.invoke(
        cli, ["--conn", "Server=x;Database=d;UID=u;PWD=hunter2", "config"], obj={}
    )
    assert result.exit_code == 0
    assert "hunter2" not in result.output and "PWD=***" in result.output


def test_env_file_is_loaded(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SQLVEC_EMBED_DIMS=384\n", encoding="utf-8")
    result = runner.invoke(cli, ["config"], obj={})
    assert json.loads(result.output)["embed_dims"] == 384


def test_bad_settings_are_a_clean_error(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["config"], obj={}, env={"SQLVEC_EMBED_DIMS": "5000"})
    assert result.exit_code != 0 and "1998" in result.output and "Traceback" not in result.output


def test_connection_failure_is_a_clean_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("TCP Provider: Error code 0x2749")

    monkeypatch.setattr(db, "connect", boom)
    for args in (["doctor"], ["init"], ["search", "x", "--mode", "exact"], ["bench", "-n", "100"]):
        result = runner.invoke(cli, args, obj={})
        assert result.exit_code == 1, args
        assert "0x2749" in result.output and "Traceback" not in result.output


def test_drop_needs_yes(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["drop"], obj={})
    assert result.exit_code != 0 and "--yes" in result.output


def test_search_hybrid_falls_back_with_notice(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    def respond(sql: str, params: tuple[Any, ...]) -> Any:
        if "sqlvec_search_hybrid', N'P'" in sql:
            return [(None,)]
        if sql.startswith("EXEC dbo.sqlvec_search_exact"):
            return [(1, "notes.md", 0, "Stop the database when idle.", 0.2)]
        return []

    conn = fake_conn(respond)
    monkeypatch.setattr(db, "connect", lambda *a, **k: conn)
    result = runner.invoke(cli, ["--env-file", "none", "search", "stop the db"], obj={})
    assert result.exit_code == 0, result.output
    assert "ran as an exact vector search instead of hybrid" in result.output
    assert "1. [0.8000] notes.md  (cosine distance 0.2000)" in result.output


def test_search_json_output(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    conn = fake_conn(lambda s, p: [(1, "a.md", 0, "text", 0.5)] if "EXEC" in s else [])
    monkeypatch.setattr(db, "connect", lambda *a, **k: conn)
    result = runner.invoke(cli, ["search", "q", "--mode", "exact", "--json", "-k", "1"], obj={})
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["source"] == "a.md"


def test_search_ann_without_index_explains(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, fake_conn: Any
) -> None:
    monkeypatch.setattr(db, "connect", lambda *a, **k: fake_conn())
    result = runner.invoke(cli, ["search", "q", "--mode", "ann"], obj={})
    assert result.exit_code == 1 and "sqlvec index" in result.output


def test_parse_key_values() -> None:
    assert parse_key_values(("a=1", "b = x=y")) == {"a": "1", "b": "x=y"}
    with pytest.raises(click.BadParameter):
        parse_key_values(("novalue",))
    with pytest.raises(click.BadParameter):
        parse_key_values(("=v",))

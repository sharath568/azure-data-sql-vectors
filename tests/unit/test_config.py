from __future__ import annotations

from pathlib import Path

import pytest

from sqlvec.config import (
    DEFAULT_CONN,
    MAX_VECTOR_DIMS,
    ConfigError,
    Settings,
    database_name,
    load_dotenv,
    parse_conn,
    redact_conn,
    validate_dims,
    validate_identifier,
    with_database,
)


def test_defaults_work_without_any_env() -> None:
    s = Settings.from_env({})
    assert s.conn == DEFAULT_CONN
    assert s.embed_provider == "hash"
    assert s.embed_dims == 1536
    assert database_name(s.conn) == "sqlvec"


def test_env_overrides() -> None:
    s = Settings.from_env(
        {
            "SQLVEC_CONN": "Server=x;Database=y",
            "SQLVEC_EMBED_DIMS": "384",
            "SQLVEC_EMBED_PROVIDER": "AZURE",
            "AZURE_OPENAI_ENDPOINT": "https://e/",
            "AZURE_OPENAI_API_KEY": "k",
        }
    )
    assert s.embed_dims == 384 and s.embed_provider == "azure" and s.has_azure_credentials


@pytest.mark.parametrize("dims", [1, 384, 1536, MAX_VECTOR_DIMS])
def test_valid_dims(dims: int) -> None:
    assert validate_dims(dims) == dims


@pytest.mark.parametrize("dims", [0, -1, MAX_VECTOR_DIMS + 1, 3072])
def test_invalid_dims(dims: int) -> None:
    with pytest.raises(ConfigError):
        validate_dims(dims)


def test_dims_must_be_int() -> None:
    with pytest.raises(ConfigError):
        validate_dims(True)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="integer"):
        Settings.from_env({"SQLVEC_EMBED_DIMS": "big"})


def test_bad_provider_and_chunking() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"SQLVEC_EMBED_PROVIDER": "openai"})
    with pytest.raises(ConfigError):
        Settings.from_env({"SQLVEC_CHUNK_SIZE": "50"})
    with pytest.raises(ConfigError):
        Settings.from_env({"SQLVEC_CHUNK_SIZE": "500", "SQLVEC_CHUNK_OVERLAP": "500"})


def test_parse_conn_handles_braces_and_case() -> None:
    parts = parse_conn("Server=tcp:h,1433;DATABASE=db1;PWD={a;b}}c};Encrypt=yes")
    assert parts == {"server": "tcp:h,1433", "database": "db1", "pwd": "a;b}c", "encrypt": "yes"}


def test_parse_conn_unbalanced_brace() -> None:
    with pytest.raises(ConfigError):
        parse_conn("PWD={oops")


def test_database_name_initial_catalog() -> None:
    assert database_name("Server=x;Initial Catalog=cat") == "cat"
    assert database_name("Server=x") is None


def test_with_database_replaces_database() -> None:
    out = with_database("Server=x;Database=app;UID=sa", "master")
    assert parse_conn(out)["database"] == "master"
    assert "app" not in out


@pytest.mark.parametrize("bad", ["", "a-b", "x]; DROP DATABASE y; --", "1abc", "a b", "x" * 129])
def test_identifiers_are_strict(bad: str) -> None:
    with pytest.raises(ConfigError):
        validate_identifier(bad)


def test_redact_conn() -> None:
    red = redact_conn("Server=x;UID=sa;PWD=secret;Password=also")
    assert "secret" not in red and "also" not in red
    assert "PWD=***" in red and "UID=sa" in red
    assert Settings.from_env({"SQLVEC_CONN": "Server=x;PWD=hunter2"}).redacted()["conn"] == (
        "Server=x;PWD=***"
    )


def test_load_dotenv_does_not_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nexport A=1\nB='two words'\nC=keep # trailing\nSQLVEC_CONN=Server=x;PWD=a!b\n",
        encoding="utf-8",
    )
    env = {"A": "already"}
    assert load_dotenv(env_file, env) == 3
    assert env == {"A": "already", "B": "two words", "C": "keep", "SQLVEC_CONN": "Server=x;PWD=a!b"}
    assert load_dotenv(tmp_path / "missing.env", env) == 0

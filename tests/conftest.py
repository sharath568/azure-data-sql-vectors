"""Shared fixtures.

Integration tests run only when ``SQLVEC_TEST_CONN`` points at a SQL Server
2025 / Azure SQL database, for example::

    docker compose up -d
    export SQLVEC_TEST_CONN="$(grep '^# SQLVEC_TEST_CONN=' .env.example | cut -d= -f2-)"
    pytest -m integration

(that's the local docker compose database with ``Database=sqlvec_test``).

They drop and recreate sqlvec's tables in that database (and create the
database if it doesn't exist), so don't point it at anything you care about.

Unit tests use :class:`FakeConnection`, which records every statement and its
parameters, so we can check that user input only ever travels as parameters.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

TEST_CONN = os.environ.get("SQLVEC_TEST_CONN")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if TEST_CONN:
        return
    skip = pytest.mark.skip(reason="set SQLVEC_TEST_CONN to run integration tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every sqlvec / Azure OpenAI variable from the environment."""
    for key in list(os.environ):
        if key.startswith(("SQLVEC_", "AZURE_OPENAI_")):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


Responder = Callable[[str, tuple[Any, ...]], list[tuple[Any, ...]] | Exception | None]


@dataclass
class FakeCursor:
    conn: FakeConnection
    rows: list[tuple[Any, ...]] = field(default_factory=list)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> FakeCursor:
        params = tuple(params)
        self.conn.calls.append((sql, params))
        result = self.conn.responder(sql, params)
        if isinstance(result, Exception):
            raise result
        self.rows = list(result or [])
        return self

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        self.conn.many.append((sql, [tuple(p) for p in seq]))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def close(self) -> None:
        pass


@dataclass(eq=False)  # hashable by identity, like a real connection
class FakeConnection:
    """Minimal DB-API connection. ``responder(sql, params)`` returns rows or an
    exception to raise."""

    responder: Responder = lambda _sql, _params: []
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    many: list[tuple[str, list[tuple[Any, ...]]]] = field(default_factory=list)
    autocommit: bool = True
    commits: int = 0
    rollbacks: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass

    def sql_matching(self, needle: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [c for c in self.calls if needle in c[0]]


@pytest.fixture
def fake_conn() -> Callable[..., FakeConnection]:
    def make(responder: Responder | None = None) -> FakeConnection:
        return FakeConnection(responder=responder or (lambda _s, _p: []))

    return make

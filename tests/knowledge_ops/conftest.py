"""Scoped P1 evidence: default Knowledge Ops tests cannot use network/workspace DB."""

from __future__ import annotations

import socket
import sqlite3
import ssl
from pathlib import Path

import pytest


class ZeroNetworkViolation(RuntimeError):
    pass


class ZeroWorkspaceDatabaseViolation(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _knowledge_ops_zero_network_and_workspace_db(monkeypatch, tmp_path: Path):
    def reject_network(*_args, **_kwargs):
        raise ZeroNetworkViolation("Knowledge Ops P1 tests are offline by default")

    real_sqlite_connect = sqlite3.connect
    allowed_root = tmp_path.resolve(strict=True)

    def guarded_sqlite_connect(database, *args, **kwargs):
        try:
            target = Path(database).resolve()
        except (OSError, TypeError, ValueError) as exc:
            raise ZeroWorkspaceDatabaseViolation(
                "Knowledge Ops tests require a path inside their pytest temp root"
            ) from exc
        if target != allowed_root and allowed_root not in target.parents:
            raise ZeroWorkspaceDatabaseViolation(
                "Knowledge Ops tests cannot connect to a workspace or shared database"
            )
        return real_sqlite_connect(database, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", reject_network)
    monkeypatch.setattr(sqlite3, "connect", guarded_sqlite_connect)
    yield

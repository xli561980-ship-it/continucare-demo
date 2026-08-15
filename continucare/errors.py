"""Stable application errors shared by atomic persistence commands."""

from __future__ import annotations

import sqlite3


class ConcurrentWriteConflict(ValueError):
    """A mutable precondition or SQLite write lock changed before commit."""


def is_sqlite_busy(error: sqlite3.OperationalError) -> bool:
    """Return whether SQLite rejected a write because another writer owns it."""

    code = getattr(error, "sqlite_errorcode", None)
    return code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(
        marker in str(error).lower() for marker in ("locked", "busy")
    )

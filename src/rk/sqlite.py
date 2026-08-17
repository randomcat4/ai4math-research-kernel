"""One durable SQLite connection seam for the product.

Callers still own transactions and busy-timeout policy.  This module owns the
durability floor so a newly added repository cannot silently fall back to
SQLite's weaker default synchronous mode.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal


def open_sqlite(
    database: str | bytes | Path,
    timeout: float = 5.0,
    detect_types: int = 0,
    isolation_level: Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"] | None = "DEFERRED",
    check_same_thread: bool = True,
    cached_statements: int = 128,
    uri: bool = False,
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        database,
        timeout=timeout,
        detect_types=detect_types,
        isolation_level=isolation_level,
        check_same_thread=check_same_thread,
        cached_statements=cached_statements,
        uri=uri,
    )
    try:
        connection.execute("PRAGMA synchronous=FULL")
    except sqlite3.OperationalError:
        # Read-only snapshot/backup connections cannot change the pragma.  They
        # do not commit product state, so the durability floor is irrelevant.
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            connection.close()
            raise
    return connection


__all__ = ["open_sqlite"]

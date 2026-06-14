"""
Base repository class for database query modules.

Provides transaction management so query functions can either:
  - Run standalone (create their own transaction), or
  - Participate in a caller's existing transaction.

Usage::

    from pf_core.db.repository import Repository

    class EntryRepo(Repository):
        def get_by_id(self, entry_id: str) -> dict | None:
            with self._tx() as conn:
                row = conn.execute(
                    text("SELECT * FROM entries WHERE id = :id"),
                    {"id": entry_id},
                ).mappings().fetchone()
                return dict(row) if row else None

    # Standalone — creates its own transaction:
    repo = EntryRepo()
    entry = repo.get_by_id("abc")

    # Inside a caller's transaction:
    with transaction() as conn:
        repo = EntryRepo(conn)
        entry = repo.get_by_id("abc")
        # both queries share the same transaction
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.engine import Connection

from pf_core.db.connection import transaction


class Repository:
    """Base class for database query modules.

    Args:
        conn: Optional existing connection. If provided, ``_tx()`` yields it
              directly (no new transaction). If omitted, ``_tx()`` creates a
              fresh transaction via ``pf_core.db.transaction()``.
    """

    def __init__(self, conn: Connection | None = None) -> None:
        self._conn = conn

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        """Yield a connection inside a transaction.

        If this repository was created with an existing connection, yields that
        connection (caller owns the transaction lifecycle). Otherwise, creates
        a new transaction that commits on clean exit / rolls back on exception.
        """
        if self._conn is not None:
            yield self._conn
        else:
            with transaction() as conn:
                yield conn

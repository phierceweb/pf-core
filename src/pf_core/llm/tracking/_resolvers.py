"""
Thread-safe ID resolvers for the ``llm_*`` reference tables.

Mirrors ``pf_core.db.models.resolve_model_id`` (which targets the legacy
``models`` table) but writes to ``llm_models`` / ``llm_agent_types`` and
caches lookups for the lifetime of the process.

Each resolver runs its own short transaction so the INSERT IGNORE commits
before the caller opens its longer write transaction — the same lock-cycle
mitigation pattern consumer apps use.
"""

from __future__ import annotations

import threading

from sqlalchemy import text
from sqlalchemy.engine import Connection

from pf_core.db.connection import transaction


_model_cache: dict[str, int] = {}
_agent_cache: dict[str, int] = {}
_lock = threading.Lock()


def _insert_ignore_prefix(conn: Connection) -> str:
    if conn.dialect.name == "sqlite":
        return "INSERT OR IGNORE"
    if conn.dialect.name == "postgresql":
        return "INSERT"
    return "INSERT IGNORE"


def _on_conflict_suffix(conn: Connection) -> str:
    return " ON CONFLICT DO NOTHING" if conn.dialect.name == "postgresql" else ""


def resolve_llm_model_id(name: str) -> int:
    """Return the ``llm_models.id`` for ``name``, inserting a new row if needed.

    Raises ``ValueError`` on empty input — model name is required for tracking.
    """
    if not name:
        raise ValueError("model name is required")

    if name in _model_cache:
        return _model_cache[name]

    with _lock:
        if name in _model_cache:
            return _model_cache[name]
        with transaction() as conn:
            prefix = _insert_ignore_prefix(conn)
            suffix = _on_conflict_suffix(conn)
            conn.execute(
                text(f"{prefix} INTO llm_models(name) VALUES (:n){suffix}"),
                {"n": name},
            )
            row = conn.execute(
                text("SELECT id FROM llm_models WHERE name = :n"), {"n": name}
            ).fetchone()
        if row is None:
            raise RuntimeError(f"failed to resolve llm_models id for {name!r}")
        _model_cache[name] = int(row[0])
        return _model_cache[name]


def resolve_agent_type_id(slug: str) -> int:
    """Return the ``llm_agent_types.id`` for ``slug``, inserting if needed."""
    if not slug:
        raise ValueError("agent_type slug is required")

    if slug in _agent_cache:
        return _agent_cache[slug]

    with _lock:
        if slug in _agent_cache:
            return _agent_cache[slug]
        with transaction() as conn:
            prefix = _insert_ignore_prefix(conn)
            suffix = _on_conflict_suffix(conn)
            conn.execute(
                text(f"{prefix} INTO llm_agent_types(slug) VALUES (:s){suffix}"),
                {"s": slug},
            )
            row = conn.execute(
                text("SELECT id FROM llm_agent_types WHERE slug = :s"), {"s": slug}
            ).fetchone()
        if row is None:
            raise RuntimeError(f"failed to resolve llm_agent_types id for {slug!r}")
        _agent_cache[slug] = int(row[0])
        return _agent_cache[slug]


def resolve_prompt_id(
    *,
    agent_type_id: int,
    part: str,
    version: int,
    content: str,
    on_change: str = "keep_first",
) -> int | None:
    """Return ``llm_prompts.id`` for ``(agent_type_id, part, version)``.

    INSERTs a new row on first sight; returns the existing id on
    subsequent calls. The UNIQUE index on
    ``(agent_type_id, part, version)`` makes INSERT-IGNORE idempotent
    so concurrent first-callers race safely.

    Behavior when the row already exists but ``content`` differs from
    the stored text is governed by ``on_change``:

    - ``"keep_first"`` (default) — silently reuse the first-seen content.
      Callers manage versions explicitly; they are expected to bump the
      version number when prompt text changes materially. Best for apps
      that want deliberate cohort boundaries in analytics.

    - ``"update_unused"`` — if no ``llm_runs`` row references this prompt
      yet, update the text in place (safe — nothing has cited it). If
      any run has referenced it, insert a NEW row at ``version+1``.
      Best for apps that auto-manage versions from text edits.

    - ``"error"`` — raise ``ValueError`` if the content differs. Useful
      in CI to catch "edited prompt but forgot to bump version."

    Args:
        agent_type_id: pre-resolved via :func:`resolve_agent_type_id`.
        part: ``"system"`` | ``"user"`` | ``"full"`` (per the CHECK
            constraint on ``llm_prompts.part``).
        version: integer ≥ 1. Caller-provided cohort label.
        content: the prompt text to register. Empty content returns
            ``None`` without any DB write.
        on_change: policy when the row exists with different content.
            See above.

    Returns:
        The resulting ``llm_prompts.id``, or ``None`` when ``content``
        is empty.
    """
    if not content or version is None:
        return None
    if on_change not in ("keep_first", "update_unused", "error"):
        raise ValueError(
            f"on_change must be keep_first|update_unused|error; got {on_change!r}"
        )

    import datetime as _dt

    with transaction() as conn:
        prefix = _insert_ignore_prefix(conn)
        suffix = _on_conflict_suffix(conn)

        # Read any existing row before we insert — need to know whether
        # to mutate (update_unused policy) or raise (error policy).
        row = conn.execute(
            text(
                """
                SELECT id, content
                  FROM llm_prompts
                 WHERE agent_type_id = :aid AND part = :part AND version = :ver
                """
            ),
            {"aid": agent_type_id, "part": part, "ver": int(version)},
        ).fetchone()

        if row is None:
            conn.execute(
                text(
                    f"{prefix} INTO llm_prompts "
                    "(agent_type_id, part, version, content, effective_date) "
                    "VALUES (:aid, :part, :ver, :content, :eff)"
                    f"{suffix}"
                ),
                {
                    "aid": agent_type_id,
                    "part": part,
                    "ver": int(version),
                    "content": content,
                    "eff": _dt.date.today().isoformat(),
                },
            )
            row = conn.execute(
                text(
                    """
                    SELECT id, content
                      FROM llm_prompts
                     WHERE agent_type_id = :aid AND part = :part AND version = :ver
                    """
                ),
                {"aid": agent_type_id, "part": part, "ver": int(version)},
            ).fetchone()
            return int(row[0]) if row else None

        # Row exists. Fast path: content matches.
        existing_id = int(row[0])
        existing_content = row[1] or ""
        if existing_content == content:
            return existing_id

        # Content differs — apply policy.
        if on_change == "keep_first":
            return existing_id

        if on_change == "error":
            raise ValueError(
                f"prompt content changed for agent_type_id={agent_type_id}, "
                f"part={part!r}, version={version} — bump the version when "
                "text changes materially, or pass on_change='update_unused'"
            )

        # on_change == "update_unused"
        used_row = conn.execute(
            text(
                "SELECT 1 FROM llm_runs WHERE system_prompt_id = :pid "
                "OR user_prompt_id = :pid LIMIT 1"
            ),
            {"pid": existing_id},
        ).fetchone()
        if used_row is None:
            # Safe to mutate in place — nothing references this row.
            conn.execute(
                text(
                    "UPDATE llm_prompts SET content = :content "
                    "WHERE id = :pid"
                ),
                {"content": content, "pid": existing_id},
            )
            return existing_id

        # Row is referenced; bump to the next available version.
        next_version_row = conn.execute(
            text(
                """
                SELECT COALESCE(MAX(version), 0) + 1
                  FROM llm_prompts
                 WHERE agent_type_id = :aid AND part = :part
                """
            ),
            {"aid": agent_type_id, "part": part},
        ).fetchone()
        next_version = int(next_version_row[0]) if next_version_row else int(version) + 1
        conn.execute(
            text(
                f"{prefix} INTO llm_prompts "
                "(agent_type_id, part, version, content, effective_date) "
                "VALUES (:aid, :part, :ver, :content, :eff)"
                f"{suffix}"
            ),
            {
                "aid": agent_type_id,
                "part": part,
                "ver": next_version,
                "content": content,
                "eff": _dt.date.today().isoformat(),
            },
        )
        new_row = conn.execute(
            text(
                """
                SELECT id FROM llm_prompts
                 WHERE agent_type_id = :aid AND part = :part AND version = :ver
                """
            ),
            {"aid": agent_type_id, "part": part, "ver": next_version},
        ).fetchone()
        return int(new_row[0]) if new_row else None


def clear_caches() -> None:
    """Drop all cached resolver state. For tests."""
    with _lock:
        _model_cache.clear()
        _agent_cache.clear()

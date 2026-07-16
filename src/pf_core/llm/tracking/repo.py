"""
Main LLM run repository.

``LlmRunRepo.record()`` is the one-call atomic write that inserts a row into
``llm_runs`` plus any of the six sidecar tables it writes (never
``llm_run_outcomes``) in a single transaction.

See ``docs/llm-tracking.md`` for the implementation reference and column
semantics.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select

from pf_core.db.repository import Repository
from pf_core.llm.tracking import schema as s
from pf_core.llm.tracking._resolvers import (
    resolve_agent_type_id,
    resolve_llm_model_id,
)


# Columns that record() unpacks from the ``sampling`` dict.
_SAMPLING_COLS = ("temperature", "top_p", "max_tokens", "seed", "stop_sequences")

# Columns that record() unpacks from the ``usage`` dict.
_USAGE_COLS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cost_usd",
    "duration_ms",
)


def _compute_input_hash(
    *,
    model: str,
    rendered_system: str | None,
    rendered_user: str | None,
    sampling: dict | None,
    configs: dict | None,
) -> str:
    """SHA256 of model + rendered prompts + sampling + configs.

    Stable across calls — dicts ordered by key for determinism. Only the
    ``_SAMPLING_COLS`` keys participate from ``sampling``: the filter lives
    HERE so every producer of this hash (``record()``, the public
    ``compute_input_hash``, the exact cache keyed on it) agrees by
    construction — a dirty sampling dict (``model``, transport kwargs)
    cannot fork the hash.
    """
    payload = {
        "model": model,
        "rendered_system": rendered_system,
        "rendered_user": rendered_user,
        "sampling": dict(
            sorted(
                (k, v) for k, v in (sampling or {}).items() if k in _SAMPLING_COLS
            )
        ),
        "configs": dict(sorted((configs or {}).items())),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_input_hash(
    *,
    model: str,
    messages: list[dict] | None = None,
    rendered_system: str | None = None,
    rendered_user: str | None = None,
    sampling: dict | None = None,
    configs: dict | None = None,
) -> str:
    """Compute the SHA256 input hash for a set of LLM call parameters.

    Accepts either pre-rendered prompts or a ``messages`` list (OpenRouter
    chat format). When ``messages`` is provided, system and user content is
    extracted from it.

    This is the same hash written to ``llm_runs.input_hash`` by
    :class:`LlmRunRepo` and is the primary key for the exact cache.

    Args:
        model: Model slug (e.g. ``"anthropic/claude-opus-4-7"``).
        messages: List of ``{role, content}`` dicts. Mutually exclusive with
            ``rendered_system`` / ``rendered_user``.
        rendered_system: Pre-extracted system prompt text.
        rendered_user: Pre-extracted user prompt text.
        sampling: Sampling kwargs (temperature, top_p, max_tokens, ...).
        configs: Project-config snapshot dict (e.g. ``{"report_config_id": 42}``).

    Returns:
        64-character lowercase hex SHA256 string.
    """
    if messages and rendered_system is None and rendered_user is None:
        from pf_core.llm.tracking.decorator import _extract_rendered_prompts

        rendered_system, rendered_user = _extract_rendered_prompts(messages)

    return _compute_input_hash(
        model=model,
        rendered_system=rendered_system,
        rendered_user=rendered_user,
        sampling=sampling,
        configs=configs,
    )


class LlmRunRepo(Repository):
    """Atomic writes and basic reads for ``llm_runs`` + sidecars."""

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        agent_type: str,
        model: str,
        system_prompt_id: int | None = None,
        user_prompt_id: int | None = None,
        sampling: dict | None = None,
        provider: str | None = None,
        model_fingerprint: str | None = None,
        usage: dict | None = None,
        items_out: int | None = None,
        status: str = "success",
        error: str | None = None,
        error_class: str | None = None,
        error_code: str | None = None,
        http_status: int | None = None,
        input_hash: str | None = None,
        configs: dict[str, int] | None = None,
        validations: list[tuple[str, bool, str, dict | None]] | None = None,
        metrics: dict[str, float] | None = None,
        tags: list[str] | None = None,
        rendered_prompts: tuple[str | None, str | None] | None = None,
        raw_response: str | None = None,
        parsed_output: Any = None,
        parent_run: tuple[int, str] | None = None,
        job_id: int | None = None,
        extra_run_values: dict[str, Any] | None = None,
    ) -> int:
        """Insert one ``llm_runs`` row plus any attached sidecar rows.

        Required: ``agent_type``, ``model``. Everything else is optional —
        the minimum-viable record is just those two plus implicit defaults.

        ``extra_run_values`` is an escape hatch for consumers that have added
        project-specific columns to the ``llm_runs`` table via migration (and
        taught the SQLAlchemy ``llm_runs`` Table about them — see
        ``schema.llm_runs.append_column``). Its keys are merged into the row
        ``INSERT`` after the framework-owned columns, so a subclass need not
        copy this whole method just to write one extra FK column. Each
        key MUST name a column that exists on the ``llm_runs`` Table or the
        ``INSERT`` will fail to compile. Keys that collide with a
        framework-owned column override it (last-write-wins).

        Returns:
            The newly created ``llm_runs.id``.
        """
        # Resolve reference-table FKs in their own short transactions before
        # opening the main write transaction. Avoids InnoDB lock cycles when
        # parallel workers log the same agent_type/model pair simultaneously.
        agent_type_id = resolve_agent_type_id(agent_type)
        model_id = resolve_llm_model_id(model)

        # Attribute to an active Job context if the caller didn't pass one
        # explicitly. Import is local to avoid pulling jobs into cold paths.
        if job_id is None:
            try:
                from pf_core.jobs.runtime import get_current_job_id

                job_id = get_current_job_id()
            except ImportError:  # pragma: no cover
                job_id = None

        rendered_system, rendered_user = (
            rendered_prompts if rendered_prompts is not None else (None, None)
        )

        if input_hash is None:
            input_hash = _compute_input_hash(
                model=model,
                rendered_system=rendered_system,
                rendered_user=rendered_user,
                sampling=sampling,
                configs=configs,
            )

        run_values: dict[str, Any] = {
            "agent_type_id": agent_type_id,
            "model_id": model_id,
            "system_prompt_id": system_prompt_id,
            "user_prompt_id": user_prompt_id,
            "provider": provider,
            "model_fingerprint": model_fingerprint,
            "items_out": items_out,
            "status": status,
            "error": error,
            "error_class": error_class,
            "error_code": error_code,
            "http_status": http_status,
            "input_hash": input_hash,
            "job_id": job_id,
        }
        for col in _SAMPLING_COLS:
            run_values[col] = (sampling or {}).get(col)
        for col in _USAGE_COLS:
            run_values[col] = (usage or {}).get(col)

        if extra_run_values:
            run_values.update(extra_run_values)

        with self._tx() as conn:
            run_id = conn.execute(s.llm_runs.insert().values(**run_values)).inserted_primary_key[0]

            payload_fields = {
                "rendered_system": rendered_system,
                "rendered_user": rendered_user,
                "raw_response": raw_response,
                "parsed_output": parsed_output,
            }
            if any(v is not None for v in payload_fields.values()):
                conn.execute(
                    s.llm_run_payloads.insert().values(llm_run_id=run_id, **payload_fields)
                )

            if configs:
                conn.execute(
                    s.llm_run_configs.insert(),
                    [
                        {"llm_run_id": run_id, "config_kind": k, "config_id": v}
                        for k, v in configs.items()
                    ],
                )

            if validations:
                conn.execute(
                    s.llm_run_validations.insert(),
                    [
                        {
                            "llm_run_id": run_id,
                            "validator": validator,
                            "passed": passed,
                            "severity": severity,
                            "details": details,
                        }
                        for (validator, passed, severity, details) in validations
                    ],
                )

            if metrics:
                conn.execute(
                    s.llm_run_metrics.insert(),
                    [
                        {"llm_run_id": run_id, "metric_name": k, "metric_value": float(v)}
                        for k, v in metrics.items()
                    ],
                )

            if tags:
                conn.execute(
                    s.llm_run_tags.insert(),
                    [{"llm_run_id": run_id, "tag": t} for t in tags],
                )

            if parent_run is not None:
                parent_id, relation = parent_run
                conn.execute(
                    s.llm_run_links.insert().values(
                        parent_run_id=parent_id,
                        child_run_id=run_id,
                        relation=relation,
                    )
                )

        return int(run_id)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, run_id: int) -> dict | None:
        """Return the ``llm_runs`` row as a dict, or ``None`` if not found."""
        with self._tx() as conn:
            row = conn.execute(
                select(s.llm_runs).where(s.llm_runs.c.id == run_id)
            ).mappings().fetchone()
        return dict(row) if row else None

    def get_with_payload(self, run_id: int) -> dict | None:
        """Return the run joined with its payload sidecar.

        Payload columns are namespaced under ``payload`` to avoid collisions.
        """
        with self._tx() as conn:
            run = conn.execute(
                select(s.llm_runs).where(s.llm_runs.c.id == run_id)
            ).mappings().fetchone()
            if run is None:
                return None
            payload = conn.execute(
                select(s.llm_run_payloads).where(
                    s.llm_run_payloads.c.llm_run_id == run_id
                )
            ).mappings().fetchone()
        out = dict(run)
        out["payload"] = dict(payload) if payload else None
        return out

    def find_by_hash(self, input_hash: str) -> list[dict]:
        """Return all runs (most recent first) sharing the given ``input_hash``.

        Used for dedup / forensic "have we sent this exact input before?" queries.
        """
        with self._tx() as conn:
            rows = conn.execute(
                select(s.llm_runs)
                .where(s.llm_runs.c.input_hash == input_hash)
                .order_by(s.llm_runs.c.created_at.desc())
            ).mappings().fetchall()
        return [dict(r) for r in rows]

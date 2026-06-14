"""Tests for pf_core.llm.tracking._resolvers.

Covers:
  - resolve_agent_type_id / resolve_llm_model_id: lookup + insert + cache
  - resolve_prompt_id: first-seen INSERT, cache reuse, and the three
    on_change policies (keep_first, update_unused, error)
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from pf_core.db import transaction
from pf_core.llm.tracking import (
    clear_resolver_caches,
    metadata,
    resolve_agent_type_id,
    resolve_llm_model_id,
    resolve_prompt_id,
)


@pytest.fixture(autouse=True)
def _clear_caches_between_tests():
    clear_resolver_caches()
    yield
    clear_resolver_caches()


@pytest.fixture()
def tracking_db(pf_engine):
    metadata.create_all(pf_engine)
    yield pf_engine
    metadata.drop_all(pf_engine)


# ---------------------------------------------------------------------------
# resolve_agent_type_id / resolve_llm_model_id (existing helpers — smoke)
# ---------------------------------------------------------------------------


def test_resolve_agent_type_id_inserts_and_caches(tracking_db):
    first = resolve_agent_type_id("drafter")
    second = resolve_agent_type_id("drafter")
    assert first == second
    assert isinstance(first, int) and first > 0


def test_resolve_llm_model_id_inserts_and_caches(tracking_db):
    first = resolve_llm_model_id("claude-opus-4-7")
    second = resolve_llm_model_id("claude-opus-4-7")
    assert first == second


# ---------------------------------------------------------------------------
# resolve_prompt_id — first-seen INSERT path
# ---------------------------------------------------------------------------


def test_first_call_inserts_and_returns_id(tracking_db):
    aid = resolve_agent_type_id("drafter")
    pid = resolve_prompt_id(
        agent_type_id=aid, part="system", version=1,
        content="drafter system prompt v1",
    )
    assert isinstance(pid, int) and pid > 0
    with transaction() as conn:
        row = conn.execute(
            text("SELECT content FROM llm_prompts WHERE id = :i"),
            {"i": pid},
        ).fetchone()
    assert row[0] == "drafter system prompt v1"


def test_empty_content_returns_none(tracking_db):
    aid = resolve_agent_type_id("drafter")
    assert resolve_prompt_id(
        agent_type_id=aid, part="system", version=1, content=""
    ) is None


def test_repeat_call_with_same_content_reuses_id(tracking_db):
    aid = resolve_agent_type_id("drafter")
    first = resolve_prompt_id(
        agent_type_id=aid, part="system", version=1, content="same text"
    )
    second = resolve_prompt_id(
        agent_type_id=aid, part="system", version=1, content="same text"
    )
    assert first == second


def test_bumped_version_gets_new_id(tracking_db):
    aid = resolve_agent_type_id("drafter")
    v1 = resolve_prompt_id(agent_type_id=aid, part="system", version=1, content="v1 text")
    v2 = resolve_prompt_id(agent_type_id=aid, part="system", version=2, content="v2 text")
    assert v1 != v2


def test_part_distinguishes_rows(tracking_db):
    """(agent, part, version) is the unique key — system and user at v1 are distinct."""
    aid = resolve_agent_type_id("drafter")
    sys_id = resolve_prompt_id(agent_type_id=aid, part="system", version=1, content="sys")
    usr_id = resolve_prompt_id(agent_type_id=aid, part="user", version=1, content="usr")
    assert sys_id != usr_id


def test_invalid_on_change_raises():
    with pytest.raises(ValueError, match="on_change must be"):
        resolve_prompt_id(
            agent_type_id=1, part="system", version=1, content="x",
            on_change="invalid_policy",
        )


# ---------------------------------------------------------------------------
# on_change = "keep_first" (default) — silently reuse existing text
# ---------------------------------------------------------------------------


class TestOnChangeKeepFirst:
    def test_differing_content_reuses_existing_row(self, tracking_db):
        aid = resolve_agent_type_id("drafter")
        first = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="original",
        )
        second = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="DIFFERENT",
            on_change="keep_first",
        )
        assert first == second
        # First-seen content is preserved.
        with transaction() as conn:
            row = conn.execute(
                text("SELECT content FROM llm_prompts WHERE id = :i"),
                {"i": first},
            ).fetchone()
        assert row[0] == "original"


# ---------------------------------------------------------------------------
# on_change = "error" — raise on mismatch
# ---------------------------------------------------------------------------


class TestOnChangeError:
    def test_matching_content_no_raise(self, tracking_db):
        aid = resolve_agent_type_id("drafter")
        first = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="x",
        )
        # Same text — fine, no raise.
        second = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="x",
            on_change="error",
        )
        assert first == second

    def test_differing_content_raises(self, tracking_db):
        aid = resolve_agent_type_id("drafter")
        resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="original",
        )
        with pytest.raises(ValueError, match="bump the version"):
            resolve_prompt_id(
                agent_type_id=aid, part="system", version=1, content="DIFFERENT",
                on_change="error",
            )


# ---------------------------------------------------------------------------
# on_change = "update_unused" — mutate if not referenced, bump if referenced
# ---------------------------------------------------------------------------


class TestOnChangeUpdateUnused:
    def test_unused_row_updated_in_place(self, tracking_db):
        """No llm_runs row points at the prompt yet — text is safe to mutate."""
        aid = resolve_agent_type_id("drafter")
        first = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="original",
        )
        second = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="UPDATED",
            on_change="update_unused",
        )
        # Same row id, new content.
        assert first == second
        with transaction() as conn:
            row = conn.execute(
                text("SELECT content, version FROM llm_prompts WHERE id = :i"),
                {"i": first},
            ).fetchone()
        assert row[0] == "UPDATED"
        assert row[1] == 1  # version unchanged

    def test_referenced_row_bumps_version(self, tracking_db):
        """An llm_runs row cites the prompt → mutate-in-place would break audit →
        insert a new row at the next version."""
        aid = resolve_agent_type_id("drafter")
        from pf_core.llm.tracking import resolve_llm_model_id
        mid = resolve_llm_model_id("test-model")
        first = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="original",
        )
        # Create an llm_runs row that references the prompt.
        with transaction() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_runs "
                    "(agent_type_id, model_id, system_prompt_id, status) "
                    "VALUES (:a, :m, :p, 'success')"
                ),
                {"a": aid, "m": mid, "p": first},
            )
        # Now change the content — must bump, not mutate.
        second = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="REVISED",
            on_change="update_unused",
        )
        assert second != first
        with transaction() as conn:
            old = conn.execute(
                text("SELECT content, version FROM llm_prompts WHERE id = :i"),
                {"i": first},
            ).fetchone()
            new = conn.execute(
                text("SELECT content, version FROM llm_prompts WHERE id = :i"),
                {"i": second},
            ).fetchone()
        assert old[0] == "original"
        assert old[1] == 1
        assert new[0] == "REVISED"
        assert new[1] == 2  # MAX(version)+1 for this agent+part

    def test_matching_content_still_reuses(self, tracking_db):
        aid = resolve_agent_type_id("drafter")
        first = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="same",
            on_change="update_unused",
        )
        second = resolve_prompt_id(
            agent_type_id=aid, part="system", version=1, content="same",
            on_change="update_unused",
        )
        assert first == second

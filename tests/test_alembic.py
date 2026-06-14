"""Tests for pf_core.alembic — Alembic migration helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pf_core.alembic import run_migrations_online


class TestRunMigrationsOnline:
    @patch("pf_core.alembic.context")
    @patch("pf_core.alembic.get_engine")
    def test_raises_in_offline_mode(self, mock_engine, mock_context):
        mock_context.is_offline_mode.return_value = True
        with pytest.raises(RuntimeError, match="Offline mode is not supported"):
            run_migrations_online()

    @patch("pf_core.alembic.context")
    @patch("pf_core.alembic.get_engine")
    @patch("pf_core.alembic.db_url")
    @patch("pf_core.alembic.is_sqlite")
    def test_online_mode_configures_context(
        self, mock_is_sqlite, mock_db_url, mock_get_engine, mock_context
    ):
        mock_context.is_offline_mode.return_value = False
        mock_db_url.return_value = "sqlite:///test.db"
        mock_is_sqlite.return_value = True

        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_engine.return_value = mock_engine

        mock_context.begin_transaction.return_value.__enter__ = MagicMock()
        mock_context.begin_transaction.return_value.__exit__ = MagicMock(return_value=False)

        run_migrations_online()

        mock_context.configure.assert_called_once_with(
            connection=mock_conn,
            target_metadata=None,
            compare_type=False,
            render_as_batch=True,
        )
        mock_context.run_migrations.assert_called_once()

    @patch("pf_core.alembic.context")
    @patch("pf_core.alembic.get_engine")
    @patch("pf_core.alembic.db_url")
    @patch("pf_core.alembic.is_sqlite")
    def test_mysql_mode_no_batch(
        self, mock_is_sqlite, mock_db_url, mock_get_engine, mock_context
    ):
        mock_context.is_offline_mode.return_value = False
        mock_db_url.return_value = "mysql://localhost/db"
        mock_is_sqlite.return_value = False

        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_engine.return_value = mock_engine

        mock_context.begin_transaction.return_value.__enter__ = MagicMock()
        mock_context.begin_transaction.return_value.__exit__ = MagicMock(return_value=False)

        run_migrations_online()

        mock_context.configure.assert_called_once_with(
            connection=mock_conn,
            target_metadata=None,
            compare_type=False,
            render_as_batch=False,
        )

    @patch("pf_core.alembic.context")
    @patch("pf_core.alembic.get_engine")
    @patch("pf_core.alembic.db_url")
    @patch("pf_core.alembic.is_sqlite")
    def test_passes_target_metadata(
        self, mock_is_sqlite, mock_db_url, mock_get_engine, mock_context
    ):
        mock_context.is_offline_mode.return_value = False
        mock_db_url.return_value = "sqlite:///test.db"
        mock_is_sqlite.return_value = True

        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_engine.return_value = mock_engine

        mock_context.begin_transaction.return_value.__enter__ = MagicMock()
        mock_context.begin_transaction.return_value.__exit__ = MagicMock(return_value=False)

        fake_metadata = MagicMock()
        run_migrations_online(target_metadata=fake_metadata, compare_type=True)

        call_kwargs = mock_context.configure.call_args[1]
        assert call_kwargs["target_metadata"] is fake_metadata
        assert call_kwargs["compare_type"] is True

    @patch("pf_core.alembic.context")
    @patch("pf_core.alembic.get_engine")
    @patch("pf_core.alembic.db_url")
    @patch("pf_core.alembic.is_sqlite")
    def test_fallback_sqlite(
        self, mock_is_sqlite, mock_db_url, mock_get_engine, mock_context
    ):
        mock_context.is_offline_mode.return_value = False
        mock_db_url.return_value = "sqlite:///fallback.db"
        mock_is_sqlite.return_value = True

        mock_conn = MagicMock()
        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_engine.return_value = mock_engine

        mock_context.begin_transaction.return_value.__enter__ = MagicMock()
        mock_context.begin_transaction.return_value.__exit__ = MagicMock(return_value=False)

        run_migrations_online(fallback_sqlite="fallback.db")

        mock_db_url.assert_called_once_with(fallback_sqlite="fallback.db")

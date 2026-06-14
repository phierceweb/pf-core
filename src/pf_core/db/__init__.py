"""Database layer: SQLAlchemy engine, transaction manager, helpers, repository base."""

from pf_core.db.connection import (
    DatabaseUnavailableError,
    db_url,
    dialect_of,
    get_engine,
    is_postgres,
    is_sqlite,
    ping,
    transaction,
)
from pf_core.db.helpers import coerce_json_col, dumps_json, now_iso, row_to_dict
from pf_core.db.json_compat import (
    SUPPORTED_DIALECTS,
    autoinc_pk,
    bool_type,
    decimal_type,
    fk_int_type,
    insert_ignore_prefix,
    json_col_type,
    json_extract_sql,
    mediumtext_type,
    now_expr,
    on_update_now_clause,
    small_autoinc_pk,
    timestamp_type,
    tiny_autoinc_pk,
)
from pf_core.db.models import resolve_model_id
from pf_core.db.repository import Repository
from pf_core.db.soft_delete import not_deleted, restore, soft_delete
from pf_core.db.upsert import insert_ignore, upsert
from pf_core.db.versioned_config import (
    append_version,
    get_latest,
    get_latest_with_fallback,
    latest_version,
)

__all__ = [
    "SUPPORTED_DIALECTS",
    "DatabaseUnavailableError",
    "Repository",
    "append_version",
    "autoinc_pk",
    "bool_type",
    "coerce_json_col",
    "db_url",
    "decimal_type",
    "dialect_of",
    "dumps_json",
    "fk_int_type",
    "get_engine",
    "get_latest",
    "get_latest_with_fallback",
    "insert_ignore",
    "insert_ignore_prefix",
    "is_postgres",
    "is_sqlite",
    "json_col_type",
    "json_extract_sql",
    "latest_version",
    "mediumtext_type",
    "not_deleted",
    "now_expr",
    "now_iso",
    "on_update_now_clause",
    "ping",
    "resolve_model_id",
    "restore",
    "row_to_dict",
    "small_autoinc_pk",
    "soft_delete",
    "timestamp_type",
    "tiny_autoinc_pk",
    "transaction",
    "upsert",
]

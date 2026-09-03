"""Canonical logical database identity shared by seeds and benchmark probes."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

BUSINESS_TABLES = (
    "orders",
    "shipments",
    "carrier_event_inbox",
    "tracking_events",
    "notifications",
)

STRUCTURAL_SCHEMA_CONTRACT_VERSION = 1
STRUCTURAL_SCHEMA_TABLES = (
    "alembic_version",
    "carriers",
    *BUSINESS_TABLES,
)
STRUCTURAL_SCHEMA_SECTIONS = ("tables", "columns", "constraints", "indexes")

STRUCTURAL_SCHEMA_QUERIES: dict[str, str] = {
    "tables": """
        SELECT jsonb_build_object(
            'schema', namespace.nspname,
            'name', relation.relname,
            'kind', CASE relation.relkind
                WHEN 'r' THEN 'table'
                WHEN 'p' THEN 'partitioned_table'
                ELSE relation.relkind::text
            END,
            'row_security', relation.relrowsecurity,
            'force_row_security', relation.relforcerowsecurity
        )::text
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p')
        ORDER BY namespace.nspname, relation.relname
    """,
    "columns": """
        WITH configured AS MATERIALIZED (
            SELECT pg_catalog.set_config('search_path', 'pg_catalog, public', true)
        )
        SELECT jsonb_build_object(
            'schema', namespace.nspname,
            'table', relation.relname,
            'ordinal', attribute.attnum,
            'name', attribute.attname,
            'type', pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
            'nullable', NOT attribute.attnotnull,
            'default', CASE
                WHEN default_value.oid IS NULL THEN NULL
                ELSE pg_catalog.pg_get_expr(default_value.adbin, default_value.adrelid, true)
            END,
            'identity', CASE attribute.attidentity
                WHEN '' THEN NULL
                WHEN 'a' THEN 'always'
                WHEN 'd' THEN 'by_default'
                ELSE attribute.attidentity::text
            END,
            'generated', CASE attribute.attgenerated
                WHEN '' THEN NULL
                WHEN 's' THEN 'stored'
                WHEN 'v' THEN 'virtual'
                ELSE attribute.attgenerated::text
            END
        )::text
        FROM pg_catalog.pg_attribute AS attribute
        JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef AS default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        CROSS JOIN configured
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY namespace.nspname, relation.relname, attribute.attnum
    """,
    "constraints": """
        WITH configured AS MATERIALIZED (
            SELECT pg_catalog.set_config('search_path', 'pg_catalog, public', true)
        )
        SELECT jsonb_build_object(
            'schema', namespace.nspname,
            'table', relation.relname,
            'name', constraint_value.conname,
            'type', CASE constraint_value.contype
                WHEN 'c' THEN 'check'
                WHEN 'f' THEN 'foreign_key'
                WHEN 'n' THEN 'not_null'
                WHEN 'p' THEN 'primary_key'
                WHEN 'u' THEN 'unique'
                WHEN 'x' THEN 'exclusion'
                ELSE constraint_value.contype::text
            END,
            'local_columns', COALESCE((
                SELECT jsonb_agg(local_attribute.attname ORDER BY local_key.ordinality)
                FROM unnest(constraint_value.conkey)
                    WITH ORDINALITY AS local_key(attnum, ordinality)
                JOIN pg_catalog.pg_attribute AS local_attribute
                  ON local_attribute.attrelid = constraint_value.conrelid
                 AND local_attribute.attnum = local_key.attnum
            ), '[]'::jsonb),
            'referenced_schema', referenced_namespace.nspname,
            'referenced_table', referenced_relation.relname,
            'referenced_columns', COALESCE((
                SELECT jsonb_agg(referenced_attribute.attname ORDER BY referenced_key.ordinality)
                FROM unnest(constraint_value.confkey)
                    WITH ORDINALITY AS referenced_key(attnum, ordinality)
                JOIN pg_catalog.pg_attribute AS referenced_attribute
                  ON referenced_attribute.attrelid = constraint_value.confrelid
                 AND referenced_attribute.attnum = referenced_key.attnum
            ), '[]'::jsonb),
            'match_type', CASE WHEN constraint_value.contype = 'f' THEN
                CASE constraint_value.confmatchtype
                    WHEN 'f' THEN 'full'
                    WHEN 'p' THEN 'partial'
                    WHEN 's' THEN 'simple'
                    ELSE constraint_value.confmatchtype::text
                END
            ELSE NULL END,
            'on_update', CASE WHEN constraint_value.contype = 'f' THEN
                CASE constraint_value.confupdtype
                    WHEN 'a' THEN 'no_action'
                    WHEN 'r' THEN 'restrict'
                    WHEN 'c' THEN 'cascade'
                    WHEN 'n' THEN 'set_null'
                    WHEN 'd' THEN 'set_default'
                    ELSE constraint_value.confupdtype::text
                END
            ELSE NULL END,
            'on_delete', CASE WHEN constraint_value.contype = 'f' THEN
                CASE constraint_value.confdeltype
                    WHEN 'a' THEN 'no_action'
                    WHEN 'r' THEN 'restrict'
                    WHEN 'c' THEN 'cascade'
                    WHEN 'n' THEN 'set_null'
                    WHEN 'd' THEN 'set_default'
                    ELSE constraint_value.confdeltype::text
                END
            ELSE NULL END,
            'deferrable', constraint_value.condeferrable,
            'initially_deferred', constraint_value.condeferred,
            'validated', constraint_value.convalidated,
            'no_inherit', constraint_value.connoinherit,
            'expression', CASE WHEN constraint_value.conbin IS NULL THEN NULL
                ELSE pg_catalog.pg_get_expr(
                    constraint_value.conbin,
                    constraint_value.conrelid,
                    true
                )
            END,
            'definition', pg_catalog.pg_get_constraintdef(constraint_value.oid, true)
        )::text
        FROM pg_catalog.pg_constraint AS constraint_value
        JOIN pg_catalog.pg_class AS relation ON relation.oid = constraint_value.conrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        LEFT JOIN pg_catalog.pg_class AS referenced_relation
          ON referenced_relation.oid = constraint_value.confrelid
        LEFT JOIN pg_catalog.pg_namespace AS referenced_namespace
          ON referenced_namespace.oid = referenced_relation.relnamespace
        CROSS JOIN configured
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p')
        ORDER BY namespace.nspname, relation.relname, constraint_value.conname
    """,
    "indexes": """
        WITH configured AS MATERIALIZED (
            SELECT pg_catalog.set_config('search_path', 'pg_catalog, public', true)
        )
        SELECT jsonb_build_object(
            'schema', namespace.nspname,
            'table', relation.relname,
            'name', index_relation.relname,
            'method', access_method.amname,
            'unique', index_value.indisunique,
            'primary', index_value.indisprimary,
            'valid', index_value.indisvalid,
            'ready', index_value.indisready,
            'live', index_value.indislive,
            'clustered', index_value.indisclustered,
            'replica_identity', index_value.indisreplident,
            'key_columns', COALESCE((
                SELECT jsonb_agg(
                    pg_catalog.pg_get_indexdef(index_value.indexrelid, position, true)
                    ORDER BY position
                )
                FROM generate_series(1, index_value.indnkeyatts) AS position
            ), '[]'::jsonb),
            'included_columns', COALESCE((
                SELECT jsonb_agg(
                    pg_catalog.pg_get_indexdef(index_value.indexrelid, position, true)
                    ORDER BY position
                )
                FROM generate_series(
                    index_value.indnkeyatts + 1,
                    index_value.indnatts
                ) AS position
            ), '[]'::jsonb),
            'expressions', CASE WHEN index_value.indexprs IS NULL THEN NULL
                ELSE pg_catalog.pg_get_expr(index_value.indexprs, index_value.indrelid, true)
            END,
            'predicate', CASE WHEN index_value.indpred IS NULL THEN NULL
                ELSE pg_catalog.pg_get_expr(index_value.indpred, index_value.indrelid, true)
            END,
            'definition', pg_catalog.pg_get_indexdef(index_value.indexrelid, 0, true)
        )::text
        FROM pg_catalog.pg_index AS index_value
        JOIN pg_catalog.pg_class AS relation ON relation.oid = index_value.indrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_class AS index_relation
          ON index_relation.oid = index_value.indexrelid
        JOIN pg_catalog.pg_am AS access_method ON access_method.oid = index_relation.relam
        CROSS JOIN configured
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p')
        ORDER BY namespace.nspname, relation.relname, index_relation.relname
    """,
}

_DATETIME_COLUMNS = frozenset(
    {
        "created_at",
        "updated_at",
        "status_occurred_at",
        "status_event_received_at",
        "shipped_at",
        "delivered_at",
        "received_at",
        "processed_at",
        "occurred_at",
        "simulated_at",
    }
)
_DATE_COLUMNS = frozenset({"estimated_delivery_date"})
_BYTE_COLUMNS = frozenset({"raw_body"})
_ARTIFACT_ONLY_COLUMNS = {"shipments": frozenset({"carrier_code"})}


class DatabaseIdentityError(ValueError):
    """Sanitized logical mismatch without row contents or connection information."""

    def __init__(self, table: str, mismatch_class: str) -> None:
        self.table = table
        self.mismatch_class = mismatch_class
        super().__init__(f"{table} {mismatch_class} divergence")


@dataclass(frozen=True, slots=True)
class DatabaseDigests:
    """Content-addressed identity safe to retain in benchmark metadata."""

    global_sha256: str
    table_sha256: dict[str, str]


@dataclass(frozen=True, slots=True)
class StructuralSchemaIdentity:
    """Release-specific digest of stable PostgreSQL structural properties."""

    contract_version: int
    sha256: str


_STRUCTURAL_KEYS = {
    "tables": frozenset({"schema", "name", "kind", "row_security", "force_row_security"}),
    "columns": frozenset(
        {
            "schema",
            "table",
            "ordinal",
            "name",
            "type",
            "nullable",
            "default",
            "identity",
            "generated",
        }
    ),
    "constraints": frozenset(
        {
            "schema",
            "table",
            "name",
            "type",
            "local_columns",
            "referenced_schema",
            "referenced_table",
            "referenced_columns",
            "match_type",
            "on_update",
            "on_delete",
            "deferrable",
            "initially_deferred",
            "validated",
            "no_inherit",
            "expression",
            "definition",
        }
    ),
    "indexes": frozenset(
        {
            "schema",
            "table",
            "name",
            "method",
            "unique",
            "primary",
            "valid",
            "ready",
            "live",
            "clustered",
            "replica_identity",
            "key_columns",
            "included_columns",
            "expressions",
            "predicate",
            "definition",
        }
    ),
}

_STRUCTURAL_SORT_KEYS = {
    "tables": ("schema", "name"),
    "columns": ("schema", "table", "ordinal", "name"),
    "constraints": ("schema", "table", "name", "type"),
    "indexes": ("schema", "table", "name"),
}


def canonical_structural_schema_document(
    sections: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    """Canonicalize the stable release-specific PostgreSQL schema identity."""
    if set(sections) != set(STRUCTURAL_SCHEMA_SECTIONS):
        raise DatabaseIdentityError("schema", "section-set")
    document: dict[str, object] = {"contract_version": STRUCTURAL_SCHEMA_CONTRACT_VERSION}
    canonical_sections: dict[str, list[dict[str, object]]] = {}
    for section in STRUCTURAL_SCHEMA_SECTIONS:
        rows: list[dict[str, object]] = []
        for raw_row in sections[section]:
            row = {str(key): _canonical_structural_value(value) for key, value in raw_row.items()}
            if set(row) != set(_STRUCTURAL_KEYS[section]):
                raise DatabaseIdentityError("schema", f"{section}-fields")
            rows.append(row)
        sort_keys = _STRUCTURAL_SORT_KEYS[section]
        rows.sort(key=lambda row: tuple(_structural_sort_value(row[key]) for key in sort_keys))
        identities = [tuple(_structural_sort_value(row[key]) for key in sort_keys) for row in rows]
        if len(set(identities)) != len(identities):
            raise DatabaseIdentityError("schema", f"{section}-identity")
        canonical_sections[section] = rows
        document[section] = rows

    expected_tables = {("public", table) for table in STRUCTURAL_SCHEMA_TABLES}
    observed_tables = {
        (str(row["schema"]), str(row["name"])) for row in canonical_sections["tables"]
    }
    if observed_tables != expected_tables:
        raise DatabaseIdentityError("schema", "table-set")
    for section in ("columns", "constraints", "indexes"):
        referenced_tables = {
            (str(row["schema"]), str(row["table"])) for row in canonical_sections[section]
        }
        if not referenced_tables <= expected_tables:
            raise DatabaseIdentityError("schema", f"{section}-table")
    return document


def canonical_structural_schema_bytes(
    sections: Mapping[str, Sequence[Mapping[str, object]]],
) -> bytes:
    """Return deterministic bytes excluding volatile PostgreSQL physical properties."""
    return _canonical_json_bytes(canonical_structural_schema_document(sections))


def structural_schema_identity(
    sections: Mapping[str, Sequence[Mapping[str, object]]],
) -> StructuralSchemaIdentity:
    """Hash tables, columns, constraints, and indexes without environment-specific state."""
    return StructuralSchemaIdentity(
        contract_version=STRUCTURAL_SCHEMA_CONTRACT_VERSION,
        sha256=hashlib.sha256(canonical_structural_schema_bytes(sections)).hexdigest(),
    )


def artifact_business_rows(document: Mapping[str, object]) -> dict[str, list[dict[str, object]]]:
    """Project the integral artifact tables to the physical business-table contract."""
    tables = document.get("tables")
    if not isinstance(tables, Mapping):
        raise DatabaseIdentityError("dataset", "schema")
    projected: dict[str, list[dict[str, object]]] = {}
    for table in BUSINESS_TABLES:
        source = tables.get(table)
        if not isinstance(source, list) or not all(isinstance(row, Mapping) for row in source):
            raise DatabaseIdentityError(table, "schema")
        excluded = _ARTIFACT_ONLY_COLUMNS.get(table, frozenset())
        rows: list[dict[str, object]] = []
        for item in source:
            row = {str(key): value for key, value in item.items() if key not in excluded}
            if table == "carrier_event_inbox":
                encoded = row.get("raw_body")
                if not isinstance(encoded, Mapping) or not isinstance(encoded.get("base64"), str):
                    raise DatabaseIdentityError(table, "bytea")
                try:
                    row["raw_body"] = base64.b64decode(encoded["base64"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise DatabaseIdentityError(table, "bytea") from exc
            rows.append(row)
        projected[table] = rows
    return projected


def official_carrier_rows() -> list[dict[str, object]]:
    """Return the complete frozen reference rows installed by the canonical migration."""
    instant = datetime(2026, 8, 28, tzinfo=UTC)
    return [
        {
            "id": UUID("00000000-0000-4000-8000-000000000100"),
            "code": "carrier-alpha",
            "name": "Carrier Alpha",
            "adapter_key": "alpha",
            "active": True,
            "created_at": instant,
            "updated_at": instant,
        },
        {
            "id": UUID("00000000-0000-4000-8000-000000000101"),
            "code": "carrier-beta",
            "name": "Carrier Beta",
            "adapter_key": "beta",
            "active": True,
            "created_at": instant,
            "updated_at": instant,
        },
    ]


def database_digests(
    rows_by_table: Mapping[str, Sequence[Mapping[str, object]]],
) -> DatabaseDigests:
    """Hash every canonical row per table and as one global logical document."""
    document = canonical_database_document(rows_by_table)
    table_sha256 = {
        table: hashlib.sha256(_canonical_json_bytes(document[table])).hexdigest()
        for table in sorted(document)
    }
    return DatabaseDigests(
        hashlib.sha256(_canonical_json_bytes(document)).hexdigest(),
        table_sha256,
    )


def canonical_database_document(
    rows_by_table: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    """Canonicalize typed SQLAlchemy rows or JSON rows emitted by PostgreSQL."""
    document: dict[str, list[dict[str, object]]] = {}
    for table in sorted(rows_by_table):
        rows = rows_by_table[table]
        canonical = [
            {str(key): _canonical_database_value(str(key), value) for key, value in row.items()}
            for row in rows
        ]
        try:
            canonical.sort(key=lambda row: str(row["id"]))
        except KeyError as exc:
            raise DatabaseIdentityError(table, "identity") from exc
        identifiers = [str(row["id"]) for row in canonical]
        if len(set(identifiers)) != len(identifiers):
            raise DatabaseIdentityError(table, "identity")
        document[table] = canonical
    return document


def compare_database_content(
    expected: Mapping[str, Sequence[Mapping[str, object]]],
    observed: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    allow_additional_rows: bool = False,
    ignored_columns_by_row: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
) -> DatabaseDigests:
    """Compare all columns while optionally ignoring new rows created after the seed."""
    expected_tables = set(expected)
    if set(observed) != expected_tables:
        raise DatabaseIdentityError("database", "table-set")
    expected_document = canonical_database_document(expected)
    observed_document = canonical_database_document(observed)
    compared: dict[str, list[dict[str, object]]] = {}
    for table in sorted(expected_document):
        expected_rows = expected_document[table]
        actual_rows = observed_document[table]
        if allow_additional_rows:
            expected_ids = {str(row["id"]) for row in expected_rows}
            actual_rows = [row for row in actual_rows if str(row["id"]) in expected_ids]
        if len(actual_rows) != len(expected_rows):
            raise DatabaseIdentityError(table, "cardinality")
        ignored = (ignored_columns_by_row or {}).get(table, {})
        expected_rows = [_without_ignored_columns(row, ignored) for row in expected_rows]
        actual_rows = [_without_ignored_columns(row, ignored) for row in actual_rows]
        expected_digest = hashlib.sha256(_canonical_json_bytes(expected_rows)).hexdigest()
        observed_digest = hashlib.sha256(_canonical_json_bytes(actual_rows)).hexdigest()
        if expected_digest != observed_digest:
            raise DatabaseIdentityError(table, "content")
        compared[table] = actual_rows
    return database_digests(compared)


def _without_ignored_columns(
    row: dict[str, object],
    ignored_by_id: Mapping[str, frozenset[str]],
) -> dict[str, object]:
    ignored = ignored_by_id.get(str(row["id"]), frozenset())
    return {key: value for key, value in row.items() if key not in ignored}


def _canonical_database_value(column: str, value: Any) -> object:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise DatabaseIdentityError("database", "timestamp")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if column in _BYTE_COLUMNS and isinstance(value, str):
        if not value.startswith("\\x"):
            raise DatabaseIdentityError("carrier_event_inbox", "bytea")
        try:
            return {"hex": bytes.fromhex(value[2:]).hex()}
        except ValueError as exc:
            raise DatabaseIdentityError("carrier_event_inbox", "bytea") from exc
    if column in _DATETIME_COLUMNS and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DatabaseIdentityError("database", "timestamp") from exc
        if parsed.tzinfo is None:
            raise DatabaseIdentityError("database", "timestamp")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if column in _DATE_COLUMNS and isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise DatabaseIdentityError("database", "date") from exc
    if isinstance(value, Mapping):
        return {str(key): _canonical_database_value(str(key), item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_database_value(column, item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise DatabaseIdentityError("database", "number")
    if isinstance(value, (str, bool, int, float)):
        return value
    raise DatabaseIdentityError("database", "type")


def _canonical_structural_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_structural_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_structural_value(item) for item in value]
    raise DatabaseIdentityError("schema", "value-type")


def _structural_sort_value(value: object) -> str | int:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return value
    raise DatabaseIdentityError("schema", "sort-key")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

"""Pure release-specific PostgreSQL structural identity tests."""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest
from benchmarks.database_contract import (
    STRUCTURAL_SCHEMA_TABLES,
    DatabaseIdentityError,
    canonical_structural_schema_bytes,
    structural_schema_identity,
)

SchemaSections = dict[str, list[dict[str, object]]]


def test_structural_identity_is_independent_from_catalog_row_and_key_order() -> None:
    original = _schema_sections()
    permuted = {
        section: [dict(reversed(tuple(row.items()))) for row in reversed(rows)]
        for section, rows in reversed(tuple(original.items()))
    }

    assert canonical_structural_schema_bytes(permuted) == canonical_structural_schema_bytes(
        original
    )
    assert structural_schema_identity(permuted) == structural_schema_identity(original)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda value: _column(value, "recipient_city").update(type="text"), id="type"),
        pytest.param(
            lambda value: _column(value, "recipient_city").update(nullable=False),
            id="nullable",
        ),
        pytest.param(
            lambda value: _column(value, "recipient_city").update(default=None),
            id="default",
        ),
        pytest.param(lambda value: _remove_constraint(value, "check"), id="check"),
        pytest.param(lambda value: _remove_constraint(value, "unique"), id="unique"),
        pytest.param(lambda value: _remove_constraint(value, "foreign_key"), id="foreign-key"),
        pytest.param(
            lambda value: _constraint(value, "foreign_key").update(on_delete="cascade"),
            id="foreign-key-action",
        ),
        pytest.param(lambda value: value["indexes"].clear(), id="index"),
        pytest.param(lambda value: value["indexes"][0].update(unique=True), id="index-unique"),
        pytest.param(
            lambda value: value["indexes"][0].update(key_columns=["created_at DESC", "status"]),
            id="index-column-order",
        ),
        pytest.param(
            lambda value: value["indexes"][0].update(predicate="status = 'CREATED'::text"),
            id="index-predicate",
        ),
        pytest.param(lambda value: value["columns"].pop(), id="column-absent"),
        pytest.param(lambda value: _add_unexpected_column(value), id="column-unexpected"),
    ],
)
def test_every_relevant_structural_mutation_changes_the_digest(
    mutation: Callable[[SchemaSections], object],
) -> None:
    original = _schema_sections()
    changed = copy.deepcopy(original)

    mutation(changed)

    assert structural_schema_identity(changed).sha256 != structural_schema_identity(original).sha256


def test_missing_or_unexpected_application_table_is_rejected() -> None:
    missing = _schema_sections()
    missing["tables"] = [row for row in missing["tables"] if row["name"] != "notifications"]
    unexpected = _schema_sections()
    unexpected["tables"].append(_table("unexpected_application_table"))

    with pytest.raises(DatabaseIdentityError, match="table-set"):
        structural_schema_identity(missing)
    with pytest.raises(DatabaseIdentityError, match="table-set"):
        structural_schema_identity(unexpected)


def _schema_sections() -> SchemaSections:
    return {
        "tables": [_table(name) for name in reversed(STRUCTURAL_SCHEMA_TABLES)],
        "columns": [
            _column_row("orders", 3, "recipient_city", "character varying(160)", True, "'X'"),
            _column_row("orders", 2, "created_at", "timestamp with time zone", False, None),
            _column_row("orders", 1, "id", "uuid", False, None),
        ],
        "constraints": [
            _constraint_row(
                "ck_orders_recipient_city_nonempty",
                "check",
                ["recipient_city"],
                expression="btrim((recipient_city)::text) <> ''::text",
                definition="CHECK (btrim((recipient_city)::text) <> ''::text)",
            ),
            _constraint_row(
                "uq_orders_recipient_city",
                "unique",
                ["recipient_city"],
                definition="UNIQUE (recipient_city)",
            ),
            _constraint_row(
                "fk_orders_carrier",
                "foreign_key",
                ["id"],
                referenced_table="carriers",
                referenced_columns=["id"],
                definition="FOREIGN KEY (id) REFERENCES carriers(id) ON DELETE RESTRICT",
                on_delete="restrict",
                on_update="no_action",
                match_type="simple",
            ),
        ],
        "indexes": [
            {
                "schema": "public",
                "table": "orders",
                "name": "ix_orders_status_created_at",
                "method": "btree",
                "unique": False,
                "primary": False,
                "valid": True,
                "ready": True,
                "live": True,
                "clustered": False,
                "replica_identity": False,
                "key_columns": ["status", "created_at DESC"],
                "included_columns": [],
                "expressions": None,
                "predicate": None,
                "definition": (
                    "CREATE INDEX ix_orders_status_created_at ON public.orders "
                    "USING btree (status, created_at DESC)"
                ),
            }
        ],
    }


def _table(name: str) -> dict[str, object]:
    return {
        "schema": "public",
        "name": name,
        "kind": "table",
        "row_security": False,
        "force_row_security": False,
    }


def _column_row(
    table: str,
    ordinal: int,
    name: str,
    data_type: str,
    nullable: bool,
    default: str | None,
) -> dict[str, object]:
    return {
        "schema": "public",
        "table": table,
        "ordinal": ordinal,
        "name": name,
        "type": data_type,
        "nullable": nullable,
        "default": default,
        "identity": None,
        "generated": None,
    }


def _constraint_row(
    name: str,
    constraint_type: str,
    local_columns: list[str],
    *,
    referenced_table: str | None = None,
    referenced_columns: list[str] | None = None,
    match_type: str | None = None,
    on_update: str | None = None,
    on_delete: str | None = None,
    expression: str | None = None,
    definition: str,
) -> dict[str, object]:
    return {
        "schema": "public",
        "table": "orders",
        "name": name,
        "type": constraint_type,
        "local_columns": local_columns,
        "referenced_schema": None if referenced_table is None else "public",
        "referenced_table": referenced_table,
        "referenced_columns": referenced_columns or [],
        "match_type": match_type,
        "on_update": on_update,
        "on_delete": on_delete,
        "deferrable": False,
        "initially_deferred": False,
        "validated": True,
        "no_inherit": False,
        "expression": expression,
        "definition": definition,
    }


def _column(sections: SchemaSections, name: str) -> dict[str, object]:
    return next(row for row in sections["columns"] if row["name"] == name)


def _constraint(sections: SchemaSections, constraint_type: str) -> dict[str, object]:
    return next(row for row in sections["constraints"] if row["type"] == constraint_type)


def _remove_constraint(sections: SchemaSections, constraint_type: str) -> None:
    sections["constraints"].remove(_constraint(sections, constraint_type))


def _add_unexpected_column(sections: SchemaSections) -> None:
    sections["columns"].append(_column_row("orders", 4, "unexpected", "integer", True, None))

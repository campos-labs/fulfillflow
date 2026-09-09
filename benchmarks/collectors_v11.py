"""Owner-aware probes; cross-database reconciliation happens only in the harness."""

from __future__ import annotations

import csv
import hashlib
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from benchmarks.campaign import SplitDatabases
from benchmarks.collectors import (
    DatabaseProbe,
    DatabaseSnapshot,
    EventObservation,
    ExternalCommandError,
)
from benchmarks.database_contract import (
    BUSINESS_TABLES,
    STRUCTURAL_SCHEMA_QUERIES,
    DatabaseIdentityError,
    StructuralSchemaIdentity,
    canonical_database_document,
    structural_schema_identity,
)
from benchmarks.seed_v11 import OWNER_SCHEMA_TABLES, OWNER_TABLES, Owner
from fulfillflow.contracts.core import ApplyEventCommand


class SplitDatabaseProbe(DatabaseProbe):
    """Reuse Core reads; route Tracking reads through its own database/role."""

    def __init__(self, container_id: str, *, timeout_seconds: float = 30) -> None:
        super().__init__(
            container_id, "fulfillflow_core", "fulfillflow_core", timeout_seconds=timeout_seconds
        )
        self.tracking = DatabaseProbe(
            container_id,
            "fulfillflow_tracking",
            "fulfillflow_tracking",
            timeout_seconds=timeout_seconds,
        )

    def verify_schemas(self, expected: SplitDatabases) -> dict[str, object]:
        result: dict[str, object] = {}
        targets: tuple[tuple[Owner, DatabaseProbe], ...] = (
            ("core", self),
            ("tracking", self.tracking),
        )
        for owner, probe in targets:
            contract = getattr(expected, owner)
            heads = probe.alembic_heads()
            sections = {
                section: probe._json_rows(sql) for section, sql in STRUCTURAL_SCHEMA_QUERIES.items()
            }
            identity = structural_schema_identity(
                sections, expected_table_names=OWNER_SCHEMA_TABLES[owner]
            )
            if heads != contract.alembic_heads or identity.sha256 != contract.schema_sha256:
                raise DatabaseIdentityError(owner, "schema-or-head")
            result[owner] = {
                "schema_sha256": identity.sha256,
                "alembic_heads": heads,
                "contract_version": identity.contract_version,
            }
        return result

    def structural_schema_identity(self) -> StructuralSchemaIdentity:
        sections = {
            section: self._json_rows(sql) for section, sql in STRUCTURAL_SCHEMA_QUERIES.items()
        }
        return structural_schema_identity(
            sections, expected_table_names=OWNER_SCHEMA_TABLES["core"]
        )

    def active_connections(self) -> int:
        return super().active_connections() + self.tracking.active_connections()

    def connection_counts(self) -> dict[str, int]:
        core = super().active_connections()
        tracking = self.tracking.active_connections()
        return {"core": core, "tracking": tracking, "postgres": core + tracking}

    def receipt_count(self) -> int:
        return int(self.query_scalar("SELECT count(*) FROM tracking_event_receipts"))

    def snapshot(self, label: str) -> DatabaseSnapshot:
        counts = {}
        targets: tuple[tuple[Owner, DatabaseProbe], ...] = (
            ("core", self),
            ("tracking", self.tracking),
        )
        for owner, probe in targets:
            for table in OWNER_TABLES[owner]:
                counts[table] = int(probe.query_scalar(f"SELECT count(*) FROM {table}"))
        return DatabaseSnapshot(
            label,
            counts,
            self.tracking.query_pairs(
                "SELECT status, count(*) FROM carrier_event_inbox GROUP BY status"
            ),
            self.tracking.query_pairs(
                "SELECT application_result, count(*) FROM tracking_events "
                "GROUP BY application_result"
            ),
            {"core_receipts": self.receipt_count()},
        )

    def business_rows(self) -> dict[str, list[dict[str, object]]]:
        result = {}
        for table in BUSINESS_TABLES:
            probe = self.tracking if table in OWNER_TABLES["tracking"] else self
            rows = probe._json_rows(f"SELECT to_jsonb(r)::text FROM {table} r ORDER BY id")
            result[table] = [
                {key: value for key, value in row.items() if key != "command"} for row in rows
            ]
        return result

    def shipment_event_counts(self, shipment_ids: list[str]) -> dict[str, int]:
        return self.tracking.shipment_event_counts(shipment_ids)

    def _event_rows(
        self,
    ) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]], list[dict[str, object]]]:
        rows = canonical_database_document(self.business_rows())
        return (
            {str(row["id"]): row for row in rows["carrier_event_inbox"]},
            {str(row["inbox_event_id"]): row for row in rows["tracking_events"]},
            rows["notifications"],
        )

    def event_observations(self, external_event_ids: list[str]) -> dict[str, EventObservation]:
        if not external_event_ids:
            return {}
        inboxes, events, notifications = self._event_rows()
        wanted = set(external_event_ids)
        result = {}
        for inbox_id, inbox in inboxes.items():
            external_id = str(inbox["external_event_id"])
            if external_id not in wanted:
                continue
            entry = events.get(inbox_id)
            if entry is None or external_id in result:
                raise ExternalCommandError("missing or ambiguous benchmark event")
            notices = [row for row in notifications if row["tracking_event_id"] == entry["id"]]
            raw = inbox["raw_body"]
            assert isinstance(raw, dict)
            result[external_id] = EventObservation(
                external_event_id=external_id,
                inbox_id=inbox_id,
                inbox_status=str(inbox["status"]),
                inbox_carrier_id=str(inbox["carrier_id"]),
                inbox_received_at=str(inbox["received_at"]),
                inbox_processed_at=str(inbox["processed_at"] or ""),
                payload_sha256=str(inbox["payload_sha256"]),
                raw_body_sha256=hashlib.sha256(bytes.fromhex(str(raw["hex"]))).hexdigest(),
                parsed_payload=inbox["parsed_payload"],
                tracking_event_id=str(entry["id"]),
                event_received_at=str(entry["received_at"]),
                shipment_id=str(entry["shipment_id"]),
                event_carrier_id=str(entry["carrier_id"]),
                external_status=str(entry["external_status"]),
                canonical_status=str(entry["canonical_status"]),
                occurred_at=str(entry["occurred_at"]),
                application_result=str(entry["application_result"]),
                previous_shipment_status=str(entry["previous_shipment_status"]),
                resulting_shipment_status=str(entry["resulting_shipment_status"]),
                notification_id=str(notices[0]["id"]) if notices else "",
                notification_count=len(notices),
                matching_notification_count=sum(
                    row["shipment_id"] == entry["shipment_id"] for row in notices
                ),
            )
        return result

    def reconcile(self, *, initial: bool = False) -> dict[str, int]:
        """Require a one-to-one command/receipt/timeline/effect chain after drain."""
        inboxes = self.tracking._json_rows(
            "SELECT to_jsonb(r)::text FROM carrier_event_inbox r ORDER BY id"
        )
        receipts = self._json_rows(
            "SELECT to_jsonb(r)::text FROM tracking_event_receipts r ORDER BY event_id"
        )
        commands = {str(row["id"]): row for row in inboxes if row["command"] is not None}
        if initial and (commands or receipts):
            raise ExternalCommandError("initial receipts and commands must be empty")
        by_event = {str(row["event_id"]): row for row in receipts}
        _, events, notices = self._event_rows()
        notice_counts = Counter(str(row["tracking_event_id"]) for row in notices)
        notices_by_event = {str(row["tracking_event_id"]): row for row in notices}
        if len(commands) != len(receipts):
            raise ExternalCommandError("Core receipt and Tracking command counts differ")
        for inbox_id, inbox in commands.items():
            command = ApplyEventCommand.model_validate(inbox["command"])
            receipt = by_event.get(str(command.event_id))
            entry = events.get(inbox_id)
            if receipt is None or entry is None or inbox["status"] != "PROCESSED":
                raise ExternalCommandError("benchmark effects are not fully finalized")
            outcome = receipt["result"]
            raw = inbox["raw_body"]
            if not isinstance(raw, str) or not raw.startswith("\\x"):
                raise ExternalCommandError("inbox bytes are unavailable for reconciliation")
            raw_hash = hashlib.sha256(bytes.fromhex(raw[2:])).hexdigest()
            notice = notices_by_event.get(str(command.event_id), {})
            if not isinstance(outcome, dict) or not (
                receipt["content_sha256"] == command.content_hash()
                and str(receipt["carrier_id"]) == str(command.carrier_id)
                and receipt["external_event_id"] == command.external_event_id
                and str(inbox["carrier_id"]) == str(command.carrier_id)
                and inbox["external_event_id"] == command.external_event_id
                and inbox["payload_sha256"] == command.payload_sha256
                and raw_hash == command.payload_sha256
                and datetime.fromisoformat(str(inbox["received_at"])) == command.received_at
                and str(entry["id"]) == str(command.event_id)
                and str(entry["carrier_id"]) == str(command.carrier_id)
                and datetime.fromisoformat(str(entry["occurred_at"])) == command.occurred_at
                and datetime.fromisoformat(str(entry["received_at"])) == command.received_at
                and entry["canonical_status"] == command.canonical_status
                and entry["external_status"] == command.external_status
                and entry["description"] == command.description
                and entry["location"] == command.location
                and outcome.get("kind") == "applied"
                and outcome.get("event_id") == str(command.event_id)
                and datetime.fromisoformat(str(outcome.get("decided_at")))
                == datetime.fromisoformat(str(entry["created_at"]))
                and outcome.get("result") == entry["application_result"] == "APPLIED"
                and outcome.get("shipment_id") == entry["shipment_id"]
                and outcome.get("previous_status") == entry["previous_shipment_status"]
                and outcome.get("current_status") == entry["resulting_shipment_status"]
                and notice_counts[str(command.event_id)] == 1
                and notice.get("shipment_id") == entry["shipment_id"]
            ):
                raise ExternalCommandError("receipt differs from its immutable command or effects")
        return {"commands": len(commands), "receipts": len(receipts), "finalized": len(commands)}


def aggregate_resources(source: Path, destination: Path, container_ids: Mapping[str, str]) -> None:
    """Sum aligned Core/Tracking samples without treating them as one container."""
    from benchmarks.collectors import validate_resource_samples

    validate_resource_samples(source, container_ids)
    with source.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    assert fields is not None
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row["service"] in {"core", "tracking"}:
            groups.setdefault(row["timestamp_utc"], []).append(row)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for instant, pair in groups.items():
            if {row["service"] for row in pair} != {"core", "tracking"} or len(pair) != 2:
                raise ExternalCommandError("incomplete Core/Tracking resource cycle")
            row = dict.fromkeys(fields, "")
            row.update(timestamp_utc=instant, service="application_total", container_id="")
            for key in (
                "cpu_percent",
                "memory_usage_bytes",
                "memory_limit_bytes",
                "postgres_active_connections",
            ):
                row[key] = str(sum(float(item[key]) for item in pair))
            writer.writerow(row)

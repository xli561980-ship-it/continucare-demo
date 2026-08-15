"""Append-only SQLite persistence owned by Follow-up Planning."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from continucare.followup_planning.models import (
    ClinicalContextCandidate,
    CompletedAnswerBinding,
    EvidenceSnapshot,
    MonitoringItemDefinition,
    PatientFollowupItem,
    PatientFollowupPlanVersion,
    PlanAggregate,
    PlanningAuditEvent,
    PlanStatus,
    ScheduleTemplate,
)


class FollowupPlanningStorageError(RuntimeError):
    pass


class ConcurrentCandidateVersionError(FollowupPlanningStorageError):
    pass


class ConcurrentPlanVersionError(FollowupPlanningStorageError):
    pass


class ImmutablePlanningRecordError(FollowupPlanningStorageError):
    pass


FOLLOWUP_PLANNING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS followup_planning_candidate_versions (
    candidate_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    case_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    confirmation_status TEXT NOT NULL CHECK (
        confirmation_status IN ('extracted', 'doctor_confirmed', 'rejected')
    ),
    synthetic INTEGER NOT NULL CHECK (synthetic = 1),
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (candidate_id, version)
);

CREATE INDEX IF NOT EXISTS idx_followup_planning_candidates_case
ON followup_planning_candidate_versions(case_id, patient_id, candidate_id, version);

CREATE TABLE IF NOT EXISTS followup_planning_evidence_snapshots (
    snapshot_hash TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    snapshot_version INTEGER NOT NULL CHECK (snapshot_version >= 1),
    exact_topic_id TEXT NOT NULL,
    review_status TEXT NOT NULL,
    record_json TEXT NOT NULL,
    stored_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_followup_planning_evidence_identity
ON followup_planning_evidence_snapshots(snapshot_id, snapshot_version, snapshot_hash);

CREATE TABLE IF NOT EXISTS followup_planning_definition_versions (
    definition_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    origin TEXT NOT NULL CHECK (origin IN ('evidence_referenced', 'doctor_authored')),
    synthetic INTEGER NOT NULL CHECK (synthetic = 1),
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (definition_id, version)
);

CREATE TABLE IF NOT EXISTS followup_planning_schedule_template_versions (
    schedule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    approval_status TEXT NOT NULL CHECK (approval_status IN ('draft', 'approved')),
    data_classification TEXT NOT NULL CHECK (data_classification = 'synthetic_demo'),
    approval_scope TEXT NOT NULL CHECK (approval_scope = 'demo_only'),
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (schedule_id, version)
);

CREATE TABLE IF NOT EXISTS followup_planning_item_versions (
    item_id TEXT NOT NULL,
    item_version INTEGER NOT NULL CHECK (item_version >= 1),
    plan_version_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'stopped')),
    series_id TEXT NOT NULL,
    synthetic INTEGER NOT NULL CHECK (synthetic = 1),
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, item_version)
);

CREATE INDEX IF NOT EXISTS idx_followup_planning_item_series
ON followup_planning_item_versions(series_id, item_id, item_version);

CREATE TABLE IF NOT EXISTS followup_planning_plan_revisions (
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    plan_version_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'published')),
    synthetic INTEGER NOT NULL CHECK (synthetic = 1),
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_followup_planning_one_published_release
ON followup_planning_plan_revisions(plan_id, plan_version)
WHERE status = 'published';

CREATE INDEX IF NOT EXISTS idx_followup_planning_patient_releases
ON followup_planning_plan_revisions(patient_id, status, plan_version DESC, revision DESC);

CREATE TABLE IF NOT EXISTS followup_planning_plan_revision_items (
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    display_order INTEGER NOT NULL CHECK (display_order >= 0),
    item_id TEXT NOT NULL,
    item_version INTEGER NOT NULL,
    PRIMARY KEY (plan_id, revision, item_id),
    UNIQUE (plan_id, revision, display_order),
    FOREIGN KEY (plan_id, revision)
        REFERENCES followup_planning_plan_revisions(plan_id, revision)
        ON DELETE RESTRICT,
    FOREIGN KEY (item_id, item_version)
        REFERENCES followup_planning_item_versions(item_id, item_version)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS followup_planning_plan_lifecycle_events (
    sequence INTEGER PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    event_type TEXT NOT NULL CHECK (event_type IN ('published', 'superseded')),
    occurred_at TEXT NOT NULL,
    audit_event_id TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_followup_planning_plan_lifecycle
ON followup_planning_plan_lifecycle_events(plan_id, plan_version, sequence);

CREATE TABLE IF NOT EXISTS followup_planning_completed_answer_bindings (
    answer_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    item_id TEXT NOT NULL,
    item_version INTEGER NOT NULL CHECK (item_version >= 1),
    series_id TEXT NOT NULL,
    prompt_snapshot TEXT NOT NULL,
    record_json TEXT NOT NULL,
    answered_at TEXT NOT NULL,
    synthetic INTEGER NOT NULL CHECK (synthetic = 1),
    FOREIGN KEY (item_id, item_version)
        REFERENCES followup_planning_item_versions(item_id, item_version)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS followup_planning_audit_events (
    sequence INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL CHECK (
        actor_role IN ('doctor', 'clinician', 'nurse', 'patient')
    ),
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_followup_planning_audit_entity
ON followup_planning_audit_events(entity_type, entity_id, sequence);
"""


def _json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SQLiteFollowupPlanningRepository:
    """Module-owned repository accepting an explicit path or open connection.

    Construction is side-effect free. A path is not opened until ``initialize``
    or another enabled repository operation is explicitly called. Caller-owned
    connections are never closed by this repository.
    """

    def __init__(self, storage: Path | str | sqlite3.Connection):
        self._provided_connection = (
            storage if isinstance(storage, sqlite3.Connection) else None
        )
        self.db_path = None if self._provided_connection else Path(storage)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        owns_connection = self._provided_connection is None
        if owns_connection:
            assert self.db_path is not None
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path)
        else:
            assert self._provided_connection is not None
            connection = self._provided_connection
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            if owns_connection:
                connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(FOLLOWUP_PLANNING_SCHEMA_SQL)
            connection.commit()

    def append_candidate(
        self,
        candidate: ClinicalContextCandidate,
        *,
        expected_version: int,
        audit_event: PlanningAuditEvent,
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM followup_planning_candidate_versions "
                "WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            current = int(row["version"] or 0)
            if current != expected_version or candidate.version != current + 1:
                raise ConcurrentCandidateVersionError(
                    f"expected candidate version {expected_version}, found {current}"
                )
            connection.execute(
                """
                INSERT INTO followup_planning_candidate_versions (
                    candidate_id, version, case_id, patient_id,
                    confirmation_status, synthetic, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.version,
                    candidate.case_id,
                    candidate.patient_id,
                    candidate.confirmation_status.value,
                    _json(candidate),
                    candidate.created_at.isoformat(),
                ),
            )
            self._insert_audit(connection, audit_event)

    def get_candidate(self, candidate_id: str) -> ClinicalContextCandidate | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT record_json FROM followup_planning_candidate_versions
                WHERE candidate_id = ? ORDER BY version DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
        return (
            ClinicalContextCandidate.model_validate_json(row["record_json"])
            if row
            else None
        )

    def list_case_candidates(self, case_id: str) -> list[ClinicalContextCandidate]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.record_json
                FROM followup_planning_candidate_versions c
                JOIN (
                    SELECT candidate_id, MAX(version) AS version
                    FROM followup_planning_candidate_versions
                    WHERE case_id = ? GROUP BY candidate_id
                ) latest
                  ON latest.candidate_id = c.candidate_id
                 AND latest.version = c.version
                ORDER BY c.candidate_id
                """,
                (case_id,),
            ).fetchall()
        return [
            ClinicalContextCandidate.model_validate_json(row["record_json"])
            for row in rows
        ]

    def append_definition(
        self,
        definition: MonitoringItemDefinition,
        *,
        expected_version: int,
        evidence_snapshots: Sequence[EvidenceSnapshot],
        audit_event: PlanningAuditEvent,
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM followup_planning_definition_versions "
                "WHERE definition_id = ?",
                (definition.definition_id,),
            ).fetchone()
            current = int(row["version"] or 0)
            if current != expected_version or definition.version != current + 1:
                raise ImmutablePlanningRecordError(
                    f"definition version must append after {current}"
                )
            for snapshot in evidence_snapshots:
                self._insert_evidence_snapshot(
                    connection, snapshot, stored_at=definition.created_at.isoformat()
                )
            connection.execute(
                """
                INSERT INTO followup_planning_definition_versions (
                    definition_id, version, origin, synthetic, record_json, created_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (
                    definition.definition_id,
                    definition.version,
                    definition.origin.value,
                    _json(definition),
                    definition.created_at.isoformat(),
                ),
            )
            self._insert_audit(connection, audit_event)

    def get_definition(
        self, definition_id: str, version: int | None = None
    ) -> MonitoringItemDefinition | None:
        query = (
            "SELECT record_json FROM followup_planning_definition_versions "
            "WHERE definition_id = ? AND version = ?"
            if version is not None
            else "SELECT record_json FROM followup_planning_definition_versions "
            "WHERE definition_id = ? ORDER BY version DESC LIMIT 1"
        )
        params: tuple[object, ...] = (
            (definition_id, version) if version is not None else (definition_id,)
        )
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return (
            MonitoringItemDefinition.model_validate_json(row["record_json"])
            if row
            else None
        )

    def append_schedule_template(
        self,
        template: ScheduleTemplate,
        *,
        expected_version: int,
        audit_event: PlanningAuditEvent,
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM "
                "followup_planning_schedule_template_versions WHERE schedule_id = ?",
                (template.schedule_id,),
            ).fetchone()
            current = int(row["version"] or 0)
            if current != expected_version or template.version != current + 1:
                raise ImmutablePlanningRecordError(
                    f"schedule template version must append after {current}"
                )
            connection.execute(
                """
                INSERT INTO followup_planning_schedule_template_versions (
                    schedule_id, version, approval_status, data_classification,
                    approval_scope, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    template.schedule_id,
                    template.version,
                    template.approval_status.value,
                    template.data_classification,
                    template.approval_scope,
                    _json(template),
                    template.created_at.isoformat(),
                ),
            )
            self._insert_audit(connection, audit_event)

    def get_schedule_template(
        self, schedule_id: str, version: int | None = None
    ) -> ScheduleTemplate | None:
        query = (
            "SELECT record_json FROM followup_planning_schedule_template_versions "
            "WHERE schedule_id = ? AND version = ?"
            if version is not None
            else "SELECT record_json FROM followup_planning_schedule_template_versions "
            "WHERE schedule_id = ? ORDER BY version DESC LIMIT 1"
        )
        params: tuple[object, ...] = (
            (schedule_id, version) if version is not None else (schedule_id,)
        )
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return ScheduleTemplate.model_validate_json(row["record_json"]) if row else None

    def append_plan_revision(
        self,
        aggregate: PlanAggregate,
        *,
        expected_revision: int,
        audit_event: PlanningAuditEvent,
        publish_version: int | None = None,
        supersede_version: int | None = None,
    ) -> None:
        plan = aggregate.plan
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT MAX(revision) AS revision FROM followup_planning_plan_revisions "
                "WHERE plan_id = ?",
                (plan.plan_id,),
            ).fetchone()
            current = int(row["revision"] or 0)
            if current != expected_revision or plan.revision != current + 1:
                raise ConcurrentPlanVersionError(
                    f"expected plan revision {expected_revision}, found {current}"
                )
            connection.execute(
                """
                INSERT INTO followup_planning_plan_revisions (
                    plan_id, revision, plan_version, plan_version_id, patient_id,
                    status, synthetic, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.revision,
                    plan.version,
                    plan.plan_version_id,
                    plan.patient_id,
                    plan.status.value,
                    _json(plan),
                    plan.created_at.isoformat(),
                ),
            )
            for item in aggregate.items:
                self._insert_immutable_item(connection, item)
                connection.execute(
                    """
                    INSERT INTO followup_planning_plan_revision_items (
                        plan_id, revision, display_order, item_id, item_version
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        plan.plan_id,
                        plan.revision,
                        item.display_order,
                        item.item_id,
                        item.item_version,
                    ),
                )
            if publish_version is not None:
                self._insert_lifecycle_event(
                    connection,
                    plan_id=plan.plan_id,
                    plan_version=publish_version,
                    event_type="published",
                    audit_event=audit_event,
                )
            if supersede_version is not None:
                self._insert_lifecycle_event(
                    connection,
                    plan_id=plan.plan_id,
                    plan_version=supersede_version,
                    event_type="superseded",
                    audit_event=audit_event,
                )
            self._insert_audit(connection, audit_event)

    def get_latest_plan(self, plan_id: str) -> PlanAggregate | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM followup_planning_plan_revisions
                WHERE plan_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (plan_id,),
            ).fetchone()
            return self._row_to_aggregate(connection, row) if row else None

    def get_plan_revision(self, plan_id: str, revision: int) -> PlanAggregate | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM followup_planning_plan_revisions
                WHERE plan_id = ? AND revision = ?
                """,
                (plan_id, revision),
            ).fetchone()
            return self._row_to_aggregate(connection, row) if row else None

    def get_published_version(
        self, plan_id: str, version: int
    ) -> PlanAggregate | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM followup_planning_plan_revisions
                WHERE plan_id = ? AND plan_version = ? AND status = 'published'
                ORDER BY revision DESC LIMIT 1
                """,
                (plan_id, version),
            ).fetchone()
            return self._row_to_aggregate(connection, row) if row else None

    def get_current_published(self, patient_id: str) -> PlanAggregate | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM followup_planning_plan_revisions
                WHERE patient_id = ? AND status = 'published'
                ORDER BY plan_version DESC, revision DESC LIMIT 1
                """,
                (patient_id,),
            ).fetchone()
            return self._row_to_aggregate(connection, row) if row else None

    def append_answer_binding(
        self,
        binding: CompletedAnswerBinding,
        *,
        audit_event: PlanningAuditEvent,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO followup_planning_completed_answer_bindings (
                    answer_id, patient_id, plan_id, plan_version, item_id,
                    item_version, series_id, prompt_snapshot, record_json,
                    answered_at, synthetic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    binding.answer_id,
                    binding.patient_id,
                    binding.plan_id,
                    binding.plan_version,
                    binding.item_id,
                    binding.item_version,
                    binding.series_id,
                    binding.prompt_snapshot,
                    _json(binding),
                    binding.answered_at.isoformat(),
                ),
            )
            self._insert_audit(connection, audit_event)

    def get_answer_binding(self, answer_id: str) -> CompletedAnswerBinding | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM followup_planning_completed_answer_bindings "
                "WHERE answer_id = ?",
                (answer_id,),
            ).fetchone()
        return (
            CompletedAnswerBinding.model_validate_json(row["record_json"])
            if row
            else None
        )

    def list_audit_events(
        self, *, entity_type: str, entity_id: str
    ) -> list[PlanningAuditEvent]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM followup_planning_audit_events
                WHERE entity_type = ? AND entity_id = ? ORDER BY sequence
                """,
                (entity_type, entity_id),
            ).fetchall()
        return [self._row_to_audit(row) for row in rows]

    @staticmethod
    def _insert_evidence_snapshot(
        connection: sqlite3.Connection,
        snapshot: EvidenceSnapshot,
        *,
        stored_at: str,
    ) -> None:
        serialized = _json(snapshot)
        row = connection.execute(
            "SELECT record_json FROM followup_planning_evidence_snapshots "
            "WHERE snapshot_hash = ?",
            (snapshot.snapshot_hash,),
        ).fetchone()
        if row is not None:
            if row["record_json"] != serialized:
                raise ImmutablePlanningRecordError(
                    "evidence snapshot hash already identifies different content"
                )
            return
        connection.execute(
            """
            INSERT INTO followup_planning_evidence_snapshots (
                snapshot_hash, snapshot_id, snapshot_version, exact_topic_id,
                review_status, record_json, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_hash,
                snapshot.snapshot_id,
                snapshot.version,
                snapshot.topic_ref.reference_id,
                snapshot.review_status.value,
                serialized,
                stored_at,
            ),
        )

    @staticmethod
    def _insert_immutable_item(
        connection: sqlite3.Connection, item: PatientFollowupItem
    ) -> None:
        serialized = _json(item)
        row = connection.execute(
            """
            SELECT record_json FROM followup_planning_item_versions
            WHERE item_id = ? AND item_version = ?
            """,
            (item.item_id, item.item_version),
        ).fetchone()
        if row is not None:
            if row["record_json"] != serialized:
                raise ImmutablePlanningRecordError(
                    "item version already identifies different content"
                )
            return
        connection.execute(
            """
            INSERT INTO followup_planning_item_versions (
                item_id, item_version, plan_version_id, status, series_id,
                synthetic, record_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                item.item_id,
                item.item_version,
                item.plan_version_id,
                item.status.value,
                item.series_id,
                serialized,
                item.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _insert_lifecycle_event(
        connection: sqlite3.Connection,
        *,
        plan_id: str,
        plan_version: int,
        event_type: str,
        audit_event: PlanningAuditEvent,
    ) -> None:
        connection.execute(
            """
            INSERT INTO followup_planning_plan_lifecycle_events (
                plan_id, plan_version, event_type, occurred_at, audit_event_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                plan_version,
                event_type,
                audit_event.occurred_at.isoformat(),
                f"{audit_event.event_id}:{event_type}:{plan_version}",
            ),
        )

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection, event: PlanningAuditEvent
    ) -> None:
        connection.execute(
            """
            INSERT INTO followup_planning_audit_events (
                event_id, entity_type, entity_id, event_type, actor_id,
                actor_role, occurred_at, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.entity_type,
                event.entity_id,
                event.event_type,
                event.actor_id,
                event.actor_role.value,
                event.occurred_at.isoformat(),
                _json(dict(event.details)),
            ),
        )

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> PlanningAuditEvent:
        return PlanningAuditEvent(
            event_id=row["event_id"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            event_type=row["event_type"],
            actor_id=row["actor_id"],
            actor_role=row["actor_role"],
            occurred_at=row["occurred_at"],
            details=json.loads(row["details_json"]),
        )

    @staticmethod
    def _row_to_aggregate(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> PlanAggregate:
        plan = PatientFollowupPlanVersion.model_validate_json(row["record_json"])
        superseded = connection.execute(
            """
            SELECT 1 FROM followup_planning_plan_lifecycle_events
            WHERE plan_id = ? AND plan_version = ? AND event_type = 'superseded'
            LIMIT 1
            """,
            (plan.plan_id, plan.version),
        ).fetchone()
        if superseded and plan.status == PlanStatus.PUBLISHED:
            plan = plan.model_copy(update={"status": PlanStatus.SUPERSEDED})
        item_rows = connection.execute(
            """
            SELECT i.record_json
            FROM followup_planning_plan_revision_items link
            JOIN followup_planning_item_versions i
              ON i.item_id = link.item_id AND i.item_version = link.item_version
            WHERE link.plan_id = ? AND link.revision = ?
            ORDER BY link.display_order
            """,
            (plan.plan_id, plan.revision),
        ).fetchall()
        items = tuple(
            PatientFollowupItem.model_validate_json(item_row["record_json"])
            for item_row in item_rows
        )
        return PlanAggregate(plan=plan, items=items)

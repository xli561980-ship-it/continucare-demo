"""Additive SQLite persistence owned exclusively by optional Case Intake."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from continucare.case_intake.models import (
    CaseAuditAction,
    CaseAuditActorType,
    CaseAuditEvent,
    CaseDraft,
    CaseRecord,
    CaseSourceType,
    case_content_hash,
)
from continucare.db import connect


class CaseIntakeStorageError(RuntimeError):
    pass


class NonSyntheticPatientError(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class ConcurrentCaseVersionError(ValueError):
    pass


CASE_INTAKE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS case_intake_case_versions (
    case_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE RESTRICT,
    encounter_id TEXT NOT NULL,
    pathway_code TEXT,
    pathway_version TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('manual', 'hospital_mock')),
    source_system TEXT NOT NULL,
    external_record_id TEXT,
    idempotency_key TEXT,
    author_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    encounter_date TEXT NOT NULL,
    chief_complaint TEXT NOT NULL,
    present_illness TEXT,
    past_history TEXT,
    medication_history_json TEXT NOT NULL,
    allergy_history_json TEXT NOT NULL,
    test_summary TEXT,
    clinician_assessment_json TEXT,
    followup_plan_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'entered_in_error')),
    supersedes_case_version INTEGER,
    synthetic INTEGER NOT NULL CHECK (synthetic = 1),
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (case_id, version),
    FOREIGN KEY (case_id, supersedes_case_version)
        REFERENCES case_intake_case_versions(case_id, version),
    CHECK ((pathway_code IS NULL) = (pathway_version IS NULL)),
    CHECK (
        (source_type = 'manual' AND external_record_id IS NULL AND idempotency_key IS NULL)
        OR
        (source_type = 'hospital_mock' AND external_record_id IS NOT NULL AND idempotency_key IS NOT NULL)
    ),
    CHECK (
        (version = 1 AND supersedes_case_version IS NULL)
        OR
        (version > 1 AND supersedes_case_version = version - 1)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_case_intake_import_idempotency
ON case_intake_case_versions(source_system, idempotency_key)
WHERE source_type = 'hospital_mock' AND idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_case_intake_external_record_identity
ON case_intake_case_versions(source_system, external_record_id)
WHERE source_type = 'hospital_mock' AND external_record_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_case_intake_patient_encounter
ON case_intake_case_versions(patient_id, encounter_date, encounter_id);

CREATE INDEX IF NOT EXISTS idx_case_intake_case_latest
ON case_intake_case_versions(case_id, version DESC);

CREATE TABLE IF NOT EXISTS case_intake_audit_events (
    sequence INTEGER PRIMARY KEY,
    audit_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL,
    case_version INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN (
            'manual_created', 'hospital_imported',
            'idempotent_replay', 'clinician_revised'
        )
    ),
    actor_type TEXT NOT NULL CHECK (actor_type IN ('doctor', 'source_adapter')),
    actor_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('manual', 'hospital_mock')),
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    FOREIGN KEY (case_id, case_version)
        REFERENCES case_intake_case_versions(case_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_case_intake_audit_case_time
ON case_intake_audit_events(case_id, case_version, occurred_at);
"""


def _json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _record_values(record: CaseRecord) -> tuple:
    return (
        record.case_id,
        record.version,
        record.patient_id,
        record.encounter_id,
        record.pathway_code,
        record.pathway_version,
        record.source_type.value,
        record.source_system,
        record.external_record_id,
        record.idempotency_key,
        _json(record.author.model_dump(mode="json")),
        record.recorded_at.isoformat(),
        record.encounter_date.isoformat(),
        record.chief_complaint,
        record.present_illness,
        record.past_history,
        _json(list(record.medication_history)),
        _json(list(record.allergy_history)),
        record.test_summary,
        (
            _json(record.clinician_assessment.model_dump(mode="json"))
            if record.clinician_assessment
            else None
        ),
        (
            _json(record.followup_plan.model_dump(mode="json"))
            if record.followup_plan
            else None
        ),
        record.status.value,
        record.supersedes_case_version,
        1,
        record.content_hash,
        record.created_at.isoformat(),
    )


def _row_to_record(row: sqlite3.Row) -> CaseRecord:
    data = dict(row)
    data["author"] = json.loads(data.pop("author_json"))
    data["medication_history"] = json.loads(data.pop("medication_history_json"))
    data["allergy_history"] = json.loads(data.pop("allergy_history_json"))
    assessment = data.pop("clinician_assessment_json")
    plan = data.pop("followup_plan_json")
    data["clinician_assessment"] = json.loads(assessment) if assessment else None
    data["followup_plan"] = json.loads(plan) if plan else None
    data["synthetic"] = bool(data["synthetic"])
    return CaseRecord.model_validate(data)


def _row_to_audit(row: sqlite3.Row) -> CaseAuditEvent:
    data = dict(row)
    data.pop("sequence")
    data["details"] = json.loads(data.pop("details_json"))
    return CaseAuditEvent.model_validate(data)


class SQLiteCaseRepository:
    """Immutable case versions plus a separate append-only audit stream."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        with connect(self.db_path) as connection:
            connection.executescript(CASE_INTAKE_SCHEMA_SQL)

    def create(
        self,
        draft: CaseDraft,
        *,
        case_id_factory: Callable[[], str],
        occurred_at: datetime,
        actor_type: CaseAuditActorType,
        actor_id: str,
    ) -> tuple[CaseRecord, bool]:
        content_hash = case_content_hash(draft)
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_synthetic_patient(connection, draft.patient_id)
            if draft.source_type == CaseSourceType.HOSPITAL_MOCK:
                existing = connection.execute(
                    """
                    SELECT * FROM case_intake_case_versions
                    WHERE source_type = 'hospital_mock'
                      AND source_system = ? AND external_record_id = ?
                    """,
                    (draft.source_system, draft.external_record_id),
                ).fetchone()
                if existing is not None:
                    record = _row_to_record(existing)
                    if record.content_hash != content_hash:
                        raise IdempotencyConflict(
                            "the external hospital record was already imported with "
                            "different case content"
                        )
                    self._insert_audit(
                        connection,
                        CaseAuditEvent(
                            audit_id=f"case_audit_{uuid4().hex}",
                            case_id=record.case_id,
                            case_version=record.version,
                            action=CaseAuditAction.IDEMPOTENT_REPLAY,
                            actor_type=actor_type,
                            actor_id=actor_id,
                            source_type=record.source_type,
                            occurred_at=occurred_at,
                            details={"content_hash": content_hash, "created": False},
                        ),
                    )
                    return record, False

            record = CaseRecord(
                **draft.model_dump(),
                case_id=case_id_factory(),
                version=1,
                supersedes_case_version=None,
                content_hash=content_hash,
                created_at=occurred_at,
            )
            self._insert_record(connection, record)
            action = (
                CaseAuditAction.HOSPITAL_IMPORTED
                if draft.source_type == CaseSourceType.HOSPITAL_MOCK
                else CaseAuditAction.MANUAL_CREATED
            )
            self._insert_audit(
                connection,
                CaseAuditEvent(
                    audit_id=f"case_audit_{uuid4().hex}",
                    case_id=record.case_id,
                    case_version=record.version,
                    action=action,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    source_type=record.source_type,
                    occurred_at=occurred_at,
                    details={"content_hash": content_hash, "created": True},
                ),
            )
        return record, True

    def revise(
        self,
        draft: CaseDraft,
        *,
        case_id: str,
        expected_version: int,
        occurred_at: datetime,
        actor_id: str,
    ) -> CaseRecord:
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM case_intake_case_versions
                WHERE case_id = ? ORDER BY version DESC LIMIT 1
                """,
                (case_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"case {case_id!r} was not found")
            current = _row_to_record(row)
            if current.version != expected_version:
                raise ConcurrentCaseVersionError(
                    f"expected case version {expected_version}, found {current.version}"
                )
            if draft.patient_id != current.patient_id or draft.encounter_id != current.encounter_id:
                raise ValueError("a revision cannot change patient_id or encounter_id")
            self._require_synthetic_patient(connection, draft.patient_id)
            record = CaseRecord(
                **draft.model_dump(),
                case_id=case_id,
                version=current.version + 1,
                supersedes_case_version=current.version,
                content_hash=case_content_hash(draft),
                created_at=occurred_at,
            )
            self._insert_record(connection, record)
            self._insert_audit(
                connection,
                CaseAuditEvent(
                    audit_id=f"case_audit_{uuid4().hex}",
                    case_id=record.case_id,
                    case_version=record.version,
                    action=CaseAuditAction.CLINICIAN_REVISED,
                    actor_type=CaseAuditActorType.DOCTOR,
                    actor_id=actor_id,
                    source_type=record.source_type,
                    occurred_at=occurred_at,
                    details={
                        "content_hash": record.content_hash,
                        "supersedes_case_version": current.version,
                    },
                ),
            )
        return record

    def get_latest(self, case_id: str) -> CaseRecord | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM case_intake_case_versions
                WHERE case_id = ? ORDER BY version DESC LIMIT 1
                """,
                (case_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_version(self, case_id: str, version: int) -> CaseRecord | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM case_intake_case_versions
                WHERE case_id = ? AND version = ?
                """,
                (case_id, version),
            ).fetchone()
        return _row_to_record(row) if row else None

    def list_versions(self, case_id: str) -> list[CaseRecord]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM case_intake_case_versions
                WHERE case_id = ? ORDER BY version
                """,
                (case_id,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_patient_cases(self, patient_id: str) -> list[CaseRecord]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT c.*
                FROM case_intake_case_versions c
                JOIN (
                    SELECT case_id, MAX(version) AS version
                    FROM case_intake_case_versions
                    WHERE patient_id = ? GROUP BY case_id
                ) latest ON latest.case_id = c.case_id AND latest.version = c.version
                ORDER BY c.encounter_date DESC, c.case_id
                """,
                (patient_id,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_audit_events(self, case_id: str) -> list[CaseAuditEvent]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM case_intake_audit_events
                WHERE case_id = ? ORDER BY sequence
                """,
                (case_id,),
            ).fetchall()
        return [_row_to_audit(row) for row in rows]

    @staticmethod
    def _require_synthetic_patient(
        connection: sqlite3.Connection, patient_id: str
    ) -> None:
        try:
            row = connection.execute(
                "SELECT synthetic FROM patients WHERE patient_id = ?", (patient_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise CaseIntakeStorageError(
                "initialize the core synthetic demo database before Case Intake"
            ) from exc
        if row is None:
            raise LookupError(f"synthetic patient {patient_id!r} was not found")
        if not bool(row["synthetic"]):
            raise NonSyntheticPatientError(
                "Case Intake is restricted to patients marked synthetic"
            )

    @staticmethod
    def _insert_record(connection: sqlite3.Connection, record: CaseRecord) -> None:
        connection.execute(
            """
            INSERT INTO case_intake_case_versions (
                case_id, version, patient_id, encounter_id,
                pathway_code, pathway_version, source_type, source_system,
                external_record_id, idempotency_key, author_json, recorded_at,
                encounter_date, chief_complaint, present_illness, past_history,
                medication_history_json, allergy_history_json, test_summary,
                clinician_assessment_json, followup_plan_json, status,
                supersedes_case_version, synthetic, content_hash, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            _record_values(record),
        )

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection, event: CaseAuditEvent
    ) -> None:
        connection.execute(
            """
            INSERT INTO case_intake_audit_events (
                audit_id, case_id, case_version, action, actor_type,
                actor_id, source_type, occurred_at, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.audit_id,
                event.case_id,
                event.case_version,
                event.action.value,
                event.actor_type.value,
                event.actor_id,
                event.source_type.value,
                event.occurred_at.isoformat(),
                _json(event.details),
            ),
        )

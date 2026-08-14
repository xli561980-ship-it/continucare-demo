"""SQLite bootstrap used by the local demo."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from continucare.config import get_settings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(db_path: Path | str | None = None) -> None:
    with connect(db_path) as connection:
        connection.executescript(SCHEMA_SQL)
        _migrate_schema(connection)
        connection.execute(
            """
            INSERT INTO demo_metadata (key, value, updated_at)
            VALUES ('data_classification', 'synthetic_only', ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (utc_now_iso(),),
        )
        _seed_demo_patient(connection)


def _migrate_schema(connection: sqlite3.Connection) -> None:
    """Apply additive local-demo migrations without deleting existing data."""

    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(observation_evidence)")
    }
    if "source_kind" not in columns:
        connection.execute(
            "ALTER TABLE observation_evidence ADD COLUMN "
            "source_kind TEXT NOT NULL DEFAULT 'pathway_monitored'"
        )
    if "terminology_match_json" not in columns:
        connection.execute(
            "ALTER TABLE observation_evidence ADD COLUMN terminology_match_json TEXT"
        )
    answer_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(confirmed_answer_contexts)")
    }
    if "terminology_match_json" not in answer_columns:
        connection.execute(
            "ALTER TABLE confirmed_answer_contexts ADD COLUMN terminology_match_json TEXT"
        )
    symptom_report_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(confirmed_symptom_reports)")
    }
    if "source_kind" not in symptom_report_columns:
        connection.execute(
            "ALTER TABLE confirmed_symptom_reports ADD COLUMN "
            "source_kind TEXT NOT NULL DEFAULT 'patient_reported_new'"
        )
    _migrate_layer4_contract_record_types(connection)


def _migrate_layer4_contract_record_types(connection: sqlite3.Connection) -> None:
    """Add new contract discriminators without losing existing immutable rows."""

    row = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'layer4_contract_records'"
    ).fetchone()
    if row is None or "state_snapshot" in (row["sql"] or ""):
        return
    for index_name in (
        "idx_layer4_contract_current",
        "idx_layer4_contract_patient_type_time",
        "idx_layer4_contract_pathway_status",
    ):
        connection.execute(f"DROP INDEX IF EXISTS {index_name}")
    connection.execute(
        "ALTER TABLE layer4_contract_records "
        "RENAME TO layer4_contract_records_legacy"
    )
    connection.execute(
        """
        CREATE TABLE layer4_contract_records (
            record_type TEXT NOT NULL CHECK (
                record_type IN (
                    'clinical_rule', 'memory_event', 'timeline_event',
                    'revision_link', 'summary_draft', 'doctor_review',
                    'state_snapshot'
                )
            ),
            record_id TEXT NOT NULL,
            record_version TEXT NOT NULL,
            patient_id TEXT REFERENCES patients(patient_id) ON DELETE CASCADE,
            pathway_code TEXT,
            status TEXT NOT NULL,
            effective_time TEXT NOT NULL,
            record_json TEXT NOT NULL,
            is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (record_type, record_id, record_version)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO layer4_contract_records (
            record_type, record_id, record_version, patient_id, pathway_code,
            status, effective_time, record_json, is_current, created_at, updated_at
        )
        SELECT
            record_type, record_id, record_version, patient_id, pathway_code,
            status, effective_time, record_json, is_current, created_at, updated_at
        FROM layer4_contract_records_legacy
        """
    )
    connection.execute("DROP TABLE layer4_contract_records_legacy")
    connection.execute(
        "CREATE UNIQUE INDEX idx_layer4_contract_current "
        "ON layer4_contract_records(record_type, record_id) WHERE is_current = 1"
    )
    connection.execute(
        "CREATE INDEX idx_layer4_contract_patient_type_time "
        "ON layer4_contract_records(patient_id, record_type, effective_time)"
    )
    connection.execute(
        "CREATE INDEX idx_layer4_contract_pathway_status "
        "ON layer4_contract_records(pathway_code, record_type, status)"
    )


def reset_demo(db_path: Path | str | None = None) -> None:
    path = Path(db_path) if db_path is not None else get_settings().db_path
    if path.exists():
        path.unlink()
    initialize_database(path)
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO audit_events (
                event_id, patient_id, entity_type, entity_id, event_type,
                actor_type, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit_{uuid4().hex}",
                "P-DEMO-001",
                "Demo",
                "local_demo",
                "demo_reset",
                "demo_operator",
                json.dumps({"synthetic_only": True}, ensure_ascii=False),
                utc_now_iso(),
            ),
        )


def _seed_demo_patient(connection: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    connection.execute(
        """
        INSERT INTO patients (
            patient_id, display_name, synthetic, pathway_code,
            enrollment_date, next_visit_date, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(patient_id) DO NOTHING
        """,
        (
            "P-DEMO-001",
            "陈女士（合成）",
            1,
            "GLP1-14D",
            now.date().isoformat(),
            (now.date() + timedelta(days=14)).isoformat(),
            "active",
            now.isoformat(),
        ),
    )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS demo_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    synthetic INTEGER NOT NULL CHECK (synthetic IN (0, 1)),
    pathway_code TEXT NOT NULL,
    enrollment_date TEXT NOT NULL,
    next_visit_date TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followup_messages (
    message_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    message_text TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    source TEXT NOT NULL,
    processing_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fhir_questionnaire_responses (
    resource_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    message_id TEXT NOT NULL UNIQUE REFERENCES followup_messages(message_id) ON DELETE CASCADE,
    resource_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS care_sessions (
    session_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    pathway_code TEXT NOT NULL,
    pathway_version TEXT NOT NULL,
    questionnaire_canonical TEXT NOT NULL,
    questionnaire_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('in_progress', 'completed', 'stopped', 'entered_in_error')
    ),
    answers_json TEXT NOT NULL,
    questionnaire_response_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES care_sessions(session_id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    input_text TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_json TEXT NOT NULL,
    status TEXT NOT NULL,
    model_provider TEXT,
    model_name TEXT,
    prompt_version TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS conversation_action_resolutions (
    action_id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES care_sessions(session_id) ON DELETE CASCADE,
    response_run_id TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected', 'unsure')),
    option_id TEXT,
    response_text TEXT,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmed_answer_contexts (
    answer_context_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES care_sessions(session_id) ON DELETE CASCADE,
    link_id TEXT NOT NULL,
    answer_json TEXT NOT NULL,
    source_run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    followup_occurrence_id TEXT NOT NULL,
    patient_timezone TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    effective_start TEXT,
    effective_end TEXT,
    temporal_kind TEXT,
    resolution_basis TEXT,
    raw_text TEXT NOT NULL,
    terminology_match_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmed_symptom_reports (
    report_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES care_sessions(session_id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL,
    preferred_zh TEXT NOT NULL,
    coding_json TEXT NOT NULL,
    terminology_match_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    evidence_text TEXT NOT NULL,
    evidence_start INTEGER NOT NULL,
    evidence_end INTEGER NOT NULL,
    followup_occurrence_id TEXT NOT NULL,
    patient_timezone TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    effective_start TEXT,
    effective_end TEXT,
    temporal_kind TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    created_at TEXT NOT NULL,
    UNIQUE(session_id, concept_id)
);

CREATE TABLE IF NOT EXISTS fhir_observations (
    observation_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    questionnaire_response_id TEXT NOT NULL
        REFERENCES fhir_questionnaire_responses(resource_id) ON DELETE CASCADE,
    effective_time TEXT NOT NULL,
    resource_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_evidence (
    observation_id TEXT PRIMARY KEY
        REFERENCES fhir_observations(observation_id) ON DELETE CASCADE,
    confidence_tier TEXT NOT NULL CHECK (
        confidence_tier IN (
            'patient_confirmed', 'verbatim_explicit',
            'model_inferred', 'needs_human_review'
        )
    ),
    evidence_text TEXT NOT NULL,
    evidence_start INTEGER NOT NULL,
    evidence_end INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'pathway_monitored',
    terminology_match_json TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    trigger_rule_id TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('open', 'acknowledged', 'escalated', 'resolved')
    ),
    sla_due_at TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_reason TEXT
);

CREATE TABLE IF NOT EXISTS alert_actions (
    action_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES alerts(alert_id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    summary_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    patient_id TEXT REFERENCES patients(patient_id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layer4_fhir_resources (
    resource_type TEXT NOT NULL CHECK (
        resource_type IN ('Communication', 'Provenance', 'Task')
    ),
    resource_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    patient_id TEXT REFERENCES patients(patient_id) ON DELETE CASCADE,
    status TEXT,
    clinical_time TEXT NOT NULL,
    resource_json TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (resource_type, resource_id, version_id)
);

CREATE TABLE IF NOT EXISTS layer4_contract_records (
    record_type TEXT NOT NULL CHECK (
        record_type IN (
            'clinical_rule', 'memory_event', 'timeline_event',
            'revision_link', 'summary_draft', 'doctor_review',
            'state_snapshot'
        )
    ),
    record_id TEXT NOT NULL,
    record_version TEXT NOT NULL,
    patient_id TEXT REFERENCES patients(patient_id) ON DELETE CASCADE,
    pathway_code TEXT,
    status TEXT NOT NULL,
    effective_time TEXT NOT NULL,
    record_json TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (record_type, record_id, record_version)
);

CREATE INDEX IF NOT EXISTS idx_messages_patient_time
ON followup_messages(patient_id, submitted_at);
CREATE INDEX IF NOT EXISTS idx_observations_patient_time
ON fhir_observations(patient_id, effective_time);
CREATE INDEX IF NOT EXISTS idx_questionnaire_responses_patient_time
ON fhir_questionnaire_responses(patient_id, created_at);
CREATE INDEX IF NOT EXISTS idx_care_sessions_patient_status
ON care_sessions(patient_id, status, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_care_session
ON care_sessions(patient_id, pathway_code) WHERE status = 'in_progress';
CREATE INDEX IF NOT EXISTS idx_agent_runs_session_time
ON agent_runs(session_id, completed_at);
CREATE INDEX IF NOT EXISTS idx_conversation_resolutions_session
ON conversation_action_resolutions(session_id, resolved_at);
CREATE INDEX IF NOT EXISTS idx_answer_contexts_session_link
ON confirmed_answer_contexts(session_id, link_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_answer_context
ON confirmed_answer_contexts(session_id, link_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_symptom_reports_session_status
ON confirmed_symptom_reports(session_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_patient_status
ON alerts(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_patient_time
ON audit_events(patient_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_layer4_fhir_current
ON layer4_fhir_resources(resource_type, resource_id) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_layer4_fhir_patient_type_time
ON layer4_fhir_resources(patient_id, resource_type, clinical_time);
CREATE INDEX IF NOT EXISTS idx_layer4_fhir_patient_status
ON layer4_fhir_resources(patient_id, resource_type, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_layer4_contract_current
ON layer4_contract_records(record_type, record_id) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_layer4_contract_patient_type_time
ON layer4_contract_records(patient_id, record_type, effective_time);
CREATE INDEX IF NOT EXISTS idx_layer4_contract_pathway_status
ON layer4_contract_records(pathway_code, record_type, status);
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from continucare.followup_planning import (
    ActorRole,
    CandidateConfirmationStatus,
    CandidateSourceKind,
    ClinicalContextCandidate,
    ConcurrentCandidateVersionError,
    FollowupPlanningDisabledError,
    FollowupPlanningService,
    PlanningActor,
    SQLiteFollowupPlanningRepository,
)
from continucare.followup_planning.models import PlanningAuditEvent


NOW = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
DOCTOR = PlanningActor(
    actor_id="Practitioner/synthetic-followup-doctor", role=ActorRole.DOCTOR
)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _candidate(version=1):
    return ClinicalContextCandidate(
        candidate_id="candidate-synthetic-repository",
        version=version,
        case_id="case-synthetic-repository",
        patient_id="Patient/synthetic-repository",
        source_reference="SyntheticCase/case-synthetic-repository/entry/1",
        source_excerpt_snapshot="合成原文。",
        source_kind=CandidateSourceKind.PATIENT_REPORT,
        extracted_text="合成患者自述",
        confirmation_status=CandidateConfirmationStatus.EXTRACTED,
        created_at=NOW,
        synthetic=True,
    )


def _audit(event_id="audit-synthetic-repository"):
    return PlanningAuditEvent(
        event_id=event_id,
        entity_type="clinical_context_candidate",
        entity_id="candidate-synthetic-repository",
        event_type="candidate_extracted",
        actor_id=DOCTOR.actor_id,
        actor_role=DOCTOR.role,
        occurred_at=NOW,
    )


def test_disabled_path_mode_creates_no_directory_file_or_table(tmp_path):
    db_path = tmp_path / "not-created" / "followup.db"
    service = FollowupPlanningService.from_storage(db_path)

    assert not db_path.parent.exists()
    with pytest.raises(FollowupPlanningDisabledError):
        service.get_doctor_plan(DOCTOR, "plan-missing")
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_disabled_caller_connection_creates_no_tables():
    connection = sqlite3.connect(":memory:")
    service = FollowupPlanningService.from_storage(connection)

    with pytest.raises(FollowupPlanningDisabledError):
        service.get_doctor_plan(DOCTOR, "plan-missing")
    assert _table_names(connection) == set()
    connection.execute("SELECT 1")  # The caller-owned connection remains open.


def test_explicit_initialization_is_idempotent_and_uses_only_module_tables(tmp_path):
    repository = SQLiteFollowupPlanningRepository(tmp_path / "followup.db")
    repository.initialize()
    repository.initialize()

    with sqlite3.connect(tmp_path / "followup.db") as connection:
        tables = _table_names(connection)

    assert tables
    assert all(name.startswith("followup_planning_") for name in tables)
    assert not any(
        token in name
        for name in tables
        for token in (
            "questionnaire_response",
            "observation",
            "task",
            "alert",
            "communication",
            "send_queue",
        )
    )


def test_candidate_versions_are_append_only_and_concurrency_checked(tmp_path):
    repository = SQLiteFollowupPlanningRepository(tmp_path / "followup.db")
    repository.initialize()
    repository.append_candidate(
        _candidate(), expected_version=0, audit_event=_audit()
    )

    with pytest.raises(ConcurrentCandidateVersionError):
        repository.append_candidate(
            _candidate(),
            expected_version=0,
            audit_event=_audit("audit-synthetic-stale"),
        )

    assert repository.get_candidate("candidate-synthetic-repository").version == 1
    assert len(
        repository.list_audit_events(
            entity_type="clinical_context_candidate",
            entity_id="candidate-synthetic-repository",
        )
    ) == 1

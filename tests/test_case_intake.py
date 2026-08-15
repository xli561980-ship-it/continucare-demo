from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from continucare.case_intake import (
    CaseAuditAction,
    CaseAuditActorType,
    CaseAuthor,
    CaseDraft,
    CaseIntakeDisabledError,
    CaseIntakeMode,
    CaseIntakeService,
    CaseRecord,
    CaseRevisionInput,
    CaseSourceType,
    ClinicianAuthoredText,
    ConcurrentCaseVersionError,
    HospitalMockRecord,
    IdempotencyConflict,
    ManualCaseInput,
    NonSyntheticPatientError,
    SyntheticHospitalMockAdapter,
    canonical_hospital_idempotency_key,
    configured_case_intake_mode,
)
from continucare.db import connect, initialize_database, reset_demo
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.services.competition_demo import (
    CompetitionDemoStage,
    read_competition_demo,
)


NOW = datetime(2026, 8, 15, 10, 30, tzinfo=timezone.utc)
AUTHOR = CaseAuthor(
    practitioner_id="Practitioner/synthetic-doctor-intake",
    display_name="合成录入医生",
)


def _note(text: str, *, author=AUTHOR, at=NOW) -> ClinicianAuthoredText:
    return ClinicianAuthoredText(
        text=text,
        practitioner_id=author.practitioner_id,
        authored_at=at,
    )


def _manual_input(**updates) -> ManualCaseInput:
    values = {
        "patient_id": DEMO_PATIENT_ID,
        "encounter_id": "ENC-MANUAL-001",
        "pathway_code": "GLP1-14D",
        "pathway_version": "1.0.0",
        "author": AUTHOR,
        "recorded_at": NOW,
        "encounter_date": date(2026, 8, 15),
        "chief_complaint": "合成主诉：复诊前回顾耐受情况。",
        "present_illness": "合成现病史，不代表患者本人确认。",
        "past_history": "合成既往史。",
        "medication_history": ("合成药物 A",),
        "allergy_history": ("合成过敏原 B",),
        "test_summary": "合成检查摘要。",
        "clinician_assessment": _note("医生手工录入的合成评估。"),
        "followup_plan": _note("医生手工录入的合成随访计划。"),
    }
    values.update(updates)
    return ManualCaseInput(**values)


def _hospital_record(**updates) -> HospitalMockRecord:
    values = {
        "patient_id": DEMO_PATIENT_ID,
        "encounter_id": "ENC-HOSPITAL-001",
        "external_record_id": "EXT-SYNTHETIC-001",
        "pathway_code": "GLP1-14D",
        "pathway_version": "1.0.0",
        "author": AUTHOR,
        "recorded_at": NOW,
        "encounter_date": date(2026, 8, 14),
        "chief_complaint": "合成医院记录主诉。",
        "present_illness": "合成医院记录现病史。",
        "medication_history": ("合成药物 H",),
        "allergy_history": (),
        "test_summary": "合成医院检查摘要。",
        "clinician_assessment": _note("来源系统中由医生手工记录的合成评估。"),
        "followup_plan": None,
    }
    values.update(updates)
    return HospitalMockRecord(**values)


def _table_names(db_path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def _clinical_counts(db_path) -> dict[str, int]:
    with connect(db_path) as connection:
        return {
            "questionnaire_responses": connection.execute(
                "SELECT COUNT(*) FROM fhir_questionnaire_responses"
            ).fetchone()[0],
            "observations": connection.execute(
                "SELECT COUNT(*) FROM fhir_observations"
            ).fetchone()[0],
            "alerts": connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
            "tasks": connection.execute(
                "SELECT COUNT(*) FROM layer4_fhir_resources "
                "WHERE resource_type = 'Task'"
            ).fetchone()[0],
        }


def test_default_disabled_mode_is_side_effect_free(tmp_path):
    absent_db = tmp_path / "disabled-must-not-create.db"
    absent_service = CaseIntakeService.from_db(absent_db, environment={})
    assert absent_service.mode == CaseIntakeMode.DISABLED
    assert not absent_db.exists()

    db_path = tmp_path / "disabled.db"
    initialize_database(db_path)
    before = _table_names(db_path)

    service = CaseIntakeService.from_db(db_path, environment={})

    assert configured_case_intake_mode({}) == CaseIntakeMode.DISABLED
    assert service.mode == CaseIntakeMode.DISABLED
    assert _table_names(db_path) == before
    assert "case_intake_case_versions" not in before
    with pytest.raises(CaseIntakeDisabledError):
        service.create_manual(_manual_input())


def test_enabled_initialization_is_additive_and_manual_is_standardized(tmp_path):
    db_path = tmp_path / "manual.db"
    initialize_database(db_path)
    baseline_tables = _table_names(db_path)
    before_clinical = _clinical_counts(db_path)
    service = CaseIntakeService.from_db(
        db_path,
        mode="manual",
        clock=lambda: NOW,
        case_id_factory=lambda: "case_manual_001",
    )

    record = service.create_manual(_manual_input())

    assert isinstance(record, CaseRecord)
    assert record.case_id == "case_manual_001"
    assert record.source_type == CaseSourceType.MANUAL
    assert record.source_system == "continucare_case_intake"
    assert record.external_record_id is None
    assert record.idempotency_key is None
    assert record.author == AUTHOR
    assert record.version == 1
    assert record.synthetic is True
    assert record.clinician_assessment.entry_method == "clinician_manual"
    assert _clinical_counts(db_path) == before_clinical
    assert _table_names(db_path) == baseline_tables | {
        "case_intake_case_versions",
        "case_intake_audit_events",
    }
    assert read_competition_demo(db_path).stage == CompetitionDemoStage.NOT_STARTED
    audits = service.repository.list_audit_events(record.case_id)
    assert [item.action for item in audits] == [CaseAuditAction.MANUAL_CREATED]
    assert audits[0].actor_id == AUTHOR.practitioner_id


def test_manual_and_hospital_mock_return_the_same_standard_model(tmp_path):
    db_path = tmp_path / "same-model.db"
    initialize_database(db_path)
    manual = CaseIntakeService.from_db(
        db_path,
        mode=CaseIntakeMode.MANUAL,
        clock=lambda: NOW,
        case_id_factory=lambda: "case_manual_same_model",
    ).create_manual(_manual_input())
    hospital = CaseIntakeService.from_db(
        db_path,
        mode=CaseIntakeMode.HOSPITAL_MOCK,
        clock=lambda: NOW,
        case_id_factory=lambda: "case_hospital_same_model",
    ).import_cases(SyntheticHospitalMockAdapter([_hospital_record()]))[0].record

    assert type(manual) is CaseRecord
    assert type(hospital) is CaseRecord
    assert set(type(manual).model_fields) == set(type(hospital).model_fields)
    assert hospital.source_type == CaseSourceType.HOSPITAL_MOCK
    assert hospital.external_record_id == "EXT-SYNTHETIC-001"
    assert hospital.author == AUTHOR


def test_hospital_mock_idempotency_is_stable_and_does_not_duplicate(tmp_path):
    db_path = tmp_path / "idempotent.db"
    initialize_database(db_path)
    ids = iter(("case_imported_001", "case_must_not_be_used"))
    service = CaseIntakeService.from_db(
        db_path,
        mode="hospital_mock",
        clock=lambda: NOW,
        case_id_factory=lambda: next(ids),
    )
    first_adapter = SyntheticHospitalMockAdapter([_hospital_record()])
    second_adapter = SyntheticHospitalMockAdapter([_hospital_record()])

    first = service.import_cases(first_adapter)[0]
    replay = service.import_cases(second_adapter)[0]

    assert first.created is True
    assert replay.created is False
    assert replay.record == first.record
    assert next(ids) == "case_must_not_be_used"
    assert (
        first_adapter.iter_cases()[0].idempotency_key
        == second_adapter.iter_cases()[0].idempotency_key
    )
    assert len(service.list_versions(first.record.case_id)) == 1
    with connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM case_intake_case_versions"
        ).fetchone()[0] == 1
    assert [
        item.action for item in service.repository.list_audit_events(first.record.case_id)
    ] == [CaseAuditAction.HOSPITAL_IMPORTED, CaseAuditAction.IDEMPOTENT_REPLAY]


def test_forged_hospital_key_cannot_duplicate_one_external_record(tmp_path):
    db_path = tmp_path / "forged-idempotency.db"
    initialize_database(db_path)
    service = CaseIntakeService.from_db(
        db_path,
        mode="hospital_mock",
        clock=lambda: NOW,
        case_id_factory=lambda: "case_external_identity_001",
    )
    adapter = SyntheticHospitalMockAdapter([_hospital_record()])
    canonical_draft = adapter.iter_cases()[0]
    first = service.import_cases(adapter)[0]
    forged = canonical_draft.model_copy(
        update={"idempotency_key": "mock_" + "f" * 64}
    )

    assert canonical_draft.idempotency_key == canonical_hospital_idempotency_key(
        adapter.source_system, canonical_draft.external_record_id
    )
    with pytest.raises(ValidationError, match="canonical"):
        CaseDraft.model_validate(forged.model_dump(mode="python"))

    class ForgedSource:
        source_type = CaseSourceType.HOSPITAL_MOCK
        source_system = adapter.source_system

        def iter_cases(self):
            return (forged,)

    with pytest.raises(ValidationError, match="canonical"):
        service.import_cases(ForgedSource())

    # Even a direct repository caller that bypasses model validation is looked
    # up by stable external identity and cannot create a second case.
    with pytest.raises(IdempotencyConflict, match="external hospital record"):
        service.repository.create(
            forged,
            case_id_factory=lambda: "case_repository_bypass",
            occurred_at=NOW,
            actor_type=CaseAuditActorType.SOURCE_ADAPTER,
            actor_id=adapter.source_system,
        )

    # The database index remains the final guard if both higher layers are bypassed.
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM case_intake_case_versions WHERE case_id = ?",
            (first.record.case_id,),
        ).fetchone()
        duplicate = dict(row)
        duplicate["case_id"] = "case_raw_sql_bypass"
        duplicate["idempotency_key"] = "mock_" + "e" * 64
        columns = tuple(duplicate)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                f"INSERT INTO case_intake_case_versions "
                f"({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                tuple(duplicate[column] for column in columns),
            )
        indexes = {
            item["name"]: item
            for item in connection.execute(
                "PRAGMA index_list(case_intake_case_versions)"
            ).fetchall()
        }
        external_index = indexes["idx_case_intake_external_record_identity"]
        assert external_index["unique"] == 1
        assert [
            item["name"]
            for item in connection.execute(
                "PRAGMA index_info(idx_case_intake_external_record_identity)"
            ).fetchall()
        ] == ["source_system", "external_record_id"]

    with connect(db_path) as connection:
        duplicate_external_record_rows = connection.execute(
            """
            SELECT COUNT(*) FROM case_intake_case_versions
            WHERE source_type = 'hospital_mock'
              AND source_system = ? AND external_record_id = ?
            """,
            (adapter.source_system, canonical_draft.external_record_id),
        ).fetchone()[0]
    assert duplicate_external_record_rows == 1


def test_same_external_record_with_changed_content_is_rejected(tmp_path):
    db_path = tmp_path / "idempotency-conflict.db"
    initialize_database(db_path)
    service = CaseIntakeService.from_db(
        db_path,
        mode="hospital_mock",
        clock=lambda: NOW,
        case_id_factory=lambda: "case_conflict_001",
    )
    service.import_cases(SyntheticHospitalMockAdapter([_hospital_record()]))

    changed = _hospital_record(chief_complaint="同一外部记录 ID 下的不同合成内容。")
    with pytest.raises(IdempotencyConflict, match="different case content"):
        service.import_cases(SyntheticHospitalMockAdapter([changed]))

    with connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM case_intake_case_versions"
        ).fetchone()[0] == 1


def test_doctor_revision_appends_version_and_preserves_hospital_source(tmp_path):
    db_path = tmp_path / "versions.db"
    initialize_database(db_path)
    service = CaseIntakeService.from_db(
        db_path,
        mode="hospital_mock",
        clock=lambda: NOW + timedelta(minutes=5),
        case_id_factory=lambda: "case_versioned_001",
    )
    original = service.import_cases(
        SyntheticHospitalMockAdapter([_hospital_record()])
    )[0].record
    revision_time = NOW + timedelta(minutes=4)
    revision = CaseRevisionInput(
        author=AUTHOR,
        recorded_at=revision_time,
        encounter_date=original.encounter_date,
        chief_complaint="医生核对后手工修订的合成主诉。",
        present_illness=original.present_illness,
        medication_history=original.medication_history,
        allergy_history=original.allergy_history,
        test_summary=original.test_summary,
        clinician_assessment=_note("医生手工修订评估。", at=revision_time),
        followup_plan=_note("医生手工补充计划。", at=revision_time),
    )

    revised = service.revise(
        original.case_id, expected_version=1, revision=revision
    )

    assert revised.version == 2
    assert revised.supersedes_case_version == 1
    assert revised.source_type == CaseSourceType.MANUAL
    assert revised.external_record_id is None
    assert revised.idempotency_key is None
    versions = service.list_versions(original.case_id)
    assert versions == [original, revised]
    assert versions[0].source_type == CaseSourceType.HOSPITAL_MOCK
    assert versions[0].external_record_id == "EXT-SYNTHETIC-001"
    replay = service.import_cases(
        SyntheticHospitalMockAdapter([_hospital_record()])
    )[0]
    assert replay.created is False
    assert replay.record == original
    assert service.get_latest(original.case_id) == revised
    assert service.list_versions(original.case_id) == [original, revised]
    assert [
        item.action for item in service.repository.list_audit_events(original.case_id)
    ] == [
        CaseAuditAction.HOSPITAL_IMPORTED,
        CaseAuditAction.CLINICIAN_REVISED,
        CaseAuditAction.IDEMPOTENT_REPLAY,
    ]
    with pytest.raises(ConcurrentCaseVersionError, match="expected case version 1"):
        service.revise(original.case_id, expected_version=1, revision=revision)


def test_only_synthetic_patients_and_strict_declared_fields_are_accepted(tmp_path):
    with pytest.raises(ValidationError):
        _manual_input(synthetic=False)
    with pytest.raises(ValidationError):
        _hospital_record(synthetic=False)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ManualCaseInput(**(_manual_input().model_dump() | {"diagnosis": "不允许"}))
    with pytest.raises(ValidationError, match="timezone"):
        _manual_input(recorded_at=datetime(2026, 8, 15, 10, 30))

    db_path = tmp_path / "non-synthetic.db"
    initialize_database(db_path)
    with connect(db_path) as connection:
        connection.execute(
            "UPDATE patients SET synthetic = 0 WHERE patient_id = ?",
            (DEMO_PATIENT_ID,),
        )
    service = CaseIntakeService.from_db(db_path, mode="manual", clock=lambda: NOW)
    with pytest.raises(NonSyntheticPatientError, match="marked synthetic"):
        service.create_manual(_manual_input())


def test_source_clinical_fields_must_retain_manual_clinician_attribution():
    other = CaseAuthor(
        practitioner_id="Practitioner/another-synthetic-doctor",
        display_name="另一位合成医生",
    )
    with pytest.raises(ValidationError, match="must match the source-record author"):
        _hospital_record(
            clinician_assessment=_note("来源不匹配。", author=other)
        )


def test_case_tables_can_be_reinitialized_after_demo_reset(tmp_path):
    db_path = tmp_path / "reset.db"
    initialize_database(db_path)
    ids = iter(("case_before_reset", "case_after_reset"))
    service = CaseIntakeService.from_db(
        db_path,
        mode="manual",
        clock=lambda: NOW,
        case_id_factory=lambda: next(ids),
    )
    service.create_manual(_manual_input())

    reset_demo(db_path)
    assert "case_intake_case_versions" not in _table_names(db_path)

    service.reinitialize()
    after = service.create_manual(
        _manual_input(encounter_id="ENC-AFTER-RESET")
    )
    assert after.case_id == "case_after_reset"
    assert service.list_patient_cases(DEMO_PATIENT_ID) == [after]
    assert _clinical_counts(db_path) == {
        "questionnaire_responses": 0,
        "observations": 0,
        "alerts": 0,
        "tasks": 0,
    }

from __future__ import annotations

from datetime import datetime, time, timezone

import pytest
from pydantic import ValidationError

from continucare.followup_planning import (
    CandidateConfirmationStatus,
    CandidateSourceKind,
    ClinicalContextCandidate,
    EvidenceReviewStatus,
    EvidenceUse,
    MonitoringDefinitionOrigin,
    MonitoringItemDefinition,
    ResponseType,
    ScheduleApprovalStatus,
    ScheduleTemplate,
    ScheduleValues,
)


NOW = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)


def _candidate(**updates):
    values = {
        "candidate_id": "candidate-synthetic-1",
        "version": 1,
        "case_id": "case-synthetic-1",
        "patient_id": "Patient/synthetic-1",
        "source_reference": "SyntheticCase/case-synthetic-1/entry/1",
        "source_excerpt_snapshot": "合成原文快照。",
        "source_kind": CandidateSourceKind.PATIENT_REPORT,
        "extracted_text": "合成患者自述",
        "coding": None,
        "confirmation_status": CandidateConfirmationStatus.EXTRACTED,
        "confirmed_by": None,
        "confirmed_at": None,
        "created_at": NOW,
        "synthetic": True,
    }
    values.update(updates)
    return ClinicalContextCandidate(**values)


def test_candidate_retains_exact_source_and_requires_explicit_confirmation():
    candidate = _candidate()
    assert candidate.source_reference.endswith("/entry/1")
    assert candidate.source_excerpt_snapshot == "合成原文快照。"

    with pytest.raises(ValidationError, match="confirmed_by"):
        _candidate(
            confirmation_status=CandidateConfirmationStatus.DOCTOR_CONFIRMED,
            version=2,
        )


def test_candidate_contract_is_synthetic_only():
    with pytest.raises(ValidationError, match="Input should be True"):
        _candidate(synthetic=False)


def test_monitoring_definition_cannot_contain_schedule_or_unsafe_use():
    field_names = MonitoringItemDefinition.model_fields
    assert "frequency" not in field_names
    assert "interval_days" not in field_names
    assert "duration_days" not in field_names
    assert "schedule" not in field_names

    with pytest.raises(ValidationError, match="not executable"):
        MonitoringItemDefinition(
            definition_id="definition-synthetic-1",
            version=1,
            label="合成检验",
            patient_prompt="请完成一项合成检验。",
            response_type=ResponseType.TEXT,
            permitted_use=EvidenceUse.LAB_ORDER,
            review_status=EvidenceReviewStatus.NOT_ASSESSED,
            origin=MonitoringDefinitionOrigin.DOCTOR_AUTHORED,
            authored_by="Practitioner/synthetic-doctor",
            created_at=NOW,
        )


def test_schedule_has_no_hidden_defaults_and_requires_iana_timezone():
    with pytest.raises(ValidationError):
        ScheduleValues(timezone="Europe/Berlin")
    with pytest.raises(ValidationError, match="IANA"):
        ScheduleValues(
            interval_days=1,
            times_of_day=(time(9),),
            duration_days=2,
            timezone="Hospital Local Time",
        )


def test_approved_template_is_explicitly_synthetic_demo_only():
    with pytest.raises(ValidationError, match="approval provenance"):
        ScheduleTemplate(
            schedule_id="schedule-synthetic-1",
            version=1,
            values=ScheduleValues(
                interval_days=1,
                times_of_day=(time(9),),
                duration_days=2,
                timezone="Europe/Berlin",
            ),
            approval_status=ScheduleApprovalStatus.APPROVED,
            authored_by="Practitioner/synthetic-author",
            created_at=NOW,
            data_classification="synthetic_demo",
            approval_scope="demo_only",
        )

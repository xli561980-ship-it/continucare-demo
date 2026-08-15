"""Optional, disabled-by-default synthetic Case Intake package."""

from continucare.case_intake.hospital_mock import (
    HospitalMockRecord,
    SyntheticHospitalMockAdapter,
)
from continucare.case_intake.models import (
    CaseAuditAction,
    CaseAuditActorType,
    CaseAuditEvent,
    CaseAuthor,
    CaseDraft,
    CaseImportResult,
    CaseIntakeMode,
    CaseRecord,
    CaseRevisionInput,
    CaseSourceType,
    CaseStatus,
    ClinicianAuthoredText,
    ManualCaseInput,
    canonical_hospital_idempotency_key,
)
from continucare.case_intake.repository import (
    ConcurrentCaseVersionError,
    IdempotencyConflict,
    NonSyntheticPatientError,
    SQLiteCaseRepository,
)
from continucare.case_intake.service import (
    CaseIntakeDisabledError,
    CaseIntakeModeError,
    CaseIntakeService,
    configured_case_intake_mode,
)
from continucare.case_intake.sources import CaseSource

__all__ = [
    "CaseAuditAction",
    "CaseAuditActorType",
    "CaseAuditEvent",
    "CaseAuthor",
    "CaseDraft",
    "CaseImportResult",
    "CaseIntakeDisabledError",
    "CaseIntakeMode",
    "CaseIntakeModeError",
    "CaseIntakeService",
    "CaseRecord",
    "CaseRevisionInput",
    "CaseSource",
    "CaseSourceType",
    "CaseStatus",
    "ClinicianAuthoredText",
    "ConcurrentCaseVersionError",
    "HospitalMockRecord",
    "IdempotencyConflict",
    "ManualCaseInput",
    "NonSyntheticPatientError",
    "SQLiteCaseRepository",
    "SyntheticHospitalMockAdapter",
    "configured_case_intake_mode",
    "canonical_hospital_idempotency_key",
]

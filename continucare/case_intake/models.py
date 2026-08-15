"""Strict, synthetic-only models for the optional Case Intake boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
Narrative = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseIntakeMode(StrEnum):
    DISABLED = "disabled"
    MANUAL = "manual"
    HOSPITAL_MOCK = "hospital_mock"


class CaseSourceType(StrEnum):
    MANUAL = "manual"
    HOSPITAL_MOCK = "hospital_mock"


class CaseStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ENTERED_IN_ERROR = "entered_in_error"


class CaseAuditAction(StrEnum):
    MANUAL_CREATED = "manual_created"
    HOSPITAL_IMPORTED = "hospital_imported"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    CLINICIAN_REVISED = "clinician_revised"


class CaseAuditActorType(StrEnum):
    DOCTOR = "doctor"
    SOURCE_ADAPTER = "source_adapter"


def canonical_hospital_idempotency_key(
    source_system: str, external_record_id: str
) -> str:
    """Derive the only accepted hospital_mock key from stable external identity."""

    if not source_system or not external_record_id:
        raise ValueError(
            "source_system and external_record_id are required for idempotency"
        )
    stable_material = f"{source_system}\x00{external_record_id}"
    return "mock_" + hashlib.sha256(stable_material.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


class CaseAuthor(StrictModel):
    """The clinician who authored the case content, not the importing system."""

    practitioner_id: Identifier
    display_name: ShortText
    role: Literal["doctor"] = "doctor"


class ClinicianAuthoredText(StrictModel):
    """A field that is explicitly documented by a clinician, never inferred."""

    text: Narrative
    practitioner_id: Identifier
    authored_at: datetime
    entry_method: Literal["clinician_manual"] = "clinician_manual"

    @field_validator("authored_at")
    @classmethod
    def authored_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "authored_at")


class CaseClinicalContent(StrictModel):
    encounter_date: date
    chief_complaint: Narrative
    present_illness: Narrative | None = None
    past_history: Narrative | None = None
    medication_history: tuple[ShortText, ...] = ()
    allergy_history: tuple[ShortText, ...] = ()
    test_summary: Narrative | None = None
    clinician_assessment: ClinicianAuthoredText | None = None
    followup_plan: ClinicianAuthoredText | None = None
    status: CaseStatus = CaseStatus.ACTIVE

    @field_validator("medication_history", "allergy_history")
    @classmethod
    def list_values_must_be_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("history entries must be unique")
        return values


class CaseDraft(CaseClinicalContent):
    """The one normalized model accepted from every Case Intake source."""

    patient_id: Identifier
    encounter_id: Identifier
    pathway_code: Identifier | None = None
    pathway_version: Identifier | None = None
    source_type: CaseSourceType
    source_system: Identifier
    external_record_id: Identifier | None = None
    idempotency_key: Identifier | None = None
    author: CaseAuthor
    recorded_at: datetime
    synthetic: Literal[True] = True

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "recorded_at")

    @model_validator(mode="after")
    def validate_source_and_authorship(self) -> "CaseDraft":
        if (self.pathway_code is None) != (self.pathway_version is None):
            raise ValueError(
                "pathway_code and pathway_version must both be present or absent"
            )
        if self.source_type == CaseSourceType.HOSPITAL_MOCK:
            if self.external_record_id is None or self.idempotency_key is None:
                raise ValueError(
                    "hospital_mock records require external_record_id and idempotency_key"
                )
            expected_key = canonical_hospital_idempotency_key(
                self.source_system, self.external_record_id
            )
            if self.idempotency_key != expected_key:
                raise ValueError(
                    "hospital_mock idempotency_key must match the canonical "
                    "source_system + external_record_id identity"
                )
        elif self.external_record_id is not None or self.idempotency_key is not None:
            raise ValueError(
                "manual records cannot claim hospital external identifiers"
            )
        for field_name in ("clinician_assessment", "followup_plan"):
            note = getattr(self, field_name)
            if note is not None and note.practitioner_id != self.author.practitioner_id:
                raise ValueError(
                    f"{field_name} practitioner must match the case author"
                )
        return self


class ManualCaseInput(CaseClinicalContent):
    """Doctor-entered input before it receives normalized source metadata."""

    patient_id: Identifier
    encounter_id: Identifier
    pathway_code: Identifier | None = None
    pathway_version: Identifier | None = None
    author: CaseAuthor
    recorded_at: datetime
    synthetic: Literal[True] = True

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "recorded_at")

    @model_validator(mode="after")
    def validate_manual_input(self) -> "ManualCaseInput":
        if (self.pathway_code is None) != (self.pathway_version is None):
            raise ValueError(
                "pathway_code and pathway_version must both be present or absent"
            )
        for field_name in ("clinician_assessment", "followup_plan"):
            note = getattr(self, field_name)
            if note is not None and note.practitioner_id != self.author.practitioner_id:
                raise ValueError(
                    f"{field_name} practitioner must match the case author"
                )
        return self


class CaseRevisionInput(CaseClinicalContent):
    """A clinician-authored replacement body for a new immutable case version."""

    author: CaseAuthor
    recorded_at: datetime
    synthetic: Literal[True] = True

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "recorded_at")

    @model_validator(mode="after")
    def validate_revision_authorship(self) -> "CaseRevisionInput":
        for field_name in ("clinician_assessment", "followup_plan"):
            note = getattr(self, field_name)
            if note is not None and note.practitioner_id != self.author.practitioner_id:
                raise ValueError(
                    f"{field_name} practitioner must match the revision author"
                )
        return self


class CaseRecord(CaseDraft):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: Identifier
    version: int = Field(ge=1)
    supersedes_case_version: int | None = Field(default=None, ge=1)
    content_hash: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$")
    ]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "created_at")

    @model_validator(mode="after")
    def validate_version_lineage(self) -> "CaseRecord":
        expected = None if self.version == 1 else self.version - 1
        if self.supersedes_case_version != expected:
            raise ValueError("case versions must form a contiguous immutable chain")
        return self


class CaseImportResult(StrictModel):
    record: CaseRecord
    created: bool


class CaseAuditEvent(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: Identifier
    case_id: Identifier
    case_version: int = Field(ge=1)
    action: CaseAuditAction
    actor_type: CaseAuditActorType
    actor_id: Identifier
    source_type: CaseSourceType
    occurred_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "occurred_at")


def case_content_hash(draft: CaseDraft) -> str:
    """Hash normalized source content for collision-safe idempotency checks."""

    payload = json.dumps(
        draft.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

"""Purely synthetic hospital adapter with no network or FHIR/HL7 transport."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import field_validator, model_validator

from continucare.case_intake.models import (
    CaseAuthor,
    CaseClinicalContent,
    CaseDraft,
    CaseSourceType,
    ClinicianAuthoredText,
    Identifier,
    StrictModel,
    _require_aware,
    canonical_hospital_idempotency_key,
)


class HospitalMockRecord(CaseClinicalContent):
    """Synthetic stand-in for one source-system record before normalization."""

    patient_id: Identifier
    encounter_id: Identifier
    external_record_id: Identifier
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
    def validate_mock_record(self) -> "HospitalMockRecord":
        if (self.pathway_code is None) != (self.pathway_version is None):
            raise ValueError(
                "pathway_code and pathway_version must both be present or absent"
            )
        for field_name in ("clinician_assessment", "followup_plan"):
            note: ClinicianAuthoredText | None = getattr(self, field_name)
            if note is not None and note.practitioner_id != self.author.practitioner_id:
                raise ValueError(
                    f"{field_name} practitioner must match the source-record author"
                )
        return self


class _SourceConfig(StrictModel):
    source_system: Identifier


class SyntheticHospitalMockAdapter:
    """Normalize deterministic synthetic records behind the ``CaseSource`` port."""

    source_type = CaseSourceType.HOSPITAL_MOCK

    def __init__(
        self,
        records: list[HospitalMockRecord] | tuple[HospitalMockRecord, ...],
        *,
        source_system: str = "synthetic_hospital_mock",
    ):
        self.source_system = _SourceConfig(
            source_system=source_system
        ).source_system
        self._records = tuple(records)
        external_ids = [item.external_record_id for item in self._records]
        if len(external_ids) != len(set(external_ids)):
            raise ValueError("hospital_mock records contain duplicate external_record_id")

    def iter_cases(self) -> tuple[CaseDraft, ...]:
        return tuple(self._normalize(item) for item in self._records)

    def _normalize(self, item: HospitalMockRecord) -> CaseDraft:
        return CaseDraft(
            **item.model_dump(),
            source_type=self.source_type,
            source_system=self.source_system,
            idempotency_key=canonical_hospital_idempotency_key(
                self.source_system, item.external_record_id
            ),
        )

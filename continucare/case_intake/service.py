"""Explicitly enabled orchestration for manual and mock-hospital intake."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from continucare.case_intake.models import (
    CaseAuditActorType,
    CaseDraft,
    CaseImportResult,
    CaseIntakeMode,
    CaseRecord,
    CaseRevisionInput,
    CaseSourceType,
    ManualCaseInput,
)
from continucare.case_intake.repository import SQLiteCaseRepository
from continucare.case_intake.sources import CaseSource


class CaseIntakeDisabledError(RuntimeError):
    pass


class CaseIntakeModeError(RuntimeError):
    pass


def configured_case_intake_mode(
    environment: dict[str, str] | os._Environ[str] | None = None,
) -> CaseIntakeMode:
    """Load the isolated feature flag; omission always fails closed."""

    source = environment if environment is not None else os.environ
    raw = source.get(
        "CONTINUCARE_CASE_INTAKE_MODE", CaseIntakeMode.DISABLED.value
    )
    try:
        return CaseIntakeMode(raw.strip().lower())
    except ValueError as exc:
        raise CaseIntakeModeError(
            "CONTINUCARE_CASE_INTAKE_MODE must be disabled, manual, or hospital_mock"
        ) from exc


class CaseIntakeService:
    """The only writer-facing Case Intake API.

    Construction is side-effect free. ``from_db`` initializes only the module's
    additive tables, and only when the selected mode is explicitly enabled.
    """

    MANUAL_SOURCE_SYSTEM = "continucare_case_intake"

    def __init__(
        self,
        repository: SQLiteCaseRepository,
        *,
        mode: CaseIntakeMode = CaseIntakeMode.DISABLED,
        clock: Callable[[], datetime] | None = None,
        case_id_factory: Callable[[], str] | None = None,
    ):
        self.repository = repository
        self.mode = mode
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._case_id_factory = case_id_factory or (
            lambda: f"case_{uuid4().hex}"
        )

    @classmethod
    def from_db(
        cls,
        db_path: Path | str,
        *,
        mode: CaseIntakeMode | str | None = None,
        environment: dict[str, str] | os._Environ[str] | None = None,
        clock: Callable[[], datetime] | None = None,
        case_id_factory: Callable[[], str] | None = None,
    ) -> "CaseIntakeService":
        selected = (
            configured_case_intake_mode(environment)
            if mode is None
            else CaseIntakeMode(mode)
        )
        repository = SQLiteCaseRepository(db_path)
        if selected != CaseIntakeMode.DISABLED:
            repository.initialize()
        return cls(
            repository,
            mode=selected,
            clock=clock,
            case_id_factory=case_id_factory,
        )

    def reinitialize(self) -> None:
        """Recreate additive tables after an explicit demo database reset."""

        self._require_enabled()
        self.repository.initialize()

    def create_manual(self, case: ManualCaseInput) -> CaseRecord:
        self._require_enabled()
        if self.mode != CaseIntakeMode.MANUAL:
            raise CaseIntakeModeError("manual case creation requires manual mode")
        draft = CaseDraft(
            **case.model_dump(),
            source_type=CaseSourceType.MANUAL,
            source_system=self.MANUAL_SOURCE_SYSTEM,
            external_record_id=None,
            idempotency_key=None,
        )
        record, _ = self.repository.create(
            draft,
            case_id_factory=self._case_id_factory,
            occurred_at=self._now(),
            actor_type=CaseAuditActorType.DOCTOR,
            actor_id=case.author.practitioner_id,
        )
        return record

    def import_cases(self, source: CaseSource) -> list[CaseImportResult]:
        self._require_enabled()
        if self.mode != CaseIntakeMode.HOSPITAL_MOCK:
            raise CaseIntakeModeError(
                "hospital source import requires hospital_mock mode"
            )
        if source.source_type != CaseSourceType.HOSPITAL_MOCK:
            raise ValueError("this release accepts only hospital_mock sources")

        results = []
        for source_draft in source.iter_cases():
            # Revalidate serialized values so a custom adapter cannot bypass
            # CaseDraft validators with model_copy/model_construct.
            draft = CaseDraft.model_validate(source_draft.model_dump(mode="python"))
            if draft.source_type != source.source_type:
                raise ValueError("source and normalized draft source_type differ")
            if draft.source_system != source.source_system:
                raise ValueError("source and normalized draft source_system differ")
            record, created = self.repository.create(
                draft,
                case_id_factory=self._case_id_factory,
                occurred_at=self._now(),
                actor_type=CaseAuditActorType.SOURCE_ADAPTER,
                actor_id=source.source_system,
            )
            results.append(CaseImportResult(record=record, created=created))
        return results

    def revise(
        self,
        case_id: str,
        *,
        expected_version: int,
        revision: CaseRevisionInput,
    ) -> CaseRecord:
        self._require_enabled()
        current = self.repository.get_latest(case_id)
        if current is None:
            raise LookupError(f"case {case_id!r} was not found")
        draft = CaseDraft(
            **revision.model_dump(),
            patient_id=current.patient_id,
            encounter_id=current.encounter_id,
            pathway_code=current.pathway_code,
            pathway_version=current.pathway_version,
            source_type=CaseSourceType.MANUAL,
            source_system=self.MANUAL_SOURCE_SYSTEM,
            external_record_id=None,
            idempotency_key=None,
        )
        return self.repository.revise(
            draft,
            case_id=case_id,
            expected_version=expected_version,
            occurred_at=self._now(),
            actor_id=revision.author.practitioner_id,
        )

    def get_latest(self, case_id: str) -> CaseRecord | None:
        self._require_enabled()
        return self.repository.get_latest(case_id)

    def list_versions(self, case_id: str) -> list[CaseRecord]:
        self._require_enabled()
        return self.repository.list_versions(case_id)

    def list_patient_cases(self, patient_id: str) -> list[CaseRecord]:
        self._require_enabled()
        return self.repository.list_patient_cases(patient_id)

    def _require_enabled(self) -> None:
        if self.mode == CaseIntakeMode.DISABLED:
            raise CaseIntakeDisabledError("Case Intake is disabled")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Case Intake clock must return a timezone-aware datetime")
        return value

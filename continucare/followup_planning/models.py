"""Strict contracts for the synthetic-only Follow-up Planning core.

The module deliberately models planning facts rather than FHIR clinical resources.
Knowledge can be referenced, but it never supplies runtime or scheduling authority.
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$",
    ),
]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
Narrative = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


def validate_iana_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanningMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class ActorRole(StrEnum):
    DOCTOR = "doctor"
    CLINICIAN = "clinician"
    NURSE = "nurse"
    PATIENT = "patient"


class PlanningActor(StrictModel):
    actor_id: Identifier
    role: ActorRole


class CandidateSourceKind(StrEnum):
    DOCTOR_RECORDED_CONDITION = "doctor_recorded_condition"
    SYMPTOM_OBSERVATION = "symptom_observation"
    PATIENT_REPORT = "patient_report"


class CandidateConfirmationStatus(StrEnum):
    EXTRACTED = "extracted"
    DOCTOR_CONFIRMED = "doctor_confirmed"
    REJECTED = "rejected"


class CodingSnapshot(StrictModel):
    system: Identifier
    code: Identifier
    display: ShortText | None = None
    version: ShortText | None = None


class ClinicalContextEntry(StrictModel):
    """A pre-structured, verbatim fact supplied by a trusted synthetic source."""

    entry_id: Identifier
    source_reference: Identifier
    source_excerpt_snapshot: Narrative
    source_kind: CandidateSourceKind
    extracted_text: Narrative
    coding: CodingSnapshot | None = None


class ClinicalContext(StrictModel):
    case_id: Identifier
    patient_id: Identifier
    entries: tuple[ClinicalContextEntry, ...] = Field(min_length=1)
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def entries_are_unique(self) -> "ClinicalContext":
        entry_ids = [entry.entry_id for entry in self.entries]
        references = [entry.source_reference for entry in self.entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("clinical context entry_id values must be unique")
        if len(references) != len(set(references)):
            raise ValueError("clinical context source references must be unique")
        return self


class ClinicalContextCandidate(StrictModel):
    candidate_id: Identifier
    version: int = Field(ge=1)
    case_id: Identifier
    patient_id: Identifier
    source_reference: Identifier
    source_excerpt_snapshot: Narrative
    source_kind: CandidateSourceKind
    extracted_text: Narrative
    coding: CodingSnapshot | None = None
    confirmation_status: CandidateConfirmationStatus
    confirmed_by: Identifier | None = None
    confirmed_at: datetime | None = None
    created_at: datetime
    synthetic: Literal[True] = True

    @field_validator("confirmed_at", "created_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value, "candidate timestamp")

    @model_validator(mode="after")
    def confirmation_is_explicit(self) -> "ClinicalContextCandidate":
        confirmed = self.confirmation_status == CandidateConfirmationStatus.DOCTOR_CONFIRMED
        if confirmed != (self.confirmed_by is not None and self.confirmed_at is not None):
            raise ValueError(
                "doctor_confirmed candidates require confirmed_by and confirmed_at"
            )
        if not confirmed and (self.confirmed_by is not None or self.confirmed_at is not None):
            raise ValueError("only doctor_confirmed candidates can carry confirmation")
        return self


class EvidenceReviewStatus(StrEnum):
    NOT_ASSESSED = "not_assessed"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    APPROVED = "approved"
    CLINICIAN_REVIEWED = "clinician_reviewed"
    INTERNALLY_CHECKED = "internally_checked"
    DESIGN_DOCUMENTED = "design_documented"


class EvidenceUse(StrEnum):
    FOLLOWUP_QUESTION = "followup_question"
    PATIENT_SELF_MEASUREMENT = "patient_self_measurement"
    PATIENT_REPORTED_RECORD = "patient_reported_record"
    REFERENCE_ONLY = "reference_only"
    LAB_ORDER = "lab_order"
    DIAGNOSTIC_INFERENCE = "diagnostic_inference"
    TREATMENT_RECOMMENDATION = "treatment_recommendation"


SAFE_MONITORING_USES = frozenset(
    {
        EvidenceUse.FOLLOWUP_QUESTION,
        EvidenceUse.PATIENT_SELF_MEASUREMENT,
        EvidenceUse.PATIENT_REPORTED_RECORD,
    }
)


class VersionedEvidenceReference(StrictModel):
    reference_id: Identifier
    version: int = Field(ge=1)


class EvidenceSnapshotPayload(StrictModel):
    """Payload read from an externally authenticated evidence envelope."""

    snapshot_id: Identifier
    topic_ref: VersionedEvidenceReference
    claim_refs: tuple[VersionedEvidenceReference, ...] = ()
    source_refs: tuple[VersionedEvidenceReference, ...] = ()
    binding_refs: tuple[VersionedEvidenceReference, ...] = ()
    statement_snapshot: Narrative
    supports: tuple[ShortText, ...] = Field(min_length=1)
    does_not_support: tuple[ShortText, ...] = Field(min_length=1)
    permitted_use: tuple[EvidenceUse, ...] = Field(min_length=1)
    applicable_scope: Mapping[str, Any]
    review_status: EvidenceReviewStatus
    version: int = Field(ge=1)
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"
    data_classification: Literal["synthetic_demo"] = "synthetic_demo"

    @field_validator("applicable_scope", mode="after")
    @classmethod
    def freeze_scope(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return MappingProxyType(dict(value))

    @field_serializer("applicable_scope")
    def serialize_scope(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)

    @model_validator(mode="after")
    def references_and_uses_are_unique(self) -> "EvidenceSnapshotPayload":
        for values, label in (
            (self.claim_refs, "claim"),
            (self.source_refs, "source"),
            (self.binding_refs, "binding"),
        ):
            keys = [(item.reference_id, item.version) for item in values]
            if len(keys) != len(set(keys)):
                raise ValueError(f"duplicate {label} references are not allowed")
        if len(self.permitted_use) != len(set(self.permitted_use)):
            raise ValueError("duplicate permitted_use values are not allowed")
        return self


class EvidenceSnapshot(EvidenceSnapshotPayload):
    """Runtime snapshot with a digest derived from its canonical payload."""

    snapshot_hash: Sha256


class EvidenceFixturePayload(StrictModel):
    snapshots: tuple[EvidenceSnapshotPayload, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def exact_topic_ids_are_unique(self) -> "EvidenceFixturePayload":
        topic_ids = [item.topic_ref.reference_id for item in self.snapshots]
        snapshot_ids = [item.snapshot_id for item in self.snapshots]
        if len(topic_ids) != len(set(topic_ids)):
            raise ValueError("evidence fixture topic references must be unique")
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("evidence fixture snapshot IDs must be unique")
        return self


class EvidenceFixtureEnvelope(StrictModel):
    schema_version: Literal["1.0"]
    payload: EvidenceFixturePayload


class EvidenceLookupStatus(StrEnum):
    MATCHED = "matched"
    NOT_ASSESSED = "not_assessed"
    NO_MATCH = "no_match"
    UNSUPPORTED_USE = "unsupported_use"


class EvidenceLookupResult(StrictModel):
    status: EvidenceLookupStatus
    exact_topic_id: Identifier
    requested_use: EvidenceUse
    snapshot: EvidenceSnapshot | None = None

    @model_validator(mode="after")
    def snapshot_matches_status(self) -> "EvidenceLookupResult":
        has_snapshot = self.snapshot is not None
        if self.status == EvidenceLookupStatus.NO_MATCH and has_snapshot:
            raise ValueError("no_match cannot carry an evidence snapshot")
        if self.status != EvidenceLookupStatus.NO_MATCH and not has_snapshot:
            raise ValueError("matched evidence status requires a snapshot")
        if has_snapshot and self.snapshot.topic_ref.reference_id != self.exact_topic_id:
            raise ValueError("lookup result topic does not match its snapshot")
        return self


class ResponseType(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    TEXT = "text"


class ResponseOption(StrictModel):
    value: Identifier
    label: ShortText


class MonitoringDefinitionOrigin(StrEnum):
    EVIDENCE_REFERENCED = "evidence_referenced"
    DOCTOR_AUTHORED = "doctor_authored"


class SnapshotReference(StrictModel):
    snapshot_id: Identifier
    version: int = Field(ge=1)
    snapshot_hash: Sha256


class MonitoringItemDefinition(StrictModel):
    definition_id: Identifier
    version: int = Field(ge=1)
    label: ShortText
    patient_prompt: Narrative
    response_type: ResponseType
    unit: ShortText | None = None
    options: tuple[ResponseOption, ...] = ()
    evidence_refs: tuple[SnapshotReference, ...] = ()
    permitted_use: EvidenceUse
    review_status: EvidenceReviewStatus
    origin: MonitoringDefinitionOrigin
    authored_by: Identifier
    created_at: datetime
    synthetic: Literal[True] = True

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "created_at")

    @model_validator(mode="after")
    def validate_response_and_origin(self) -> "MonitoringItemDefinition":
        if self.permitted_use not in SAFE_MONITORING_USES:
            raise ValueError("monitoring definition permitted_use is not executable")
        choice = self.response_type in {
            ResponseType.SINGLE_CHOICE,
            ResponseType.MULTIPLE_CHOICE,
        }
        if choice != bool(self.options):
            raise ValueError("choice response types require options, others forbid them")
        option_values = [item.value for item in self.options]
        if len(option_values) != len(set(option_values)):
            raise ValueError("response option values must be unique")
        numeric = self.response_type in {ResponseType.INTEGER, ResponseType.DECIMAL}
        if self.unit is not None and not numeric:
            raise ValueError("unit is only valid for numeric response types")
        evidence_origin = self.origin == MonitoringDefinitionOrigin.EVIDENCE_REFERENCED
        if evidence_origin != bool(self.evidence_refs):
            raise ValueError(
                "evidence_referenced definitions require evidence_refs; doctor_authored forbids them"
            )
        return self


class ScheduleApprovalStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class ScheduleBasis(StrEnum):
    DOCTOR_MANUAL = "doctor_manual"
    INSTITUTION_TEMPLATE = "institution_template"


class ScheduleValues(StrictModel):
    interval_days: int = Field(ge=1)
    times_of_day: tuple[time, ...] = Field(min_length=1)
    duration_days: int = Field(ge=1)
    timezone: ShortText

    @field_validator("times_of_day")
    @classmethod
    def times_are_unique_and_sorted(cls, values: tuple[time, ...]) -> tuple[time, ...]:
        if any(value.tzinfo is not None for value in values):
            raise ValueError("times_of_day must be local wall-clock times without tzinfo")
        if len(values) != len(set(values)):
            raise ValueError("times_of_day values must be unique")
        return tuple(sorted(values))

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return validate_iana_timezone(value)


class ScheduleTemplate(StrictModel):
    schedule_id: Identifier
    version: int = Field(ge=1)
    values: ScheduleValues | None = None
    basis: Literal[ScheduleBasis.INSTITUTION_TEMPLATE] = (
        ScheduleBasis.INSTITUTION_TEMPLATE
    )
    approval_status: ScheduleApprovalStatus
    authored_by: Identifier
    approved_by: Identifier | None = None
    approved_at: datetime | None = None
    created_at: datetime
    data_classification: Literal["synthetic_demo"] = "synthetic_demo"
    approval_scope: Literal["demo_only"] = "demo_only"

    @field_validator("approved_at", "created_at")
    @classmethod
    def schedule_timestamps_are_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        return None if value is None else require_aware(value, "schedule timestamp")

    @model_validator(mode="after")
    def approval_is_explicit_and_demo_only(self) -> "ScheduleTemplate":
        approved = self.approval_status == ScheduleApprovalStatus.APPROVED
        if approved and (
            self.values is None
            or self.approved_by is None
            or self.approved_at is None
        ):
            raise ValueError(
                "approved synthetic demo template requires values and approval provenance"
            )
        if not approved and (self.approved_by is not None or self.approved_at is not None):
            raise ValueError("draft template cannot carry approval provenance")
        return self


class VersionedReference(StrictModel):
    reference_id: Identifier
    version: int = Field(ge=1)


class FieldOrigin(StrEnum):
    KNOWLEDGE_REFERENCE = "knowledge_reference"
    INSTITUTION_TEMPLATE = "institution_template"
    DOCTOR_CURRENT_SETTING = "doctor_current_setting"
    DOCTOR_PATIENT_OVERRIDE = "doctor_patient_override"


class FieldProvenance(StrictModel):
    field_path: Identifier
    origin: FieldOrigin
    source_reference: Identifier | None = None


class ScheduleFieldProvenance(StrictModel):
    interval_days: FieldProvenance
    times_of_day: FieldProvenance
    duration_days: FieldProvenance
    timezone: FieldProvenance

    @model_validator(mode="after")
    def field_paths_match(self) -> "ScheduleFieldProvenance":
        expected = {
            "interval_days": self.interval_days,
            "times_of_day": self.times_of_day,
            "duration_days": self.duration_days,
            "timezone": self.timezone,
        }
        for field_name, provenance in expected.items():
            if provenance.field_path != f"schedule.{field_name}":
                raise ValueError("schedule provenance field_path does not match field")
        return self


class ResolvedSchedule(StrictModel):
    values: ScheduleValues
    basis: ScheduleBasis
    source_template_ref: VersionedReference | None = None
    source_template_values: ScheduleValues | None = None
    field_provenance: ScheduleFieldProvenance

    @model_validator(mode="after")
    def template_provenance_is_complete(self) -> "ResolvedSchedule":
        has_template = self.source_template_ref is not None
        if has_template != (self.source_template_values is not None):
            raise ValueError("template reference and value snapshot must appear together")
        if self.basis == ScheduleBasis.INSTITUTION_TEMPLATE and not has_template:
            raise ValueError("institution_template schedule requires a template snapshot")
        if self.basis == ScheduleBasis.INSTITUTION_TEMPLATE:
            if self.values != self.source_template_values:
                raise ValueError("unmodified template schedule must retain exact values")
        return self


class PlanStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class PatientFollowupPlanVersion(StrictModel):
    plan_id: Identifier
    plan_version_id: Identifier
    patient_id: Identifier
    pathway_reference: Identifier | None = None
    version: int = Field(ge=1)
    revision: int = Field(ge=1)
    status: PlanStatus
    effective_from: datetime
    created_by: Identifier
    created_at: datetime
    change_summary: ShortText
    supersedes_version: int | None = Field(default=None, ge=1)
    published_by: Identifier | None = None
    published_at: datetime | None = None
    synthetic: Literal[True] = True

    @field_validator("effective_from", "created_at", "published_at")
    @classmethod
    def plan_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value, "plan timestamp")

    @model_validator(mode="after")
    def validate_lineage_and_publication(self) -> "PatientFollowupPlanVersion":
        expected_superseded = None if self.version == 1 else self.version - 1
        if self.supersedes_version != expected_superseded:
            raise ValueError("plan versions must form a contiguous release chain")
        if self.plan_version_id != f"{self.plan_id}:v{self.version}":
            raise ValueError("plan_version_id must identify the stable release version")
        published = self.status in {PlanStatus.PUBLISHED, PlanStatus.SUPERSEDED}
        if published != (self.published_by is not None and self.published_at is not None):
            raise ValueError("published plan status requires publication provenance")
        return self


class FollowupItemStatus(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"


class ReviewRole(StrEnum):
    DOCTOR = "doctor"


ITEM_REQUIRED_PROVENANCE_FIELDS = frozenset(
    {
        "source_candidate_ids",
        "monitoring_definition_ref",
        "evidence_snapshot_refs",
        "prompt",
        "response_type",
        "unit",
        "options",
        "display_order",
        "review_role",
        "include_in_summary",
        "status",
        "series_id",
        "effective_start",
        "effective_end",
        "change_reason",
    }
)


class PatientFollowupItem(StrictModel):
    item_id: Identifier
    item_version: int = Field(ge=1)
    plan_version_id: Identifier
    source_template_item_id: Identifier | None = None
    source_candidate_ids: tuple[Identifier, ...] = Field(min_length=1)
    monitoring_definition_ref: VersionedReference | None = None
    evidence_snapshot_refs: tuple[SnapshotReference, ...] = ()
    prompt: Narrative
    response_type: ResponseType
    unit: ShortText | None = None
    options: tuple[ResponseOption, ...] = ()
    schedule: ResolvedSchedule | None = None
    display_order: int = Field(ge=0)
    review_role: ReviewRole = ReviewRole.DOCTOR
    include_in_summary: bool = True
    status: FollowupItemStatus = FollowupItemStatus.ACTIVE
    series_id: Identifier
    effective_start: date
    effective_end: date | None = None
    change_reason: ShortText
    field_provenance: tuple[FieldProvenance, ...] = Field(min_length=2)
    created_by: Identifier
    created_at: datetime
    synthetic: Literal[True] = True

    @field_validator("created_at")
    @classmethod
    def item_created_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "created_at")

    @model_validator(mode="after")
    def validate_item(self) -> "PatientFollowupItem":
        if len(self.source_candidate_ids) != len(set(self.source_candidate_ids)):
            raise ValueError("source_candidate_ids must be unique")
        field_paths = [item.field_path for item in self.field_provenance]
        if len(field_paths) != len(set(field_paths)):
            raise ValueError("field provenance paths must be unique")
        required = set(ITEM_REQUIRED_PROVENANCE_FIELDS)
        if self.source_template_item_id is not None:
            required.add("source_template_item_id")
        missing = required.difference(field_paths)
        if missing:
            raise ValueError(
                "item field provenance is missing: " + ", ".join(sorted(missing))
            )
        choice = self.response_type in {
            ResponseType.SINGLE_CHOICE,
            ResponseType.MULTIPLE_CHOICE,
        }
        if choice != bool(self.options):
            raise ValueError("choice response types require options, others forbid them")
        if self.unit is not None and self.response_type not in {
            ResponseType.INTEGER,
            ResponseType.DECIMAL,
        }:
            raise ValueError("unit is only valid for numeric responses")
        if self.status == FollowupItemStatus.STOPPED and self.effective_end is None:
            raise ValueError("stopped item requires effective_end")
        if self.status == FollowupItemStatus.ACTIVE and self.effective_end is not None:
            raise ValueError("active item cannot have effective_end")
        if self.effective_end is not None and self.effective_end < self.effective_start:
            raise ValueError("effective_end cannot precede effective_start")
        return self


class PlanAggregate(StrictModel):
    plan: PatientFollowupPlanVersion
    items: tuple[PatientFollowupItem, ...]

    @model_validator(mode="after")
    def items_match_plan(self) -> "PlanAggregate":
        if any(item.plan_version_id != self.plan.plan_version_id for item in self.items):
            raise ValueError("all items must belong to the plan release version")
        orders = [item.display_order for item in self.items]
        if orders != sorted(orders) or len(orders) != len(set(orders)):
            raise ValueError("plan item display_order values must be unique and sorted")
        timezones = {
            item.schedule.values.timezone
            for item in self.items
            if item.schedule is not None
        }
        if len(timezones) > 1:
            raise ValueError("all scheduled items in a patient plan must use one timezone")
        return self


class PublishConfirmation(StrictModel):
    plan_id: Identifier
    expected_revision: int = Field(ge=1)
    confirmed_by: Identifier
    confirmed: Literal[True]
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def confirmed_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "confirmed_at")


class CompletedAnswerBinding(StrictModel):
    answer_id: Identifier
    patient_id: Identifier
    plan_id: Identifier
    plan_version: int = Field(ge=1)
    item_id: Identifier
    item_version: int = Field(ge=1)
    series_id: Identifier
    prompt_snapshot: Narrative
    response_type_snapshot: ResponseType
    response_snapshot: Any
    answered_at: datetime
    synthetic: Literal[True] = True

    @field_validator("answered_at")
    @classmethod
    def answered_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "answered_at")


class ScheduledItem(StrictModel):
    item_id: Identifier
    item_version: int = Field(ge=1)
    local_date: date
    local_time: time
    timezone: ShortText
    scheduled_at: datetime

    @field_validator("scheduled_at")
    @classmethod
    def scheduled_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "scheduled_at")


class ScheduledSession(StrictModel):
    local_date: date
    local_time: time
    timezone: ShortText
    items: tuple[ScheduledItem, ...] = Field(min_length=1)


class SchedulePreview(StrictModel):
    window_start: date
    window_end_exclusive: date
    sessions: tuple[ScheduledSession, ...]


class PatientBurden(StrictModel):
    scheduled_sessions: int = Field(ge=0)
    total_items: int = Field(ge=0)
    busiest_day_items: int = Field(ge=0)


class NursePlanView(StrictModel):
    plan_id: Identifier
    patient_id: Identifier
    version: int = Field(ge=1)
    effective_from: datetime
    change_summary: ShortText
    items: tuple[PatientFollowupItem, ...]


class PatientItemView(StrictModel):
    item_id: Identifier
    item_version: int = Field(ge=1)
    prompt: Narrative
    response_type: ResponseType
    unit: ShortText | None = None
    options: tuple[ResponseOption, ...] = ()
    schedule: ResolvedSchedule
    display_order: int = Field(ge=0)


class PatientPlanView(StrictModel):
    plan_id: Identifier
    patient_id: Identifier
    version: int = Field(ge=1)
    effective_from: datetime
    items: tuple[PatientItemView, ...]


class PlanningAuditEvent(StrictModel):
    event_id: Identifier
    entity_type: Identifier
    entity_id: Identifier
    event_type: Identifier
    actor_id: Identifier
    actor_role: ActorRole
    occurred_at: datetime
    details: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value, "occurred_at")

    @field_validator("details", mode="after")
    @classmethod
    def freeze_details(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return MappingProxyType(dict(value))

    @field_serializer("details")
    def serialize_details(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)

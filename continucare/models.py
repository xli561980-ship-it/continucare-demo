"""Validated business entities for the local ContinuCare workflow."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from continucare.fhir.r4 import validate_r4_resource


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfidenceTier(str, Enum):
    PATIENT_CONFIRMED = "patient_confirmed"
    VERBATIM_EXPLICIT = "verbatim_explicit"
    MODEL_INFERRED = "model_inferred"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class AlertStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class CareSessionStatus(str, Enum):
    """Lifecycle for one execution of a locked Questionnaire version."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ENTERED_IN_ERROR = "entered_in_error"


class Patient(StrictModel):
    patient_id: str
    display_name: str
    synthetic: bool
    pathway_code: str
    enrollment_date: str
    next_visit_date: str
    status: str
    created_at: str


class FollowUpMessage(StrictModel):
    message_id: str
    patient_id: str
    message_text: str = Field(min_length=1)
    submitted_at: str
    source: str = "patient_demo_web"
    processing_status: str


class CareSession(StrictModel):
    """Persisted Layer-2 execution state, separate from clinical FHIR data."""

    session_id: str
    patient_id: str
    pathway_code: str
    pathway_version: str
    questionnaire_canonical: str
    questionnaire_version: str
    status: CareSessionStatus = CareSessionStatus.IN_PROGRESS
    answers: dict[str, Any] = Field(default_factory=dict)
    questionnaire_response_id: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class ConfirmedAnswerContext(StrictModel):
    """Layer-3 provenance and effective time for one confirmed draft answer."""

    answer_context_id: str
    session_id: str
    link_id: str
    answer: Any
    source_run_id: str
    followup_occurrence_id: str
    patient_timezone: str
    reported_at: str
    effective_start: str | None = None
    effective_end: str | None = None
    temporal_kind: str | None = None
    resolution_basis: str | None = None
    raw_text: str = Field(min_length=1)
    terminology_match: dict[str, Any] | None = None
    status: str = "active"
    created_at: str


class ConfirmedSymptomReport(StrictModel):
    """A patient-confirmed symptom outside the locked Questionnaire fields."""

    report_id: str
    session_id: str
    concept_id: str
    preferred_zh: str
    coding: dict[str, Any]
    terminology_match: dict[str, Any]
    source_kind: str
    source_run_id: str
    evidence_text: str = Field(min_length=1)
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)
    followup_occurrence_id: str
    patient_timezone: str
    reported_at: str
    effective_start: str | None = None
    effective_end: str | None = None
    temporal_kind: str | None = None
    status: str = "active"
    created_at: str


class ObservationEvidence(StrictModel):
    """Application evidence metadata kept outside the FHIR resource body."""

    questionnaire_response_id: str
    confidence_tier: ConfidenceTier
    evidence_text: str
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(ge=0)
    recorded_at: str
    source_kind: str = "pathway_monitored"
    terminology_match: dict[str, Any] | None = None

    @field_validator("evidence_end")
    @classmethod
    def evidence_end_must_follow_start(cls, value: int, info):
        start = info.data.get("evidence_start", 0)
        if value <= start:
            raise ValueError("evidence_end must be greater than evidence_start")
        return value


class Observation(StrictModel):
    """A validated FHIR R4 Observation plus non-clinical extraction evidence.

    ``resource`` is the exact exchange/persistence payload. Evidence offsets and
    extraction confidence are application metadata and are intentionally not
    inserted as non-standard FHIR properties.
    """

    resource: dict[str, Any]
    evidence: ObservationEvidence

    @model_validator(mode="after")
    def validate_fhir_and_trace(self) -> "Observation":
        self.resource = validate_r4_resource(
            self.resource, expected_resource_type="Observation"
        )
        expected = (
            f"QuestionnaireResponse/{self.evidence.questionnaire_response_id}"
        )
        derived_from = {
            item.get("reference") for item in self.resource.get("derivedFrom", [])
        }
        if expected not in derived_from:
            raise ValueError(
                "FHIR Observation.derivedFrom must reference the evidence "
                "QuestionnaireResponse"
            )
        return self

    @property
    def observation_id(self) -> str:
        return self.resource["id"]

    @property
    def patient_id(self) -> str:
        reference = self.resource["subject"]["reference"]
        return reference.removeprefix("Patient/")

    @property
    def message_id(self) -> str:
        return self.evidence.questionnaire_response_id

    @property
    def code(self) -> str:
        return self.resource["code"]["coding"][0]["code"]

    @property
    def code_system(self) -> str:
        return self.resource["code"]["coding"][0]["system"]

    @property
    def code_display(self) -> str:
        coding = self.resource["code"]["coding"][0]
        return coding.get("display") or self.resource["code"].get("text") or self.code

    @property
    def value(self) -> Any:
        if "valueQuantity" in self.resource:
            return self.resource["valueQuantity"].get("value")
        if "valueCodeableConcept" in self.resource:
            concept = self.resource["valueCodeableConcept"]
            coding = concept.get("coding", [])
            return coding[0].get("code") if coding else concept.get("text")
        for field_name in (
            "valueBoolean",
            "valueInteger",
            "valueString",
            "valueDateTime",
            "valueTime",
        ):
            if field_name in self.resource:
                return self.resource[field_name]
        return None

    @property
    def value_display(self) -> str:
        if "valueQuantity" in self.resource:
            quantity = self.resource["valueQuantity"]
            return f"{quantity.get('value')} {quantity.get('unit', quantity.get('code', ''))}".strip()
        if "valueCodeableConcept" in self.resource:
            concept = self.resource["valueCodeableConcept"]
            coding = concept.get("coding", [])
            if coding:
                return coding[0].get("display") or coding[0].get("code", "")
            return concept.get("text", "")
        return str(self.value)

    @property
    def unit(self) -> str | None:
        quantity = self.resource.get("valueQuantity")
        if quantity:
            return quantity.get("code") or quantity.get("unit")
        return None

    @property
    def effective_time(self) -> str:
        if "effectiveDateTime" in self.resource:
            return self.resource["effectiveDateTime"]
        period = self.resource.get("effectivePeriod", {})
        return period.get("end") or period.get("start") or self.resource["issued"]

    @property
    def source(self) -> str:
        return "patient_reported"

    @property
    def confidence_tier(self) -> ConfidenceTier:
        return self.evidence.confidence_tier

    @property
    def evidence_text(self) -> str:
        return self.evidence.evidence_text

    @property
    def evidence_start(self) -> int:
        return self.evidence.evidence_start

    @property
    def evidence_end(self) -> int:
        return self.evidence.evidence_end

    @property
    def created_at(self) -> str:
        return self.evidence.recorded_at

    def as_fhir(self) -> dict[str, Any]:
        return self.resource.copy()


class Alert(StrictModel):
    alert_id: str
    patient_id: str
    severity: str
    title: str
    trigger_rule_id: str
    trigger_reason: str
    evidence_refs: list[str]
    owner_role: str
    status: AlertStatus = AlertStatus.OPEN
    sla_due_at: str | None = None
    created_at: str
    resolved_at: str | None = None
    resolution_reason: str | None = None


class AlertAction(StrictModel):
    action_id: str
    alert_id: str
    action_type: str
    actor_role: str
    note: str
    created_at: str


class SummaryItem(StrictModel):
    text: str
    evidence_refs: list[str] = Field(min_length=1)


class SummaryContent(StrictModel):
    overview: list[SummaryItem] = Field(default_factory=list)
    key_changes: list[SummaryItem] = Field(default_factory=list)
    alerts_and_actions: list[SummaryItem] = Field(default_factory=list)
    patient_questions: list[SummaryItem] = Field(default_factory=list)
    missing_data: list[SummaryItem] = Field(default_factory=list)
    doctor_to_confirm: list[SummaryItem] = Field(default_factory=list)


class Summary(StrictModel):
    summary_id: str
    patient_id: str
    period_start: str
    period_end: str
    status: str
    summary_json: SummaryContent
    created_at: str
    reviewed_at: str | None = None


class AuditEvent(StrictModel):
    event_id: str
    patient_id: str | None = None
    entity_type: str
    entity_id: str
    event_type: str
    actor_type: str
    details_json: dict[str, Any]
    created_at: str


class ExtractionResult(StrictModel):
    observations: list[Observation] = Field(default_factory=list)
    extractor_mode: str = "local_mock_rules"

    @property
    def has_current_emergency_signal(self) -> bool:
        return False


class SummaryContext(StrictModel):
    patient: Patient
    messages: list[FollowUpMessage]
    observations: list[Observation]
    alerts: list[Alert]
    alert_actions: list[AlertAction]


class SummaryDraft(StrictModel):
    content: SummaryContent


class RiskDecision(StrictModel):
    severity: str
    create_alert: bool
    title: str | None = None
    trigger_rule_id: str | None = None
    trigger_reason: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    owner_role: str | None = None
    sla_hours: int | None = None


class DeliveryResult(StrictModel):
    delivered: bool
    channel: str
    delivery_id: str
    label: str
    detail: str

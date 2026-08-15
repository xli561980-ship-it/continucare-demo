"""Strict, JSON-serializable contracts used between Layer-3 agents."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SemanticStatus(str, Enum):
    NEEDS_CONFIRMATION = "needs_confirmation"
    NEEDS_CLARIFICATION = "needs_clarification"
    CONTEXT_RESOLVED = "context_resolved"
    NO_MATCH = "no_match"
    BLOCKED = "blocked"


class Temporality(str, Enum):
    CURRENT = "current"
    EXPLICIT_24H = "explicit_24h"
    UNSPECIFIED = "unspecified"
    HISTORICAL = "historical"


class SubjectType(str, Enum):
    PATIENT = "patient"
    OTHER_PERSON = "other_person"
    UNKNOWN = "unknown"


class ClarificationKind(str, Enum):
    CONFIRM_TIME_WINDOW = "confirm_time_window"
    CONFIRM_CURRENT = "confirm_current"
    CLARIFY_COUNT = "clarify_count"
    CLARIFY_QUANTITY = "clarify_quantity"
    RESOLVE_CONFLICT = "resolve_conflict"
    SEMANTIC_REVIEW = "semantic_review"
    MISSING_DEPENDENT_VALUE = "missing_dependent_value"
    TERMINOLOGY_DISAMBIGUATION = "terminology_disambiguation"


class CandidateIssueAction(str, Enum):
    REJECTED = "rejected"
    CLARIFICATION_REQUIRED = "clarification_required"


class MissingItemStatus(str, Enum):
    SUPPORTED = "supported"
    AMBIGUOUS = "ambiguous"
    NOT_MENTIONED = "not_mentioned"
    NOT_APPLICABLE = "not_applicable"


class TemporalKind(str, Enum):
    POINT_IN_TIME = "point_in_time"
    ROLLING_24H = "rolling_24h"
    PARTIAL_LOCAL_DAY = "partial_local_day"
    LOCAL_CALENDAR_DAY = "local_calendar_day"


class TemporalResolutionBasis(str, Enum):
    EXPLICIT_PATIENT_TEXT = "explicit_patient_text"
    PENDING_QUESTION = "pending_question"
    PATIENT_CONFIRMATION = "patient_confirmation"


class PendingActionType(str, Enum):
    CANDIDATE_CONFIRMATION = "candidate_confirmation"
    CLARIFICATION = "clarification"


class ContextResolutionDecision(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNSURE = "unsure"


class CandidateOrigin(str, Enum):
    PATHWAY_MONITORED = "pathway_monitored"
    PATIENT_REPORTED_NEW = "patient_reported_new"


class CandidateSource(str, Enum):
    DETERMINISTIC_MOCK = "deterministic_mock"
    MIMO = "mimo"
    AILY = "aily"


class CodingContract(StrictModel):
    system: str
    code: str
    display: str | None = None
    version: str | None = None


class TerminologyMatchContract(StrictModel):
    """Auditable result of retrieving a code from a governed terminology source."""

    catalog_id: str
    catalog_version: str
    concept_id: str
    preferred_zh: str
    coding: CodingContract
    target_coding: CodingContract
    matched_text: str
    matched_alias: str
    match_method: str
    validation_status: str
    approval_status: str
    target_hospital_validation_required: bool = True


class ReportedSymptomMention(StrictModel):
    mention_id: str
    symptom_text: str = Field(min_length=1, max_length=200)
    evidence_text: str = Field(min_length=1, max_length=1000)
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)
    subject: SubjectType = SubjectType.PATIENT
    temporality: Temporality = Temporality.UNSPECIFIED
    negated: bool = False
    source_mode: CandidateSource = CandidateSource.DETERMINISTIC_MOCK

    @model_validator(mode="after")
    def validate_span(self) -> "ReportedSymptomMention":
        if self.evidence_end <= self.evidence_start:
            raise ValueError("evidence_end must be greater than evidence_start")
        return self


class AnswerOptionContract(StrictModel):
    code: str
    system: str
    display: str | None = None
    semantic_aliases: list[str] = Field(default_factory=list)


class EnableWhenContract(StrictModel):
    """Provider-neutral form of a FHIR Questionnaire.enableWhen condition."""

    question: str
    operator: str
    answer: Any


class TemporalMention(StrictModel):
    expression: str = Field(min_length=1)
    kind: TemporalKind
    effective_start: str
    effective_end: str
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_span(self) -> "TemporalMention":
        if self.evidence_end <= self.evidence_start:
            raise ValueError("evidence_end must be greater than evidence_start")
        return self


class TemporalResolution(StrictModel):
    kind: TemporalKind
    expression: str | None = None
    effective_start: str
    effective_end: str
    timezone: str
    anchor_at: str
    basis: TemporalResolutionBasis
    inherited_from_action_id: str | None = None


class TemporalContext(StrictModel):
    patient_timezone: str
    received_at_utc: str
    received_at_local: str
    local_date: str
    scheduled_at: str
    scheduled_local_date: str
    followup_occurrence_id: str
    rolling_24h_start: str
    rolling_24h_end: str
    detected_mentions: list[TemporalMention] = Field(default_factory=list)


class ConversationTurnContext(StrictModel):
    run_id: str
    message_text: str = Field(min_length=1, max_length=4000)
    status: SemanticStatus
    completed_at: str
    candidate_link_ids: list[str] = Field(default_factory=list)


class PendingActionContext(StrictModel):
    action_id: str
    action_type: PendingActionType
    source_run_id: str
    link_id: str | None = None
    kind: ClarificationKind | None = None
    prompt: str = Field(min_length=1)
    option_ids: list[str] = Field(default_factory=list)
    proposed_answer: Any | None = None


class ConversationContext(StrictModel):
    recent_turns: list[ConversationTurnContext] = Field(default_factory=list)
    pending_actions: list[PendingActionContext] = Field(default_factory=list)
    memory_scope: str = "daily_followup_session"
    followup_occurrence_id: str | None = None
    max_turns: int = Field(default=50, ge=1, le=200)


class LongTermMemoryItem(StrictModel):
    observation_id: str
    code_system: str
    code: str
    display: str
    value_display: str
    effective_time: str
    source_kind: str


class ContextResolution(StrictModel):
    decision: ContextResolutionDecision
    source_run_id: str
    action_ids: list[str] = Field(min_length=1)
    applied_link_ids: list[str] = Field(default_factory=list)
    response_text: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ContextEvidenceBinding(StrictModel):
    source_run_id: str
    source_action_id: str
    binding_type: str = "answer_to_pending_question"


class QuestionnaireItemContract(StrictModel):
    link_id: str
    item_type: str
    text: str
    codes: list[CodingContract] = Field(default_factory=list)
    answer_options: list[AnswerOptionContract] = Field(default_factory=list)
    enable_when: list[EnableWhenContract] = Field(default_factory=list)
    enable_behavior: str | None = None
    required: bool = False
    repeats: bool = False


class SemanticTask(StrictModel):
    task_id: str
    patient_id: str
    session_id: str
    pathway_code: str
    pathway_version: str
    questionnaire_canonical: str
    questionnaire_version: str
    terminology_catalog_id: str | None = None
    terminology_catalog_version: str | None = None
    message_text: str = Field(min_length=1, max_length=4000)
    existing_answers: dict[str, Any] = Field(default_factory=dict)
    conversation_context: ConversationContext = Field(
        default_factory=ConversationContext
    )
    long_term_memory: list[LongTermMemoryItem] = Field(default_factory=list)
    temporal_context: TemporalContext
    allowed_items: list[QuestionnaireItemContract] = Field(min_length=1)
    created_at: str

    @field_validator("message_text")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message_text cannot be blank")
        return value


class SemanticCandidate(StrictModel):
    candidate_id: str
    link_id: str
    answer: Any
    questionnaire_code: CodingContract | None = None
    evidence_text: str = Field(min_length=1)
    evidence_start: int = Field(ge=0)
    evidence_end: int = Field(gt=0)
    subject: SubjectType = SubjectType.PATIENT
    temporality: Temporality
    negated: bool = False
    context_binding: ContextEvidenceBinding | None = None
    effective_time: TemporalResolution | None = None
    requires_patient_confirmation: bool = True
    patient_message: str = Field(min_length=1)
    template_id: str
    origin: CandidateOrigin = CandidateOrigin.PATHWAY_MONITORED
    terminology_match: TerminologyMatchContract | None = None
    source_mode: CandidateSource = CandidateSource.DETERMINISTIC_MOCK

    @model_validator(mode="after")
    def validate_span(self) -> "SemanticCandidate":
        if self.evidence_end <= self.evidence_start:
            raise ValueError("evidence_end must be greater than evidence_start")
        return self


class ClarificationOption(StrictModel):
    option_id: str
    label: str
    accepts_candidate: bool = False
    terminology_match: TerminologyMatchContract | None = None


class ClarificationRequest(StrictModel):
    clarification_id: str
    kind: ClarificationKind
    prompt: str = Field(min_length=1)
    target_link_id: str | None = None
    options: list[ClarificationOption] = Field(min_length=2)
    proposed_candidate: SemanticCandidate | None = None
    reported_symptom: ReportedSymptomMention | None = None


class CandidateIssue(StrictModel):
    """Patient-readable disposition for a model proposal that was not accepted."""

    issue_id: str
    candidate_id: str
    link_id: str
    field_label: str
    proposed_answer: Any | None = None
    evidence_text: str = Field(min_length=1)
    action: CandidateIssueAction
    reason_codes: list[str] = Field(min_length=1)
    explanation: str = Field(min_length=1)


class MissingItemFinding(StrictModel):
    """Safety-Critic disposition for an allowed item omitted by extraction."""

    link_id: str
    status: MissingItemStatus
    evidence_text: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)


class AgentStageTrace(StrictModel):
    """Auditable stage metadata kept inside the persisted top-level AgentRun."""

    stage: str
    agent_name: str
    agent_version: str
    mode: str
    status: str
    model_provider: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    model_usage: dict[str, int] | None = None
    provider_request_id: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


class SemanticResult(StrictModel):
    run_id: str
    task_id: str
    status: SemanticStatus
    mode: str
    care_agent_version: str
    safety_agent_version: str
    language_policy_version: str
    candidates: list[SemanticCandidate] = Field(default_factory=list)
    reported_symptom_mentions: list[ReportedSymptomMention] = Field(
        default_factory=list
    )
    clarifications: list[ClarificationRequest] = Field(default_factory=list)
    candidate_issues: list[CandidateIssue] = Field(default_factory=list)
    missing_items: list[MissingItemFinding] = Field(default_factory=list)
    stage_traces: list[AgentStageTrace] = Field(default_factory=list)
    temporal_context: TemporalContext | None = None
    context_resolution: ContextResolution | None = None
    ignored_reasons: list[str] = Field(default_factory=list)
    safety_violations: list[str] = Field(default_factory=list)
    model_usage: dict[str, int] | None = None
    provider_request_id: str | None = None
    completed_at: str


class SafetyReviewTask(StrictModel):
    """Input contract for the independently registered Safety Agent."""

    task_id: str
    semantic_task: SemanticTask
    draft: SemanticResult


class AgentRunRecord(StrictModel):
    run_id: str
    task_id: str
    patient_id: str
    session_id: str
    agent_name: str
    agent_version: str
    mode: str
    input_text: str
    input_hash: str
    output_json: dict[str, Any]
    status: str
    model_provider: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    started_at: str
    completed_at: str
    error_code: str | None = None


class AgentRuntimeOutcome(StrictModel):
    result: SemanticResult
    started_at: str
    completed_at: str
    agent_name: str
    agent_version: str
    idempotent_replay: bool = False

"""Strict, versioned contracts for Layer-4 memory and workflow services.

These models describe deterministic application state. FHIR resources remain the
exchange source of truth and are validated separately in ``continucare.layer4.fhir``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


LAYER4_CONTRACT_VERSION = "1.0.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _parse_instant(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 instant") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed


class EvidenceRole(str, Enum):
    SOURCE = "source"
    TRIGGER = "trigger"
    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"
    REVIEW = "review"


class MemoryEventKind(str, Enum):
    QUESTIONNAIRE_RESPONSE = "questionnaire_response"
    OBSERVATION = "observation"
    COMMUNICATION = "communication"
    TASK = "task"
    REVIEW = "review"
    AUDIT = "audit"
    CONFLICT = "conflict"
    MISSING_DATA = "missing_data"


class TimelineEventState(str, Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    ENTERED_IN_ERROR = "entered_in_error"
    CONFLICT = "conflict"


class RevisionRelationship(str, Enum):
    SUPERSEDES = "supersedes"
    AMENDS = "amends"
    CORRECTS = "corrects"
    RETRACTS = "retracts"
    ENTERED_IN_ERROR = "entered_in_error"


class RuleLifecycle(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class ApprovalDecision(str, Enum):
    NOT_REVIEWED = "not_reviewed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"


class RuleOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"


class RuleEvaluationStatus(str, Enum):
    NOT_ASSESSED = "not_assessed"
    NO_MATCH = "no_match"
    MATCHED = "matched"


class RuleConditionStatus(str, Enum):
    NOT_ASSESSED = "not_assessed"
    NOT_MATCHED = "not_matched"
    MATCHED = "matched"


class SummaryDraftStatus(str, Enum):
    DRAFT = "draft"
    SAFETY_REVIEWED = "safety_reviewed"
    DOCTOR_REVIEWED = "doctor_reviewed"
    REJECTED = "rejected"


class SummaryFactKind(str, Enum):
    TIMELINE = "timeline"
    METRIC_STATE = "metric_state"
    NUMERIC_TREND = "numeric_trend"


class ControlledSummaryStatus(str, Enum):
    LLM_ASSISTED = "llm_assisted"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class DoctorReviewDecision(str, Enum):
    ACCEPT = "accept"
    MODIFY = "modify"
    REJECT = "reject"


class MetricStateStatus(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class TrendCalculationStatus(str, Enum):
    CALCULATED = "calculated"
    INSUFFICIENT_DATA = "insufficient_data"
    CONFLICT = "conflict"
    UNIT_MISMATCH = "unit_mismatch"


class TrendDirection(str, Enum):
    """Raw numeric direction only; this is not a clinical interpretation."""

    INCREASING = "increasing"
    DECREASING = "decreasing"
    UNCHANGED = "unchanged"


class WorkbenchRole(str, Enum):
    DOCTOR = "doctor"
    CLINICAL_AUDITOR = "clinical_auditor"
    NURSE = "nurse"


class WorkbenchPurpose(str, Enum):
    TREATMENT = "treatment"
    AUDIT = "audit"
    OPERATIONS = "operations"


class WorkbenchComponent(str, Enum):
    TIMELINE = "timeline"
    STATE = "state"
    SUMMARY = "summary"
    TASKS = "tasks"


class ComponentReadStatus(str, Enum):
    AVAILABLE = "available"
    EMPTY = "empty"
    DEGRADED = "degraded"


class EvidenceArtifactType(str, Enum):
    FHIR_RESOURCE = "fhir_resource"
    CONTRACT_RECORD = "contract_record"
    METRIC_DEFINITION = "metric_definition"
    APPLICATION_RECORD = "application_record"


class ResourceReference(StrictModel):
    reference: str = Field(min_length=3)
    display: str | None = None
    version_id: str | None = None


class EvidenceReference(StrictModel):
    evidence_id: str = Field(min_length=1)
    resource: ResourceReference
    role: EvidenceRole
    effective_start: str | None = None
    effective_end: str | None = None
    evidence_text: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_effective_period(self) -> "EvidenceReference":
        if (self.effective_start is None) != (self.effective_end is None):
            raise ValueError("evidence effective period requires both start and end")
        if self.effective_start and self.effective_end:
            start = _parse_instant(self.effective_start, field_name="effective_start")
            end = _parse_instant(self.effective_end, field_name="effective_end")
            if end < start:
                raise ValueError("evidence effective_end cannot precede effective_start")
        return self


class RevisionLink(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    link_id: str
    patient_id: str
    predecessor: ResourceReference
    successor: ResourceReference
    relationship: RevisionRelationship
    reason: str = Field(min_length=1)
    actor_reference: str = Field(min_length=3)
    provenance_reference: str = Field(min_length=3)
    created_at: str

    @model_validator(mode="after")
    def validate_revision(self) -> "RevisionLink":
        if self.predecessor.reference == self.successor.reference:
            if self.predecessor.version_id == self.successor.version_id:
                raise ValueError("revision predecessor and successor must differ")
        _parse_instant(self.created_at, field_name="created_at")
        return self


class MemoryEvent(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    event_id: str
    patient_id: str
    pathway_code: str
    pathway_version: str
    kind: MemoryEventKind
    source: ResourceReference
    source_status: str = Field(min_length=1)
    effective_start: str
    effective_end: str
    recorded_at: str
    deduplication_key: str = Field(min_length=1)
    evidence_refs: list[EvidenceReference] = Field(min_length=1)
    provenance_refs: list[ResourceReference] = Field(default_factory=list)
    current: bool = True
    conflict_group_id: str | None = None
    expectation_id: str | None = None

    @model_validator(mode="after")
    def validate_times_and_evidence(self) -> "MemoryEvent":
        start = _parse_instant(self.effective_start, field_name="effective_start")
        end = _parse_instant(self.effective_end, field_name="effective_end")
        _parse_instant(self.recorded_at, field_name="recorded_at")
        if end < start:
            raise ValueError("memory event effective_end cannot precede effective_start")
        evidence_ids = [item.evidence_id for item in self.evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("memory event contains duplicate evidence ids")
        return self


class TimelineEvent(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    timeline_event_id: str
    patient_id: str
    memory_event_id: str
    pathway_code: str
    pathway_version: str
    kind: MemoryEventKind
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=2000)
    effective_start: str
    effective_end: str
    recorded_at: str
    state: TimelineEventState = TimelineEventState.CURRENT
    source: ResourceReference
    evidence_refs: list[EvidenceReference] = Field(min_length=1)
    conflict_group_id: str | None = None
    expectation_id: str | None = None

    @model_validator(mode="after")
    def validate_times(self) -> "TimelineEvent":
        start = _parse_instant(self.effective_start, field_name="effective_start")
        end = _parse_instant(self.effective_end, field_name="effective_end")
        _parse_instant(self.recorded_at, field_name="recorded_at")
        if end < start:
            raise ValueError("timeline effective_end cannot precede effective_start")
        return self


class RuleApplicability(StrictModel):
    pathway_code: str
    pathway_version: str
    synthetic_only: bool = True
    product_code: str | None = None
    population: str = Field(min_length=1)
    region: str = Field(min_length=1)


class RuleObservationInput(StrictModel):
    input_id: str
    code_system: str
    code: str
    unit: str | None = None
    lookback_hours: int = Field(gt=0)
    required: bool = True
    accepted_statuses: list[Literal["final", "amended", "corrected"]] = Field(
        default_factory=lambda: ["final"], min_length=1
    )


class MissingDataExpectation(StrictModel):
    """A governed expectation used to express absence without inventing a fact."""

    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    expectation_id: str
    patient_id: str
    pathway_code: str
    pathway_version: str
    code_system: str
    code: str
    display: str = Field(min_length=1)
    period_start: str
    period_end: str
    pathway_reference: ResourceReference

    @model_validator(mode="after")
    def validate_period(self) -> "MissingDataExpectation":
        start = _parse_instant(self.period_start, field_name="period_start")
        end = _parse_instant(self.period_end, field_name="period_end")
        if end < start:
            raise ValueError("expectation period_end cannot precede period_start")
        return self


class ClinicalMemoryBuildResult(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    patient_id: str
    memory_event_ids: list[str] = Field(default_factory=list)
    timeline_event_ids: list[str] = Field(default_factory=list)
    provenance_ids: list[str] = Field(default_factory=list)
    conflict_event_ids: list[str] = Field(default_factory=list)
    missing_event_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ClinicalMemoryBuildResult":
        for field_name in (
            "memory_event_ids",
            "timeline_event_ids",
            "provenance_ids",
            "conflict_event_ids",
            "missing_event_ids",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} contains duplicate ids")
        return self


class RuleCondition(StrictModel):
    input_id: str
    operator: RuleOperator
    expected_value: Any
    unit: str | None = None


class RuleTaskAction(StrictModel):
    action_type: Literal["create_task"] = "create_task"
    task_code_system: str
    task_code: str
    task_code_display: str
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=2000)
    priority: Literal["routine", "urgent", "asap", "stat"] = "routine"
    owner_role: str = Field(min_length=1)
    sla_hours: int = Field(gt=0)
    deduplication_window_hours: int = Field(gt=0)


class RuleApproval(StrictModel):
    clinical_status: ApprovalDecision = ApprovalDecision.NOT_REVIEWED
    terminology_status: ApprovalDecision = ApprovalDecision.NOT_REVIEWED
    clinical_approver: str | None = None
    terminology_approver: str | None = None
    clinical_approved_at: str | None = None
    terminology_approved_at: str | None = None

    @model_validator(mode="after")
    def validate_approval_records(self) -> "RuleApproval":
        pairs = (
            (
                self.clinical_status,
                self.clinical_approver,
                self.clinical_approved_at,
                "clinical",
            ),
            (
                self.terminology_status,
                self.terminology_approver,
                self.terminology_approved_at,
                "terminology",
            ),
        )
        for status, approver, approved_at, label in pairs:
            if status == ApprovalDecision.APPROVED:
                if not approver or not approved_at:
                    raise ValueError(
                        f"approved {label} decision requires approver and timestamp"
                    )
                _parse_instant(approved_at, field_name=f"{label}_approved_at")
            elif approver or approved_at:
                raise ValueError(
                    f"{label} approver and timestamp require approved status"
                )
        return self

    def fully_approved(self) -> bool:
        return (
            self.clinical_status == ApprovalDecision.APPROVED
            and self.terminology_status == ApprovalDecision.APPROVED
            and bool(self.clinical_approver)
            and bool(self.terminology_approver)
            and bool(self.clinical_approved_at)
            and bool(self.terminology_approved_at)
        )


class ClinicalRuleDefinition(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    rule_id: str
    version: str
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    lifecycle: RuleLifecycle = RuleLifecycle.DRAFT
    applicability: RuleApplicability
    evidence_refs: list[EvidenceReference] = Field(min_length=1)
    inputs: list[RuleObservationInput] = Field(min_length=1)
    conditions: list[RuleCondition] = Field(min_length=1)
    condition_logic: Literal["all", "any"] = "all"
    action: RuleTaskAction
    approval: RuleApproval = Field(default_factory=RuleApproval)
    test_case_ids: list[str] = Field(min_length=1)
    rollback_plan: str = Field(min_length=1)
    created_at: str

    @model_validator(mode="after")
    def validate_governance(self) -> "ClinicalRuleDefinition":
        input_ids = [item.input_id for item in self.inputs]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("clinical rule contains duplicate input ids")
        unknown = {item.input_id for item in self.conditions} - set(input_ids)
        if unknown:
            raise ValueError(
                "clinical rule conditions reference unknown inputs: "
                + ", ".join(sorted(unknown))
            )
        inputs_by_id = {item.input_id: item for item in self.inputs}
        for condition in self.conditions:
            input_unit = inputs_by_id[condition.input_id].unit
            if condition.unit and input_unit and condition.unit != input_unit:
                raise ValueError(
                    f"condition unit for {condition.input_id} conflicts with input unit"
                )
        _parse_instant(self.created_at, field_name="created_at")
        if self.lifecycle in {RuleLifecycle.APPROVED, RuleLifecycle.ACTIVE}:
            if not self.approval.fully_approved():
                raise ValueError(
                    "approved or active clinical rule requires clinical and "
                    "terminology approvals with approvers and timestamps"
                )
        return self


class RuleConditionExplanation(StrictModel):
    condition_index: int = Field(ge=0)
    input_id: str
    operator: RuleOperator
    status: RuleConditionStatus
    expected_value: Any
    actual_value: Any | None = None
    unit: str | None = None
    reason_code: str | None = None
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)


class RuleEvaluationResult(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    evaluation_id: str
    patient_id: str
    pathway_code: str
    pathway_version: str
    rule_id: str
    rule_version: str
    status: RuleEvaluationStatus
    condition_logic: Literal["all", "any"]
    conditions: list[RuleConditionExplanation] = Field(min_length=1)
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)
    task_reference: str | None = None
    task_created: bool = False
    provenance_reference: str
    evaluated_at: str

    @model_validator(mode="after")
    def validate_evaluation(self) -> "RuleEvaluationResult":
        _parse_instant(self.evaluated_at, field_name="evaluated_at")
        if self.status == RuleEvaluationStatus.MATCHED and not self.task_reference:
            raise ValueError("matched rule evaluation requires a Task reference")
        if self.task_created and not self.task_reference:
            raise ValueError("task_created requires a Task reference")
        evidence_ids = [item.evidence_id for item in self.evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("rule evaluation contains duplicate evidence ids")
        return self


class RuleEvaluationBatch(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    batch_id: str
    patient_id: str
    pathway_code: str
    pathway_version: str
    status: RuleEvaluationStatus
    reason_codes: list[str] = Field(default_factory=list)
    evaluations: list[RuleEvaluationResult] = Field(default_factory=list)
    task_references: list[str] = Field(default_factory=list)
    provenance_references: list[str] = Field(default_factory=list)
    evaluated_at: str

    @model_validator(mode="after")
    def validate_batch(self) -> "RuleEvaluationBatch":
        _parse_instant(self.evaluated_at, field_name="evaluated_at")
        if len(self.task_references) != len(set(self.task_references)):
            raise ValueError("rule evaluation batch contains duplicate Task references")
        return self


class TaskTransitionResult(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    transition_id: str
    patient_id: str
    task_id: str
    from_status: str
    to_status: str
    from_version: str
    to_version: str
    actor_reference: str = Field(min_length=3)
    note: str = Field(min_length=1, max_length=4000)
    task_reference: str
    provenance_reference: str
    transitioned_at: str

    @model_validator(mode="after")
    def validate_transition(self) -> "TaskTransitionResult":
        _parse_instant(self.transitioned_at, field_name="transitioned_at")
        if self.from_status == self.to_status:
            raise ValueError("Task transition must change status")
        if self.from_version == self.to_version:
            raise ValueError("Task transition must create a new version")
        return self


SummarySection = Literal[
    "overview",
    "key_changes",
    "tasks_and_actions",
    "patient_questions",
    "missing_data",
    "conflicts",
    "doctor_to_confirm",
]


class SummaryEvidenceItem(StrictModel):
    item_id: str
    section: SummarySection
    text: str = Field(min_length=1, max_length=3000)
    evidence_refs: list[EvidenceReference] = Field(min_length=1)
    requires_doctor_confirmation: bool = False

    @model_validator(mode="after")
    def validate_evidence(self) -> "SummaryEvidenceItem":
        evidence_ids = [item.evidence_id for item in self.evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("summary item contains duplicate evidence ids")
        return self


class SummaryFact(StrictModel):
    """One immutable, locally rendered fact offered to the Summary LLM."""

    fact_id: str = Field(min_length=1)
    kind: SummaryFactKind
    section: SummarySection
    canonical_text: str = Field(min_length=1, max_length=3000)
    evidence_refs: list[EvidenceReference] = Field(min_length=1)
    mandatory: bool = True
    priority: int = Field(default=50, ge=0, le=100)
    requires_doctor_confirmation: bool = False

    @model_validator(mode="after")
    def validate_evidence(self) -> "SummaryFact":
        evidence_ids = [item.evidence_id for item in self.evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("summary fact contains duplicate evidence ids")
        return self


class SummaryFactLedger(StrictModel):
    """Metric-agnostic fact list; no metric names are fixed in the LLM schema."""

    ledger_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    pathway_code: str = Field(min_length=1)
    pathway_version: str = Field(min_length=1)
    period_start: str
    period_end: str
    assembled_at: str
    facts: list[SummaryFact] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ledger(self) -> "SummaryFactLedger":
        start = _parse_instant(self.period_start, field_name="period_start")
        end = _parse_instant(self.period_end, field_name="period_end")
        assembled = _parse_instant(self.assembled_at, field_name="assembled_at")
        if end < start:
            raise ValueError("fact ledger period_end cannot precede period_start")
        if assembled < end:
            raise ValueError("fact ledger assembled_at cannot precede period_end")
        fact_ids = [item.fact_id for item in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact ledger contains duplicate fact ids")
        return self


class SummaryOutlineGroup(StrictModel):
    group_id: str = Field(min_length=1, max_length=100)
    section: SummarySection
    fact_ids: list[str] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def validate_fact_ids(self) -> "SummaryOutlineGroup":
        if any(not item.strip() for item in self.fact_ids):
            raise ValueError("summary outline group contains a blank fact id")
        if len(self.fact_ids) != len(set(self.fact_ids)):
            raise ValueError("summary outline group contains duplicate fact ids")
        return self


class SummaryAgentTask(StrictModel):
    task_id: str = Field(min_length=1)
    ledger: SummaryFactLedger


class SummaryAgentDecision(StrictModel):
    groups: list[SummaryOutlineGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_group_ids(self) -> "SummaryAgentDecision":
        group_ids = [item.group_id for item in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("summary outline contains duplicate group ids")
        return self


class SummaryModelOutcome(StrictModel):
    decision: SummaryAgentDecision
    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    model_usage: dict[str, int] | None = None
    provider_request_id: str | None = None
    latency_ms: int = Field(ge=0)
    attempt_count: int = Field(ge=1, le=2)

    @model_validator(mode="after")
    def validate_usage(self) -> "SummaryModelOutcome":
        if self.model_usage is not None and any(
            not key.strip() or value < 0 for key, value in self.model_usage.items()
        ):
            raise ValueError("summary model usage requires named non-negative counters")
        return self


class Layer4SummaryDraft(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    summary_id: str
    version: str = "1"
    patient_id: str
    pathway_code: str | None = None
    pathway_version: str | None = None
    summary_kind: Literal["timeline_evidence", "manual_review_brief"] = (
        "timeline_evidence"
    )
    period_start: str
    period_end: str
    status: SummaryDraftStatus = SummaryDraftStatus.DRAFT
    items: list[SummaryEvidenceItem] = Field(default_factory=list)
    source_timeline_event_ids: list[str] = Field(default_factory=list)
    provenance_refs: list[ResourceReference] = Field(default_factory=list)
    generation_mode: Literal["deterministic", "llm_assisted"] = "deterministic"
    generator_version: str = Field(
        default="deterministic-summary-v1", min_length=1
    )
    source_state_snapshot_reference: str | None = Field(default=None, min_length=3)
    source_fact_ids: list[str] = Field(default_factory=list)
    outline_digest: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    model_provider: str | None = Field(default=None, min_length=1)
    model_name: str | None = Field(default=None, min_length=1)
    prompt_version: str | None = Field(default=None, min_length=1)
    agent_version: str | None = Field(default=None, min_length=1)
    model_usage: dict[str, int] | None = None
    provider_request_id: str | None = Field(default=None, min_length=1)
    fallback_reason_codes: list[str] = Field(default_factory=list)
    source_evidence_digest: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    created_at: str

    @model_validator(mode="after")
    def validate_period(self) -> "Layer4SummaryDraft":
        start = _parse_instant(self.period_start, field_name="period_start")
        end = _parse_instant(self.period_end, field_name="period_end")
        _parse_instant(self.created_at, field_name="created_at")
        if end < start:
            raise ValueError("summary period_end cannot precede period_start")
        if (self.pathway_code is None) != (self.pathway_version is None):
            raise ValueError("summary pathway requires both code and version")
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("summary contains duplicate item ids")
        if len(self.source_timeline_event_ids) != len(
            set(self.source_timeline_event_ids)
        ):
            raise ValueError("summary contains duplicate source timeline event ids")
        if len(self.source_fact_ids) != len(set(self.source_fact_ids)):
            raise ValueError("summary contains duplicate source fact ids")
        if any(not item.strip() for item in self.source_fact_ids):
            raise ValueError("summary contains a blank source fact id")
        if len(self.fallback_reason_codes) != len(set(self.fallback_reason_codes)):
            raise ValueError("summary contains duplicate fallback reason codes")
        if any(not item.strip() for item in self.fallback_reason_codes):
            raise ValueError("summary contains a blank fallback reason code")
        if self.model_usage is not None and any(
            not key.strip() or value < 0 for key, value in self.model_usage.items()
        ):
            raise ValueError("summary model usage requires named non-negative counters")
        if self.generation_mode == "deterministic":
            if any(
                value is not None
                for value in (
                    self.model_provider,
                    self.model_name,
                    self.prompt_version,
                    self.agent_version,
                    self.model_usage,
                    self.provider_request_id,
                )
            ):
                raise ValueError(
                    "deterministic summary cannot declare model execution metadata"
                )
        else:
            if not self.model_name or not self.prompt_version:
                raise ValueError(
                    "llm_assisted summary requires model_name and prompt_version"
                )
            if not all(
                (
                    self.model_provider,
                    self.agent_version,
                    self.outline_digest,
                    self.source_fact_ids,
                )
            ):
                raise ValueError(
                    "llm_assisted summary requires provider, agent, outline, and facts"
                )
            if self.fallback_reason_codes:
                raise ValueError("llm_assisted summary cannot declare fallback reasons")
        return self


class ControlledSummaryOutcome(StrictModel):
    summary: Layer4SummaryDraft
    status: ControlledSummaryStatus
    reason_codes: list[str] = Field(default_factory=list)
    fact_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_status(self) -> "ControlledSummaryOutcome":
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("controlled summary outcome contains duplicate reasons")
        if any(not item.strip() for item in self.reason_codes):
            raise ValueError("controlled summary outcome contains a blank reason")
        if self.status == ControlledSummaryStatus.LLM_ASSISTED:
            if self.reason_codes or self.summary.generation_mode != "llm_assisted":
                raise ValueError("LLM-assisted outcome cannot contain fallback reasons")
        elif self.summary.generation_mode != "deterministic":
            raise ValueError("fallback outcome requires deterministic summary")
        return self


class DoctorReview(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    review_id: str
    version: str = "1"
    summary_id: str
    summary_version: str
    patient_id: str
    reviewer_reference: str = Field(min_length=3)
    decision: DoctorReviewDecision
    note: str | None = Field(default=None, min_length=1, max_length=4000)
    amended_summary_id: str | None = None
    amended_summary_version: str | None = None
    result_summary_id: str | None = None
    result_summary_version: str | None = None
    provenance_reference: str | None = None
    reviewed_at: str

    @model_validator(mode="after")
    def validate_decision(self) -> "DoctorReview":
        _parse_instant(self.reviewed_at, field_name="reviewed_at")
        if self.decision == DoctorReviewDecision.MODIFY:
            if (
                not self.note
                or not self.amended_summary_id
                or not self.amended_summary_version
            ):
                raise ValueError(
                    "modify review requires a note and amended summary id/version"
                )
        if self.decision == DoctorReviewDecision.REJECT and not self.note:
            raise ValueError("reject review requires a note")
        if (self.result_summary_id is None) != (self.result_summary_version is None):
            raise ValueError("review result summary requires both id and version")
        return self


class DoctorReviewOutcome(StrictModel):
    review: DoctorReview
    summary: Layer4SummaryDraft
    idempotent_replay: bool = False


class StateMetricDefinition(StrictModel):
    """Versioned configuration for one deterministic state/trend calculation."""

    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    metric_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    pathway_code: str = Field(min_length=1)
    pathway_version: str = Field(min_length=1)
    code_system: str = Field(min_length=1)
    code: str = Field(min_length=1)
    display: str = Field(min_length=1)
    unit: str | None = None
    unit_system: str | None = None
    lookback_hours: int = Field(gt=0)
    stale_after_hours: int = Field(gt=0)
    trend_window_hours: int = Field(gt=0)
    minimum_trend_points: int = Field(default=2, ge=2)
    algorithm_version: str = Field(default="endpoint-delta-v1", min_length=1)

    @model_validator(mode="after")
    def validate_windows(self) -> "StateMetricDefinition":
        if self.stale_after_hours > self.lookback_hours:
            raise ValueError("stale_after_hours cannot exceed lookback_hours")
        if self.unit_system and not self.unit:
            raise ValueError("unit_system requires unit")
        return self


class MetricState(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    metric_id: str
    metric_version: str
    status: MetricStateStatus
    as_of: str
    latest_value: Any | None = None
    unit: str | None = None
    unit_system: str | None = None
    effective_start: str | None = None
    effective_end: str | None = None
    recorded_at: str | None = None
    age_hours: float | None = Field(default=None, ge=0)
    latest_observation: ResourceReference | None = None
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_state(self) -> "MetricState":
        _parse_instant(self.as_of, field_name="as_of")
        known_fields = (
            self.effective_start,
            self.effective_end,
            self.recorded_at,
            self.age_hours,
            self.latest_observation,
        )
        if self.status in {MetricStateStatus.CURRENT, MetricStateStatus.STALE}:
            if self.latest_value is None or any(item is None for item in known_fields):
                raise ValueError("current or stale metric state requires a last known value")
            if not self.evidence_refs:
                raise ValueError("current or stale metric state requires evidence")
            start = _parse_instant(self.effective_start or "", field_name="effective_start")
            end = _parse_instant(self.effective_end or "", field_name="effective_end")
            _parse_instant(self.recorded_at or "", field_name="recorded_at")
            if end < start:
                raise ValueError("metric effective_end cannot precede effective_start")
        elif self.status == MetricStateStatus.UNKNOWN:
            if self.latest_value is not None or any(item is not None for item in known_fields):
                raise ValueError("unknown metric state cannot contain a last known value")
            if self.evidence_refs:
                raise ValueError("unknown metric state cannot claim observation evidence")
        elif self.status == MetricStateStatus.CONFLICT:
            if self.latest_value is not None or any(item is not None for item in known_fields):
                raise ValueError("conflicting metric state cannot choose a last known value")
            if len(self.evidence_refs) < 2:
                raise ValueError("conflicting metric state requires at least two evidence items")
        evidence_ids = [item.evidence_id for item in self.evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("metric state contains duplicate evidence ids")
        return self


class NumericTrend(StrictModel):
    """Unit-consistent endpoint delta without good/bad clinical semantics."""

    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    metric_id: str
    metric_version: str
    status: TrendCalculationStatus
    direction: TrendDirection | None = None
    first_value: str | None = None
    last_value: str | None = None
    delta: str | None = None
    unit: str | None = None
    unit_system: str | None = None
    point_count: int = Field(ge=0)
    period_start: str
    period_end: str
    algorithm_version: str = Field(min_length=1)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_trend(self) -> "NumericTrend":
        start = _parse_instant(self.period_start, field_name="period_start")
        end = _parse_instant(self.period_end, field_name="period_end")
        if end < start:
            raise ValueError("trend period_end cannot precede period_start")
        values = (self.first_value, self.last_value, self.delta)
        if self.status == TrendCalculationStatus.CALCULATED:
            if self.direction is None or any(item is None for item in values):
                raise ValueError("calculated trend requires direction and endpoint values")
            if self.point_count < 2 or len(self.evidence_refs) != self.point_count:
                raise ValueError("calculated trend requires evidence for every point")
        elif self.direction is not None or any(item is not None for item in values):
            raise ValueError("uncalculated trend cannot expose a direction or delta")
        evidence_ids = [item.evidence_id for item in self.evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("numeric trend contains duplicate evidence ids")
        return self


class ClinicalStateSnapshot(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    snapshot_id: str
    version: str = "1"
    patient_id: str
    pathway_code: str
    pathway_version: str
    as_of: str
    metric_definitions: list[StateMetricDefinition] = Field(default_factory=list)
    metric_definition_refs: list[ResourceReference] = Field(default_factory=list)
    states: list[MetricState] = Field(default_factory=list)
    trends: list[NumericTrend] = Field(default_factory=list)
    source_observation_refs: list[ResourceReference] = Field(default_factory=list)
    provenance_refs: list[ResourceReference] = Field(default_factory=list)
    algorithm_version: str = Field(default="clinical-state-v1", min_length=1)
    created_at: str

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ClinicalStateSnapshot":
        _parse_instant(self.as_of, field_name="as_of")
        _parse_instant(self.created_at, field_name="created_at")
        state_ids = [item.metric_id for item in self.states]
        trend_ids = [item.metric_id for item in self.trends]
        embedded_definition_ids = [item.metric_id for item in self.metric_definitions]
        definition_ids = [item.reference for item in self.metric_definition_refs]
        source_ids = [
            (item.reference, item.version_id) for item in self.source_observation_refs
        ]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("state snapshot contains duplicate metric states")
        if len(trend_ids) != len(set(trend_ids)):
            raise ValueError("state snapshot contains duplicate metric trends")
        if set(state_ids) != set(trend_ids):
            raise ValueError("state snapshot requires one trend result per metric state")
        if set(state_ids) != set(embedded_definition_ids):
            raise ValueError("state snapshot requires each metric definition for replay")
        if len(embedded_definition_ids) != len(set(embedded_definition_ids)):
            raise ValueError("state snapshot contains duplicate embedded definitions")
        if len(definition_ids) != len(set(definition_ids)):
            raise ValueError("state snapshot contains duplicate metric definitions")
        if len(definition_ids) != len(self.metric_definitions):
            raise ValueError("state snapshot definition references are incomplete")
        expected_definition_refs = {
            (
                f"urn:continucare:metric-definition:{item.metric_id}"
                f":version:{item.version}",
                item.version,
            )
            for item in self.metric_definitions
        }
        actual_definition_refs = {
            (item.reference, item.version_id) for item in self.metric_definition_refs
        }
        if actual_definition_refs != expected_definition_refs:
            raise ValueError("state snapshot definition references do not match payloads")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("state snapshot contains duplicate source observations")
        return self


class WorkbenchAccessContext(StrictModel):
    actor_reference: str = Field(min_length=3)
    role: WorkbenchRole
    purpose: WorkbenchPurpose
    permitted_patient_ids: list[str] = Field(min_length=1)
    identity_verified: bool = False

    @model_validator(mode="after")
    def validate_access_claim(self) -> "WorkbenchAccessContext":
        if len(self.permitted_patient_ids) != len(set(self.permitted_patient_ids)):
            raise ValueError("workbench access contains duplicate patient ids")
        expected_purpose = {
            WorkbenchRole.DOCTOR: WorkbenchPurpose.TREATMENT,
            WorkbenchRole.CLINICAL_AUDITOR: WorkbenchPurpose.AUDIT,
            WorkbenchRole.NURSE: WorkbenchPurpose.OPERATIONS,
        }[self.role]
        if self.purpose != expected_purpose:
            raise ValueError("workbench role and purpose are inconsistent")
        return self


class WorkbenchComponentResult(StrictModel):
    component: WorkbenchComponent
    status: ComponentReadStatus
    reason_code: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def validate_degradation(self) -> "WorkbenchComponentResult":
        if self.status == ComponentReadStatus.DEGRADED:
            if not self.reason_code or not self.message:
                raise ValueError("degraded component requires reason code and message")
        elif self.reason_code or self.message:
            raise ValueError("non-degraded component cannot declare an error")
        return self


class DoctorWorkbenchView(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    view_id: str
    patient_id: str
    pathway_code: str
    pathway_version: str
    as_of: str
    generated_at: str
    actor_reference: str
    role: WorkbenchRole
    timeline: list[TimelineEvent] = Field(default_factory=list)
    state_snapshot: ClinicalStateSnapshot | None = None
    summary: Layer4SummaryDraft | None = None
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    evidence_roots: list[str] = Field(default_factory=list)
    components: list[WorkbenchComponentResult]
    degraded: bool = False

    @model_validator(mode="after")
    def validate_view(self) -> "DoctorWorkbenchView":
        as_of = _parse_instant(self.as_of, field_name="as_of")
        generated = _parse_instant(self.generated_at, field_name="generated_at")
        if generated < as_of:
            raise ValueError("workbench generated_at cannot precede as_of")
        component_names = [item.component for item in self.components]
        if set(component_names) != set(WorkbenchComponent):
            raise ValueError("workbench requires one result for every component")
        if len(component_names) != len(set(component_names)):
            raise ValueError("workbench contains duplicate component results")
        has_degraded = any(
            item.status == ComponentReadStatus.DEGRADED for item in self.components
        )
        if self.degraded != has_degraded:
            raise ValueError("workbench degraded flag conflicts with component results")
        if self.state_snapshot is not None and (
            self.state_snapshot.patient_id != self.patient_id
            or self.state_snapshot.pathway_code != self.pathway_code
            or self.state_snapshot.pathway_version != self.pathway_version
        ):
            raise ValueError("workbench state snapshot is outside the requested scope")
        if self.summary is not None and (
            self.summary.patient_id != self.patient_id
            or self.summary.pathway_code != self.pathway_code
            or self.summary.pathway_version != self.pathway_version
        ):
            raise ValueError("workbench summary is outside the requested scope")
        if len(self.evidence_roots) != len(set(self.evidence_roots)):
            raise ValueError("workbench contains duplicate evidence roots")
        return self


class EvidenceTraceArtifact(StrictModel):
    reference: str
    artifact_type: EvidenceArtifactType
    resource_type: str | None = None
    record_type: str | None = None
    version: str | None = None
    payload: dict[str, Any]


class EvidenceTraceEdge(StrictModel):
    source_reference: str
    target_reference: str
    relation: str = Field(min_length=1)


class EvidenceTrace(StrictModel):
    contract_version: Literal["1.0.0"] = LAYER4_CONTRACT_VERSION
    trace_id: str
    patient_id: str
    root_reference: str
    as_of: str
    actor_reference: str
    artifacts: list[EvidenceTraceArtifact] = Field(default_factory=list)
    edges: list[EvidenceTraceEdge] = Field(default_factory=list)
    unresolved_references: list[str] = Field(default_factory=list)
    degraded: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    truncated: bool = False

    @model_validator(mode="after")
    def validate_trace(self) -> "EvidenceTrace":
        _parse_instant(self.as_of, field_name="as_of")
        artifact_refs = [item.reference for item in self.artifacts]
        if len(artifact_refs) != len(set(artifact_refs)):
            raise ValueError("evidence trace contains duplicate artifacts")
        if len(self.unresolved_references) != len(set(self.unresolved_references)):
            raise ValueError("evidence trace contains duplicate unresolved references")
        if self.degraded != bool(self.reason_codes):
            raise ValueError("evidence trace degraded flag conflicts with reason codes")
        return self


Layer4ContractRecord = (
    ClinicalRuleDefinition
    | MemoryEvent
    | TimelineEvent
    | RevisionLink
    | Layer4SummaryDraft
    | DoctorReview
    | ClinicalStateSnapshot
)

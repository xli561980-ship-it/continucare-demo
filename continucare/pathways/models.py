"""Governance manifest for versioned, FHIR-native Care Pathways."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathwayStatus(str, Enum):
    DRAFT = "draft"
    CLINICAL_REVIEW = "clinical_review"
    PILOT = "pilot"
    ACTIVE = "active"
    RETIRED = "retired"


class ApprovalStatus(str, Enum):
    NOT_REVIEWED = "not_reviewed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"


class KnowledgeSource(StrictModel):
    source_id: str
    title: str
    source_type: str
    authority: str
    url: str
    version: str | None = None
    applicable_section: str | None = None
    accessed_at: str


class ApprovalPolicy(StrictModel):
    required: bool = True
    status: ApprovalStatus = ApprovalStatus.NOT_REVIEWED
    clinical_approver: str | None = None
    terminology_approver: str | None = None
    approved_at: str | None = None


class FHIRArtifactReference(StrictModel):
    resource_type: Literal["Questionnaire", "PlanDefinition"]
    canonical: str
    version: str
    file_name: str


class ClinicalRule(StrictModel):
    rule_id: str
    title: str
    description: str
    evidence_source_ids: list[str] = Field(min_length=1)
    status: ApprovalStatus = ApprovalStatus.NOT_REVIEWED
    clinical_approver: str | None = None


class PathwayDefinition(StrictModel):
    pathway_id: str
    code: str
    name: str
    department: str
    version: str
    status: PathwayStatus
    synthetic_only: bool = True
    description: str
    clinical_safety_note: str
    fhir_version: Literal["4.0.1"]
    artifacts: list[FHIRArtifactReference] = Field(min_length=2)
    observation_mapping_file: str
    evidence_document: str
    knowledge_sources: list[KnowledgeSource] = Field(min_length=1)
    clinical_rules: list[ClinicalRule] = Field(default_factory=list)
    approval: ApprovalPolicy

    @model_validator(mode="after")
    def validate_governance(self) -> "PathwayDefinition":
        source_ids = [item.source_id for item in self.knowledge_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("pathway contains duplicate knowledge source ids")

        artifact_keys = [
            (item.resource_type, item.canonical, item.version) for item in self.artifacts
        ]
        if len(artifact_keys) != len(set(artifact_keys)):
            raise ValueError("pathway contains duplicate FHIR artifact references")
        artifact_types = {item.resource_type for item in self.artifacts}
        if artifact_types != {"Questionnaire", "PlanDefinition"}:
            raise ValueError(
                "pathway must reference the governed Questionnaire and PlanDefinition types"
            )

        source_set = set(source_ids)
        for rule in self.clinical_rules:
            unknown = set(rule.evidence_source_ids) - source_set
            if unknown:
                raise ValueError(
                    f"clinical rule {rule.rule_id} references unknown evidence sources: "
                    f"{', '.join(sorted(unknown))}"
                )
            if rule.status == ApprovalStatus.APPROVED and not rule.clinical_approver:
                raise ValueError(
                    f"approved clinical rule {rule.rule_id} requires a clinical approver"
                )

        if self.status == PathwayStatus.ACTIVE:
            if self.synthetic_only:
                raise ValueError("an active pathway cannot remain synthetic_only")
            if self.approval.status != ApprovalStatus.APPROVED:
                raise ValueError("an active pathway must have full approval")
            if not self.approval.clinical_approver:
                raise ValueError("an active pathway requires a clinical approver")
            if not self.approval.terminology_approver:
                raise ValueError("an active pathway requires a terminology approver")
            if any(
                rule.status != ApprovalStatus.APPROVED
                for rule in self.clinical_rules
            ):
                raise ValueError("every active clinical rule must be approved")
        return self

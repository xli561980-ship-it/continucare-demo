"""Strict, versioned contracts for read-only Knowledge Evidence.

These models describe registered knowledge and its relationship to governed
artifacts.  They deliberately contain no execution or publication authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)


KNOWLEDGE_CONTRACT_VERSION = "1.0.0"
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_LOCATOR_PLACEHOLDERS = frozenset(
    {
        "n/a",
        "na",
        "none",
        "unknown",
        "whole document",
        "not available",
        "not applicable",
        "tbd",
        "todo",
    }
)


def reject_locator_placeholder(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    first_token = normalized.split(maxsplit=1)[0].rstrip(".:#")
    if normalized in _LOCATOR_PLACEHOLDERS or first_token in {"tbd", "todo", "unknown"}:
        raise ValueError("citation locator cannot be placeholder text")
    return value


def safe_relative_parts(value: str) -> tuple[str, ...]:
    """Return canonical POSIX path parts or reject traversal and ambiguity."""

    if not value or "\\" in value or value.startswith("/"):
        raise ValueError("resource path must be a non-empty POSIX relative path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("resource path cannot contain empty, '.' or '..' segments")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or tuple(parsed.parts) != tuple(raw_parts):
        raise ValueError("resource path is not canonical")
    return tuple(raw_parts)


class KnowledgeBundleError(RuntimeError):
    """Base error for atomic knowledge-bundle loading."""


class KnowledgePinnedFileError(KnowledgeBundleError):
    pass


class KnowledgeSchemaError(KnowledgeBundleError):
    pass


class KnowledgeReferenceError(KnowledgeBundleError):
    pass


class KnowledgeArtifactUnresolved(KnowledgeBundleError):
    pass


class KnowledgeCurrentSelectionError(KnowledgeBundleError):
    pass


class KnowledgeAuthorityError(KnowledgeBundleError):
    pass


class KnowledgeSourceArtifactError(KnowledgeBundleError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class SourceRef(StrictModel):
    source_id: NonBlank
    record_version: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        return self.source_id, self.record_version

    def __str__(self) -> str:
        return f"src:{self.source_id}@{self.record_version}"


class ClaimRef(StrictModel):
    claim_id: NonBlank
    claim_version: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        return self.claim_id, self.claim_version

    def __str__(self) -> str:
        return f"claim:{self.claim_id}@{self.claim_version}"


class BindingRef(StrictModel):
    binding_id: NonBlank
    binding_version: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        return self.binding_id, self.binding_version

    def __str__(self) -> str:
        return f"binding:{self.binding_id}@{self.binding_version}"


class CoverageGapRef(StrictModel):
    gap_id: NonBlank
    gap_version: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        return self.gap_id, self.gap_version

    def __str__(self) -> str:
        return f"gap:{self.gap_id}@{self.gap_version}"


class SymptomIndexRef(StrictModel):
    symptom_index_id: NonBlank
    record_version: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        return self.symptom_index_id, self.record_version

    def __str__(self) -> str:
        return f"symptom-index:{self.symptom_index_id}@{self.record_version}"


class CatalogTermRef(StrictModel):
    catalog_id: NonBlank
    catalog_version: NonBlank
    concept_id: NonBlank

    def key(self) -> tuple[str, str, str]:
        return self.catalog_id, self.catalog_version, self.concept_id


class FileRef(StrictModel):
    file_id: NonBlank
    file_version: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        return self.file_id, self.file_version

    def __str__(self) -> str:
        return f"file:{self.file_id}@{self.file_version}"


class PathwayRef(StrictModel):
    pathway_code: NonBlank
    pathway_version: NonBlank

    def key(self) -> tuple[str, str]:
        return self.pathway_code, self.pathway_version


class CitationRef(StrictModel):
    claim: ClaimRef
    citation_id: NonBlank

    def key(self) -> tuple[str, int, str]:
        return (*self.claim.key(), self.citation_id)


class PageLocator(StrictModel):
    locator_type: Literal["page"] = "page"
    page_number: int = Field(ge=1)
    page_label: NonBlank | None = None

    @field_validator("page_label")
    @classmethod
    def validate_page_label(cls, value: str | None) -> str | None:
        return None if value is None else reject_locator_placeholder(value)


class SectionLocator(StrictModel):
    locator_type: Literal["section"] = "section"
    section_number: Annotated[str, StringConstraints(pattern=r"^.*[A-Za-z0-9].*$")]
    section_title: NonBlank | None = None

    @field_validator("section_number", "section_title")
    @classmethod
    def validate_exact_text(cls, value: str | None) -> str | None:
        return None if value is None else reject_locator_placeholder(value)

    @model_validator(mode="after")
    def validate_structured_section(self) -> "SectionLocator":
        if not any(character.isdigit() for character in self.section_number):
            if self.section_title is None:
                raise ValueError(
                    "a non-numeric section identifier requires an exact section title"
                )
        return self


class TableOrFigureLocator(StrictModel):
    locator_type: Literal["table_or_figure"] = "table_or_figure"
    kind: Literal["table", "figure"]
    label: Annotated[str, StringConstraints(pattern=r"^.*[0-9].*$")]

    @field_validator("label")
    @classmethod
    def validate_exact_label(cls, value: str) -> str:
        return reject_locator_placeholder(value)


class TerminologyConceptLocator(StrictModel):
    locator_type: Literal["terminology_concept"] = "terminology_concept"
    code_system: NonBlank
    code_system_release: NonBlank
    code: NonBlank

    @field_validator("code_system", "code_system_release", "code")
    @classmethod
    def validate_exact_concept_fields(cls, value: str) -> str:
        return reject_locator_placeholder(value)


class AnswerListLocator(StrictModel):
    locator_type: Literal["answer_list"] = "answer_list"
    code_system: NonBlank
    code_system_release: NonBlank
    list_code: NonBlank

    @field_validator("code_system", "code_system_release", "list_code")
    @classmethod
    def validate_exact_answer_list_fields(cls, value: str) -> str:
        return reject_locator_placeholder(value)


class UrlFragmentLocator(StrictModel):
    locator_type: Literal["url_fragment"] = "url_fragment"
    fragment: NonBlank

    @field_validator("fragment")
    @classmethod
    def validate_exact_fragment(cls, value: str) -> str:
        return reject_locator_placeholder(value)


CitationLocator = Annotated[
    PageLocator
    | SectionLocator
    | TableOrFigureLocator
    | TerminologyConceptLocator
    | AnswerListLocator
    | UrlFragmentLocator,
    Field(discriminator="locator_type"),
]


class SourceType(StrEnum):
    REGULATORY_PRODUCT_INFORMATION = "regulatory_product_information"
    REGULATORY_SAFETY_COMMUNICATION = "regulatory_safety_communication"
    CLINICAL_PRACTICE_GUIDELINE = "clinical_practice_guideline"
    EXPERT_CONSENSUS = "expert_consensus"
    PEER_REVIEWED_STUDY = "peer_reviewed_study"
    STANDARD_INSTRUMENT = "standard_instrument"
    INTEROPERABILITY_STANDARD = "interoperability_standard"
    TERMINOLOGY_STANDARD = "terminology_standard"
    UNIT_STANDARD = "unit_standard"
    PUBLIC_INSTITUTION_MATERIAL = "public_institution_material"
    INSTITUTIONAL_PROTOCOL = "institutional_protocol"


class AuthorityType(StrEnum):
    REGULATOR = "regulator"
    STANDARDS_BODY = "standards_body"
    PROFESSIONAL_SOCIETY = "professional_society"
    JOURNAL = "journal"
    PUBLIC_INSTITUTION = "public_institution"
    HOSPITAL = "hospital"


class TypedJurisdiction(StrictModel):
    system: Literal["iso3166_1", "iso3166_2", "unm49", "supranational", "global"]
    code: NonBlank


class DocumentIdentifier(StrictModel):
    scheme: Literal[
        "doi",
        "pmid",
        "pmcid",
        "fda_reference_id",
        "ema_product_number",
        "dailymed_setid",
        "isbn",
        "urn",
        "other",
    ]
    value: NonBlank


class AccessUrl(StrictModel):
    url: AnyHttpUrl
    url_role: Literal["landing", "fulltext", "pdf", "mirror"]


class Retrieval(StrictModel):
    precision: Literal["date", "instant"]
    retrieved_on: date
    retrieved_at: datetime | None = None

    @model_validator(mode="after")
    def validate_precision(self) -> "Retrieval":
        if self.precision == "date" and self.retrieved_at is not None:
            raise ValueError("date-precision retrieval cannot include retrieved_at")
        if self.precision == "instant":
            if self.retrieved_at is None or self.retrieved_at.tzinfo is None:
                raise ValueError("instant retrieval requires timezone-aware retrieved_at")
            if self.retrieved_at.date() != self.retrieved_on:
                raise ValueError("retrieved_at date must equal retrieved_on")
        return self


class LinkOnlyAccess(StrictModel):
    mode: Literal["link_only"] = "link_only"
    integrity: Literal["not_content_fixed"] = "not_content_fixed"


class LocalArtifactRef(StrictModel):
    relative_path: NonBlank
    third_party_content_sha256: Sha256
    size: int | None = Field(default=None, ge=0)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        safe_relative_parts(value)
        return value


class LocalArtifactAccess(StrictModel):
    mode: Literal["local_artifact"] = "local_artifact"
    artifact: LocalArtifactRef


SourceAccess = Annotated[LinkOnlyAccess | LocalArtifactAccess, Field(discriminator="mode")]


class SourceRegistryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class RepositoryMetadataStatus(StrEnum):
    REPOSITORY_DECLARED_UNVERIFIED = "repository_declared_unverified"
    NOT_AVAILABLE_IN_REPOSITORY = "not_available_in_repository"


class SourceRecord(StrictModel):
    source_id: NonBlank
    record_version: int = Field(ge=1)
    title: NonBlank
    language: NonBlank = "und"
    source_type: SourceType
    authority_metadata_status: RepositoryMetadataStatus
    issuing_authority: NonBlank | None = None
    authority_type: AuthorityType | None = None
    jurisdiction_metadata_status: RepositoryMetadataStatus
    jurisdictions: tuple[TypedJurisdiction, ...] = Field(default_factory=tuple)
    document_identifiers: tuple[DocumentIdentifier, ...] = Field(default_factory=tuple)
    canonical_url: AnyHttpUrl | None = None
    access_urls: tuple[AccessUrl, ...] = Field(min_length=1)
    document_version: NonBlank | None = None
    document_effective_date: date | None = None
    document_publication_date: date | None = None
    retrieval: Retrieval
    access: SourceAccess
    license_terms_uri: AnyHttpUrl | None = None
    license_terms_locator: CitationLocator | None = None
    manifestation_of: SourceRef | None = None
    registry_status: SourceRegistryStatus = SourceRegistryStatus.ACTIVE
    supersedes: SourceRef | None = None
    registered_at: datetime
    registered_by: NonBlank

    @model_validator(mode="after")
    def validate_record(self) -> "SourceRecord":
        if self.registered_at.tzinfo is None:
            raise ValueError("registered_at must include a timezone")
        if (self.issuing_authority is None) != (self.authority_type is None):
            raise ValueError("issuing_authority and authority_type must be supplied together")
        if self.authority_metadata_status == RepositoryMetadataStatus.NOT_AVAILABLE_IN_REPOSITORY:
            if self.issuing_authority is not None:
                raise ValueError("unavailable authority metadata cannot include an authority")
        elif self.issuing_authority is None:
            raise ValueError("repository-declared authority metadata requires an authority")
        if self.jurisdiction_metadata_status == RepositoryMetadataStatus.NOT_AVAILABLE_IN_REPOSITORY:
            if self.jurisdictions:
                raise ValueError("unavailable jurisdiction metadata cannot include jurisdictions")
        elif not self.jurisdictions:
            raise ValueError("repository-declared jurisdiction metadata requires a jurisdiction")
        if self.supersedes and self.supersedes.source_id != self.source_id:
            raise ValueError("a source can only supersede the same logical source_id")
        if self.record_version == 1 and self.supersedes is not None:
            raise ValueError("source version 1 cannot supersede another version")
        if self.manifestation_of is not None and self.manifestation_of.key() == (
            self.source_id,
            self.record_version,
        ):
            raise ValueError("a source cannot be a manifestation of itself")
        return self

    @property
    def ref(self) -> SourceRef:
        return SourceRef(source_id=self.source_id, record_version=self.record_version)


class ScopeValue(StrictModel):
    system: NonBlank
    code: NonBlank
    display: NonBlank | None = None


class AnyScopeDimension(StrictModel):
    mode: Literal["any"] = "any"


class IncludeScopeDimension(StrictModel):
    mode: Literal["include"] = "include"
    values: tuple[ScopeValue, ...] = Field(min_length=1)


class NotApplicableScopeDimension(StrictModel):
    mode: Literal["not_applicable"] = "not_applicable"


ScopeDimension = Annotated[
    AnyScopeDimension | IncludeScopeDimension | NotApplicableScopeDimension,
    Field(discriminator="mode"),
]


class PathwayWhitelistScope(StrictModel):
    mode: Literal["pathway_whitelist"] = "pathway_whitelist"
    pathways: tuple[PathwayRef, ...] = Field(min_length=1)
    conditions: ScopeDimension
    products: ScopeDimension
    jurisdictions: ScopeDimension
    care_settings: ScopeDimension
    age: ScopeDimension
    population: NonBlank


class UniversalNonclinicalStandardScope(StrictModel):
    mode: Literal["universal_nonclinical_standard"] = "universal_nonclinical_standard"
    domain: Literal["terminology", "unit", "interoperability"]


ApplicableScope = Annotated[
    PathwayWhitelistScope | UniversalNonclinicalStandardScope,
    Field(discriminator="mode"),
]


class CitationRole(StrEnum):
    PRIMARY_BASIS = "primary_basis"
    CORROBORATING = "corroborating"
    LIMITATION = "limitation"
    TERMINOLOGY_DEFINITION = "terminology_definition"
    CONTRADICTING = "contradicting"


class Citation(StrictModel):
    citation_id: NonBlank
    source: SourceRef
    locator: CitationLocator
    role: CitationRole
    quote: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None


class ClaimLifecycle(StrEnum):
    DRAFT = "draft"
    REGISTERED = "registered"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class ClaimType(StrEnum):
    COLLECTION_RATIONALE = "collection_rationale"
    FOLLOWUP_FREQUENCY_CANDIDATE = "followup_frequency_candidate"
    TERMINOLOGY_SUPPORT = "terminology_support"
    RULE_CANDIDATE = "rule_candidate"
    RED_FLAG_CANDIDATE = "red_flag_candidate"
    EDUCATION_CANDIDATE = "education_candidate"
    SUMMARY_ELEMENT_CANDIDATE = "summary_element_candidate"
    SAFETY_LIMITATION = "safety_limitation"


class EvidenceStrength(StrEnum):
    REGULATORY_LABEL = "regulatory_label"
    CLINICAL_PRACTICE_GUIDELINE = "clinical_practice_guideline"
    EXPERT_CONSENSUS = "expert_consensus"
    STANDARDS_BODY = "standards_body"
    SINGLE_STUDY = "single_study"
    INSTITUTIONAL_PROTOCOL = "institutional_protocol"


class SourcedClinicalClaim(StrictModel):
    claim_kind: Literal["sourced_clinical_claim"] = "sourced_clinical_claim"
    claim_id: NonBlank
    claim_version: int = Field(ge=1)
    claim_type: ClaimType
    statement: NonBlank
    supports: tuple[NonBlank, ...] = Field(min_length=1)
    does_not_support: tuple[NonBlank, ...] = Field(min_length=1)
    applicable_scope: ApplicableScope
    evidence_strength: EvidenceStrength
    citations: tuple[Citation, ...] = Field(min_length=1)
    lifecycle: ClaimLifecycle = ClaimLifecycle.DRAFT
    supersedes: ClaimRef | None = None
    retraction_reason: NonBlank | None = None
    registered_at: datetime

    @model_validator(mode="after")
    def validate_claim(self) -> "SourcedClinicalClaim":
        if self.registered_at.tzinfo is None:
            raise ValueError("registered_at must include a timezone")
        if self.supersedes and self.supersedes.claim_id != self.claim_id:
            raise ValueError("a claim can only supersede the same logical claim_id")
        citation_ids = [item.citation_id for item in self.citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("citation_id values must be unique within a claim version")
        if self.lifecycle == ClaimLifecycle.RETRACTED and not self.retraction_reason:
            raise ValueError("retracted claim requires retraction_reason")
        if self.lifecycle != ClaimLifecycle.RETRACTED and self.retraction_reason:
            raise ValueError("retraction_reason is only valid for a retracted claim")
        if isinstance(self.applicable_scope, UniversalNonclinicalStandardScope):
            if self.claim_type != ClaimType.TERMINOLOGY_SUPPORT:
                raise ValueError("universal nonclinical scope only supports terminology claims")
            if self.evidence_strength != EvidenceStrength.STANDARDS_BODY:
                raise ValueError("universal nonclinical scope requires standards_body evidence")
        return self

    @property
    def ref(self) -> ClaimRef:
        return ClaimRef(claim_id=self.claim_id, claim_version=self.claim_version)


class WorkflowDesignDecision(StrictModel):
    claim_kind: Literal["workflow_design_decision"] = "workflow_design_decision"
    claim_id: NonBlank
    claim_version: int = Field(ge=1)
    statement: NonBlank
    supports: tuple[NonBlank, ...] = Field(min_length=1)
    does_not_support: tuple[NonBlank, ...] = Field(min_length=1)
    applicable_scope: PathwayWhitelistScope
    owner_role: NonBlank
    decision_rationale: NonBlank
    decision_status: Literal["proposed", "accepted", "rejected"]
    citations: tuple[None, ...] = Field(default_factory=tuple, max_length=0)
    evidence_strength: Literal["not_evidence_based"] = "not_evidence_based"
    lifecycle: ClaimLifecycle = ClaimLifecycle.DRAFT
    supersedes: ClaimRef | None = None
    retraction_reason: NonBlank | None = None
    registered_at: datetime

    @model_validator(mode="after")
    def validate_decision(self) -> "WorkflowDesignDecision":
        if self.registered_at.tzinfo is None:
            raise ValueError("registered_at must include a timezone")
        if self.supersedes and self.supersedes.claim_id != self.claim_id:
            raise ValueError("a claim can only supersede the same logical claim_id")
        if self.lifecycle == ClaimLifecycle.RETRACTED and not self.retraction_reason:
            raise ValueError("retracted claim requires retraction_reason")
        if self.lifecycle != ClaimLifecycle.RETRACTED and self.retraction_reason:
            raise ValueError("retraction_reason is only valid for a retracted claim")
        return self

    @property
    def ref(self) -> ClaimRef:
        return ClaimRef(claim_id=self.claim_id, claim_version=self.claim_version)


KnowledgeClaim = Annotated[
    SourcedClinicalClaim | WorkflowDesignDecision,
    Field(discriminator="claim_kind"),
]


class QuestionnaireItemRef(StrictModel):
    artifact_kind: Literal["questionnaire_item"] = "questionnaire_item"
    questionnaire_canonical: NonBlank
    questionnaire_version: NonBlank
    link_id: NonBlank


class ObservationMappingItemRef(StrictModel):
    artifact_kind: Literal["observation_mapping_item"] = "observation_mapping_item"
    pathway_code: NonBlank
    pathway_version: NonBlank
    mapping_file: NonBlank
    link_id: NonBlank


class QuestionnaireTerminologyBindingRef(StrictModel):
    artifact_kind: Literal["questionnaire_terminology_binding"] = (
        "questionnaire_terminology_binding"
    )
    catalog_id: NonBlank
    catalog_version: NonBlank
    link_id: NonBlank


class TerminologyConceptRef(StrictModel):
    artifact_kind: Literal["terminology_concept"] = "terminology_concept"
    catalog_id: NonBlank
    catalog_version: NonBlank
    concept_id: NonBlank


class PlanDefinitionRef(StrictModel):
    artifact_kind: Literal["plan_definition"] = "plan_definition"
    canonical: NonBlank
    version: NonBlank


class VersionedArtifactRef(StrictModel):
    artifact_kind: Literal[
        "clinical_rule",
        "red_flag_workflow",
        "education_content",
        "summary_definition",
    ]
    artifact_id: NonBlank
    artifact_version: NonBlank


ArtifactRef = Annotated[
    QuestionnaireItemRef
    | ObservationMappingItemRef
    | QuestionnaireTerminologyBindingRef
    | TerminologyConceptRef
    | PlanDefinitionRef
    | VersionedArtifactRef,
    Field(discriminator="artifact_kind"),
]


class BindingPurpose(StrEnum):
    JUSTIFIES_COLLECTION = "justifies_collection"
    JUSTIFIES_TERMINOLOGY_CHOICE = "justifies_terminology_choice"
    JUSTIFIES_DISPLAY = "justifies_display"
    DOCUMENTS_LIMITATION = "documents_limitation"
    DOCUMENTS_DESIGN_DECISION = "documents_design_decision"
    CANDIDATE_REFERENCE = "candidate_reference"


class ApprovalRequirement(StrEnum):
    CLINICAL_CONTENT_APPROVAL = "clinical_content_approval"
    TERMINOLOGY_APPROVAL = "terminology_approval"
    ARTIFACT_OWNER_APPROVAL = "artifact_owner_approval"
    QUESTIONNAIRE_PUBLICATION = "questionnaire_publication"
    OBSERVATION_MAPPING_APPROVAL = "observation_mapping_approval"
    PLAN_DEFINITION_PUBLICATION = "plan_definition_publication"
    CLINICAL_RULE_APPROVAL = "clinical_rule_approval"
    RED_FLAG_WORKFLOW_APPROVAL = "red_flag_workflow_approval"
    EDUCATION_PUBLICATION = "education_publication"
    SUMMARY_DEFINITION_APPROVAL = "summary_definition_approval"


PURPOSE_REQUIREMENTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        BindingPurpose.JUSTIFIES_COLLECTION: frozenset(
            {ApprovalRequirement.CLINICAL_CONTENT_APPROVAL}
        ),
        BindingPurpose.JUSTIFIES_DISPLAY: frozenset(
            {ApprovalRequirement.CLINICAL_CONTENT_APPROVAL}
        ),
        BindingPurpose.DOCUMENTS_LIMITATION: frozenset(
            {ApprovalRequirement.CLINICAL_CONTENT_APPROVAL}
        ),
        BindingPurpose.CANDIDATE_REFERENCE: frozenset(
            {ApprovalRequirement.CLINICAL_CONTENT_APPROVAL}
        ),
        BindingPurpose.JUSTIFIES_TERMINOLOGY_CHOICE: frozenset(
            {ApprovalRequirement.TERMINOLOGY_APPROVAL}
        ),
        BindingPurpose.DOCUMENTS_DESIGN_DECISION: frozenset(
            {ApprovalRequirement.ARTIFACT_OWNER_APPROVAL}
        ),
    }
)

ARTIFACT_REQUIREMENTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "questionnaire_item": frozenset(
            {ApprovalRequirement.QUESTIONNAIRE_PUBLICATION}
        ),
        "questionnaire_terminology_binding": frozenset(
            {ApprovalRequirement.QUESTIONNAIRE_PUBLICATION}
        ),
        "observation_mapping_item": frozenset(
            {ApprovalRequirement.OBSERVATION_MAPPING_APPROVAL}
        ),
        "terminology_concept": frozenset(
            {ApprovalRequirement.TERMINOLOGY_APPROVAL}
        ),
        "plan_definition": frozenset(
            {ApprovalRequirement.PLAN_DEFINITION_PUBLICATION}
        ),
        "clinical_rule": frozenset({ApprovalRequirement.CLINICAL_RULE_APPROVAL}),
        "red_flag_workflow": frozenset(
            {ApprovalRequirement.RED_FLAG_WORKFLOW_APPROVAL}
        ),
        "education_content": frozenset(
            {ApprovalRequirement.EDUCATION_PUBLICATION}
        ),
        "summary_definition": frozenset(
            {ApprovalRequirement.SUMMARY_DEFINITION_APPROVAL}
        ),
    }
)


def required_approvals(artifact_kind: str, purpose: str) -> tuple[str, ...]:
    return tuple(
        sorted(PURPOSE_REQUIREMENTS[purpose] | ARTIFACT_REQUIREMENTS[artifact_kind])
    )


class BindingLifecycle(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class BindingRecord(StrictModel):
    binding_id: NonBlank
    binding_version: int = Field(ge=1)
    pathway: PathwayRef
    artifact: ArtifactRef
    claim: ClaimRef
    binding_purpose: BindingPurpose
    required_independent_approvals: tuple[ApprovalRequirement, ...]
    lifecycle: BindingLifecycle = BindingLifecycle.ACTIVE
    supersedes: BindingRef | None = None
    note: NonBlank | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_binding(self) -> "BindingRecord":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        expected = required_approvals(self.artifact.artifact_kind, self.binding_purpose)
        actual = tuple(str(item) for item in self.required_independent_approvals)
        if actual != expected:
            raise ValueError(
                "required_independent_approvals must equal the deterministic "
                f"policy result {expected}"
            )
        if self.supersedes and self.supersedes.binding_id != self.binding_id:
            raise ValueError("a binding can only supersede the same logical binding_id")
        return self

    @property
    def ref(self) -> BindingRef:
        return BindingRef(binding_id=self.binding_id, binding_version=self.binding_version)


class CoverageGapKind(StrEnum):
    DESIGN_GOVERNANCE_METADATA = "design_governance_metadata"
    EXACT_TERMINOLOGY_BASIS = "exact_terminology_basis"
    PATIENT_EXPRESSION_EVIDENCE = "patient_expression_evidence"


class CoverageGapRecord(StrictModel):
    gap_id: NonBlank
    gap_version: int = Field(ge=1)
    pathway: PathwayRef
    artifact: ArtifactRef
    gap_kind: CoverageGapKind
    reason: NonBlank
    lifecycle: Literal["open", "superseded", "resolved"] = "open"
    supersedes: CoverageGapRef | None = None
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_gap(self) -> "CoverageGapRecord":
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
        if self.supersedes and self.supersedes.gap_id != self.gap_id:
            raise ValueError("a gap can only supersede the same logical gap_id")
        return self

    @property
    def ref(self) -> CoverageGapRef:
        return CoverageGapRef(gap_id=self.gap_id, gap_version=self.gap_version)


class SymptomIndexLifecycle(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class SymptomIndexRecord(StrictModel):
    """Reference-only symptom view key with no clinical or terminology payload."""

    symptom_index_id: NonBlank
    record_version: int = Field(ge=1)
    catalog_term: CatalogTermRef
    claim_refs: tuple[ClaimRef, ...] = Field(default_factory=tuple)
    binding_refs: tuple[BindingRef, ...] = Field(default_factory=tuple)
    coverage_gap_refs: tuple[CoverageGapRef, ...] = Field(default_factory=tuple)
    lifecycle: SymptomIndexLifecycle = SymptomIndexLifecycle.ACTIVE
    supersedes: SymptomIndexRef | None = None
    registered_at: datetime

    @model_validator(mode="after")
    def validate_record(self) -> "SymptomIndexRecord":
        if self.registered_at.tzinfo is None:
            raise ValueError("registered_at must include a timezone")
        if self.supersedes and self.supersedes.symptom_index_id != self.symptom_index_id:
            raise ValueError(
                "a symptom index can only supersede the same logical symptom_index_id"
            )
        if self.record_version == 1 and self.supersedes is not None:
            raise ValueError("symptom index version 1 cannot supersede another version")
        for refs, label in (
            (self.claim_refs, "claim"),
            (self.binding_refs, "binding"),
            (self.coverage_gap_refs, "coverage gap"),
        ):
            keys = [item.key() for item in refs]
            if len(keys) != len(set(keys)):
                raise ValueError(f"symptom index contains duplicate {label} refs")
        return self

    @property
    def ref(self) -> SymptomIndexRef:
        return SymptomIndexRef(
            symptom_index_id=self.symptom_index_id,
            record_version=self.record_version,
        )


class ReviewDecision(StrEnum):
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    APPROVED = "approved"


class FinalDecision(StrEnum):
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    APPROVED = "approved"


class ReviewEventBase(StrictModel):
    event_id: NonBlank
    supersedes_event_id: NonBlank | None = None
    actor_reference: NonBlank
    decided_at: datetime
    rationale: NonBlank

    @field_validator("decided_at")
    @classmethod
    def decided_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("decided_at must include a timezone")
        return value


class ClinicalReviewEvent(ReviewEventBase):
    event_type: Literal["clinical"] = "clinical"
    subject: ClaimRef
    decision: ReviewDecision
    claimed_role: Literal["clinician"]


class PharmacyReviewEvent(ReviewEventBase):
    event_type: Literal["pharmacy"] = "pharmacy"
    subject: ClaimRef
    decision: ReviewDecision
    claimed_role: Literal["pharmacist"]


class TerminologyReviewEvent(ReviewEventBase):
    event_type: Literal["terminology"] = "terminology"
    subject: ClaimRef
    decision: ReviewDecision
    claimed_role: Literal["terminologist"]


class InternalConsistencyReviewEvent(ReviewEventBase):
    event_type: Literal["internal_consistency"] = "internal_consistency"
    subject: SourceRef | ClaimRef
    decision: ReviewDecision
    claimed_role: Literal["knowledge_curator"]


class CitationVerificationEvent(ReviewEventBase):
    event_type: Literal["citation_verification"] = "citation_verification"
    subject: CitationRef
    decision: ReviewDecision
    claimed_role: Literal["knowledge_curator", "librarian", "terminologist", "clinician"]


class LicenseDecisionPayload(StrictModel):
    allowed_uses: tuple[
        Literal["link", "short_quote", "local_copy", "redistribute"], ...
    ] = Field(min_length=1)


class LicenseDecisionEvent(ReviewEventBase):
    event_type: Literal["license_decision"] = "license_decision"
    subject: SourceRef
    decision: FinalDecision
    claimed_role: Literal["rights_officer", "compliance_officer"]
    payload: LicenseDecisionPayload | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "LicenseDecisionEvent":
        if self.decision == FinalDecision.APPROVED and self.payload is None:
            raise ValueError("approved license decision requires allowed_uses")
        if self.decision != FinalDecision.APPROVED and self.payload is not None:
            raise ValueError("non-approved license decision cannot include allowed_uses")
        return self


class EquivalenceSubject(StrictModel):
    manifestation: SourceRef
    canonical: SourceRef


class EquivalenceDecisionEvent(ReviewEventBase):
    event_type: Literal["equivalence"] = "equivalence"
    subject: EquivalenceSubject
    decision: FinalDecision
    claimed_role: Literal["librarian", "knowledge_curator"]


ReviewEvent = Annotated[
    ClinicalReviewEvent
    | PharmacyReviewEvent
    | TerminologyReviewEvent
    | InternalConsistencyReviewEvent
    | CitationVerificationEvent
    | LicenseDecisionEvent
    | EquivalenceDecisionEvent,
    Field(discriminator="event_type"),
]


class LegacySourceAlias(StrictModel):
    namespace: Literal["pathway_manifest", "terminology_catalog"]
    legacy_id: NonBlank
    target: SourceRef


class ArtifactOwnership(StrictModel):
    catalog_id: NonBlank
    catalog_version: NonBlank
    owner: PathwayRef


class PinnedFile(StrictModel):
    ref: FileRef
    relative_path: NonBlank
    manifest_sha256: Sha256
    size: int | None = Field(default=None, ge=0)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        safe_relative_parts(value)
        return value


class EnvelopeBase(StrictModel):
    file_id: NonBlank
    file_version: int = Field(ge=1)
    contract_version: Literal[KNOWLEDGE_CONTRACT_VERSION] = KNOWLEDGE_CONTRACT_VERSION

    @property
    def ref(self) -> FileRef:
        return FileRef(file_id=self.file_id, file_version=self.file_version)


class SourceRegistryFile(EnvelopeBase):
    file_kind: Literal["source_registry"] = "source_registry"
    records: tuple[SourceRecord, ...]


class ClaimRegistryFile(EnvelopeBase):
    file_kind: Literal["claim_registry"] = "claim_registry"
    records: tuple[KnowledgeClaim, ...]


class BindingManifestFile(EnvelopeBase):
    file_kind: Literal["binding_manifest"] = "binding_manifest"
    pathway: PathwayRef
    records: tuple[BindingRecord, ...]


class GovernanceRegistryFile(EnvelopeBase):
    file_kind: Literal["governance_registry"] = "governance_registry"
    review_events: tuple[ReviewEvent, ...] = Field(default_factory=tuple)
    legacy_source_aliases: tuple[LegacySourceAlias, ...] = Field(default_factory=tuple)
    artifact_ownership: tuple[ArtifactOwnership, ...] = Field(default_factory=tuple)
    coverage_gaps: tuple[CoverageGapRecord, ...] = Field(default_factory=tuple)


class SymptomIndexFile(EnvelopeBase):
    file_kind: Literal["symptom_index"] = "symptom_index"
    records: tuple[SymptomIndexRecord, ...]


PayloadEnvelope = Annotated[
    SourceRegistryFile
    | ClaimRegistryFile
    | BindingManifestFile
    | GovernanceRegistryFile
    | SymptomIndexFile,
    Field(discriminator="file_kind"),
]


class KnowledgeBundleIndex(StrictModel):
    file_kind: Literal["bundle_index"] = "bundle_index"
    bundle_id: NonBlank
    bundle_version: int = Field(ge=1)
    contract_version: Literal[KNOWLEDGE_CONTRACT_VERSION] = KNOWLEDGE_CONTRACT_VERSION
    files: tuple[PinnedFile, ...] = Field(min_length=1)
    current_source_refs: tuple[SourceRef, ...]
    current_claim_refs: tuple[ClaimRef, ...]
    current_binding_refs: tuple[BindingRef, ...]
    current_gap_refs: tuple[CoverageGapRef, ...]
    current_symptom_index_refs: tuple[SymptomIndexRef, ...] = Field(
        default_factory=tuple
    )


class ReviewAggregate(StrEnum):
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    CLINICIAN_REVIEWED = "clinician_reviewed"
    INTERNALLY_CHECKED = "internally_checked"
    IN_REVIEW = "in_review"
    NOT_ASSESSED = "not_assessed"
    DESIGN_DOCUMENTED = "design_documented"


class ReviewSummary(StrictModel):
    aggregate: ReviewAggregate
    axes: Mapping[str, str]
    pharmacy: str | None = None

    @field_validator("axes", mode="after")
    @classmethod
    def freeze_axes(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @field_serializer("axes")
    def serialize_axes(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


def artifact_key(artifact: ArtifactRef) -> tuple[str, ...]:
    values = artifact.model_dump(mode="json")
    return (artifact.artifact_kind, *(str(values[key]) for key in sorted(values) if key != "artifact_kind"))

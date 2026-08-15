"""Strict v2 contracts for governed knowledge operations.

The v1 Knowledge Evidence registry remains the curated, read-only knowledge
surface.  These contracts describe how future material may be discovered,
staged, reviewed, and assessed for an informational release.  They do not
grant clinical or runtime authority.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


KNOWLEDGE_OPS_CONTRACT_VERSION = "2.0.0"
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SafeId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
LanguageCode = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$"),
]


class KnowledgeOpsError(RuntimeError):
    """Base error for v2 governance and acquisition operations."""


class KnowledgeOpsManifestError(KnowledgeOpsError):
    pass


class KnowledgeOpsPolicyError(KnowledgeOpsError):
    pass


class KnowledgeOpsIntegrityError(KnowledgeOpsError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class AuthorProvenance(StrictModel):
    """Structured authorship pin; it is not itself a clinical approval."""

    author_identity_id: SafeId
    author_principal_id: SafeId
    authored_at: datetime
    provenance_reference: NonBlank
    provenance_evidence_sha256: Sha256 | None = None
    synthetic: bool

    @field_validator("authored_at")
    @classmethod
    def authored_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("authored_at must include a timezone")
        return value


def safe_relative_parts(value: str) -> tuple[str, ...]:
    """Return canonical POSIX path parts or reject traversal and ambiguity."""

    if not value or "\\" in value or value.startswith("/"):
        raise ValueError("path must be a non-empty POSIX relative path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("path cannot contain empty, '.' or '..' segments")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or tuple(parsed.parts) != tuple(raw_parts):
        raise ValueError("path is not canonical")
    return tuple(raw_parts)


class KnowledgeLayer(StrEnum):
    L1_TERMINOLOGY = "L1_terminology"
    L2_PATIENT_QUESTION = "L2_patient_question"
    L3_SEVERITY_GRADING = "L3_severity_grading"
    L4_HIGH_RISK_ESCALATION = "L4_high_risk_escalation"
    L5_FREQUENCY_PHARMACOVIGILANCE = "L5_frequency_pharmacovigilance"
    L6_PATIENT_EDUCATION = "L6_patient_education"


class IntendedUse(StrEnum):
    INTERNAL_KNOWLEDGE_OPERATIONS = "internal_knowledge_operations"
    ACQUISITION_BASIS_EXPLANATION = "acquisition_basis_explanation"
    INFORMATIONAL_DISPLAY = "informational_display"


class ProhibitedUse(StrEnum):
    DIAGNOSIS = "diagnosis"
    TREATMENT_RECOMMENDATION = "treatment_recommendation"
    EMERGENCY_TRIAGE = "emergency_triage"
    AUTOMATED_CLINICAL_DECISION = "automated_clinical_decision"
    RUNTIME_STATE_TRANSITION = "runtime_state_transition"
    PATIENT_SPECIFIC_WEB_SEARCH = "patient_specific_web_search"


class SafetyBoundary(StrictModel):
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"
    launch_jurisdiction: Literal["CN"] = "CN"
    launch_language: Literal["zh-CN"] = "zh-CN"
    allowed_intended_uses: tuple[IntendedUse, ...] = Field(min_length=3, max_length=3)
    prohibited_uses: tuple[ProhibitedUse, ...] = Field(min_length=6, max_length=6)
    patient_data_allowed: Literal[False] = False
    live_network_default_enabled: Literal[False] = False
    automatic_clinical_approval_allowed: Literal[False] = False
    synthetic_approvals_count_toward_release: Literal[False] = False

    @model_validator(mode="after")
    def validate_complete_boundary(self) -> "SafetyBoundary":
        if set(self.allowed_intended_uses) != {item.value for item in IntendedUse}:
            raise ValueError("allowed_intended_uses must enumerate the complete safe set")
        if set(self.prohibited_uses) != {item.value for item in ProhibitedUse}:
            raise ValueError("prohibited_uses must enumerate the complete prohibited set")
        return self


class Jurisdiction(StrictModel):
    system: Literal["iso3166_1", "global"]
    code: NonBlank

    @model_validator(mode="after")
    def validate_code(self) -> "Jurisdiction":
        if self.system == "global" and self.code != "GLOBAL":
            raise ValueError("global jurisdiction code must be GLOBAL")
        if self.system == "iso3166_1" and (
            len(self.code) != 2
            or not self.code.isascii()
            or not self.code.isalpha()
            or not self.code.isupper()
        ):
            raise ValueError("iso3166_1 jurisdiction must be an uppercase alpha-2 code")
        return self


class ScopeValue(StrictModel):
    system: NonBlank
    code: NonBlank
    display: NonBlank | None = None


class AnyScopeDimension(StrictModel):
    mode: Literal["any"] = "any"


class IncludeScopeDimension(StrictModel):
    mode: Literal["include"] = "include"
    values: tuple[ScopeValue, ...] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def unique_values(cls, value: tuple[ScopeValue, ...]) -> tuple[ScopeValue, ...]:
        keys = [(item.system, item.code) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("scope values must be unique")
        return value


class NotApplicableScopeDimension(StrictModel):
    mode: Literal["not_applicable"] = "not_applicable"


ScopeDimension = Annotated[
    AnyScopeDimension | IncludeScopeDimension | NotApplicableScopeDimension,
    Field(discriminator="mode"),
]


class ContextDomain(StrEnum):
    CLINICAL = "clinical"
    TERMINOLOGY = "terminology"
    INTEROPERABILITY = "interoperability"


class ClinicalContextScope(StrictModel):
    """Explicit context for a candidate clinical or non-clinical assertion.

    Clinical scopes can never use a global jurisdiction.  Global sources may
    still be registered by SourcePolicy and cited by a future claim scoped to
    a concrete product jurisdiction.
    """

    domain: ContextDomain
    jurisdictions: tuple[Jurisdiction, ...] = Field(min_length=1)
    languages: tuple[LanguageCode, ...] = Field(min_length=1)
    conditions: ScopeDimension
    products: ScopeDimension
    care_settings: ScopeDimension
    populations: ScopeDimension
    intended_uses: tuple[IntendedUse, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scope(self) -> "ClinicalContextScope":
        jurisdiction_keys = [(item.system, item.code) for item in self.jurisdictions]
        if len(jurisdiction_keys) != len(set(jurisdiction_keys)):
            raise ValueError("jurisdictions must be unique")
        if len(self.languages) != len(set(self.languages)):
            raise ValueError("languages must be unique")
        if len(self.intended_uses) != len(set(self.intended_uses)):
            raise ValueError("intended_uses must be unique")
        if self.domain == ContextDomain.CLINICAL and any(
            item.system == "global" for item in self.jurisdictions
        ):
            raise ValueError("clinical context must use explicit product jurisdictions")
        return self


class SourceOperation(StrEnum):
    REGISTER_LINK_METADATA = "register_link_metadata"
    DISCOVER_METADATA = "discover_metadata"
    FETCH_FOR_CHANGE_DETECTION = "fetch_for_change_detection"
    PERSIST_SNAPSHOT = "persist_snapshot"
    PERSIST_FULL_TEXT = "persist_full_text"
    SHORT_QUOTE = "short_quote"
    TRANSLATE = "translate"
    ADAPT = "adapt"
    REDISTRIBUTE = "redistribute"
    CREATE_MAPPING = "create_mapping"
    COMMERCIAL_USE = "commercial_use"
    MODEL_TRAINING = "model_training"
    VECTOR_INDEX = "vector_index"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    OFFLINE_FIXTURE_ONLY = "offline_fixture_only"
    REVIEW_REQUIRED = "review_required"
    DENY = "deny"


class LicensePosture(StrEnum):
    NEEDS_VERIFICATION = "needs_verification"
    REGISTRATION_REQUIRED = "registration_required"
    LICENSE_REQUIRED = "license_required"
    VERIFIED_RESTRICTED = "verified_restricted"
    VERIFIED_OPEN = "verified_open"


class SourcePolicyOperationRule(StrictModel):
    operation: SourceOperation
    decision: PolicyDecision
    rationale: NonBlank


class SourceRightsEvidence(StrictModel):
    evidence_id: SafeId
    official_document_url: AnyHttpUrl
    retrieved_at: datetime
    document_sha256: Sha256
    digest_scope: Literal["official_document_bytes"] = "official_document_bytes"
    recorded_by: NonBlank
    reviewed_by: Literal["none-formal-rights-officer-unavailable"] = (
        "none-formal-rights-officer-unavailable"
    )
    conclusion: Literal["metadata_discovery_only_rights_unresolved"] = (
        "metadata_discovery_only_rights_unresolved"
    )
    known_limitations: tuple[NonBlank, ...] = Field(min_length=1)
    formal_rights_review_completed: Literal[False] = False

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("rights evidence retrieval time must include a timezone")
        return value


class SourcePolicy(StrictModel):
    policy_id: SafeId
    policy_version: int = Field(ge=1)
    display_name: NonBlank
    issuing_authority: NonBlank
    source_types: tuple[NonBlank, ...] = Field(min_length=1)
    source_jurisdictions: tuple[Jurisdiction, ...] = Field(min_length=1)
    languages: tuple[LanguageCode, ...] = Field(min_length=1)
    allowed_origins: tuple[AnyHttpUrl, ...] = Field(min_length=1)
    allow_subdomains: bool = False
    allowed_query_parameters: tuple[SafeId, ...] = Field(default_factory=tuple)
    allowed_path_templates: tuple[NonBlank, ...] = Field(default_factory=tuple)
    allowed_content_types: tuple[NonBlank, ...] = Field(min_length=1)
    maximum_response_bytes: int = Field(gt=0, le=50_000_000)
    license_posture: LicensePosture
    terms_uri: AnyHttpUrl | None = None
    rights_evidence: tuple[SourceRightsEvidence, ...] = Field(default_factory=tuple)
    operation_rules: tuple[SourcePolicyOperationRule, ...] = Field(min_length=1)
    live_network_enabled: Literal[False] = False
    status: Literal["active", "retired"] = "active"
    registered_at: datetime
    registered_by: NonBlank
    notes: tuple[NonBlank, ...] = Field(default_factory=tuple)

    @field_validator("registered_at")
    @classmethod
    def registered_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("registered_at must include a timezone")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, value: tuple[AnyHttpUrl, ...]) -> tuple[AnyHttpUrl, ...]:
        canonical: list[str] = []
        for origin in value:
            parsed = urlsplit(str(origin))
            if parsed.scheme != "https":
                raise ValueError("source policy origins must use https")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("source policy origin cannot include credentials/query/fragment")
            if parsed.port not in {None, 443}:
                raise ValueError("source policy origin may only use port 443")
            if parsed.path not in {"", "/"}:
                raise ValueError("source policy origin cannot include a path")
            host = (parsed.hostname or "").lower().rstrip(".")
            if not host or host == "localhost" or host.endswith((".local", ".internal")):
                raise ValueError("source policy origin host is unsafe")
            try:
                ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("source policy origin cannot be an IP literal")
            canonical.append(f"https://{host}")
        if len(canonical) != len(set(canonical)):
            raise ValueError("source policy origins must be unique")
        return value

    @model_validator(mode="after")
    def validate_rules(self) -> "SourcePolicy":
        operations = [item.operation for item in self.operation_rules]
        if len(operations) != len(set(operations)):
            raise ValueError("source policy operation rules must be unique")
        if len(self.allowed_path_templates) != len(set(self.allowed_path_templates)):
            raise ValueError("source policy path templates must be unique")
        for template in self.allowed_path_templates:
            if (
                not template.startswith("/")
                or "\\" in template
                or "?" in template
                or "#" in template
                or any(part in {"", ".", ".."} for part in template.split("/")[1:])
            ):
                raise ValueError("source policy path template is not canonical")
        evidence_ids = [item.evidence_id for item in self.rights_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("source policy rights evidence IDs must be unique")
        automatically_safe = {
            SourceOperation.REGISTER_LINK_METADATA,
            SourceOperation.DISCOVER_METADATA,
        }
        unsafe_automatic = [
            item.operation
            for item in self.operation_rules
            if item.decision == PolicyDecision.ALLOW
            and item.operation not in automatically_safe
        ]
        if unsafe_automatic:
            raise ValueError(
                "only link registration and metadata discovery may be automatic; "
                f"found {sorted(str(item) for item in unsafe_automatic)}"
            )
        if self.license_posture in {
            LicensePosture.NEEDS_VERIFICATION,
            LicensePosture.REGISTRATION_REQUIRED,
            LicensePosture.LICENSE_REQUIRED,
        }:
            high_risk = {
                SourceOperation.PERSIST_FULL_TEXT,
                SourceOperation.SHORT_QUOTE,
                SourceOperation.TRANSLATE,
                SourceOperation.ADAPT,
                SourceOperation.REDISTRIBUTE,
                SourceOperation.CREATE_MAPPING,
                SourceOperation.COMMERCIAL_USE,
                SourceOperation.MODEL_TRAINING,
                SourceOperation.VECTOR_INDEX,
            }
            if any(
                item.operation in high_risk
                and item.decision in {PolicyDecision.ALLOW, PolicyDecision.OFFLINE_FIXTURE_ONLY}
                for item in self.operation_rules
            ):
                raise ValueError("unverified/restricted policy cannot allow high-risk reuse")
        return self

    def decision_for(self, operation: SourceOperation | str) -> str:
        operation_value = str(operation)
        for rule in self.operation_rules:
            if str(rule.operation) == operation_value:
                return str(rule.decision)
        return PolicyDecision.DENY.value


class SourcePolicyRef(StrictModel):
    policy_id: SafeId
    policy_version: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        return self.policy_id, self.policy_version


class ReviewerRole(StrEnum):
    KNOWLEDGE_CURATOR = "knowledge_curator"
    RIGHTS_OFFICER = "rights_officer"
    TERMINOLOGIST = "terminologist"
    CLINICAL_REVIEWER = "clinical_reviewer"
    PHARMACIST = "pharmacist"


class GovernanceGate(StrEnum):
    SOURCE_PROMOTION = "source_promotion"
    CONTENT_PERSISTENCE = "content_persistence"
    TERMINOLOGY_MAPPING_PROMOTION = "terminology_mapping_promotion"
    TRANSLATION_PROMOTION = "translation_promotion"
    CLINICAL_CLAIM_APPROVAL = "clinical_claim_approval"
    BINDING_APPROVAL = "binding_approval"
    PATIENT_CONTENT_APPROVAL = "patient_content_approval"
    KNOWLEDGE_RELEASE = "knowledge_release"


class ReviewGatePolicy(StrictModel):
    gate: GovernanceGate
    required_roles: tuple[ReviewerRole, ...] = Field(min_length=1)
    automatic_approval_allowed: Literal[False] = False
    synthetic_events_allowed_for_tests: Literal[True] = True
    synthetic_events_count_toward_release: Literal[False] = False
    rationale: NonBlank

    @field_validator("required_roles")
    @classmethod
    def unique_roles(cls, value: tuple[ReviewerRole, ...]) -> tuple[ReviewerRole, ...]:
        if len(value) != len(set(value)):
            raise ValueError("required reviewer roles must be unique")
        return value


class ValidationDomain(StrEnum):
    MEDICATION_FOLLOWUP = "medication_followup"
    CHRONIC_CARDIOPULMONARY = "chronic_cardiopulmonary"
    ONCOLOGY_PRO = "oncology_pro"
    ACUTE_HIGH_RISK_SYMPTOMS = "acute_high_risk_symptoms"
    RARE_DISEASE_TERMINOLOGY = "rare_disease_terminology"


class CoverageValidationProfile(StrictModel):
    profile_id: SafeId
    profile_version: int = Field(ge=1)
    domain: ValidationDomain
    display_name: NonBlank
    purpose: NonBlank
    layers: tuple[KnowledgeLayer, ...] = Field(min_length=1)
    scope: ClinicalContextScope
    required_roles: tuple[ReviewerRole, ...] = Field(min_length=1)
    synthetic_fixture_only: Literal[True] = True
    clinical_content_seeded: Literal[False] = False
    notes: tuple[NonBlank, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_profile(self) -> "CoverageValidationProfile":
        if len(self.layers) != len(set(self.layers)):
            raise ValueError("profile layers must be unique")
        if len(self.required_roles) != len(set(self.required_roles)):
            raise ValueError("profile roles must be unique")
        if self.domain == ValidationDomain.RARE_DISEASE_TERMINOLOGY:
            if self.scope.domain != ContextDomain.TERMINOLOGY:
                raise ValueError("rare disease terminology profile must be non-clinical")
        elif self.scope.domain != ContextDomain.CLINICAL:
            raise ValueError("clinical validation profile requires a clinical scope")
        return self


class FileRef(StrictModel):
    file_id: SafeId
    file_version: int = Field(ge=1)

    def key(self) -> tuple[str, int]:
        return self.file_id, self.file_version


class PinnedFile(StrictModel):
    ref: FileRef
    relative_path: NonBlank
    manifest_sha256: Sha256
    size: int = Field(ge=0)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        safe_relative_parts(value)
        return value


class GovernanceManifestEvidence(StrictModel):
    file_id: SafeId
    file_version: int = Field(ge=1)
    manifest_sha256: Sha256


class EnvelopeBase(StrictModel):
    file_id: SafeId
    file_version: int = Field(ge=1)
    contract_version: Literal[KNOWLEDGE_OPS_CONTRACT_VERSION] = (
        KNOWLEDGE_OPS_CONTRACT_VERSION
    )

    @property
    def ref(self) -> FileRef:
        return FileRef(file_id=self.file_id, file_version=self.file_version)


class SafetyBoundaryManifest(EnvelopeBase):
    file_kind: Literal["safety_boundary"] = "safety_boundary"
    boundary: SafetyBoundary


class SourcePolicyManifest(EnvelopeBase):
    file_kind: Literal["source_policy_registry"] = "source_policy_registry"
    extends: FileRef | None = None
    policies: tuple[SourcePolicy, ...]

    @model_validator(mode="after")
    def validate_extension(self) -> "SourcePolicyManifest":
        if self.file_version == 1 and self.extends is not None:
            raise ValueError("source policy file version 1 cannot extend a predecessor")
        if self.file_version > 1 and self.extends != FileRef(
            file_id=self.file_id,
            file_version=self.file_version - 1,
        ):
            raise ValueError("source policy successor must extend its immediate predecessor")
        if not self.policies:
            raise ValueError("source policy manifest requires at least one policy")
        return self


class CoverageProfileManifest(EnvelopeBase):
    file_kind: Literal["coverage_validation_profiles"] = "coverage_validation_profiles"
    profiles: tuple[CoverageValidationProfile, ...]


class ReviewPolicyManifest(EnvelopeBase):
    file_kind: Literal["review_policy_registry"] = "review_policy_registry"
    gates: tuple[ReviewGatePolicy, ...]


class KnowledgeReleaseIntent(StrictModel):
    release_intent_id: SafeId
    release_intent_version: int = Field(ge=1)
    target_jurisdiction: Literal["CN"] = "CN"
    target_language: Literal["zh-CN"] = "zh-CN"
    intended_uses: tuple[IntendedUse, ...] = Field(min_length=3, max_length=3)
    artifact_selection: Literal["none_until_formal_review"] = (
        "none_until_formal_review"
    )
    selected_artifact_count: Literal[0] = 0
    formal_reviewers_available: Literal[False] = False
    formal_license_decisions_available: Literal[False] = False
    release_ready: Literal[False] = False
    status: Literal["readiness_only_blocked"] = "readiness_only_blocked"
    reason: NonBlank
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_intended_uses(self) -> "KnowledgeReleaseIntent":
        if set(self.intended_uses) != {item.value for item in IntendedUse}:
            raise ValueError("release intent must enumerate the exact safe intended uses")
        return self


class ReleaseIntentManifest(EnvelopeBase):
    file_kind: Literal["knowledge_release_intent"] = "knowledge_release_intent"
    intent: KnowledgeReleaseIntent


class ReadinessGapKind(StrEnum):
    LIVE_VALIDATION_NOT_ATTEMPTED = "live_validation_not_attempted"
    RIGHTS_UNRESOLVED = "rights_unresolved"
    COLD_IMPORT_SOCKET_PROOF_PENDING = "cold_import_socket_proof_pending"
    TERMINOLOGY_ALIAS_REVIEW_PENDING = "terminology_alias_review_pending"


class ReadinessBlock(StrEnum):
    PERSISTENT_GOVERNANCE_VALIDATION = "persistent_governance_validation"
    REUSE_BEYOND_METADATA_LINK_ONLY = "reuse_beyond_metadata_link_only"
    PRODUCTION_ELIGIBILITY = "production_eligibility"
    KNOWLEDGE_RELEASE = "knowledge_release"
    P1B_LIVE_VALIDATION = "p1b_live_validation"
    CONSUMER_INTEGRATION = "consumer_integration"


class SourcePolicyGapSubject(StrictModel):
    subject_kind: Literal["source_policy"] = "source_policy"
    source_policy: SourcePolicyRef


class GovernanceGateGapSubject(StrictModel):
    subject_kind: Literal["governance_gate"] = "governance_gate"
    gate: Literal["cold_import_socket_proof"] = "cold_import_socket_proof"


class CoreSymptomCatalogGapSubject(StrictModel):
    subject_kind: Literal["core_symptom_catalog"] = "core_symptom_catalog"
    catalog_id: Literal["continucare-core-symptom-catalog"] = (
        "continucare-core-symptom-catalog"
    )
    catalog_version: Literal["2.0.0"] = "2.0.0"
    concept_refs: tuple[SafeId, ...] = Field(min_length=1)

    @field_validator("concept_refs")
    @classmethod
    def unique_concept_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("readiness catalog concept refs must be unique")
        return value


ReadinessGapSubject = Annotated[
    SourcePolicyGapSubject
    | GovernanceGateGapSubject
    | CoreSymptomCatalogGapSubject,
    Field(discriminator="subject_kind"),
]


class ReadinessGap(StrictModel):
    gap_id: SafeId
    gap_kind: ReadinessGapKind
    subject: ReadinessGapSubject
    lifecycle: Literal["open"] = "open"
    blocks: tuple[ReadinessBlock, ...] = Field(min_length=1)
    reason: NonBlank
    created_at: datetime
    created_by_principal_id: SafeId
    contains_patient_data: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_open_gap(self) -> "ReadinessGap":
        if self.created_at.tzinfo is None:
            raise ValueError("readiness Gap creation time must include a timezone")
        if len(self.blocks) != len(set(self.blocks)):
            raise ValueError("readiness Gap blockers must be unique")
        kind = ReadinessGapKind(self.gap_kind)
        if kind in {
            ReadinessGapKind.LIVE_VALIDATION_NOT_ATTEMPTED,
            ReadinessGapKind.RIGHTS_UNRESOLVED,
        }:
            if not isinstance(self.subject, SourcePolicyGapSubject):
                raise ValueError("source readiness Gap requires SourcePolicyRef subject")
        elif kind == ReadinessGapKind.COLD_IMPORT_SOCKET_PROOF_PENDING:
            if not isinstance(self.subject, GovernanceGateGapSubject):
                raise ValueError("cold-import Gap requires its governance gate subject")
        elif not isinstance(self.subject, CoreSymptomCatalogGapSubject):
            raise ValueError("terminology readiness Gap requires catalog subject")
        if kind == ReadinessGapKind.TERMINOLOGY_ALIAS_REVIEW_PENDING and set(
            self.blocks
        ) != {ReadinessBlock.CONSUMER_INTEGRATION.value}:
            raise ValueError("catalog Gap may only block v2 consumer integration")
        return self


class ReadinessGapRegistryManifest(EnvelopeBase):
    file_kind: Literal["readiness_gap_registry"] = "readiness_gap_registry"
    gaps: tuple[ReadinessGap, ...] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def validate_frozen_open_set(self) -> "ReadinessGapRegistryManifest":
        if len({item.gap_id for item in self.gaps}) != len(self.gaps):
            raise ValueError("readiness Gap IDs must be unique")
        counts = {
            kind.value: sum(item.gap_kind == kind.value for item in self.gaps)
            for kind in ReadinessGapKind
        }
        if counts != {
            ReadinessGapKind.LIVE_VALIDATION_NOT_ATTEMPTED.value: 5,
            ReadinessGapKind.RIGHTS_UNRESOLVED.value: 5,
            ReadinessGapKind.COLD_IMPORT_SOCKET_PROOF_PENDING.value: 1,
            ReadinessGapKind.TERMINOLOGY_ALIAS_REVIEW_PENDING.value: 1,
        }:
            raise ValueError("readiness Gap registry v1 requires the frozen 5+5+1+1 set")
        live_refs = {
            item.subject.source_policy.key()
            for item in self.gaps
            if item.gap_kind == ReadinessGapKind.LIVE_VALIDATION_NOT_ATTEMPTED.value
            and isinstance(item.subject, SourcePolicyGapSubject)
        }
        rights_refs = {
            item.subject.source_policy.key()
            for item in self.gaps
            if item.gap_kind == ReadinessGapKind.RIGHTS_UNRESOLVED.value
            and isinstance(item.subject, SourcePolicyGapSubject)
        }
        if len(live_refs) != 5 or live_refs != rights_refs:
            raise ValueError("live and rights readiness Gaps must cover the same five policies")
        return self


CORE_SYMPTOM_REUSED_BENCHMARK_KEYS = (
    "nausea",
    "vomiting",
    "diarrhea",
    "abdominal-pain",
    "constipation",
    "decreased-appetite",
    "fatigue",
    "dizziness",
    "dyspnea",
)


class AliasAuditReason(StrEnum):
    PREFERRED_DISPLAY_ONLY = (
        "v1_preferred_display_label_not_formal_patient_expression_review"
    )
    COLLOQUIAL_UNREVIEWED = "colloquial_expression_requires_formal_terminology_review"
    BROAD_OR_AMBIGUOUS = "broad_or_ambiguous_expression_requires_semantic_scope_review"
    STOOL_DESCRIPTION = "stool_description_requires_semantic_equivalence_review"
    FUNCTIONAL_DESCRIPTION = "functional_expression_requires_semantic_equivalence_review"
    DIZZINESS_SUBTYPE = "dizziness_expression_requires_subtype_disambiguation"
    DYSPNEA_BOUNDARY = "dyspnea_expression_cannot_imply_red_flag_or_emergency"


class CoreSymptomAliasAuditEntry(StrictModel):
    source_alias_index: int = Field(ge=0)
    alias_zh: NonBlank
    source_role: Literal[
        "v1_preferred_display_label",
        "inherited_v1_alias",
    ]
    disposition: Literal[
        "display_label_only_pending_formal_terminology_review",
        "withheld_pending_formal_terminology_review",
    ]
    boundary_reason: AliasAuditReason
    display_label: bool
    matchable: Literal[False] = False
    semantic_equivalence_status: Literal["not_established"] = "not_established"
    formal_terminology_review_completed: Literal[False] = False

    @model_validator(mode="after")
    def validate_disposition(self) -> "CoreSymptomAliasAuditEntry":
        if self.source_role == "v1_preferred_display_label":
            if (
                self.disposition
                != "display_label_only_pending_formal_terminology_review"
                or self.boundary_reason != AliasAuditReason.PREFERRED_DISPLAY_ONLY.value
                or not self.display_label
            ):
                raise ValueError(
                    "v1 preferred alias may only be an unreviewed display label"
                )
        elif (
            self.disposition != "withheld_pending_formal_terminology_review"
            or self.boundary_reason == AliasAuditReason.PREFERRED_DISPLAY_ONLY.value
            or self.display_label
        ):
            raise ValueError(
                "non-preferred inherited alias must remain withheld and non-display"
            )
        return self


class CoreSymptomAliasConceptAudit(StrictModel):
    benchmark_id: SafeId
    benchmark_key: SafeId
    existing_concept_ref: SafeId
    preferred_zh: NonBlank
    benchmark_label_en: NonBlank
    english_label_disposition: Literal[
        "benchmark_display_only_pending_formal_translation_review"
    ] = "benchmark_display_only_pending_formal_translation_review"
    formal_translation_review_completed: Literal[False] = False
    aliases: tuple[CoreSymptomAliasAuditEntry, ...] = Field(min_length=1)
    approved_match_aliases: tuple[NonBlank, ...] = Field(
        default_factory=tuple,
        max_length=0,
    )
    terminology_review_status: Literal["pending_formal_terminology_review"] = (
        "pending_formal_terminology_review"
    )

    @model_validator(mode="after")
    def validate_alias_partition(self) -> "CoreSymptomAliasConceptAudit":
        if self.benchmark_id != f"core-symptom-{self.benchmark_key}":
            raise ValueError("alias audit benchmark ID differs from its stable key")
        if tuple(item.source_alias_index for item in self.aliases) != tuple(
            range(len(self.aliases))
        ):
            raise ValueError("alias audit source indices must be ordered and contiguous")
        if len({item.alias_zh for item in self.aliases}) != len(self.aliases):
            raise ValueError("alias audit values must be unique within a concept")
        preferred = tuple(
            item
            for item in self.aliases
            if item.source_role == "v1_preferred_display_label"
        )
        if len(preferred) != 1 or preferred[0].alias_zh != self.preferred_zh:
            raise ValueError("alias audit requires exactly one matching display label")
        if any(
            item.disposition != "withheld_pending_formal_terminology_review"
            for item in self.aliases
            if item.source_role == "inherited_v1_alias"
        ):
            raise ValueError("every non-preferred inherited alias must remain withheld")
        return self


class CoreSymptomAliasAudit(StrictModel):
    audit_id: Literal["core-symptom-alias-technical-boundary-audit"]
    audit_version: Literal[1]
    audit_kind: Literal["technical_boundary_audit"] = "technical_boundary_audit"
    catalog_id: Literal["continucare-core-symptom-catalog"]
    catalog_version: Literal["2.0.0"]
    catalog_sha256: Sha256
    source_catalog_id: Literal["continucare-glp1-patient-reported-symptoms"]
    source_catalog_version: Literal["1.0.0"]
    source_catalog_sha256: Sha256
    concept_audits: tuple[CoreSymptomAliasConceptAudit, ...] = Field(
        min_length=9,
        max_length=9,
    )
    terminology_review_status: Literal["pending_formal_terminology_review"] = (
        "pending_formal_terminology_review"
    )
    formal_terminologist_review_completed: Literal[False] = False
    clinical_patient_expression_validation_completed: Literal[False] = False
    technical_audit_only: Literal[True] = True
    contains_patient_data: Literal[False] = False
    release_ready: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_complete_reused_set(self) -> "CoreSymptomAliasAudit":
        keys = tuple(item.benchmark_key for item in self.concept_audits)
        refs = tuple(item.existing_concept_ref for item in self.concept_audits)
        if keys != CORE_SYMPTOM_REUSED_BENCHMARK_KEYS or refs != keys:
            raise ValueError(
                "alias audit must contain the exact ordered nine reused concepts"
            )
        return self


class CoreSymptomAliasAuditManifest(EnvelopeBase):
    file_kind: Literal["core_symptom_alias_audit"] = "core_symptom_alias_audit"
    audit: CoreSymptomAliasAudit


PayloadEnvelope = Annotated[
    SafetyBoundaryManifest
    | SourcePolicyManifest
    | CoverageProfileManifest
    | ReviewPolicyManifest
    | ReleaseIntentManifest
    | ReadinessGapRegistryManifest
    | CoreSymptomAliasAuditManifest,
    Field(discriminator="file_kind"),
]


class KnowledgeOpsBundleIndex(StrictModel):
    file_kind: Literal["knowledge_ops_bundle_index"] = "knowledge_ops_bundle_index"
    bundle_id: SafeId
    bundle_version: int = Field(ge=1)
    contract_version: Literal[KNOWLEDGE_OPS_CONTRACT_VERSION] = (
        KNOWLEDGE_OPS_CONTRACT_VERSION
    )
    files: tuple[PinnedFile, ...] = Field(min_length=1)
    current_file_refs: tuple[FileRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_index(self) -> "KnowledgeOpsBundleIndex":
        pinned = [item.ref.key() for item in self.files]
        current = [item.key() for item in self.current_file_refs]
        if len(pinned) != len(set(pinned)):
            raise ValueError("pinned files must have unique refs")
        if len(current) != len(set(current)):
            raise ValueError("current file refs must be unique")
        if not set(current).issubset(set(pinned)):
            raise ValueError("current file refs must select pinned policy files")
        paths = [item.relative_path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("pinned file paths must be unique")
        return self

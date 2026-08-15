"""Reviewer identities, immutable Review Packets, and append-only events."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from continucare.knowledge.ops.acquisition import (
    ChangeSet,
    KnowledgeGap,
    SourceCandidate,
    SourcePolicyRef,
    SourceSnapshot,
)
from continucare.knowledge.ops.manifests import KnowledgeOpsBundle
from continucare.knowledge.ops.models import (
    AuthorProvenance,
    ClinicalContextScope,
    GovernanceManifestEvidence,
    GovernanceGate,
    Jurisdiction,
    KnowledgeOpsIntegrityError,
    KnowledgeOpsPolicyError,
    NonBlank,
    ReviewerRole,
    SafeId,
    Sha256,
    SourceOperation,
    StrictModel,
)
from continucare.knowledge.ops.promotion import GovernedSourceV2, PromotionDecision
from continucare.knowledge.ops.security import (
    DigestTrustProfile,
    assert_no_sensitive_data,
    digest_derived_internal_id,
)
from continucare.knowledge.ops.store import (
    AppendOnlyLedger,
    LedgerCollection,
    LedgerEntry,
    LedgerRef,
)


_TYPED_REVIEW_EVIDENCE_DIGEST_PROFILES = {
    (
        LedgerCollection.SNAPSHOT.value,
        "source_snapshot",
    ): DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
    (
        LedgerCollection.EVIDENCE_CANDIDATE.value,
        "evidence_candidate_v2",
    ): DigestTrustProfile.EVIDENCE_CANDIDATE,
}
_TYPED_REVIEW_EVIDENCE_COLLECTIONS = frozenset(
    collection for collection, _ in _TYPED_REVIEW_EVIDENCE_DIGEST_PROFILES
)
_TYPED_REVIEW_EVIDENCE_PAYLOAD_TYPES = frozenset(
    payload_type for _, payload_type in _TYPED_REVIEW_EVIDENCE_DIGEST_PROFILES
)


class ReviewerAssurance(StrEnum):
    SYNTHETIC_TEST = "synthetic_test"
    IDENTITY_UNVERIFIED = "identity_unverified"
    FORMALLY_VERIFIED = "formally_verified"


class ReviewerIdentity(StrictModel):
    identity_id: SafeId
    principal_id: SafeId
    display_name: NonBlank
    roles: tuple[ReviewerRole, ...] = Field(min_length=1)
    authorized_jurisdictions: tuple[Jurisdiction, ...] = Field(min_length=1)
    authorized_scopes: tuple[ClinicalContextScope, ...] = Field(min_length=1)
    authorization_valid_from: datetime
    authorization_valid_until: datetime
    assurance: ReviewerAssurance
    synthetic: bool
    verification_reference: NonBlank | None = None
    verification_evidence_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    verified_by: NonBlank | None = None
    verified_at: datetime | None = None
    active: bool = True

    @model_validator(mode="after")
    def validate_identity(self) -> "ReviewerIdentity":
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("reviewer roles must be unique")
        if self.authorization_valid_from.tzinfo is None:
            raise ValueError("authorization_valid_from must include a timezone")
        if self.authorization_valid_until.tzinfo is None:
            raise ValueError("authorization_valid_until must include a timezone")
        if self.authorization_valid_until <= self.authorization_valid_from:
            raise ValueError("reviewer authorization validity interval is empty")
        jurisdiction_keys = [
            (item.system, item.code) for item in self.authorized_jurisdictions
        ]
        if len(jurisdiction_keys) != len(set(jurisdiction_keys)):
            raise ValueError("authorized reviewer jurisdictions must be unique")
        scope_keys = [_canonical_model_key(item) for item in self.authorized_scopes]
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("authorized reviewer scopes must be unique")
        authorized_jurisdictions = set(jurisdiction_keys)
        if any(
            (jurisdiction.system, jurisdiction.code) not in authorized_jurisdictions
            for scope in self.authorized_scopes
            for jurisdiction in scope.jurisdictions
        ):
            raise ValueError(
                "authorized reviewer scope exceeds authorized jurisdictions"
            )
        if self.assurance == ReviewerAssurance.SYNTHETIC_TEST:
            if not self.synthetic:
                raise ValueError("synthetic reviewer assurance requires synthetic=true")
            if any(
                item is not None
                for item in (
                    self.verification_reference,
                    self.verification_evidence_sha256,
                    self.verified_by,
                    self.verified_at,
                )
            ):
                raise ValueError("synthetic reviewer cannot claim formal verification")
        elif self.assurance == ReviewerAssurance.IDENTITY_UNVERIFIED:
            if self.synthetic:
                raise ValueError("unverified real identity cannot be marked synthetic")
            if any(
                item is not None
                for item in (
                    self.verification_reference,
                    self.verification_evidence_sha256,
                    self.verified_by,
                    self.verified_at,
                )
            ):
                raise ValueError("unverified identity cannot carry verification evidence")
        else:
            if self.synthetic:
                raise ValueError("formally verified reviewer cannot be synthetic")
            if any(
                item is None
                for item in (
                    self.verification_reference,
                    self.verification_evidence_sha256,
                    self.verified_by,
                    self.verified_at,
                )
            ):
                raise ValueError("formal reviewer requires verification evidence and time")
            if self.verified_at.tzinfo is None:
                raise ValueError("verified_at must include a timezone")
            if not (
                self.authorization_valid_from
                <= self.verified_at
                < self.authorization_valid_until
            ):
                raise ValueError(
                    "formal verification time must fall within authorization validity"
                )
        return self

    def is_current_at(self, value: datetime) -> bool:
        return (
            value.tzinfo is not None
            and self.active
            and self.authorization_valid_from <= value < self.authorization_valid_until
        )

    def authorizes(
        self,
        *,
        role: ReviewerRole,
        scope: ClinicalContextScope,
        at: datetime,
    ) -> bool:
        return (
            self.is_current_at(at)
            and ReviewerRole(role) in self.roles
            and scope in self.authorized_scopes
            and (
                self.assurance != ReviewerAssurance.FORMALLY_VERIFIED
                or (self.verified_at is not None and self.verified_at <= at)
            )
            and all(
                jurisdiction in self.authorized_jurisdictions
                for jurisdiction in scope.jurisdictions
            )
        )

    def is_production_eligible_at(self, value: datetime) -> bool:
        return (
            self.is_current_at(value)
            and self.assurance == ReviewerAssurance.FORMALLY_VERIFIED
            and not self.synthetic
        )


class ReviewEventAttestation(StrictModel):
    attestation_id: SafeId
    event_claim_sha256: Sha256
    issued_at: datetime
    valid_until: datetime
    verifier_reference: NonBlank
    attestation_sha256: Sha256
    synthetic: bool

    @model_validator(mode="after")
    def validate_attestation(self) -> "ReviewEventAttestation":
        if self.issued_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("review attestation times must include a timezone")
        if self.valid_until <= self.issued_at:
            raise ValueError("review attestation validity interval is empty")
        return self


class ReviewerVerifier(Protocol):
    """Trusted current-identity resolver and event-attestation verifier."""

    def resolve(self, identity_id: str) -> ReviewerIdentity | None: ...

    def verify_identity_authorization(
        self,
        identity: ReviewerIdentity,
        *,
        role: ReviewerRole,
        scope: ClinicalContextScope,
        at: datetime,
    ) -> bool: ...

    def issue_review_attestation(
        self,
        identity: ReviewerIdentity,
        *,
        role: ReviewerRole,
        scope: ClinicalContextScope,
        event_claim_sha256: str,
        issued_at: datetime,
    ) -> ReviewEventAttestation | None: ...

    def verify_review_attestation(
        self,
        identity: ReviewerIdentity,
        *,
        role: ReviewerRole,
        scope: ClinicalContextScope,
        event_claim_sha256: str,
        attestation: ReviewEventAttestation,
        evaluated_at: datetime,
    ) -> bool: ...


class InMemoryReviewerDirectory:
    """Ephemeral readiness verifier; it can never assert a formal reviewer."""

    def __init__(
        self,
        reviewers: tuple[ReviewerIdentity, ...] = (),
    ) -> None:
        identities = [item.identity_id for item in reviewers]
        if len(identities) != len(set(identities)):
            raise ValueError("reviewer identities must be unique")
        if any(
            item.assurance == ReviewerAssurance.FORMALLY_VERIFIED
            for item in reviewers
        ):
            raise KnowledgeOpsPolicyError(
                "in-memory reviewer directory cannot assert formal production identity"
            )
        self._reviewers = {item.identity_id: item for item in reviewers}
        self._attestation_key = secrets.token_bytes(32)

    def resolve(self, identity_id: str) -> ReviewerIdentity | None:
        return self._reviewers.get(identity_id)

    def verify_identity_authorization(
        self,
        identity: ReviewerIdentity,
        *,
        role: ReviewerRole,
        scope: ClinicalContextScope,
        at: datetime,
    ) -> bool:
        current = self.resolve(identity.identity_id)
        return (
            current == identity
            and current.assurance != ReviewerAssurance.FORMALLY_VERIFIED
            and current.authorizes(role=role, scope=scope, at=at)
        )

    def issue_review_attestation(
        self,
        identity: ReviewerIdentity,
        *,
        role: ReviewerRole,
        scope: ClinicalContextScope,
        event_claim_sha256: str,
        issued_at: datetime,
    ) -> ReviewEventAttestation | None:
        if not self.verify_identity_authorization(
            identity, role=role, scope=scope, at=issued_at
        ):
            return None
        attestation_id = _attestation_id(event_claim_sha256)
        valid_until = identity.authorization_valid_until
        verifier_reference = "urn:continucare:readiness:ephemeral-review-verifier"
        digest = _review_attestation_digest(
            key=self._attestation_key,
            identity_id=identity.identity_id,
            attestation_id=attestation_id,
            event_claim_sha256=event_claim_sha256,
            issued_at=issued_at,
            valid_until=valid_until,
            verifier_reference=verifier_reference,
            synthetic=identity.synthetic,
        )
        return ReviewEventAttestation(
            attestation_id=attestation_id,
            event_claim_sha256=event_claim_sha256,
            issued_at=issued_at,
            valid_until=valid_until,
            verifier_reference=verifier_reference,
            attestation_sha256=digest,
            synthetic=identity.synthetic,
        )

    def verify_review_attestation(
        self,
        identity: ReviewerIdentity,
        *,
        role: ReviewerRole,
        scope: ClinicalContextScope,
        event_claim_sha256: str,
        attestation: ReviewEventAttestation,
        evaluated_at: datetime,
    ) -> bool:
        if (
            not self.verify_identity_authorization(
                identity, role=role, scope=scope, at=evaluated_at
            )
            or attestation.event_claim_sha256 != event_claim_sha256
            or attestation.synthetic != identity.synthetic
            or attestation.issued_at > evaluated_at
            or attestation.valid_until <= evaluated_at
            or attestation.valid_until > identity.authorization_valid_until
        ):
            return False
        expected = _review_attestation_digest(
            key=self._attestation_key,
            identity_id=identity.identity_id,
            attestation_id=attestation.attestation_id,
            event_claim_sha256=event_claim_sha256,
            issued_at=attestation.issued_at,
            valid_until=attestation.valid_until,
            verifier_reference=attestation.verifier_reference,
            synthetic=attestation.synthetic,
        )
        return hmac.compare_digest(expected, attestation.attestation_sha256)


class ReviewSubjectKind(StrEnum):
    SOURCE_CANDIDATE = "source_candidate"
    SOURCE_SNAPSHOT = "source_snapshot"
    SOURCE = "source"
    CHANGE_SET = "change_set"
    CLINICAL_CLAIM = "clinical_claim"
    BINDING = "binding"
    TERMINOLOGY_MAPPING = "terminology_mapping"
    TRANSLATION = "translation"
    PATIENT_CONTENT = "patient_content"
    KNOWLEDGE_RELEASE = "knowledge_release"


_SUBJECT_COLLECTIONS = {
    ReviewSubjectKind.SOURCE_CANDIDATE: LedgerCollection.CANDIDATE,
    ReviewSubjectKind.SOURCE_SNAPSHOT: LedgerCollection.SNAPSHOT,
    ReviewSubjectKind.SOURCE: LedgerCollection.SOURCE,
    ReviewSubjectKind.CHANGE_SET: LedgerCollection.CHANGE_SET,
    ReviewSubjectKind.CLINICAL_CLAIM: LedgerCollection.CLAIM,
    ReviewSubjectKind.BINDING: LedgerCollection.BINDING,
    ReviewSubjectKind.TERMINOLOGY_MAPPING: LedgerCollection.TERMINOLOGY_MAPPING,
    ReviewSubjectKind.TRANSLATION: LedgerCollection.TRANSLATION,
    ReviewSubjectKind.PATIENT_CONTENT: LedgerCollection.PATIENT_CONTENT,
    ReviewSubjectKind.KNOWLEDGE_RELEASE: LedgerCollection.RELEASE_CANDIDATE,
}

_SUBJECT_GATES = {
    ReviewSubjectKind.SOURCE_CANDIDATE: GovernanceGate.SOURCE_PROMOTION,
    ReviewSubjectKind.SOURCE_SNAPSHOT: GovernanceGate.CONTENT_PERSISTENCE,
    ReviewSubjectKind.SOURCE: GovernanceGate.SOURCE_PROMOTION,
    ReviewSubjectKind.CHANGE_SET: GovernanceGate.CONTENT_PERSISTENCE,
    ReviewSubjectKind.CLINICAL_CLAIM: GovernanceGate.CLINICAL_CLAIM_APPROVAL,
    ReviewSubjectKind.BINDING: GovernanceGate.BINDING_APPROVAL,
    ReviewSubjectKind.TERMINOLOGY_MAPPING: GovernanceGate.TERMINOLOGY_MAPPING_PROMOTION,
    ReviewSubjectKind.TRANSLATION: GovernanceGate.TRANSLATION_PROMOTION,
    ReviewSubjectKind.PATIENT_CONTENT: GovernanceGate.PATIENT_CONTENT_APPROVAL,
    ReviewSubjectKind.KNOWLEDGE_RELEASE: GovernanceGate.KNOWLEDGE_RELEASE,
}

_AUTHOR_REVIEW_SEPARATION_GATES = {
    GovernanceGate.CLINICAL_CLAIM_APPROVAL,
    GovernanceGate.BINDING_APPROVAL,
    GovernanceGate.TERMINOLOGY_MAPPING_PROMOTION,
    GovernanceGate.TRANSLATION_PROMOTION,
    GovernanceGate.PATIENT_CONTENT_APPROVAL,
    GovernanceGate.KNOWLEDGE_RELEASE,
}


class ReviewSubject(StrictModel):
    subject_kind: ReviewSubjectKind
    object_ref: LedgerRef


class ReviewPacket(StrictModel):
    packet_id: SafeId
    subject: ReviewSubject
    subject_entry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    governance_bundle_id: SafeId
    governance_bundle_version: int = Field(ge=1)
    governance_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    governance_manifests: tuple[GovernanceManifestEvidence, ...] = Field(
        min_length=1
    )
    gate: GovernanceGate
    requested_roles: tuple[ReviewerRole, ...] = Field(min_length=1)
    requested_source_operations: tuple[SourceOperation, ...] = ()
    source_policy: SourcePolicyRef | None = None
    scope: ClinicalContextScope
    evidence_refs: tuple[LedgerRef, ...] = ()
    open_gap_refs: tuple[LedgerRef, ...] = ()
    author_provenance: AuthorProvenance | None = None
    known_limitations: tuple[NonBlank, ...] = Field(min_length=1)
    safety_boundary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: NonBlank
    synthetic: bool
    contains_patient_data: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_packet(self) -> "ReviewPacket":
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must include a timezone")
        if self.subject_entry_sha256 != self.subject.object_ref.entry_sha256:
            raise ValueError("Review Packet subject digest must match its exact LedgerRef")
        if self.gate in _AUTHOR_REVIEW_SEPARATION_GATES:
            if self.author_provenance is None:
                raise ValueError(
                    "Review Packet requires structured author provenance for this gate"
                )
            if self.author_provenance.authored_at > self.generated_at:
                raise ValueError("Review Packet predates its pinned author provenance")
        for values, label in (
            (self.requested_roles, "requested role"),
            (self.requested_source_operations, "requested source operation"),
            (self.evidence_refs, "evidence ref"),
            (self.open_gap_refs, "open gap ref"),
        ):
            keys = [
                item if isinstance(item, str) else json.dumps(item.model_dump(mode="json"), sort_keys=True)
                for item in values
            ]
            if len(keys) != len(set(keys)):
                raise ValueError(f"Review Packet contains duplicate {label}")
        manifest_keys = [
            (item.file_id, item.file_version) for item in self.governance_manifests
        ]
        if len(manifest_keys) != len(set(manifest_keys)):
            raise ValueError("Review Packet contains duplicate governance manifest")
        return self


class ReviewCheckResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class ReviewCheck(StrictModel):
    check_id: SafeId
    result: ReviewCheckResult
    evidence_refs: tuple[LedgerRef, ...] = ()
    note: NonBlank


class OperationDecision(StrEnum):
    APPROVED = "approved"
    PROHIBITED = "prohibited"
    NEEDS_VERIFICATION = "needs_verification"


class SourceOperationReview(StrictModel):
    operation: SourceOperation
    decision: OperationDecision
    conditions: tuple[NonBlank, ...] = ()


class ReviewDecisionPayload(StrictModel):
    checklist: tuple[ReviewCheck, ...] = Field(min_length=1)
    source_operation_decisions: tuple[SourceOperationReview, ...] = ()
    confirmed_scope: ClinicalContextScope | None = None
    limitations: tuple[NonBlank, ...] = Field(min_length=1)
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_payload(self) -> "ReviewDecisionPayload":
        check_ids = [item.check_id for item in self.checklist]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("review checklist IDs must be unique")
        operations = [item.operation for item in self.source_operation_decisions]
        if len(operations) != len(set(operations)):
            raise ValueError("source operation decisions must be unique")
        return self


class ReviewAxis(StrEnum):
    METADATA_QUALITY = "metadata_quality"
    RIGHTS = "rights"
    TERMINOLOGY = "terminology"
    CLINICAL = "clinical"
    PHARMACY = "pharmacy"
    CITATION_VERIFICATION = "citation_verification"
    INTERNAL_CONSISTENCY = "internal_consistency"
    APPLICABILITY = "applicability"
    RELEASE = "release"


_ROLE_AXES = {
    ReviewerRole.KNOWLEDGE_CURATOR: {
        ReviewAxis.METADATA_QUALITY,
        ReviewAxis.CITATION_VERIFICATION,
        ReviewAxis.INTERNAL_CONSISTENCY,
        ReviewAxis.RELEASE,
    },
    ReviewerRole.RIGHTS_OFFICER: {ReviewAxis.RIGHTS, ReviewAxis.RELEASE},
    ReviewerRole.TERMINOLOGIST: {
        ReviewAxis.TERMINOLOGY,
        ReviewAxis.APPLICABILITY,
    },
    ReviewerRole.CLINICAL_REVIEWER: {
        ReviewAxis.CLINICAL,
        ReviewAxis.APPLICABILITY,
        ReviewAxis.RELEASE,
    },
    ReviewerRole.PHARMACIST: {ReviewAxis.PHARMACY, ReviewAxis.APPLICABILITY},
}


class ReviewDecision(StrEnum):
    IN_REVIEW = "in_review"
    REVISION_REQUESTED = "revision_requested"
    REJECTED = "rejected"
    APPROVED = "approved"


class ReviewEvent(StrictModel):
    event_id: SafeId
    subject: ReviewSubject
    packet_ref: LedgerRef
    gate: GovernanceGate
    axis: ReviewAxis
    decision: ReviewDecision
    reviewer_identity_id: SafeId
    reviewer_principal_id: SafeId
    reviewer_role: ReviewerRole
    reviewer_roles: tuple[ReviewerRole, ...] = Field(min_length=1)
    reviewer_authorized_jurisdictions: tuple[Jurisdiction, ...] = Field(
        min_length=1
    )
    reviewer_authorized_scopes: tuple[ClinicalContextScope, ...] = Field(
        min_length=1
    )
    reviewer_authorization_valid_from: datetime
    reviewer_authorization_valid_until: datetime
    reviewer_active: bool
    reviewer_synthetic: bool
    reviewer_assurance: ReviewerAssurance
    reviewer_verification_reference: NonBlank | None = None
    reviewer_verification_evidence_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    reviewer_verified_by: NonBlank | None = None
    reviewer_verified_at: datetime | None = None
    reviewer_identity_assertion_sha256: Sha256
    decision_payload: ReviewDecisionPayload | None = None
    rationale: NonBlank
    decided_at: datetime
    expected_predecessor_sha256: Sha256 | None = None
    review_attestation: ReviewEventAttestation
    counts_toward_release: bool
    synthetic: bool
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_event(self) -> "ReviewEvent":
        if self.decided_at.tzinfo is None:
            raise ValueError("decided_at must include a timezone")
        if not self.reviewer_active:
            raise ValueError("ReviewEvent cannot claim an inactive reviewer")
        if self.reviewer_role not in self.reviewer_roles:
            raise ValueError("ReviewEvent role is absent from reviewer role snapshot")
        if self.reviewer_authorization_valid_from.tzinfo is None:
            raise ValueError("reviewer authorization start must include a timezone")
        if self.reviewer_authorization_valid_until.tzinfo is None:
            raise ValueError("reviewer authorization end must include a timezone")
        if not (
            self.reviewer_authorization_valid_from
            <= self.decided_at
            < self.reviewer_authorization_valid_until
        ):
            raise ValueError("ReviewEvent falls outside reviewer authorization validity")
        if self.decision == ReviewDecision.APPROVED and self.decision_payload is None:
            raise ValueError("approved ReviewEvent requires a decision payload")
        verification_evidence = (
            self.reviewer_verification_reference,
            self.reviewer_verification_evidence_sha256,
            self.reviewer_verified_by,
            self.reviewer_verified_at,
        )
        if self.reviewer_assurance == ReviewerAssurance.FORMALLY_VERIFIED:
            if any(item is None for item in verification_evidence):
                raise ValueError("formal ReviewEvent requires identity verification evidence")
            if self.reviewer_verified_at.tzinfo is None:
                raise ValueError("reviewer verification time must include a timezone")
        elif any(item is not None for item in verification_evidence):
            raise ValueError(
                "non-formal ReviewEvent cannot carry formal identity verification evidence"
            )
        if self.counts_toward_release:
            if self.synthetic or self.decision != ReviewDecision.APPROVED:
                raise ValueError("only non-synthetic approved event can count toward release")
            if self.reviewer_assurance != ReviewerAssurance.FORMALLY_VERIFIED:
                raise ValueError("release-counting event requires formally verified reviewer")
            if (
                self.reviewer_verification_reference is None
                or self.reviewer_verification_evidence_sha256 is None
                or self.reviewer_verified_by is None
                or self.reviewer_verified_at is None
            ):
                raise ValueError("release-counting event requires identity verification evidence")
        if (
            self.reviewer_identity_assertion_sha256
            != _reviewer_event_identity_assertion_sha256(self)
        ):
            raise ValueError("ReviewEvent reviewer identity assertion digest mismatch")
        if self.review_attestation.issued_at != self.decided_at:
            raise ValueError("ReviewEvent attestation time must equal decision time")
        if (
            self.review_attestation.event_claim_sha256
            != _review_event_claim_sha256(self)
        ):
            raise ValueError("ReviewEvent attestation does not bind the event claim")
        return self


class GateDecision(StrictModel):
    subject_ref: LedgerRef
    gate: GovernanceGate
    scope: ClinicalContextScope
    approved_roles: tuple[ReviewerRole, ...]
    evidence_refs: tuple[LedgerRef, ...]
    blocking_gap_refs: tuple[LedgerRef, ...] = ()
    synthetic: bool
    production_eligible: bool


class ReviewPacketBuilder:
    def __init__(
        self, *, bundle: KnowledgeOpsBundle, ledger: AppendOnlyLedger
    ) -> None:
        self._bundle = bundle
        self._ledger = ledger

    def build(
        self,
        *,
        subject_kind: ReviewSubjectKind,
        subject_ref: LedgerRef,
        gate: GovernanceGate,
        scope: ClinicalContextScope,
        generated_by: str,
        known_limitations: tuple[str, ...],
        requested_source_operations: tuple[SourceOperation, ...] = (),
        source_policy: SourcePolicyRef | None = None,
        additional_required_roles: tuple[ReviewerRole, ...] = (),
        evidence_refs: tuple[LedgerRef, ...] = (),
        open_gap_refs: tuple[LedgerRef, ...] = (),
        generated_at: datetime | None = None,
    ) -> LedgerRef:
        subject_kind_value = ReviewSubjectKind(subject_kind)
        expected_collection = _SUBJECT_COLLECTIONS[subject_kind_value]
        expected_gate = _SUBJECT_GATES.get(subject_kind_value)
        if expected_gate is None or expected_gate != GovernanceGate(gate):
            raise KnowledgeOpsPolicyError("Review subject is incompatible with gate")
        subject_entry = self._ledger.get(subject_ref)
        if subject_entry.payload_type == "evidence_candidate_v2":
            raise KnowledgeOpsPolicyError(
                "EvidenceCandidate must be promoted to a typed draft Claim before Claim review"
            )
        if subject_entry.collection != expected_collection.value:
            raise KnowledgeOpsPolicyError("Review subject collection is incompatible")
        if self._ledger.head(subject_ref.collection, subject_ref.record_id).ref != subject_ref:
            raise KnowledgeOpsPolicyError("Review Packet cannot target a stale subject")
        _assert_review_subject_no_sensitive_data(
            bundle=self._bundle,
            ledger=self._ledger,
            subject_kind=subject_kind_value,
            entry=subject_entry,
        )
        author_provenance = _subject_author_provenance(
            gate=GovernanceGate(gate),
            subject_payload=subject_entry.payload,
            subject_synthetic=subject_entry.synthetic,
        )
        assert_no_sensitive_data(
            {
                "known_limitations": known_limitations,
                "generated_by": generated_by,
            }
        )
        gate_policy = self._bundle.review_gate(gate)
        requested_roles = tuple(
            dict.fromkeys((*gate_policy.required_roles, *additional_required_roles))
        )
        source_policy_ref = source_policy
        operations = tuple(
            dict.fromkeys(
                (
                    *_default_operations_for_gate(gate),
                    *requested_source_operations,
                )
            )
        )
        owning_candidate_ref = _owning_candidate_ref(
            subject_kind=subject_kind_value,
            subject_ref=subject_ref,
            subject_payload=subject_entry.payload,
        )
        if owning_candidate_ref is not None:
            candidate_entry = self._ledger.get(owning_candidate_ref)
            if candidate_entry.collection != LedgerCollection.CANDIDATE.value:
                raise KnowledgeOpsPolicyError(
                    "Review subject does not reference a SourceCandidate"
                )
            candidate = SourceCandidate.model_validate(candidate_entry.payload)
            assert_no_sensitive_data(candidate.model_dump(mode="json"))
            profile = next(
                (
                    item
                    for item in self._bundle.coverage_profiles
                    if item.profile_id == candidate.validation_profile_id
                ),
                None,
            )
            if profile is None:
                raise KnowledgeOpsPolicyError(
                    "Review subject references an unknown validation profile"
                )
            if scope != profile.scope:
                raise KnowledgeOpsPolicyError("Review Packet scope differs from candidate profile")
            source_policy_ref = candidate.policy
            policy = self._bundle.source_policy(
                candidate.policy.policy_id, candidate.policy.policy_version
            )
            if policy.status != "active":
                raise KnowledgeOpsPolicyError("Review Packet cannot use retired SourcePolicy")
            for operation in operations:
                if policy.decision_for(operation) == "deny":
                    raise KnowledgeOpsPolicyError(
                        f"SourcePolicy denies requested review operation {operation}"
                    )
        elif operations:
            if source_policy_ref is None:
                raise KnowledgeOpsPolicyError(
                    "source operations require an exact SourcePolicyRef"
                )
            policy = self._bundle.source_policy(
                source_policy_ref.policy_id, source_policy_ref.policy_version
            )
            if policy.status != "active":
                raise KnowledgeOpsPolicyError("Review Packet cannot use retired SourcePolicy")
            for operation in operations:
                if policy.decision_for(operation) == "deny":
                    raise KnowledgeOpsPolicyError(
                        f"SourcePolicy denies requested review operation {operation}"
                    )

        discovered_gap_refs = _discover_open_related_gap_refs(
            ledger=self._ledger,
            subject_kind=subject_kind_value,
            subject_ref=subject_ref,
            subject_payload=subject_entry.payload,
        )
        effective_open_gap_refs = _deduplicate_refs(
            (*open_gap_refs, *discovered_gap_refs)
        )

        synthetic = subject_entry.synthetic
        for reference in evidence_refs:
            entry = _assert_review_evidence_no_sensitive_data(
                ledger=self._ledger,
                reference=reference,
            )
            synthetic = synthetic or entry.synthetic
        for reference in effective_open_gap_refs:
            entry = self._ledger.get(reference)
            if (
                entry.collection != LedgerCollection.GAP.value
                or entry.payload_type != "knowledge_gap"
            ):
                raise KnowledgeOpsPolicyError("open_gap_refs must reference KnowledgeGap")
            gap = KnowledgeGap.model_validate(entry.payload)
            _verify_gap_digest_context(self._ledger, gap)
            assert_no_sensitive_data(
                gap.model_dump(mode="json"),
                digest_trust_profile=DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP,
            )
            if gap.lifecycle != "open":
                raise KnowledgeOpsPolicyError("Review Packet gap must be open")
            synthetic = synthetic or entry.synthetic

        timestamp = generated_at or datetime.now(timezone.utc)
        subject = ReviewSubject(subject_kind=subject_kind, object_ref=subject_ref)
        packet_id = _chain_id("packet", subject_ref, str(gate))
        packet = ReviewPacket(
            packet_id=packet_id,
            subject=subject,
            subject_entry_sha256=subject_ref.entry_sha256,
            governance_bundle_id=self._bundle.index.bundle_id,
            governance_bundle_version=self._bundle.index.bundle_version,
            governance_index_sha256=self._bundle.index_sha256(),
            governance_manifests=self._bundle.manifest_evidence(),
            gate=gate,
            requested_roles=requested_roles,
            requested_source_operations=operations,
            source_policy=source_policy_ref,
            scope=scope,
            evidence_refs=evidence_refs,
            open_gap_refs=effective_open_gap_refs,
            author_provenance=author_provenance,
            known_limitations=known_limitations,
            safety_boundary_sha256=_boundary_digest(self._bundle),
            generated_at=timestamp,
            generated_by=generated_by,
            synthetic=synthetic,
        )
        assert_no_sensitive_data(
            packet.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.REVIEW_PACKET,
        )
        return self._ledger.append(
            LedgerCollection.REVIEW_PACKET,
            packet_id,
            payload_type="review_packet",
            payload=packet,
            recorded_by=generated_by,
            recorded_at=timestamp,
            synthetic=synthetic,
        ).ref


class ReviewEventService:
    def __init__(
        self,
        *,
        bundle: KnowledgeOpsBundle,
        ledger: AppendOnlyLedger,
        reviewers: ReviewerVerifier,
    ) -> None:
        self._bundle = bundle
        self._ledger = ledger
        self._reviewers = reviewers

    def record(
        self,
        *,
        packet_ref: LedgerRef,
        reviewer_identity_id: str,
        reviewer_role: ReviewerRole,
        axis: ReviewAxis,
        decision: ReviewDecision,
        rationale: str,
        decision_payload: ReviewDecisionPayload | None = None,
        decided_at: datetime | None = None,
    ) -> LedgerRef:
        packet_entry = self._ledger.get(packet_ref)
        if packet_entry.collection != LedgerCollection.REVIEW_PACKET.value:
            raise KnowledgeOpsPolicyError("ReviewEvent requires a Review Packet")
        if self._ledger.head(packet_ref.collection, packet_ref.record_id).ref != packet_ref:
            raise KnowledgeOpsPolicyError("ReviewEvent cannot use a stale Review Packet")
        packet = ReviewPacket.model_validate(packet_entry.payload)
        subject_entry, packet_material_entries = _resolve_packet_material(
            bundle=self._bundle,
            ledger=self._ledger,
            packet=packet,
        )
        assert_no_sensitive_data(
            packet_entry.payload,
            digest_trust_profile=DigestTrustProfile.REVIEW_PACKET,
        )
        timestamp = decided_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise KnowledgeOpsPolicyError("review decision time must include a timezone")
        identity = self._reviewers.resolve(reviewer_identity_id)
        if (
            identity is None
            or identity.identity_id != reviewer_identity_id
        ):
            raise KnowledgeOpsPolicyError("reviewer identity cannot be resolved")
        role = ReviewerRole(reviewer_role)
        try:
            identity_authorized = self._reviewers.verify_identity_authorization(
                identity,
                role=role,
                scope=packet.scope,
                at=timestamp,
            )
        except Exception as exc:
            raise KnowledgeOpsPolicyError(
                "reviewer identity authorization could not be verified"
            ) from exc
        if not identity_authorized:
            raise KnowledgeOpsPolicyError(
                "reviewer identity is inactive, expired, or outside authorized scope"
            )
        _assert_author_reviewer_separation(packet, identity)
        if role not in packet.requested_roles:
            raise KnowledgeOpsPolicyError("reviewer role is not authorized for this packet")
        axis_value = ReviewAxis(axis)
        if axis_value not in _ROLE_AXES[role]:
            raise KnowledgeOpsPolicyError("review axis is incompatible with reviewer role")
        decision_value = ReviewDecision(decision)
        if (
            decision_value == ReviewDecision.APPROVED
            and identity.assurance == ReviewerAssurance.IDENTITY_UNVERIFIED
        ):
            raise KnowledgeOpsPolicyError("unverified reviewer cannot approve")
        if decision_value == ReviewDecision.APPROVED:
            if decision_payload is None:
                raise KnowledgeOpsPolicyError("approved review requires decision payload")
            _validate_approved_payload(packet, role, decision_payload)

        supplemental_evidence_synthetic = False
        if decision_payload is not None:
            _assert_review_open_text_no_sensitive_data(
                decision_payload=decision_payload,
                rationale=rationale,
            )
            for check in decision_payload.checklist:
                for evidence_ref in check.evidence_refs:
                    evidence_entry = _assert_review_evidence_no_sensitive_data(
                        ledger=self._ledger,
                        reference=evidence_ref,
                    )
                    supplemental_evidence_synthetic = (
                        supplemental_evidence_synthetic or evidence_entry.synthetic
                    )
        else:
            _assert_review_open_text_no_sensitive_data(
                decision_payload=None,
                rationale=rationale,
            )

        gate_policy = self._bundle.review_gate(packet.gate)
        if identity.synthetic:
            if not gate_policy.synthetic_events_allowed_for_tests:
                raise KnowledgeOpsPolicyError("gate does not allow synthetic test events")
            if not (
                packet_entry.synthetic
                or packet.synthetic
                or subject_entry.synthetic
                or any(entry.synthetic for entry in packet_material_entries)
            ):
                raise KnowledgeOpsPolicyError(
                    "synthetic reviewer cannot approve or annotate non-synthetic subject"
                )

        synthetic = (
            packet_entry.synthetic
            or packet.synthetic
            or subject_entry.synthetic
            or any(entry.synthetic for entry in packet_material_entries)
            or identity.synthetic
            or supplemental_evidence_synthetic
        )
        counts = (
            decision_value == ReviewDecision.APPROVED
            and identity.is_production_eligible_at(timestamp)
            and not synthetic
        )
        event_record_id = _chain_id("review", packet.subject.object_ref, role.value)
        predecessor = self._ledger.head(
            LedgerCollection.REVIEW_EVENT, event_record_id
        )
        event_fields = dict(
            event_id=_event_id(event_record_id, timestamp),
            subject=packet.subject,
            packet_ref=packet_ref,
            gate=packet.gate,
            axis=axis_value,
            decision=decision_value,
            reviewer_identity_id=identity.identity_id,
            reviewer_principal_id=identity.principal_id,
            reviewer_role=role,
            reviewer_roles=identity.roles,
            reviewer_authorized_jurisdictions=identity.authorized_jurisdictions,
            reviewer_authorized_scopes=identity.authorized_scopes,
            reviewer_authorization_valid_from=identity.authorization_valid_from,
            reviewer_authorization_valid_until=identity.authorization_valid_until,
            reviewer_active=identity.active,
            reviewer_synthetic=identity.synthetic,
            reviewer_assurance=identity.assurance,
            reviewer_verification_reference=identity.verification_reference,
            reviewer_verification_evidence_sha256=(
                identity.verification_evidence_sha256
            ),
            reviewer_verified_by=identity.verified_by,
            reviewer_verified_at=identity.verified_at,
            reviewer_identity_assertion_sha256=(
                _reviewer_identity_assertion_sha256(identity)
            ),
            decision_payload=decision_payload,
            rationale=rationale,
            decided_at=timestamp,
            expected_predecessor_sha256=(
                None if predecessor is None else predecessor.entry_sha256
            ),
            counts_toward_release=counts,
            synthetic=synthetic,
            knowledge_effect="informational_only",
            runtime_authority="none",
        )
        event_claim_sha256 = _review_event_claim_sha256(event_fields)
        try:
            attestation = self._reviewers.issue_review_attestation(
                identity,
                role=role,
                scope=packet.scope,
                event_claim_sha256=event_claim_sha256,
                issued_at=timestamp,
            )
        except Exception as exc:
            raise KnowledgeOpsPolicyError(
                "review event attestation could not be issued"
            ) from exc
        if attestation is None:
            raise KnowledgeOpsPolicyError("review event attestation was denied")
        event = ReviewEvent(**event_fields, review_attestation=attestation)
        try:
            attestation_verified = self._reviewers.verify_review_attestation(
                identity,
                role=role,
                scope=packet.scope,
                event_claim_sha256=_review_event_claim_sha256(event),
                attestation=event.review_attestation,
                evaluated_at=timestamp,
            )
        except Exception as exc:
            raise KnowledgeOpsPolicyError(
                "issued review event attestation could not be verified"
            ) from exc
        if not attestation_verified:
            raise KnowledgeOpsPolicyError(
                "issued review event attestation failed verification"
            )
        assert_no_sensitive_data(
            event.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.REVIEW_EVENT,
        )
        return self._ledger.append(
            LedgerCollection.REVIEW_EVENT,
            event_record_id,
            payload_type="review_event_v3",
            payload=event,
            recorded_by=identity.identity_id,
            recorded_at=timestamp,
            synthetic=synthetic,
        ).ref


class ReviewLedgerDecisionProvider:
    """Resolve only the unique latest event for every role required by a gate."""

    def __init__(
        self,
        *,
        bundle: KnowledgeOpsBundle,
        ledger: AppendOnlyLedger,
        reviewers: ReviewerVerifier,
    ) -> None:
        self._bundle = bundle
        self._ledger = ledger
        self._reviewers = reviewers

    def resolve_gate(
        self,
        subject_ref: LedgerRef,
        gate: GovernanceGate,
        *,
        evaluated_at: datetime | None = None,
    ) -> GateDecision | None:
        evaluation_time = evaluated_at or datetime.now(timezone.utc)
        if evaluation_time.tzinfo is None:
            return None
        gate_policy = self._bundle.review_gate(gate)
        packet_id = _chain_id("packet", subject_ref, str(gate))
        packet_head = self._ledger.head(LedgerCollection.REVIEW_PACKET, packet_id)
        if packet_head is None:
            return None
        try:
            packet = ReviewPacket.model_validate(packet_head.payload)
        except ValidationError:
            return None
        if packet.subject.object_ref != subject_ref or packet.gate != str(gate):
            return None
        if not set(gate_policy.required_roles).issubset(packet.requested_roles):
            return None
        try:
            subject_head, packet_material_entries = _resolve_packet_material(
                bundle=self._bundle,
                ledger=self._ledger,
                packet=packet,
            )
            assert_no_sensitive_data(
                packet_head.payload,
                digest_trust_profile=DigestTrustProfile.REVIEW_PACKET,
            )
        except (
            KeyError,
            KnowledgeOpsIntegrityError,
            KnowledgeOpsPolicyError,
            ValidationError,
            ValueError,
        ):
            return None
        current_related_gaps = _discover_open_related_gap_refs(
            ledger=self._ledger,
            subject_kind=packet.subject.subject_kind,
            subject_ref=subject_ref,
            subject_payload=subject_head.payload,
        )
        packet_gap_keys = {_ref_key(reference) for reference in packet.open_gap_refs}
        if any(
            _ref_key(reference) not in packet_gap_keys
            for reference in current_related_gaps
        ):
            return None
        for reference in packet.open_gap_refs:
            gap_head = self._ledger.head(reference.collection, reference.record_id)
            if gap_head is None or gap_head.ref != reference:
                return None
        events: list[tuple[LedgerRef, ReviewEvent, ReviewerIdentity]] = []
        supplemental_evidence_synthetic = False
        for role in packet.requested_roles:
            record_id = _chain_id("review", subject_ref, str(role))
            head = self._ledger.head(LedgerCollection.REVIEW_EVENT, record_id)
            if head is None:
                return None
            try:
                event = ReviewEvent.model_validate(head.payload)
            except ValidationError:
                return None
            if (
                event.subject != packet.subject
                or event.gate != str(gate)
                or event.reviewer_role != str(role)
                or event.decision != ReviewDecision.APPROVED.value
                or event.axis not in _ROLE_AXES[ReviewerRole(role)]
            ):
                return None
            if event.packet_ref != packet_head.ref:
                return None
            if (
                head.recorded_at != event.decided_at
                or head.recorded_by != event.reviewer_identity_id
                or head.supersedes_entry_sha256
                != event.expected_predecessor_sha256
                or event.decided_at > evaluation_time
            ):
                return None
            try:
                _assert_review_open_text_no_sensitive_data(
                    decision_payload=event.decision_payload,
                    rationale=event.rationale,
                )
                _validate_approved_payload(
                    packet,
                    ReviewerRole(role),
                    event.decision_payload,
                )
                for check in event.decision_payload.checklist:
                    for evidence_ref in check.evidence_refs:
                        evidence_entry = _assert_review_evidence_no_sensitive_data(
                            ledger=self._ledger,
                            reference=evidence_ref,
                        )
                        supplemental_evidence_synthetic = (
                            supplemental_evidence_synthetic
                            or evidence_entry.synthetic
                        )
                identity = self._reviewers.resolve(event.reviewer_identity_id)
                if (
                    identity is None
                    or not _event_identity_snapshot_matches(event, identity)
                    or _is_author_reviewer_conflict(packet, identity)
                    or identity.assurance
                    == ReviewerAssurance.IDENTITY_UNVERIFIED
                    or not self._reviewers.verify_identity_authorization(
                        identity,
                        role=ReviewerRole(role),
                        scope=packet.scope,
                        at=event.decided_at,
                    )
                    or not self._reviewers.verify_identity_authorization(
                        identity,
                        role=ReviewerRole(role),
                        scope=packet.scope,
                        at=evaluation_time,
                    )
                    or not self._reviewers.verify_review_attestation(
                        identity,
                        role=ReviewerRole(role),
                        scope=packet.scope,
                        event_claim_sha256=_review_event_claim_sha256(event),
                        attestation=event.review_attestation,
                        evaluated_at=evaluation_time,
                    )
                ):
                    return None
                assert_no_sensitive_data(
                    head.payload,
                    digest_trust_profile=DigestTrustProfile.REVIEW_EVENT,
                )
            except Exception:
                return None
            events.append((head.ref, event, identity))
        synthetic = (
            packet_head.synthetic
            or packet.synthetic
            or subject_head.synthetic
            or any(entry.synthetic for entry in packet_material_entries)
            or any(
                self._ledger.get(reference).synthetic or event.synthetic
                for reference, event, _ in events
            )
            or any(identity.synthetic for _, _, identity in events)
            or supplemental_evidence_synthetic
        )
        if len({identity.principal_id for _, _, identity in events}) != len(events):
            return None
        production_eligible = all(
            identity.is_production_eligible_at(evaluation_time)
            and not event.synthetic
            and not event.review_attestation.synthetic
            for _, event, identity in events
        ) and not synthetic
        return GateDecision(
            subject_ref=subject_ref,
            gate=gate,
            scope=packet.scope,
            approved_roles=tuple(packet.requested_roles),
            evidence_refs=tuple(reference for reference, _, _ in events),
            blocking_gap_refs=packet.open_gap_refs,
            synthetic=synthetic,
            production_eligible=production_eligible,
        )

    def decision_for(
        self, subject_ref: LedgerRef, gate: GovernanceGate
    ) -> PromotionDecision | None:
        if GovernanceGate(gate) != GovernanceGate.SOURCE_PROMOTION:
            return None
        resolved = self.resolve_gate(subject_ref, gate)
        if resolved is None:
            return None
        return PromotionDecision(
            subject_ref=resolved.subject_ref,
            approved_roles=resolved.approved_roles,
            evidence_refs=resolved.evidence_refs,
            blocking_gap_refs=resolved.blocking_gap_refs,
            synthetic=resolved.synthetic,
            production_eligible=resolved.production_eligible,
        )


def _resolve_packet_material(
    *,
    bundle: KnowledgeOpsBundle,
    ledger: AppendOnlyLedger,
    packet: ReviewPacket,
):
    if (
        packet.governance_bundle_id != bundle.index.bundle_id
        or packet.governance_bundle_version != bundle.index.bundle_version
        or packet.governance_index_sha256 != bundle.index_sha256()
        or packet.governance_manifests != bundle.manifest_evidence()
        or packet.safety_boundary_sha256 != _boundary_digest(bundle)
    ):
        raise KnowledgeOpsPolicyError(
            "Review Packet does not pin the currently loaded governance bundle"
        )

    subject_kind = ReviewSubjectKind(packet.subject.subject_kind)
    expected_gate = _SUBJECT_GATES[subject_kind]
    expected_collection = _SUBJECT_COLLECTIONS[subject_kind]
    if packet.gate != expected_gate or packet.subject.object_ref.collection != expected_collection:
        raise KnowledgeOpsPolicyError("Review Packet subject is incompatible with its gate")
    gate_policy = bundle.review_gate(packet.gate)
    if not set(gate_policy.required_roles).issubset(packet.requested_roles):
        raise KnowledgeOpsPolicyError("Review Packet omits a gate-required role")
    required_operations = set(_default_operations_for_gate(packet.gate))
    if not required_operations.issubset(packet.requested_source_operations):
        raise KnowledgeOpsPolicyError("Review Packet omits a gate-required Source operation")

    subject_entry = ledger.get(packet.subject.object_ref)
    if subject_entry.payload_type == "evidence_candidate_v2":
        raise KnowledgeOpsPolicyError(
            "EvidenceCandidate cannot be used as a Claim review subject"
        )
    _assert_review_subject_no_sensitive_data(
        bundle=bundle,
        ledger=ledger,
        subject_kind=subject_kind,
        entry=subject_entry,
    )
    current_author_provenance = _subject_author_provenance(
        gate=GovernanceGate(packet.gate),
        subject_payload=subject_entry.payload,
        subject_synthetic=subject_entry.synthetic,
    )
    if current_author_provenance != packet.author_provenance:
        raise KnowledgeOpsPolicyError(
            "Review Packet author provenance differs from its exact subject"
        )
    subject_head = ledger.head(
        packet.subject.object_ref.collection,
        packet.subject.object_ref.record_id,
    )
    if subject_head is None or subject_head.ref != packet.subject.object_ref:
        raise KnowledgeOpsPolicyError("Review Packet subject is not the current ledger head")

    source_policy_ref = packet.source_policy
    owning_candidate_ref = _owning_candidate_ref(
        subject_kind=subject_kind,
        subject_ref=packet.subject.object_ref,
        subject_payload=subject_entry.payload,
    )
    if owning_candidate_ref is not None:
        candidate_entry = ledger.get(owning_candidate_ref)
        if candidate_entry.collection != LedgerCollection.CANDIDATE.value:
            raise KnowledgeOpsPolicyError("Review Packet candidate reference is invalid")
        candidate_head = ledger.head(
            owning_candidate_ref.collection,
            owning_candidate_ref.record_id,
        )
        if candidate_head is None or candidate_head.ref != owning_candidate_ref:
            raise KnowledgeOpsPolicyError("Review Packet candidate is not current")
        candidate = SourceCandidate.model_validate(candidate_entry.payload)
        assert_no_sensitive_data(candidate.model_dump(mode="json"))
        profile = next(
            (
                item
                for item in bundle.coverage_profiles
                if item.profile_id == candidate.validation_profile_id
            ),
            None,
        )
        if profile is None or packet.scope != profile.scope:
            raise KnowledgeOpsPolicyError(
                "Review Packet scope differs from the exact candidate profile"
            )
        if source_policy_ref != candidate.policy:
            raise KnowledgeOpsPolicyError(
                "Review Packet SourcePolicy differs from its SourceCandidate"
            )

    if packet.requested_source_operations:
        if source_policy_ref is None:
            raise KnowledgeOpsPolicyError(
                "Review Packet Source operations require an exact SourcePolicy"
            )
        policy = bundle.source_policy(
            source_policy_ref.policy_id,
            source_policy_ref.policy_version,
        )
        if policy.status != "active":
            raise KnowledgeOpsPolicyError("Review Packet uses a retired SourcePolicy")
        if any(
            policy.decision_for(operation) == "deny"
            for operation in packet.requested_source_operations
        ):
            raise KnowledgeOpsPolicyError("Review Packet requests a denied Source operation")

    material_entries = []
    for reference in packet.evidence_refs:
        entry = _assert_review_evidence_no_sensitive_data(
            ledger=ledger,
            reference=reference,
        )
        material_entries.append(entry)
    for reference in packet.open_gap_refs:
        entry = ledger.get(reference)
        if (
            entry.collection != LedgerCollection.GAP.value
            or entry.payload_type != "knowledge_gap"
        ):
            raise KnowledgeOpsPolicyError("Review Packet open gap ref is not a KnowledgeGap")
        gap = KnowledgeGap.model_validate(entry.payload)
        _verify_gap_digest_context(ledger, gap)
        assert_no_sensitive_data(
            gap.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP,
        )
        material_entries.append(entry)
        gap_head = ledger.head(reference.collection, reference.record_id)
        if gap.lifecycle != "open" or gap_head is None or gap_head.ref != reference:
            raise KnowledgeOpsPolicyError("Review Packet does not pin a current open gap")

    current_related_gaps = _discover_open_related_gap_refs(
        ledger=ledger,
        subject_kind=subject_kind,
        subject_ref=packet.subject.object_ref,
        subject_payload=subject_entry.payload,
    )
    packet_gap_keys = {_ref_key(reference) for reference in packet.open_gap_refs}
    if any(_ref_key(reference) not in packet_gap_keys for reference in current_related_gaps):
        raise KnowledgeOpsPolicyError("Review Packet omits a current related open gap")
    return subject_entry, tuple(material_entries)


def _validate_approved_payload(
    packet: ReviewPacket,
    role: ReviewerRole,
    payload: ReviewDecisionPayload,
) -> None:
    results = {item.result for item in payload.checklist}
    if ReviewCheckResult.FAIL.value in results or ReviewCheckResult.PASS.value not in results:
        raise KnowledgeOpsPolicyError(
            "approved review checklist requires at least one pass and no failures"
        )
    if role == ReviewerRole.RIGHTS_OFFICER:
        requested = set(packet.requested_source_operations)
        decisions = {
            item.operation: item.decision for item in payload.source_operation_decisions
        }
        if set(decisions) != requested or any(
            decision != OperationDecision.APPROVED.value
            for decision in decisions.values()
        ):
            raise KnowledgeOpsPolicyError(
                "rights approval must explicitly approve every requested source operation"
            )
    elif payload.source_operation_decisions:
        raise KnowledgeOpsPolicyError(
            "only rights officer can decide source reuse operations"
        )
    if role in {
        ReviewerRole.CLINICAL_REVIEWER,
        ReviewerRole.TERMINOLOGIST,
        ReviewerRole.PHARMACIST,
    } and payload.confirmed_scope != packet.scope:
        raise KnowledgeOpsPolicyError("specialist approval must confirm exact packet scope")


def _assert_review_open_text_no_sensitive_data(
    *,
    decision_payload: ReviewDecisionPayload | None,
    rationale: str,
) -> None:
    """Scan open review text before any digest path receives verified trust."""

    value: dict[str, object] = {"rationale": rationale}
    if decision_payload is not None:
        value["decision_payload"] = {
            "checklist": [
                {
                    "check_id": check.check_id,
                    "result": check.result,
                    "note": check.note,
                }
                for check in decision_payload.checklist
            ],
            "source_operation_decisions": [
                {
                    "operation": item.operation,
                    "decision": item.decision,
                    "conditions": item.conditions,
                }
                for item in decision_payload.source_operation_decisions
            ],
            "confirmed_scope": (
                None
                if decision_payload.confirmed_scope is None
                else decision_payload.confirmed_scope.model_dump(mode="json")
            ),
            "limitations": decision_payload.limitations,
        }
    assert_no_sensitive_data(value)


def _assert_review_evidence_no_sensitive_data(
    *,
    ledger: AppendOnlyLedger,
    reference: LedgerRef,
) -> LedgerEntry:
    """Replay evidence and grant digest trust only for an exact verified type."""

    entry = ledger.get(reference)
    dispatch_key = (entry.collection, entry.payload_type)
    profile = _TYPED_REVIEW_EVIDENCE_DIGEST_PROFILES.get(dispatch_key)
    if profile is None:
        if (
            entry.collection in _TYPED_REVIEW_EVIDENCE_COLLECTIONS
            or entry.payload_type in _TYPED_REVIEW_EVIDENCE_PAYLOAD_TYPES
        ):
            raise KnowledgeOpsPolicyError(
                "typed review evidence collection/payload_type mismatch"
            )
        # Unknown and legacy evidence receive no digest trust.
        assert_no_sensitive_data(entry.payload)
        return entry
    if profile == DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT:
        _assert_source_snapshot_entry_no_sensitive_data(ledger, entry)
        return entry
    if profile == DigestTrustProfile.EVIDENCE_CANDIDATE:
        _assert_evidence_candidate_entry_no_sensitive_data(ledger, entry)
        return entry
    raise KnowledgeOpsPolicyError("unsupported typed review evidence profile")


def _assert_source_snapshot_entry_no_sensitive_data(
    ledger: AppendOnlyLedger,
    entry: LedgerEntry,
) -> None:
    if (
        entry.collection != LedgerCollection.SNAPSHOT.value
        or entry.payload_type != "source_snapshot"
    ):
        raise KnowledgeOpsPolicyError(
            "SourceSnapshot evidence requires snapshot/source_snapshot"
        )
    snapshot = SourceSnapshot.model_validate(entry.payload)
    if snapshot.snapshot_id != entry.record_id:
        raise KnowledgeOpsPolicyError("SourceSnapshot ledger identity differs")
    if snapshot.synthetic is not entry.synthetic:
        raise KnowledgeOpsPolicyError("SourceSnapshot synthetic lineage differs")
    _verify_source_snapshot_digest_context(ledger, snapshot)
    assert_no_sensitive_data(
        snapshot.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
    )


def _assert_evidence_candidate_entry_no_sensitive_data(
    ledger: AppendOnlyLedger,
    entry: LedgerEntry,
) -> None:
    """Replay exact synthetic evidence lineage before trusting nested digests."""

    from continucare.knowledge.ops.evidence import EvidenceCandidate

    if (
        entry.collection != LedgerCollection.EVIDENCE_CANDIDATE.value
        or entry.payload_type != "evidence_candidate_v2"
    ):
        raise KnowledgeOpsPolicyError(
            "EvidenceCandidate review evidence requires the exact v2 payload"
        )
    evidence = EvidenceCandidate.model_validate(entry.payload)
    source_entry = _replay_typed_entry(
        ledger,
        evidence.source_candidate_ref,
        collection=LedgerCollection.CANDIDATE,
        payload_type="source_candidate",
    )
    snapshot_entry = _replay_typed_entry(
        ledger,
        evidence.source_snapshot_ref,
        collection=LedgerCollection.SNAPSHOT,
        payload_type="source_snapshot",
    )
    source = SourceCandidate.model_validate(source_entry.payload)
    snapshot = SourceSnapshot.model_validate(snapshot_entry.payload)
    if (
        evidence.candidate_id != entry.record_id
        or evidence.synthetic is not entry.synthetic
        or source.candidate_id != evidence.source_candidate_ref.record_id
        or snapshot.snapshot_id != evidence.source_snapshot_ref.record_id
        or snapshot.candidate_ref != evidence.source_candidate_ref
        or evidence.whole_record_sha256 != snapshot.content_sha256
        or source.synthetic is not source_entry.synthetic
        or snapshot.synthetic is not snapshot_entry.synthetic
        or evidence.synthetic is not source.synthetic
        or evidence.synthetic is not snapshot.synthetic
    ):
        raise KnowledgeOpsPolicyError("EvidenceCandidate review lineage differs")
    assert_no_sensitive_data(source.model_dump(mode="json"))
    assert_no_sensitive_data(
        snapshot.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
    )
    assert_no_sensitive_data(
        evidence.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.EVIDENCE_CANDIDATE,
    )


def _assert_review_subject_no_sensitive_data(
    *,
    bundle: KnowledgeOpsBundle,
    ledger: AppendOnlyLedger,
    subject_kind: ReviewSubjectKind,
    entry: LedgerEntry,
) -> None:
    """Select digest trust only after an exact subject schema is established."""

    kind = ReviewSubjectKind(subject_kind)
    if kind == ReviewSubjectKind.SOURCE_CANDIDATE:
        if entry.payload_type != "source_candidate":
            raise KnowledgeOpsPolicyError(
                "SourceCandidate review requires the exact source_candidate payload"
            )
        subject = SourceCandidate.model_validate(entry.payload)
        if subject.candidate_id != entry.record_id:
            raise KnowledgeOpsPolicyError("SourceCandidate ledger identity differs")
        assert_no_sensitive_data(subject.model_dump(mode="json"))
        return
    if kind == ReviewSubjectKind.SOURCE_SNAPSHOT:
        _assert_source_snapshot_entry_no_sensitive_data(ledger, entry)
        return
    if kind == ReviewSubjectKind.SOURCE:
        if entry.payload_type != "governed_source_v2":
            raise KnowledgeOpsPolicyError(
                "Source review requires the exact governed_source_v2 payload"
            )
        subject = GovernedSourceV2.model_validate(entry.payload)
        if subject.source_id != entry.record_id:
            raise KnowledgeOpsPolicyError("governed Source ledger identity differs")
        _verify_governed_source_digest_context(ledger, subject)
        assert_no_sensitive_data(
            subject.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.GOVERNED_SOURCE,
        )
        return
    if kind == ReviewSubjectKind.CHANGE_SET:
        if entry.payload_type != "change_set":
            raise KnowledgeOpsPolicyError(
                "ChangeSet review requires the exact change_set payload"
            )
        subject = ChangeSet.model_validate(entry.payload)
        if subject.change_set_id != entry.record_id:
            raise KnowledgeOpsPolicyError("ChangeSet ledger identity differs")
        _verify_change_set_digest_context(ledger, subject)
        assert_no_sensitive_data(
            subject.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_CHANGE_SET,
        )
        return
    if (
        kind == ReviewSubjectKind.CLINICAL_CLAIM
        and entry.payload_type == "machine_draft_claim_v2"
    ):
        from continucare.knowledge.ops.evidence import MachineDraftClaim

        subject = MachineDraftClaim.model_validate(entry.payload)
        if subject.claim_id != entry.record_id:
            raise KnowledgeOpsPolicyError("machine draft Claim ledger identity differs")
        _verify_machine_draft_digest_context(ledger, subject)
        assert_no_sensitive_data(
            subject.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.MACHINE_DRAFT_CLAIM,
        )
        return
    if kind == ReviewSubjectKind.KNOWLEDGE_RELEASE:
        if entry.payload_type != "knowledge_release_candidate":
            raise KnowledgeOpsPolicyError(
                "release review requires the exact knowledge_release_candidate payload"
            )
        from continucare.knowledge.ops.release import KnowledgeReleaseCandidate

        subject = KnowledgeReleaseCandidate.model_validate(entry.payload)
        if subject.release_candidate_id != entry.record_id:
            raise KnowledgeOpsPolicyError("release candidate ledger identity differs")
        _verify_release_candidate_digest_context(bundle, ledger, subject)
        assert_no_sensitive_data(
            subject.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.KNOWLEDGE_RELEASE_CANDIDATE,
        )
        return
    # Legacy binding/content/translation/terminology and non-machine Claim payloads
    # have no digest-bearing v2 schema in this slice, so they receive no trust.
    assert_no_sensitive_data(entry.payload)


def _replay_typed_entry(
    ledger: AppendOnlyLedger,
    reference: LedgerRef,
    *,
    collection: LedgerCollection,
    payload_type: str,
) -> LedgerEntry:
    entry = ledger.get(reference)
    if entry.collection != collection.value or entry.payload_type != payload_type:
        raise KnowledgeOpsPolicyError(
            f"verified digest context requires {collection.value}/{payload_type}"
        )
    return entry


def _verify_gap_digest_context(
    ledger: AppendOnlyLedger,
    gap: KnowledgeGap,
) -> None:
    if gap.subject_ref is not None:
        ledger.get(gap.subject_ref)


def _verify_source_snapshot_digest_context(
    ledger: AppendOnlyLedger,
    snapshot: SourceSnapshot,
) -> None:
    candidate_entry = _replay_typed_entry(
        ledger,
        snapshot.candidate_ref,
        collection=LedgerCollection.CANDIDATE,
        payload_type="source_candidate",
    )
    candidate = SourceCandidate.model_validate(candidate_entry.payload)
    if candidate.candidate_id != snapshot.candidate_ref.record_id:
        raise KnowledgeOpsPolicyError("SourceSnapshot candidate identity differs")
    if (
        candidate.synthetic is not candidate_entry.synthetic
        or candidate.synthetic is not snapshot.synthetic
    ):
        raise KnowledgeOpsPolicyError("SourceSnapshot candidate lineage differs")
    assert_no_sensitive_data(candidate.model_dump(mode="json"))


def _verify_governed_source_digest_context(
    ledger: AppendOnlyLedger,
    source: GovernedSourceV2,
) -> None:
    candidate_entry = _replay_typed_entry(
        ledger,
        source.candidate_ref,
        collection=LedgerCollection.CANDIDATE,
        payload_type="source_candidate",
    )
    snapshot_entry = _replay_typed_entry(
        ledger,
        source.snapshot_ref,
        collection=LedgerCollection.SNAPSHOT,
        payload_type="source_snapshot",
    )
    candidate = SourceCandidate.model_validate(candidate_entry.payload)
    snapshot = SourceSnapshot.model_validate(snapshot_entry.payload)
    if (
        candidate.candidate_id != source.candidate_ref.record_id
        or snapshot.snapshot_id != source.snapshot_ref.record_id
        or snapshot.candidate_ref != source.candidate_ref
        or snapshot.content_sha256 != source.content_sha256
    ):
        raise KnowledgeOpsPolicyError("governed Source digest lineage differs")
    assert_no_sensitive_data(candidate.model_dump(mode="json"))
    assert_no_sensitive_data(
        snapshot.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
    )
    for reference in source.promotion_evidence_refs:
        evidence_entry = ledger.get(reference)
        if evidence_entry.collection != LedgerCollection.REVIEW_EVENT.value:
            raise KnowledgeOpsPolicyError(
                "governed Source promotion evidence is not a ReviewEvent"
            )
    for reference in source.unresolved_gap_refs:
        gap_entry = _replay_typed_entry(
            ledger,
            reference,
            collection=LedgerCollection.GAP,
            payload_type="knowledge_gap",
        )
        gap = KnowledgeGap.model_validate(gap_entry.payload)
        _verify_gap_digest_context(ledger, gap)
        assert_no_sensitive_data(
            gap.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP,
        )


def _verify_change_set_digest_context(
    ledger: AppendOnlyLedger,
    change: ChangeSet,
) -> None:
    candidate_entry = _replay_typed_entry(
        ledger,
        change.candidate_ref,
        collection=LedgerCollection.CANDIDATE,
        payload_type="source_candidate",
    )
    candidate = SourceCandidate.model_validate(candidate_entry.payload)
    if candidate.candidate_id != change.candidate_ref.record_id:
        raise KnowledgeOpsPolicyError("ChangeSet candidate identity differs")
    assert_no_sensitive_data(candidate.model_dump(mode="json"))
    snapshot_refs = (
        (change.previous_snapshot_ref,)
        if change.previous_snapshot_ref is not None
        else ()
    ) + (change.current_snapshot_ref,)
    for reference in snapshot_refs:
        snapshot_entry = _replay_typed_entry(
            ledger,
            reference,
            collection=LedgerCollection.SNAPSHOT,
            payload_type="source_snapshot",
        )
        snapshot = SourceSnapshot.model_validate(snapshot_entry.payload)
        if (
            snapshot.snapshot_id != reference.record_id
            or snapshot.candidate_ref != change.candidate_ref
        ):
            raise KnowledgeOpsPolicyError("ChangeSet snapshot lineage differs")
        assert_no_sensitive_data(
            snapshot.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
        )


def _verify_machine_draft_digest_context(
    ledger: AppendOnlyLedger,
    claim,
) -> None:
    from continucare.knowledge.ops.evidence import EvidenceCandidate

    evidence_entry = _replay_typed_entry(
        ledger,
        claim.evidence_candidate_ref,
        collection=LedgerCollection.EVIDENCE_CANDIDATE,
        payload_type="evidence_candidate_v2",
    )
    source_entry = _replay_typed_entry(
        ledger,
        claim.source_candidate_ref,
        collection=LedgerCollection.CANDIDATE,
        payload_type="source_candidate",
    )
    snapshot_entry = _replay_typed_entry(
        ledger,
        claim.source_snapshot_ref,
        collection=LedgerCollection.SNAPSHOT,
        payload_type="source_snapshot",
    )
    evidence = EvidenceCandidate.model_validate(evidence_entry.payload)
    source = SourceCandidate.model_validate(source_entry.payload)
    snapshot = SourceSnapshot.model_validate(snapshot_entry.payload)
    if (
        evidence.candidate_id != claim.evidence_candidate_ref.record_id
        or source.candidate_id != claim.source_candidate_ref.record_id
        or snapshot.snapshot_id != claim.source_snapshot_ref.record_id
        or evidence.source_candidate_ref != claim.source_candidate_ref
        or evidence.source_snapshot_ref != claim.source_snapshot_ref
        or snapshot.candidate_ref != claim.source_candidate_ref
        or evidence.whole_record_sha256 != snapshot.content_sha256
    ):
        raise KnowledgeOpsPolicyError("machine draft Claim digest lineage differs")
    assert_no_sensitive_data(source.model_dump(mode="json"))
    assert_no_sensitive_data(
        snapshot.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
    )
    assert_no_sensitive_data(
        evidence.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.EVIDENCE_CANDIDATE,
    )


def _verify_release_candidate_digest_context(
    bundle: KnowledgeOpsBundle,
    ledger: AppendOnlyLedger,
    candidate,
) -> None:
    from continucare.knowledge.ops.release import (
        _ARTIFACT_COLLECTIONS,
        _assert_release_artifact_no_sensitive_data,
    )

    if (
        candidate.governance_bundle_id != bundle.index.bundle_id
        or candidate.governance_bundle_version != bundle.index.bundle_version
        or candidate.governance_index_sha256 != bundle.index_sha256()
        or candidate.governance_manifests != bundle.manifest_evidence()
    ):
        raise KnowledgeOpsPolicyError(
            "release candidate does not pin the loaded governance bundle"
        )
    for artifact in candidate.artifacts:
        artifact_entry = ledger.get(artifact.object_ref)
        expected_collection = _ARTIFACT_COLLECTIONS[artifact.artifact_kind]
        if artifact_entry.collection != expected_collection.value:
            raise KnowledgeOpsPolicyError(
                "release artifact kind differs from its exact collection"
            )
        _assert_release_artifact_no_sensitive_data(
            ledger, artifact, artifact_entry
        )
    for reference in candidate.blocking_gap_refs:
        gap_entry = _replay_typed_entry(
            ledger,
            reference,
            collection=LedgerCollection.GAP,
            payload_type="knowledge_gap",
        )
        gap = KnowledgeGap.model_validate(gap_entry.payload)
        _verify_gap_digest_context(ledger, gap)
        assert_no_sensitive_data(
            gap.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP,
        )


def _subject_author_provenance(
    *,
    gate: GovernanceGate,
    subject_payload: dict[str, object],
    subject_synthetic: bool,
) -> AuthorProvenance | None:
    if GovernanceGate(gate) not in _AUTHOR_REVIEW_SEPARATION_GATES:
        return None
    raw_author = subject_payload.get("author_provenance")
    if raw_author is None:
        raise KnowledgeOpsPolicyError(
            "review subject requires structured author provenance"
        )
    try:
        author = AuthorProvenance.model_validate(raw_author)
    except ValidationError as exc:
        raise KnowledgeOpsPolicyError(
            "review subject author provenance is invalid"
        ) from exc
    if author.synthetic != subject_synthetic:
        raise KnowledgeOpsPolicyError(
            "review subject and author provenance synthetic status differ"
        )
    return author


def _is_author_reviewer_conflict(
    packet: ReviewPacket, identity: ReviewerIdentity
) -> bool:
    author = packet.author_provenance
    return (
        packet.gate in _AUTHOR_REVIEW_SEPARATION_GATES
        and author is not None
        and (
            identity.identity_id == author.author_identity_id
            or identity.principal_id == author.author_principal_id
        )
    )


def _assert_author_reviewer_separation(
    packet: ReviewPacket, identity: ReviewerIdentity
) -> None:
    if _is_author_reviewer_conflict(packet, identity):
        raise KnowledgeOpsPolicyError(
            "author and reviewer must be different identities and principals"
        )


def _default_operations_for_gate(
    gate: GovernanceGate,
) -> tuple[SourceOperation, ...]:
    defaults = {
        GovernanceGate.SOURCE_PROMOTION: (SourceOperation.REGISTER_LINK_METADATA,),
        GovernanceGate.CONTENT_PERSISTENCE: (SourceOperation.PERSIST_SNAPSHOT,),
        GovernanceGate.TERMINOLOGY_MAPPING_PROMOTION: (
            SourceOperation.CREATE_MAPPING,
        ),
        GovernanceGate.TRANSLATION_PROMOTION: (SourceOperation.TRANSLATE,),
    }
    return defaults.get(GovernanceGate(gate), ())


def _boundary_digest(bundle: KnowledgeOpsBundle) -> str:
    payload = json.dumps(
        bundle.boundary.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _related_subject_refs(
    *,
    subject_kind: ReviewSubjectKind,
    subject_ref: LedgerRef,
    subject_payload: dict[str, object],
) -> tuple[LedgerRef, ...]:
    related = [subject_ref]
    owning_candidate_ref = _owning_candidate_ref(
        subject_kind=subject_kind,
        subject_ref=subject_ref,
        subject_payload=subject_payload,
    )
    if owning_candidate_ref is not None:
        related.append(owning_candidate_ref)
    return _deduplicate_refs(tuple(related))


def _owning_candidate_ref(
    *,
    subject_kind: ReviewSubjectKind,
    subject_ref: LedgerRef,
    subject_payload: dict[str, object],
) -> LedgerRef | None:
    if subject_kind == ReviewSubjectKind.SOURCE_CANDIDATE:
        SourceCandidate.model_validate(subject_payload)
        return subject_ref
    if subject_kind == ReviewSubjectKind.SOURCE_SNAPSHOT:
        return SourceSnapshot.model_validate(subject_payload).candidate_ref
    if subject_kind == ReviewSubjectKind.CHANGE_SET:
        return ChangeSet.model_validate(subject_payload).candidate_ref
    if subject_kind == ReviewSubjectKind.SOURCE:
        return GovernedSourceV2.model_validate(subject_payload).candidate_ref
    return None


def _discover_open_related_gap_refs(
    *,
    ledger: AppendOnlyLedger,
    subject_kind: ReviewSubjectKind,
    subject_ref: LedgerRef,
    subject_payload: dict[str, object],
) -> tuple[LedgerRef, ...]:
    related_subject_refs = _related_subject_refs(
        subject_kind=subject_kind,
        subject_ref=subject_ref,
        subject_payload=subject_payload,
    )
    return tuple(
        entry.ref
        for entry in ledger.list_heads(LedgerCollection.GAP)
        if _is_open_related_gap(entry.payload, related_subject_refs)
    )


def _is_open_related_gap(
    payload: dict[str, object], related_subject_refs: tuple[LedgerRef, ...]
) -> bool:
    gap = KnowledgeGap.model_validate(payload)
    return (
        gap.lifecycle == "open"
        and gap.subject_ref is not None
        and _ref_key(gap.subject_ref)
        in {_ref_key(reference) for reference in related_subject_refs}
    )


def _deduplicate_refs(references: tuple[LedgerRef, ...]) -> tuple[LedgerRef, ...]:
    unique: dict[tuple[str, str, int, str], LedgerRef] = {}
    for reference in references:
        unique.setdefault(_ref_key(reference), reference)
    return tuple(unique.values())


def _ref_key(reference: LedgerRef) -> tuple[str, str, int, str]:
    return (
        str(reference.collection),
        reference.record_id,
        reference.record_version,
        reference.entry_sha256,
    )


def _chain_id(prefix: str, subject_ref: LedgerRef, discriminator: str) -> str:
    raw = (
        f"{prefix}-{subject_ref.collection}-{subject_ref.record_id}-"
        f"{subject_ref.record_version}-{subject_ref.entry_sha256[:12]}-{discriminator}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest_derived_internal_id(prefix, digest, digest_characters=32)


def _event_id(record_id: str, timestamp: datetime) -> str:
    digest = hashlib.sha256(
        f"{record_id}|{timestamp.isoformat()}".encode("utf-8")
    ).hexdigest()
    return digest_derived_internal_id("event", digest, digest_characters=20)


def _attestation_id(event_claim_sha256: str) -> str:
    return digest_derived_internal_id(
        "attest", event_claim_sha256, digest_characters=32
    )


def _canonical_model_key(value: StrictModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_json_value(value):
    if isinstance(value, StrictModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _canonical_json_value(item) for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_json_value(item) for item in value]
    return value


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        _canonical_json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reviewer_identity_assertion_payload(
    identity: ReviewerIdentity,
) -> dict[str, object]:
    return {
        "identity_id": identity.identity_id,
        "principal_id": identity.principal_id,
        "roles": identity.roles,
        "authorized_jurisdictions": identity.authorized_jurisdictions,
        "authorized_scopes": identity.authorized_scopes,
        "authorization_valid_from": identity.authorization_valid_from,
        "authorization_valid_until": identity.authorization_valid_until,
        "active": identity.active,
        "synthetic": identity.synthetic,
        "assurance": identity.assurance,
        "verification_reference": identity.verification_reference,
        "verification_evidence_sha256": identity.verification_evidence_sha256,
        "verified_by": identity.verified_by,
        "verified_at": identity.verified_at,
    }


def _reviewer_identity_assertion_sha256(identity: ReviewerIdentity) -> str:
    return _canonical_sha256(_reviewer_identity_assertion_payload(identity))


def _reviewer_event_identity_assertion_sha256(event: ReviewEvent) -> str:
    return _canonical_sha256(
        {
            "identity_id": event.reviewer_identity_id,
            "principal_id": event.reviewer_principal_id,
            "roles": event.reviewer_roles,
            "authorized_jurisdictions": event.reviewer_authorized_jurisdictions,
            "authorized_scopes": event.reviewer_authorized_scopes,
            "authorization_valid_from": event.reviewer_authorization_valid_from,
            "authorization_valid_until": event.reviewer_authorization_valid_until,
            "active": event.reviewer_active,
            "synthetic": event.reviewer_synthetic,
            "assurance": event.reviewer_assurance,
            "verification_reference": event.reviewer_verification_reference,
            "verification_evidence_sha256": (
                event.reviewer_verification_evidence_sha256
            ),
            "verified_by": event.reviewer_verified_by,
            "verified_at": event.reviewer_verified_at,
        }
    )


def _event_identity_snapshot_matches(
    event: ReviewEvent, identity: ReviewerIdentity
) -> bool:
    return (
        event.reviewer_identity_id == identity.identity_id
        and event.reviewer_principal_id == identity.principal_id
        and event.reviewer_roles == identity.roles
        and event.reviewer_authorized_jurisdictions
        == identity.authorized_jurisdictions
        and event.reviewer_authorized_scopes == identity.authorized_scopes
        and event.reviewer_authorization_valid_from
        == identity.authorization_valid_from
        and event.reviewer_authorization_valid_until
        == identity.authorization_valid_until
        and event.reviewer_active == identity.active
        and event.reviewer_synthetic == identity.synthetic
        and event.reviewer_assurance == identity.assurance
        and event.reviewer_verification_reference == identity.verification_reference
        and event.reviewer_verification_evidence_sha256
        == identity.verification_evidence_sha256
        and event.reviewer_verified_by == identity.verified_by
        and event.reviewer_verified_at == identity.verified_at
        and event.reviewer_identity_assertion_sha256
        == _reviewer_identity_assertion_sha256(identity)
    )


def _review_event_claim_sha256(
    value: ReviewEvent | dict[str, object],
) -> str:
    if isinstance(value, ReviewEvent):
        payload = value.model_dump(mode="json")
    else:
        payload = _canonical_json_value(value)
    claim = {
        key: item
        for key, item in payload.items()
        if key not in {"counts_toward_release", "review_attestation"}
    }
    return _canonical_sha256(claim)


def _review_attestation_digest(
    *,
    key: bytes,
    identity_id: str,
    attestation_id: str,
    event_claim_sha256: str,
    issued_at: datetime,
    valid_until: datetime,
    verifier_reference: str,
    synthetic: bool,
) -> str:
    payload = {
        "identity_id": identity_id,
        "attestation_id": attestation_id,
        "event_claim_sha256": event_claim_sha256,
        "issued_at": issued_at,
        "valid_until": valid_until,
        "verifier_reference": verifier_reference,
        "synthetic": synthetic,
    }
    encoded = json.dumps(
        _canonical_json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()

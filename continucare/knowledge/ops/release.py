"""KnowledgeRelease candidate staging and fail-closed readiness assessment."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from continucare.knowledge.ops.acquisition import (
    KnowledgeGap,
    SourceCandidate,
    SourceSnapshot,
)
from continucare.knowledge.ops.manifests import KnowledgeOpsBundle
from continucare.knowledge.ops.models import (
    AuthorProvenance,
    ClinicalContextScope,
    GovernanceManifestEvidence,
    GovernanceGate,
    IntendedUse,
    KnowledgeOpsIntegrityError,
    KnowledgeOpsPolicyError,
    NonBlank,
    ReadinessBlock,
    SafeId,
    Sha256,
    StrictModel,
)
from continucare.knowledge.ops.promotion import GovernedSourceV2
from continucare.knowledge.ops.review import ReviewLedgerDecisionProvider
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


class KnowledgeReleaseBlocked(KnowledgeOpsPolicyError):
    def __init__(self, message: str, *, readiness_report_ref: LedgerRef) -> None:
        super().__init__(message)
        self.readiness_report_ref = readiness_report_ref


class ReleaseArtifactKind(StrEnum):
    SOURCE = "source"
    CLINICAL_CLAIM = "clinical_claim"
    BINDING = "binding"
    TERMINOLOGY_MAPPING = "terminology_mapping"
    TRANSLATION = "translation"
    PATIENT_CONTENT = "patient_content"


_ARTIFACT_COLLECTIONS = {
    ReleaseArtifactKind.SOURCE: LedgerCollection.SOURCE,
    ReleaseArtifactKind.CLINICAL_CLAIM: LedgerCollection.CLAIM,
    ReleaseArtifactKind.BINDING: LedgerCollection.BINDING,
    ReleaseArtifactKind.TERMINOLOGY_MAPPING: LedgerCollection.TERMINOLOGY_MAPPING,
    ReleaseArtifactKind.TRANSLATION: LedgerCollection.TRANSLATION,
    ReleaseArtifactKind.PATIENT_CONTENT: LedgerCollection.PATIENT_CONTENT,
}

_ARTIFACT_GATES = {
    ReleaseArtifactKind.SOURCE: GovernanceGate.SOURCE_PROMOTION,
    ReleaseArtifactKind.CLINICAL_CLAIM: GovernanceGate.CLINICAL_CLAIM_APPROVAL,
    ReleaseArtifactKind.BINDING: GovernanceGate.BINDING_APPROVAL,
    ReleaseArtifactKind.TERMINOLOGY_MAPPING: GovernanceGate.TERMINOLOGY_MAPPING_PROMOTION,
    ReleaseArtifactKind.TRANSLATION: GovernanceGate.TRANSLATION_PROMOTION,
    ReleaseArtifactKind.PATIENT_CONTENT: GovernanceGate.PATIENT_CONTENT_APPROVAL,
}


class ReleaseArtifact(StrictModel):
    artifact_kind: ReleaseArtifactKind
    object_ref: LedgerRef
    validation_profile_id: SafeId
    scope: ClinicalContextScope


class KnowledgeReleaseCandidate(StrictModel):
    release_candidate_id: SafeId
    target_jurisdiction: Literal["CN"] = "CN"
    target_language: Literal["zh-CN"] = "zh-CN"
    intended_uses: tuple[IntendedUse, ...] = Field(min_length=1)
    governance_bundle_id: SafeId
    governance_bundle_version: int = Field(ge=1)
    governance_index_sha256: Sha256
    governance_manifests: tuple[GovernanceManifestEvidence, ...] = Field(
        min_length=1
    )
    artifacts: tuple[ReleaseArtifact, ...] = ()
    blocking_gap_refs: tuple[LedgerRef, ...] = ()
    created_at: datetime
    created_by: SafeId
    author_provenance: AuthorProvenance
    synthetic: bool
    contains_patient_data: Literal[False] = False
    runtime_activation_requested: Literal[False] = False
    clinical_rule_refs: tuple[None, ...] = Field(default_factory=tuple, max_length=0)
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_candidate(self) -> "KnowledgeReleaseCandidate":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        if self.created_by != self.author_provenance.author_identity_id:
            raise ValueError("created_by must equal structured author identity")
        if self.created_at != self.author_provenance.authored_at:
            raise ValueError("created_at must equal structured authorship time")
        if self.synthetic != self.author_provenance.synthetic:
            raise ValueError(
                "release candidate and author provenance synthetic status differ"
            )
        if len(self.intended_uses) != len(set(self.intended_uses)):
            raise ValueError("release intended_uses must be unique")
        if any(
            item not in {
                IntendedUse.INTERNAL_KNOWLEDGE_OPERATIONS,
                IntendedUse.ACQUISITION_BASIS_EXPLANATION,
                IntendedUse.INFORMATIONAL_DISPLAY,
            }
            for item in self.intended_uses
        ):
            raise ValueError("release candidate contains an unsupported intended use")
        artifact_keys = [
            (item.artifact_kind, item.object_ref.collection, item.object_ref.record_id, item.object_ref.record_version)
            for item in self.artifacts
        ]
        if len(artifact_keys) != len(set(artifact_keys)):
            raise ValueError("release artifacts must be unique")
        if len(self.blocking_gap_refs) != len(set(_ref_key(item) for item in self.blocking_gap_refs)):
            raise ValueError("release gap refs must be unique")
        manifest_keys = [
            (item.file_id, item.file_version) for item in self.governance_manifests
        ]
        if len(manifest_keys) != len(set(manifest_keys)):
            raise ValueError("governance manifest evidence must be unique")
        return self


class ReadinessBlockerCode(StrEnum):
    GOVERNANCE_RELEASE_INTENT_BLOCKED = "governance_release_intent_blocked"
    GOVERNANCE_MANIFEST_MISMATCH = "governance_manifest_mismatch"
    GOVERNANCE_READINESS_GAP_OPEN = "governance_readiness_gap_open"
    EMPTY_RELEASE = "empty_release"
    STALE_RELEASE_CANDIDATE = "stale_release_candidate"
    UNKNOWN_OR_STALE_ARTIFACT = "unknown_or_stale_artifact"
    ARTIFACT_COLLECTION_MISMATCH = "artifact_collection_mismatch"
    SYNTHETIC_RELEASE_CANDIDATE = "synthetic_release_candidate"
    SYNTHETIC_ARTIFACT = "synthetic_artifact"
    NONPRODUCTION_SOURCE = "nonproduction_source"
    JURISDICTION_SCOPE_MISMATCH = "jurisdiction_scope_mismatch"
    LANGUAGE_SCOPE_MISMATCH = "language_scope_mismatch"
    ARTIFACT_PROFILE_SCOPE_MISMATCH = "artifact_profile_scope_mismatch"
    OPEN_BLOCKING_GAP = "open_blocking_gap"
    INVALID_GAP_REFERENCE = "invalid_gap_reference"
    PATIENT_DATA_RISK = "patient_data_risk"
    ARTIFACT_REVIEW_MISSING = "artifact_review_missing"
    ARTIFACT_REVIEW_NONPRODUCTION = "artifact_review_nonproduction"
    RELEASE_REVIEW_MISSING = "release_review_missing"
    RELEASE_REVIEW_NONPRODUCTION = "release_review_nonproduction"
    RELEASE_REQUIRED_ROLE_MISSING = "release_required_role_missing"


class ReadinessBlocker(StrictModel):
    code: ReadinessBlockerCode
    message: NonBlank
    subject_ref: LedgerRef | None = None
    readiness_gap_id: SafeId | None = None


class ReleaseReadinessReport(StrictModel):
    report_id: SafeId
    release_candidate_ref: LedgerRef
    ready: bool
    blockers: tuple[ReadinessBlocker, ...]
    assessed_at: datetime
    assessed_by: NonBlank
    inspected_artifact_count: int = Field(ge=0)
    production_review_count: int = Field(ge=0)
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_report(self) -> "ReleaseReadinessReport":
        if self.assessed_at.tzinfo is None:
            raise ValueError("assessed_at must include a timezone")
        if self.ready != (not self.blockers):
            raise ValueError("readiness is true exactly when blockers are empty")
        return self


class KnowledgeRelease(StrictModel):
    release_id: SafeId
    release_candidate_ref: LedgerRef
    readiness_report_ref: LedgerRef
    target_jurisdiction: Literal["CN"] = "CN"
    target_language: Literal["zh-CN"] = "zh-CN"
    intended_uses: tuple[IntendedUse, ...]
    governance_bundle_id: SafeId
    governance_bundle_version: int = Field(ge=1)
    governance_index_sha256: Sha256
    governance_manifests: tuple[GovernanceManifestEvidence, ...] = Field(min_length=1)
    artifacts: tuple[ReleaseArtifact, ...] = Field(min_length=1)
    candidate_author_provenance: AuthorProvenance
    finalized_at: datetime
    finalized_by: NonBlank
    release_status: Literal["released_informational"] = "released_informational"
    synthetic: Literal[False] = False
    contains_patient_data: Literal[False] = False
    clinical_rule_refs: tuple[None, ...] = Field(default_factory=tuple, max_length=0)
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @field_validator("finalized_at")
    @classmethod
    def finalized_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("finalized_at must include a timezone")
        return value


class ReleaseReadinessService:
    def __init__(
        self,
        *,
        bundle: KnowledgeOpsBundle,
        ledger: AppendOnlyLedger,
        decisions: ReviewLedgerDecisionProvider,
    ) -> None:
        self._bundle = bundle
        self._ledger = ledger
        self._decisions = decisions

    def stage_candidate(
        self, candidate: KnowledgeReleaseCandidate
    ) -> LedgerRef:
        _assert_release_candidate_open_material_no_sensitive_data(candidate)
        expected_manifests = self._bundle.manifest_evidence()
        if (
            candidate.governance_bundle_id != self._bundle.index.bundle_id
            or candidate.governance_bundle_version != self._bundle.index.bundle_version
            or candidate.governance_index_sha256 != self._bundle.index_sha256()
            or candidate.governance_manifests != expected_manifests
        ):
            raise KnowledgeOpsPolicyError(
                "release candidate governance manifest evidence does not match loaded bundle"
            )
        for artifact in candidate.artifacts:
            entry = self._ledger.get(artifact.object_ref)
            expected_collection = _ARTIFACT_COLLECTIONS[artifact.artifact_kind]
            if entry.collection != expected_collection.value:
                raise KnowledgeOpsPolicyError(
                    "release artifact kind does not match its ledger collection"
                )
            _assert_release_artifact_no_sensitive_data(self._ledger, artifact, entry)
        for gap_ref in candidate.blocking_gap_refs:
            gap_entry = self._ledger.get(gap_ref)
            if (
                gap_entry.collection != LedgerCollection.GAP.value
                or gap_entry.payload_type != "knowledge_gap"
            ):
                raise KnowledgeOpsPolicyError(
                    "release blocking gap must reference an exact KnowledgeGap"
                )
            gap = KnowledgeGap.model_validate(gap_entry.payload)
            _verify_gap_digest_context(self._ledger, gap)
            assert_no_sensitive_data(
                gap.model_dump(mode="json"),
                digest_trust_profile=DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP,
            )
        assert_no_sensitive_data(
            candidate.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.KNOWLEDGE_RELEASE_CANDIDATE,
        )
        return self._ledger.append(
            LedgerCollection.RELEASE_CANDIDATE,
            candidate.release_candidate_id,
            payload_type="knowledge_release_candidate",
            payload=candidate,
            recorded_by=candidate.created_by,
            recorded_at=candidate.created_at,
            synthetic=candidate.synthetic,
        ).ref

    def assess(
        self,
        release_candidate_ref: LedgerRef,
        *,
        assessed_by: str = "system:knowledge-release-readiness",
        assessed_at: datetime | None = None,
    ) -> LedgerRef:
        candidate_entry = self._ledger.get(release_candidate_ref)
        if (
            candidate_entry.collection != LedgerCollection.RELEASE_CANDIDATE.value
            or candidate_entry.payload_type != "knowledge_release_candidate"
        ):
            raise KnowledgeOpsPolicyError("readiness subject must be a release candidate")
        candidate = KnowledgeReleaseCandidate.model_validate(candidate_entry.payload)
        blockers: list[ReadinessBlocker] = []
        production_reviews = 0

        try:
            _assert_release_candidate_open_material_no_sensitive_data(candidate)
        except KnowledgeOpsPolicyError:
            blockers.append(
                _blocker(
                    ReadinessBlockerCode.PATIENT_DATA_RISK,
                    "Release candidate failed the no-patient-data guard.",
                    release_candidate_ref,
                )
            )

        if (
            candidate.governance_bundle_id != self._bundle.index.bundle_id
            or candidate.governance_bundle_version != self._bundle.index.bundle_version
            or candidate.governance_index_sha256 != self._bundle.index_sha256()
            or candidate.governance_manifests != self._bundle.manifest_evidence()
        ):
            blockers.append(
                _blocker(
                    ReadinessBlockerCode.GOVERNANCE_MANIFEST_MISMATCH,
                    "Release candidate does not pin the currently loaded governance bundle.",
                    release_candidate_ref,
                )
            )
        if not self._bundle.release_intent.release_ready:
            blockers.append(
                _blocker(
                    ReadinessBlockerCode.GOVERNANCE_RELEASE_INTENT_BLOCKED,
                    "The pinned governance bundle explicitly declares release readiness blocked.",
                    release_candidate_ref,
                )
            )
        for readiness_gap in self._bundle.readiness_gaps:
            if ReadinessBlock.KNOWLEDGE_RELEASE.value in readiness_gap.blocks:
                blockers.append(
                    ReadinessBlocker(
                        code=ReadinessBlockerCode.GOVERNANCE_READINESS_GAP_OPEN,
                        message=(
                            "Persistent governance readiness Gap remains open: "
                            f"{readiness_gap.gap_id}."
                        ),
                        readiness_gap_id=readiness_gap.gap_id,
                    )
                )
        head = self._ledger.head(
            release_candidate_ref.collection, release_candidate_ref.record_id
        )
        if head is None or head.ref != release_candidate_ref:
            blockers.append(
                _blocker(
                    ReadinessBlockerCode.STALE_RELEASE_CANDIDATE,
                    "Release candidate is not the current append-only head.",
                    release_candidate_ref,
                )
            )
        if not candidate.artifacts:
            blockers.append(
                _blocker(
                    ReadinessBlockerCode.EMPTY_RELEASE,
                    "Release candidate contains no governed knowledge artifacts.",
                    release_candidate_ref,
                )
            )
        if candidate.synthetic:
            blockers.append(
                _blocker(
                    ReadinessBlockerCode.SYNTHETIC_RELEASE_CANDIDATE,
                    "Synthetic release candidate cannot become a production KnowledgeRelease.",
                    release_candidate_ref,
                )
            )

        required_release_roles: set[str] = {
            str(item)
            for item in self._bundle.review_gate(
                GovernanceGate.KNOWLEDGE_RELEASE
            ).required_roles
        }
        effective_gap_refs = list(candidate.blocking_gap_refs)
        related_gap_subject_keys: set[tuple[str, str, int, str]] = {
            _ref_key(release_candidate_ref)
        }
        for artifact in candidate.artifacts:
            related_gap_subject_keys.add(_ref_key(artifact.object_ref))
            profile = next(
                (
                    item
                    for item in self._bundle.coverage_profiles
                    if item.profile_id == artifact.validation_profile_id
                ),
                None,
            )
            if profile is None:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.UNKNOWN_OR_STALE_ARTIFACT,
                        "Artifact references an unknown validation profile.",
                        artifact.object_ref,
                    )
                )
                continue
            required_release_roles.update(str(item) for item in profile.required_roles)
            if artifact.scope != profile.scope:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.ARTIFACT_PROFILE_SCOPE_MISMATCH,
                        "Artifact scope differs from its exact validation profile.",
                        artifact.object_ref,
                    )
                )
            jurisdiction_codes = {
                item.code
                for item in artifact.scope.jurisdictions
                if item.system == "iso3166_1"
            }
            if candidate.target_jurisdiction not in jurisdiction_codes:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.JURISDICTION_SCOPE_MISMATCH,
                        "Artifact scope does not explicitly include launch jurisdiction CN.",
                        artifact.object_ref,
                    )
                )
            if candidate.target_language not in artifact.scope.languages:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.LANGUAGE_SCOPE_MISMATCH,
                        "Artifact scope does not explicitly include zh-CN.",
                        artifact.object_ref,
                    )
                )
            try:
                entry = self._ledger.get(artifact.object_ref)
            except Exception:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.UNKNOWN_OR_STALE_ARTIFACT,
                        "Artifact reference cannot be resolved with exact digest.",
                        artifact.object_ref,
                    )
                )
                continue
            expected_collection = _ARTIFACT_COLLECTIONS[artifact.artifact_kind]
            if entry.collection != expected_collection.value:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.ARTIFACT_COLLECTION_MISMATCH,
                        "Artifact kind does not match its ledger collection.",
                        artifact.object_ref,
                    )
                )
                continue
            artifact_head = self._ledger.head(
                artifact.object_ref.collection, artifact.object_ref.record_id
            )
            if artifact_head is None or artifact_head.ref != artifact.object_ref:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.UNKNOWN_OR_STALE_ARTIFACT,
                        "Artifact is not the current append-only head.",
                        artifact.object_ref,
                    )
                )
            try:
                _assert_release_artifact_no_sensitive_data(
                    self._ledger, artifact, entry
                )
            except KnowledgeOpsPolicyError:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.PATIENT_DATA_RISK,
                        "Artifact payload failed the no-patient-data guard.",
                        artifact.object_ref,
                    )
                )
            except (KnowledgeOpsIntegrityError, ValidationError, ValueError):
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.UNKNOWN_OR_STALE_ARTIFACT,
                        "Artifact payload does not satisfy its strict v2 contract.",
                        artifact.object_ref,
                    )
                )
                continue
            if entry.synthetic:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.SYNTHETIC_ARTIFACT,
                        "Synthetic artifact cannot enter a production KnowledgeRelease.",
                        artifact.object_ref,
                    )
                )

            review_subject = artifact.object_ref
            if artifact.artifact_kind == ReleaseArtifactKind.SOURCE:
                try:
                    source = GovernedSourceV2.model_validate(entry.payload)
                except ValidationError:
                    blockers.append(
                        _blocker(
                            ReadinessBlockerCode.UNKNOWN_OR_STALE_ARTIFACT,
                            "Source artifact does not satisfy the governed v2 Source contract.",
                            artifact.object_ref,
                        )
                    )
                    continue
                review_subject = source.candidate_ref
                related_gap_subject_keys.add(_ref_key(source.candidate_ref))
                try:
                    source_candidate_entry = self._ledger.get(source.candidate_ref)
                    if (
                        source_candidate_entry.collection
                        != LedgerCollection.CANDIDATE.value
                        or source_candidate_entry.payload_type != "source_candidate"
                    ):
                        raise ValueError("wrong SourceCandidate collection")
                    source_candidate = SourceCandidate.model_validate(
                        source_candidate_entry.payload
                    )
                    assert_no_sensitive_data(
                        source_candidate.model_dump(mode="json")
                    )
                except KnowledgeOpsPolicyError:
                    blockers.append(
                        _blocker(
                            ReadinessBlockerCode.PATIENT_DATA_RISK,
                            "SourceCandidate failed the no-patient-data guard.",
                            artifact.object_ref,
                        )
                    )
                    continue
                except (KnowledgeOpsIntegrityError, ValidationError, ValueError):
                    blockers.append(
                        _blocker(
                            ReadinessBlockerCode.UNKNOWN_OR_STALE_ARTIFACT,
                            "Source does not resolve to its exact SourceCandidate.",
                            artifact.object_ref,
                        )
                    )
                    continue
                if source_candidate.validation_profile_id != artifact.validation_profile_id:
                    blockers.append(
                        _blocker(
                            ReadinessBlockerCode.ARTIFACT_PROFILE_SCOPE_MISMATCH,
                            "Source artifact validation profile differs from its Candidate.",
                            artifact.object_ref,
                        )
                    )
                if not source.production_eligible or source.synthetic:
                    blockers.append(
                        _blocker(
                            ReadinessBlockerCode.NONPRODUCTION_SOURCE,
                            "Source lacks production-eligible curator/rights promotion.",
                            artifact.object_ref,
                        )
                    )
                effective_gap_refs.extend(source.unresolved_gap_refs)
            decision = self._decisions.resolve_gate(
                review_subject, _ARTIFACT_GATES[artifact.artifact_kind]
            )
            if decision is None:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.ARTIFACT_REVIEW_MISSING,
                        "Artifact lacks the complete latest role approvals for its gate.",
                        artifact.object_ref,
                    )
                )
            elif decision.scope != artifact.scope:
                effective_gap_refs.extend(decision.blocking_gap_refs)
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.ARTIFACT_PROFILE_SCOPE_MISMATCH,
                        "Artifact release scope differs from its exact review packet.",
                        artifact.object_ref,
                    )
                )
            elif not decision.production_eligible:
                effective_gap_refs.extend(decision.blocking_gap_refs)
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.ARTIFACT_REVIEW_NONPRODUCTION,
                        "Artifact approvals are synthetic or not formally verified.",
                        artifact.object_ref,
                    )
                )
            else:
                effective_gap_refs.extend(decision.blocking_gap_refs)
                production_reviews += len(decision.evidence_refs)

        release_decision = self._decisions.resolve_gate(
            release_candidate_ref, GovernanceGate.KNOWLEDGE_RELEASE
        )
        if release_decision is None:
            blockers.append(
                _blocker(
                    ReadinessBlockerCode.RELEASE_REVIEW_MISSING,
                    "Release candidate lacks a complete release-level approval packet.",
                    release_candidate_ref,
                )
            )
        else:
            effective_gap_refs.extend(release_decision.blocking_gap_refs)
            missing_roles = required_release_roles - set(release_decision.approved_roles)
            if missing_roles:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.RELEASE_REQUIRED_ROLE_MISSING,
                        f"Release-level approval is missing roles: {sorted(missing_roles)}.",
                        release_candidate_ref,
                    )
                )
            if not release_decision.production_eligible:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.RELEASE_REVIEW_NONPRODUCTION,
                        "Release-level approvals are synthetic or not formally verified.",
                        release_candidate_ref,
                    )
                )
            else:
                production_reviews += len(release_decision.evidence_refs)

        for gap_entry in self._ledger.list_heads(LedgerCollection.GAP):
            try:
                if gap_entry.payload_type != "knowledge_gap":
                    raise ValueError("wrong gap payload type")
                gap = KnowledgeGap.model_validate(gap_entry.payload)
                _verify_gap_digest_context(self._ledger, gap)
                assert_no_sensitive_data(
                    gap.model_dump(mode="json"),
                    digest_trust_profile=(
                        DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP
                    ),
                )
            except KnowledgeOpsPolicyError:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.PATIENT_DATA_RISK,
                        "Gap ledger head failed the no-patient-data guard.",
                        gap_entry.ref,
                    )
                )
                continue
            except (KnowledgeOpsIntegrityError, ValidationError, ValueError):
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.INVALID_GAP_REFERENCE,
                        "Gap ledger head does not satisfy the KnowledgeGap contract.",
                        gap_entry.ref,
                    )
                )
                continue
            if (
                gap.lifecycle == "open"
                and gap.subject_ref is not None
                and _ref_key(gap.subject_ref) in related_gap_subject_keys
            ):
                effective_gap_refs.append(gap_entry.ref)

        unique_gap_refs = _deduplicate_refs(tuple(effective_gap_refs))
        for gap_ref in unique_gap_refs:
            try:
                gap_entry = self._ledger.get(gap_ref)
                if (
                    gap_entry.collection != LedgerCollection.GAP.value
                    or gap_entry.payload_type != "knowledge_gap"
                ):
                    raise ValueError("wrong collection")
                gap = KnowledgeGap.model_validate(gap_entry.payload)
                _verify_gap_digest_context(self._ledger, gap)
                assert_no_sensitive_data(
                    gap.model_dump(mode="json"),
                    digest_trust_profile=(
                        DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP
                    ),
                )
                gap_head = self._ledger.head(
                    LedgerCollection.GAP, gap_ref.record_id
                )
                if gap_head is None:
                    raise ValueError("missing gap head")
                if gap_head.ref != gap_ref:
                    blockers.append(
                        _blocker(
                            ReadinessBlockerCode.INVALID_GAP_REFERENCE,
                            "Release candidate contains a stale Gap reference.",
                            gap_ref,
                        )
                    )
                    gap_entry = gap_head
                    if gap_entry.payload_type != "knowledge_gap":
                        raise ValueError("wrong gap payload type")
                    gap = KnowledgeGap.model_validate(gap_entry.payload)
                    _verify_gap_digest_context(self._ledger, gap)
                    assert_no_sensitive_data(
                        gap.model_dump(mode="json"),
                        digest_trust_profile=(
                            DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP
                        ),
                    )
            except KnowledgeOpsPolicyError:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.PATIENT_DATA_RISK,
                        "Gap reference failed the no-patient-data guard.",
                        gap_ref,
                    )
                )
                continue
            except (KnowledgeOpsIntegrityError, ValidationError, ValueError):
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.INVALID_GAP_REFERENCE,
                        "Release candidate contains an invalid Gap reference.",
                        gap_ref,
                    )
                )
                continue
            if gap.lifecycle == "open":
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.OPEN_BLOCKING_GAP,
                        "Open CoverageGap blocks KnowledgeRelease readiness.",
                        gap_entry.ref,
                    )
                )

        if _release_candidate_digest_context_verified(
            bundle=self._bundle,
            ledger=self._ledger,
            candidate=candidate,
        ):
            try:
                assert_no_sensitive_data(
                    candidate.model_dump(mode="json"),
                    digest_trust_profile=(
                        DigestTrustProfile.KNOWLEDGE_RELEASE_CANDIDATE
                    ),
                )
            except KnowledgeOpsPolicyError:
                blockers.append(
                    _blocker(
                        ReadinessBlockerCode.PATIENT_DATA_RISK,
                        "Release candidate failed the no-patient-data guard.",
                        release_candidate_ref,
                    )
                )

        timestamp = assessed_at or datetime.now(timezone.utc)
        blockers = _unique_blockers(blockers)
        report_id = _derived_id("readiness", release_candidate_ref)
        report = ReleaseReadinessReport(
            report_id=report_id,
            release_candidate_ref=release_candidate_ref,
            ready=not blockers,
            blockers=tuple(blockers),
            assessed_at=timestamp,
            assessed_by=assessed_by,
            inspected_artifact_count=len(candidate.artifacts),
            production_review_count=production_reviews,
        )
        assert_no_sensitive_data(
            report.model_dump(mode="json"),
            digest_trust_profile=_readiness_report_digest_profile(
                self._ledger, report
            ),
        )
        return self._ledger.append(
            LedgerCollection.READINESS_REPORT,
            report_id,
            payload_type="knowledge_release_readiness",
            payload=report,
            recorded_by=assessed_by,
            recorded_at=timestamp,
            synthetic=candidate.synthetic,
        ).ref

    def finalize(
        self,
        release_candidate_ref: LedgerRef,
        *,
        finalized_by: str,
        finalized_at: datetime | None = None,
    ) -> LedgerRef:
        timestamp = finalized_at or datetime.now(timezone.utc)
        report_ref = self.assess(
            release_candidate_ref,
            assessed_by=finalized_by,
            assessed_at=timestamp,
        )
        report_entry = self._ledger.get(report_ref)
        if (
            report_entry.collection != LedgerCollection.READINESS_REPORT.value
            or report_entry.payload_type != "knowledge_release_readiness"
        ):
            raise KnowledgeOpsPolicyError(
                "KnowledgeRelease requires an exact readiness report"
            )
        report = ReleaseReadinessReport.model_validate(report_entry.payload)
        assert_no_sensitive_data(
            report.model_dump(mode="json"),
            digest_trust_profile=_readiness_report_digest_profile(
                self._ledger, report
            ),
        )
        if not report.ready:
            raise KnowledgeReleaseBlocked(
                "KnowledgeRelease candidate is not ready",
                readiness_report_ref=report_ref,
            )
        candidate_entry = self._ledger.get(release_candidate_ref)
        if (
            candidate_entry.collection != LedgerCollection.RELEASE_CANDIDATE.value
            or candidate_entry.payload_type != "knowledge_release_candidate"
        ):
            raise KnowledgeOpsPolicyError(
                "KnowledgeRelease requires an exact release candidate"
            )
        candidate = KnowledgeReleaseCandidate.model_validate(candidate_entry.payload)
        if not _release_candidate_digest_context_verified(
            bundle=self._bundle,
            ledger=self._ledger,
            candidate=candidate,
        ):
            raise KnowledgeOpsPolicyError(
                "KnowledgeRelease candidate digest context is not verified"
            )
        assert_no_sensitive_data(
            candidate.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.KNOWLEDGE_RELEASE_CANDIDATE,
        )
        release = KnowledgeRelease(
            release_id=candidate.release_candidate_id,
            release_candidate_ref=release_candidate_ref,
            readiness_report_ref=report_ref,
            intended_uses=candidate.intended_uses,
            governance_bundle_id=candidate.governance_bundle_id,
            governance_bundle_version=candidate.governance_bundle_version,
            governance_index_sha256=candidate.governance_index_sha256,
            governance_manifests=candidate.governance_manifests,
            artifacts=candidate.artifacts,
            candidate_author_provenance=candidate.author_provenance,
            finalized_at=timestamp,
            finalized_by=finalized_by,
        )
        assert_no_sensitive_data(
            release.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.KNOWLEDGE_RELEASE,
        )
        return self._ledger.append(
            LedgerCollection.RELEASE,
            release.release_id,
            payload_type="knowledge_release",
            payload=release,
            recorded_by=finalized_by,
            recorded_at=timestamp,
            synthetic=False,
        ).ref


def _assert_release_candidate_open_material_no_sensitive_data(
    candidate: KnowledgeReleaseCandidate,
) -> None:
    """Scan every untrusted field before verified release digests receive trust."""

    payload = candidate.model_dump(mode="json")
    payload.pop("governance_index_sha256")
    for manifest in payload["governance_manifests"]:
        manifest.pop("manifest_sha256")
    for artifact in payload["artifacts"]:
        artifact["object_ref"].pop("entry_sha256")
    for gap_ref in payload["blocking_gap_refs"]:
        gap_ref.pop("entry_sha256")
    assert_no_sensitive_data(payload)


def _assert_release_artifact_no_sensitive_data(
    ledger: AppendOnlyLedger,
    artifact: ReleaseArtifact,
    entry: LedgerEntry,
) -> None:
    """Select artifact digest trust only for an exact strict v2 payload."""

    if artifact.artifact_kind == ReleaseArtifactKind.SOURCE:
        if entry.payload_type != "governed_source_v2":
            raise ValueError("Source artifact has an unexpected payload type")
        source = GovernedSourceV2.model_validate(entry.payload)
        if source.source_id != entry.record_id:
            raise ValueError("Source artifact ledger identity differs")
        _verify_governed_source_digest_context(ledger, source)
        assert_no_sensitive_data(
            source.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.GOVERNED_SOURCE,
        )
        return
    if (
        artifact.artifact_kind == ReleaseArtifactKind.CLINICAL_CLAIM
        and entry.payload_type == "machine_draft_claim_v2"
    ):
        from continucare.knowledge.ops.evidence import MachineDraftClaim

        claim = MachineDraftClaim.model_validate(entry.payload)
        if claim.claim_id != entry.record_id:
            raise ValueError("Claim artifact ledger identity differs")
        _verify_machine_draft_digest_context(ledger, claim)
        assert_no_sensitive_data(
            claim.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.MACHINE_DRAFT_CLAIM,
        )
        return
    # Other artifact kinds have no strict digest-bearing v2 model in this slice.
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
        raise ValueError(f"expected {collection.value}/{payload_type}")
    return entry


def _verify_gap_digest_context(
    ledger: AppendOnlyLedger,
    gap: KnowledgeGap,
) -> None:
    if gap.subject_ref is not None:
        ledger.get(gap.subject_ref)


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
        raise ValueError("governed Source digest lineage differs")
    assert_no_sensitive_data(candidate.model_dump(mode="json"))
    assert_no_sensitive_data(
        snapshot.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
    )
    for reference in source.promotion_evidence_refs:
        evidence_entry = ledger.get(reference)
        if evidence_entry.collection != LedgerCollection.REVIEW_EVENT.value:
            raise ValueError("Source promotion evidence is not a ReviewEvent")
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
        raise ValueError("machine draft Claim digest lineage differs")
    assert_no_sensitive_data(source.model_dump(mode="json"))
    assert_no_sensitive_data(
        snapshot.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
    )
    assert_no_sensitive_data(
        evidence.model_dump(mode="json"),
        digest_trust_profile=DigestTrustProfile.EVIDENCE_CANDIDATE,
    )


def _readiness_report_digest_profile(
    ledger: AppendOnlyLedger,
    report: ReleaseReadinessReport,
) -> DigestTrustProfile:
    if all(
        blocker.subject_ref is None
        or _ledger_ref_resolves(ledger, blocker.subject_ref)
        for blocker in report.blockers
    ):
        return DigestTrustProfile.RELEASE_READINESS_REPORT
    return DigestTrustProfile.RELEASE_READINESS_REPORT_BASE


def _release_candidate_digest_context_verified(
    *,
    bundle: KnowledgeOpsBundle,
    ledger: AppendOnlyLedger,
    candidate: KnowledgeReleaseCandidate,
) -> bool:
    if (
        candidate.governance_bundle_id != bundle.index.bundle_id
        or candidate.governance_bundle_version != bundle.index.bundle_version
        or candidate.governance_index_sha256 != bundle.index_sha256()
        or candidate.governance_manifests != bundle.manifest_evidence()
    ):
        return False
    try:
        for artifact in candidate.artifacts:
            entry = ledger.get(artifact.object_ref)
            if entry.collection != _ARTIFACT_COLLECTIONS[artifact.artifact_kind].value:
                return False
            if artifact.artifact_kind == ReleaseArtifactKind.SOURCE:
                if entry.payload_type != "governed_source_v2":
                    return False
                GovernedSourceV2.model_validate(entry.payload)
            elif (
                artifact.artifact_kind == ReleaseArtifactKind.CLINICAL_CLAIM
                and entry.payload_type == "machine_draft_claim_v2"
            ):
                from continucare.knowledge.ops.evidence import MachineDraftClaim

                MachineDraftClaim.model_validate(entry.payload)
        for gap_ref in candidate.blocking_gap_refs:
            gap_entry = ledger.get(gap_ref)
            if (
                gap_entry.collection != LedgerCollection.GAP.value
                or gap_entry.payload_type != "knowledge_gap"
            ):
                return False
            KnowledgeGap.model_validate(gap_entry.payload)
    except (KeyError, KnowledgeOpsIntegrityError, ValidationError, ValueError):
        return False
    return True


def _ledger_ref_resolves(ledger: AppendOnlyLedger, reference: LedgerRef) -> bool:
    try:
        ledger.get(reference)
    except (KeyError, KnowledgeOpsIntegrityError, ValidationError, ValueError):
        return False
    return True


def _blocker(
    code: ReadinessBlockerCode,
    message: str,
    subject_ref: LedgerRef | None,
) -> ReadinessBlocker:
    return ReadinessBlocker(code=code, message=message, subject_ref=subject_ref)


def _unique_blockers(blockers: list[ReadinessBlocker]) -> list[ReadinessBlocker]:
    unique: dict[tuple[str, tuple | None, str | None], ReadinessBlocker] = {}
    for blocker in blockers:
        key = (
            str(blocker.code),
            None if blocker.subject_ref is None else _ref_key(blocker.subject_ref),
            blocker.readiness_gap_id,
        )
        unique.setdefault(key, blocker)
    return list(unique.values())


def _ref_key(reference: LedgerRef) -> tuple[str, str, int, str]:
    return (
        str(reference.collection),
        reference.record_id,
        reference.record_version,
        reference.entry_sha256,
    )


def _deduplicate_refs(references: tuple[LedgerRef, ...]) -> tuple[LedgerRef, ...]:
    unique: dict[tuple[str, str, int, str], LedgerRef] = {}
    for reference in references:
        unique.setdefault(_ref_key(reference), reference)
    return tuple(unique.values())


def _derived_id(prefix: str, reference: LedgerRef) -> str:
    raw = (
        f"{prefix}-{reference.record_id}-{reference.record_version}-"
        f"{reference.entry_sha256[:16]}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest_derived_internal_id(prefix, digest, digest_characters=32)

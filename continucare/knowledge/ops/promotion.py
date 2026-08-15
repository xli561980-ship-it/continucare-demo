"""Fail-closed SourceCandidate to governed v2 Source promotion."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from continucare.knowledge.ops.acquisition import (
    AcquisitionEnvironment,
    KnowledgeGap,
    SourceCandidate,
    SourcePolicyRef,
    SourceSnapshot,
)
from continucare.knowledge.ops.manifests import KnowledgeOpsBundle
from continucare.knowledge.ops.models import (
    GovernanceGate,
    Jurisdiction,
    KnowledgeOpsPolicyError,
    LanguageCode,
    NonBlank,
    ReadinessBlock,
    ReviewerRole,
    SafeId,
    Sha256,
    SourceOperation,
    StrictModel,
)
from continucare.knowledge.ops.store import (
    AppendOnlyLedger,
    LedgerCollection,
    LedgerRef,
)
from continucare.knowledge.ops.security import (
    DigestTrustProfile,
    assert_no_sensitive_data,
    validate_url_against_policy,
)


class PromotionDecision(StrictModel):
    subject_ref: LedgerRef
    gate: Literal["source_promotion"] = "source_promotion"
    approved_roles: tuple[ReviewerRole, ...] = Field(min_length=1)
    evidence_refs: tuple[LedgerRef, ...] = Field(min_length=1)
    blocking_gap_refs: tuple[LedgerRef, ...] = ()
    synthetic: bool
    production_eligible: bool

    @model_validator(mode="after")
    def validate_decision(self) -> "PromotionDecision":
        if len(self.approved_roles) != len(set(self.approved_roles)):
            raise ValueError("promotion approval roles must be unique")
        if len(self.evidence_refs) != len(
            {_ref_key(item) for item in self.evidence_refs}
        ):
            raise ValueError("promotion evidence refs must be unique")
        if len(self.blocking_gap_refs) != len(
            {_ref_key(item) for item in self.blocking_gap_refs}
        ):
            raise ValueError("promotion blocking gap refs must be unique")
        if self.synthetic and self.production_eligible:
            raise ValueError("synthetic decision cannot be production eligible")
        return self


class PromotionDecisionProvider(Protocol):
    def decision_for(
        self, subject_ref: LedgerRef, gate: GovernanceGate
    ) -> PromotionDecision | None: ...


class GovernedSourceV2(StrictModel):
    source_id: SafeId
    candidate_ref: LedgerRef
    snapshot_ref: LedgerRef
    policy: SourcePolicyRef
    canonical_url: AnyHttpUrl
    title: NonBlank
    issuing_authority: NonBlank
    source_type: NonBlank
    jurisdictions: tuple[Jurisdiction, ...] = Field(min_length=1)
    languages: tuple[LanguageCode, ...] = Field(min_length=1)
    document_version: NonBlank
    content_sha256: Sha256
    access_mode: Literal["link_only", "quarantined_synthetic_fixture"]
    promotion_evidence_refs: tuple[LedgerRef, ...] = Field(min_length=1)
    unresolved_gap_refs: tuple[LedgerRef, ...] = ()
    promoted_at: datetime
    promoted_by: NonBlank
    registry_status: Literal["synthetic_fixture", "registered"]
    synthetic: bool
    production_eligible: bool
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @field_validator("promoted_at")
    @classmethod
    def promoted_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("promoted_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_synthetic_boundary(self) -> "GovernedSourceV2":
        if len(self.promotion_evidence_refs) != len(
            {_ref_key(item) for item in self.promotion_evidence_refs}
        ):
            raise ValueError("Source promotion evidence refs must be unique")
        if len(self.unresolved_gap_refs) != len(
            {_ref_key(item) for item in self.unresolved_gap_refs}
        ):
            raise ValueError("Source unresolved gap refs must be unique")
        if self.production_eligible and self.unresolved_gap_refs:
            raise ValueError("production-eligible Source cannot carry unresolved gaps")
        if self.synthetic:
            if self.production_eligible:
                raise ValueError("synthetic Source cannot be production eligible")
            if self.access_mode != "quarantined_synthetic_fixture":
                raise ValueError("synthetic Source must remain in fixture quarantine")
            if self.registry_status != "synthetic_fixture":
                raise ValueError("synthetic Source cannot claim production registration")
        elif self.registry_status != "registered":
            raise ValueError("non-synthetic Source must use registered status")
        elif not self.production_eligible or self.access_mode != "link_only":
            raise ValueError(
                "registered non-synthetic Source must be production eligible and link-only"
            )
        return self


class SourcePromotionService:
    def __init__(
        self,
        *,
        bundle: KnowledgeOpsBundle,
        ledger: AppendOnlyLedger,
        decisions: PromotionDecisionProvider,
        environment: AcquisitionEnvironment = AcquisitionEnvironment.SYNTHETIC_TEST,
    ) -> None:
        self._bundle = bundle
        self._ledger = ledger
        self._decisions = decisions
        self._environment = AcquisitionEnvironment(environment)

    def promote(
        self,
        *,
        source_id: str,
        candidate_ref: LedgerRef,
        snapshot_ref: LedgerRef,
        promoted_by: str,
        promoted_at: datetime | None = None,
    ) -> LedgerRef:
        candidate_entry = self._ledger.get(candidate_ref)
        snapshot_entry = self._ledger.get(snapshot_ref)
        if candidate_entry.payload_type == "evidence_candidate_v2":
            raise KnowledgeOpsPolicyError(
                "EvidenceCandidate cannot be promoted as a SourceCandidate"
            )
        if (
            candidate_entry.collection != LedgerCollection.CANDIDATE.value
            or candidate_entry.payload_type != "source_candidate"
        ):
            raise KnowledgeOpsPolicyError("promotion subject must be a SourceCandidate")
        if (
            snapshot_entry.collection != LedgerCollection.SNAPSHOT.value
            or snapshot_entry.payload_type != "source_snapshot"
        ):
            raise KnowledgeOpsPolicyError("promotion requires a SourceSnapshot")
        if self._ledger.head(candidate_ref.collection, candidate_ref.record_id).ref != candidate_ref:
            raise KnowledgeOpsPolicyError("stale SourceCandidate cannot be promoted")
        if self._ledger.head(snapshot_ref.collection, snapshot_ref.record_id).ref != snapshot_ref:
            raise KnowledgeOpsPolicyError("stale SourceSnapshot cannot be promoted")
        candidate = SourceCandidate.model_validate(candidate_entry.payload)
        snapshot = SourceSnapshot.model_validate(snapshot_entry.payload)
        assert_no_sensitive_data(candidate.model_dump(mode="json"))
        if (
            candidate.candidate_id != candidate_ref.record_id
            or snapshot.snapshot_id != snapshot_ref.record_id
            or snapshot.candidate_ref != candidate_ref
        ):
            raise KnowledgeOpsPolicyError("SourceSnapshot does not belong to candidate")
        assert_no_sensitive_data(
            snapshot.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
        )
        policy = self._bundle.source_policy(
            candidate.policy.policy_id, candidate.policy.policy_version
        )
        if policy.status != "active":
            raise KnowledgeOpsPolicyError("retired SourcePolicy cannot promote a Source")
        canonical_url = validate_url_against_policy(str(candidate.canonical_url), policy)
        snapshot_url = validate_url_against_policy(str(snapshot.canonical_url), policy)
        if snapshot_url != canonical_url:
            raise KnowledgeOpsPolicyError(
                "SourceSnapshot canonical URL differs from SourceCandidate"
            )
        if snapshot.content_type not in policy.allowed_content_types:
            raise KnowledgeOpsPolicyError(
                "SourceSnapshot content type is outside SourcePolicy"
            )
        if snapshot.content_size > policy.maximum_response_bytes:
            raise KnowledgeOpsPolicyError("SourceSnapshot exceeds SourcePolicy byte limit")
        if candidate.source_type not in policy.source_types:
            raise KnowledgeOpsPolicyError("SourceCandidate type is outside SourcePolicy")
        policy_jurisdictions = {
            (item.system, item.code) for item in policy.source_jurisdictions
        }
        if not {
            (item.system, item.code) for item in candidate.jurisdictions
        }.issubset(policy_jurisdictions):
            raise KnowledgeOpsPolicyError(
                "SourceCandidate jurisdiction is outside SourcePolicy"
            )
        if not set(candidate.languages).issubset(set(policy.languages)):
            raise KnowledgeOpsPolicyError("SourceCandidate language is outside SourcePolicy")
        if policy.decision_for(SourceOperation.REGISTER_LINK_METADATA) != "allow":
            raise KnowledgeOpsPolicyError("SourcePolicy blocks Source registration")

        gate = self._bundle.review_gate(GovernanceGate.SOURCE_PROMOTION)
        decision = self._decisions.decision_for(
            candidate_ref, GovernanceGate.SOURCE_PROMOTION
        )
        if decision is None or decision.subject_ref != candidate_ref:
            raise KnowledgeOpsPolicyError("Source promotion lacks a review decision")
        required = set(gate.required_roles)
        approved = set(decision.approved_roles)
        if not required.issubset(approved):
            missing = sorted(required - approved)
            raise KnowledgeOpsPolicyError(
                f"Source promotion lacks required roles: {missing}"
            )
        for evidence_ref in decision.evidence_refs:
            evidence_entry = self._ledger.get(evidence_ref)
            if evidence_entry.collection != LedgerCollection.REVIEW_EVENT.value:
                raise KnowledgeOpsPolicyError(
                    "promotion decision evidence must reference review events"
                )
            if self._ledger.head(
                evidence_ref.collection, evidence_ref.record_id
            ).ref != evidence_ref:
                raise KnowledgeOpsPolicyError(
                    "promotion decision uses a stale review event"
                )
        for gap_ref in decision.blocking_gap_refs:
            gap_entry = self._ledger.get(gap_ref)
            if gap_entry.collection != LedgerCollection.GAP.value:
                raise KnowledgeOpsPolicyError(
                    "promotion blocking gaps must reference KnowledgeGap records"
                )
            gap = KnowledgeGap.model_validate(gap_entry.payload)
            if gap.lifecycle != "open":
                raise KnowledgeOpsPolicyError(
                    "promotion decision may only carry current open gaps"
                )
            if self._ledger.head(gap_ref.collection, gap_ref.record_id).ref != gap_ref:
                raise KnowledgeOpsPolicyError(
                    "promotion decision uses a stale KnowledgeGap"
                )
            if gap.subject_ref is not None:
                self._ledger.get(gap.subject_ref)
            assert_no_sensitive_data(
                gap.model_dump(mode="json"),
                digest_trust_profile=DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP,
            )
        assert_no_sensitive_data(
            decision.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.PROMOTION_DECISION,
        )

        if self._environment == AcquisitionEnvironment.SYNTHETIC_TEST:
            if not candidate.synthetic or not snapshot.synthetic or not decision.synthetic:
                raise KnowledgeOpsPolicyError(
                    "synthetic promotion requires synthetic candidate, snapshot, and decision"
                )
            if decision.production_eligible:
                raise KnowledgeOpsPolicyError(
                    "synthetic promotion decision cannot count toward production"
                )
            access_mode = "quarantined_synthetic_fixture"
            production_eligible = False
            registry_status = "synthetic_fixture"
        else:
            if candidate.synthetic or snapshot.synthetic or decision.synthetic:
                raise KnowledgeOpsPolicyError(
                    "production promotion rejects synthetic evidence"
                )
            if any(
                ReadinessBlock.PRODUCTION_ELIGIBILITY.value in gap.blocks
                for gap in self._bundle.readiness_gaps
            ):
                raise KnowledgeOpsPolicyError(
                    "production Source promotion is blocked by persistent readiness Gaps"
                )
            if not decision.production_eligible:
                raise KnowledgeOpsPolicyError(
                    "production promotion requires formally verified decision evidence"
                )
            if decision.blocking_gap_refs:
                raise KnowledgeOpsPolicyError(
                    "production Source promotion is blocked by unresolved gaps"
                )
            if (
                not self._bundle.release_intent.formal_reviewers_available
                or not self._bundle.release_intent.formal_license_decisions_available
            ):
                raise KnowledgeOpsPolicyError(
                    "governance bundle has no formal reviewer/license authority"
                )
            access_mode = "link_only"
            production_eligible = True
            registry_status = "registered"

        timestamp = promoted_at or datetime.now(timezone.utc)
        source = GovernedSourceV2(
            source_id=source_id,
            candidate_ref=candidate_ref,
            snapshot_ref=snapshot_ref,
            policy=candidate.policy,
            canonical_url=canonical_url,
            title=candidate.title,
            issuing_authority=candidate.issuing_authority,
            source_type=candidate.source_type,
            jurisdictions=candidate.jurisdictions,
            languages=candidate.languages,
            document_version=candidate.document_version,
            content_sha256=snapshot.content_sha256,
            access_mode=access_mode,
            promotion_evidence_refs=decision.evidence_refs,
            unresolved_gap_refs=decision.blocking_gap_refs,
            promoted_at=timestamp,
            promoted_by=promoted_by,
            registry_status=registry_status,
            synthetic=candidate.synthetic,
            production_eligible=production_eligible,
        )
        assert_no_sensitive_data(
            source.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.GOVERNED_SOURCE,
        )
        return self._ledger.append(
            LedgerCollection.SOURCE,
            source_id,
            payload_type="governed_source_v2",
            payload=source,
            recorded_by=promoted_by,
            recorded_at=timestamp,
            synthetic=source.synthetic,
        ).ref


def _ref_key(reference: LedgerRef) -> tuple[str, str, int, str]:
    return (
        str(reference.collection),
        reference.record_id,
        reference.record_version,
        reference.entry_sha256,
    )

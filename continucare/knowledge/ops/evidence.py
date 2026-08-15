"""Typed EvidenceCandidate staging and synthetic-only draft Claim promotion."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, Field, StringConstraints, field_validator, model_validator

from continucare.knowledge.ops.acquisition import SourceCandidate, SourceSnapshot
from continucare.knowledge.ops.manifests import KnowledgeOpsBundle
from continucare.knowledge.ops.models import (
    AuthorProvenance,
    ClinicalContextScope,
    KnowledgeLayer,
    KnowledgeOpsPolicyError,
    LicensePosture,
    NonBlank,
    PolicyDecision,
    SafeId,
    Sha256,
    SourceOperation,
    StrictModel,
)
from continucare.knowledge.ops.security import (
    DigestTrustProfile,
    assert_no_sensitive_data,
    validate_url_against_policy,
)
from continucare.knowledge.ops.store import (
    AppendOnlyLedger,
    LedgerCollection,
    LedgerEntry,
    LedgerRef,
)


EvidenceCandidateId = Annotated[
    str, StringConstraints(pattern=r"^evc-[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
]
MachineDraftClaimId = Annotated[
    str, StringConstraints(pattern=r"^dcl-[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
]
SemanticVersion = Annotated[
    str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")
]
BenchmarkSymptomRef = Annotated[
    str, StringConstraints(pattern=r"^core-symptom-[a-z][a-z0-9-]{0,63}$")
]


class EvidenceLimitationCode(StrEnum):
    SYNTHETIC_FIXTURE_ONLY = "synthetic-fixture-only"
    RIGHTS_UNRESOLVED = "rights-unresolved"
    METADATA_ONLY = "metadata-only"
    CLINICAL_INTERPRETATION_UNREVIEWED = "clinical-interpretation-unreviewed"
    TERMINOLOGY_UNREVIEWED = "terminology-unreviewed"


class EvidenceLocator(StrictModel):
    canonical_url: AnyHttpUrl
    stable_source_key: SafeId
    document_version: NonBlank
    locator_kind: Literal["metadata_record"] = "metadata_record"
    contains_source_text: Literal[False] = False


class EvidenceDerivationProvenance(StrictModel):
    derivation_method: Literal["synthetic_metadata_fixture"] = (
        "synthetic_metadata_fixture"
    )
    fixture_set_id: SafeId
    extraction_profile_id: SafeId
    selected_metadata_fields: tuple[SafeId, ...] = Field(min_length=1)
    fingerprint_policy: Literal["whole_record_digest_only"] = (
        "whole_record_digest_only"
    )
    source_text_materialized: Literal[False] = False
    source_substrings_stored: Literal[False] = False
    reconstructive_fingerprints_stored: Literal[False] = False

    @field_validator("selected_metadata_fields")
    @classmethod
    def selected_fields_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("selected metadata fields must be unique")
        return value


class EvidenceCandidate(StrictModel):
    record_type: Literal["evidence_candidate"] = "evidence_candidate"
    candidate_id: EvidenceCandidateId
    candidate_version: int = Field(ge=1)
    source_candidate_ref: LedgerRef
    source_snapshot_ref: LedgerRef
    connector_id: SafeId
    connector_version: SemanticVersion
    parser_id: SafeId
    parser_version: SemanticVersion
    whole_record_sha256: Sha256
    locator: EvidenceLocator
    benchmark_symptom_refs: tuple[BenchmarkSymptomRef, ...] = Field(min_length=1)
    proposed_knowledge_layer: KnowledgeLayer
    proposed_claim_type: SafeId
    proposed_scope: ClinicalContextScope
    derivation_provenance: EvidenceDerivationProvenance
    known_limitation_codes: tuple[EvidenceLimitationCode, ...] = Field(min_length=1)
    machine_generated: Literal[True] = True
    author_provenance: AuthorProvenance
    review_status: Literal["unreviewed", "review_requested"] = "unreviewed"
    synthetic: bool
    contains_patient_data: Literal[False] = False
    contains_source_text: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_candidate_boundary(self) -> "EvidenceCandidate":
        if self.source_candidate_ref.collection != LedgerCollection.CANDIDATE:
            raise ValueError("EvidenceCandidate requires an exact SourceCandidate ref")
        if self.source_snapshot_ref.collection != LedgerCollection.SNAPSHOT:
            raise ValueError("EvidenceCandidate requires an exact SourceSnapshot ref")
        if len(self.benchmark_symptom_refs) != len(set(self.benchmark_symptom_refs)):
            raise ValueError("benchmark symptom refs must be unique")
        if len(self.known_limitation_codes) != len(set(self.known_limitation_codes)):
            raise ValueError("known limitation codes must be unique")
        required = {
            EvidenceLimitationCode.SYNTHETIC_FIXTURE_ONLY.value,
            EvidenceLimitationCode.METADATA_ONLY.value,
            EvidenceLimitationCode.CLINICAL_INTERPRETATION_UNREVIEWED.value,
        }
        if self.synthetic and not required.issubset(set(self.known_limitation_codes)):
            raise ValueError("synthetic EvidenceCandidate omits mandatory limitations")
        if self.author_provenance.synthetic != self.synthetic:
            raise ValueError("candidate and author provenance synthetic status differ")
        return self


class MachineDraftClaim(StrictModel):
    record_type: Literal["machine_draft_claim"] = "machine_draft_claim"
    claim_id: MachineDraftClaimId
    claim_version: int = Field(ge=1)
    evidence_candidate_ref: LedgerRef
    source_candidate_ref: LedgerRef
    source_snapshot_ref: LedgerRef
    lifecycle: Literal["draft"] = "draft"
    machine_generated: Literal[True] = True
    draft_assertion_code: SafeId
    knowledge_layer: KnowledgeLayer
    scope: ClinicalContextScope
    benchmark_symptom_refs: tuple[BenchmarkSymptomRef, ...] = Field(min_length=1)
    limitation_codes: tuple[EvidenceLimitationCode, ...] = Field(min_length=1)
    author_provenance: AuthorProvenance
    created_at: datetime
    created_by: NonBlank
    review_status: Literal["unreviewed"] = "unreviewed"
    formal_citation_count: Literal[0] = 0
    binding_created: Literal[False] = False
    clinical_rule_created: Literal[False] = False
    patient_content_created: Literal[False] = False
    production_eligible: Literal[False] = False
    release_ready: Literal[False] = False
    synthetic: bool
    contains_patient_data: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_draft_boundary(self) -> "MachineDraftClaim":
        if self.evidence_candidate_ref.collection != LedgerCollection.EVIDENCE_CANDIDATE:
            raise ValueError("draft Claim requires an EvidenceCandidate ref")
        if self.source_candidate_ref.collection != LedgerCollection.CANDIDATE:
            raise ValueError("draft Claim requires a SourceCandidate ref")
        if self.source_snapshot_ref.collection != LedgerCollection.SNAPSHOT:
            raise ValueError("draft Claim requires a SourceSnapshot ref")
        if self.created_at.tzinfo is None:
            raise ValueError("draft Claim creation time must include a timezone")
        if self.author_provenance.authored_at > self.created_at:
            raise ValueError("draft Claim predates its author provenance")
        if self.author_provenance.synthetic != self.synthetic:
            raise ValueError("draft Claim and author provenance synthetic status differ")
        if len(self.benchmark_symptom_refs) != len(set(self.benchmark_symptom_refs)):
            raise ValueError("draft Claim symptom refs must be unique")
        return self


class EvidenceCandidateService:
    """Stages only synthetic, metadata-derived candidates in this P1 release."""

    def __init__(self, *, bundle: KnowledgeOpsBundle, ledger: AppendOnlyLedger) -> None:
        self._bundle = bundle
        self._ledger = ledger

    def stage(
        self,
        *,
        candidate_id: str,
        source_candidate_ref: LedgerRef,
        source_snapshot_ref: LedgerRef,
        connector_version: str,
        parser_id: str,
        parser_version: str,
        benchmark_symptom_refs: tuple[str, ...],
        proposed_knowledge_layer: KnowledgeLayer,
        proposed_claim_type: str,
        proposed_scope: ClinicalContextScope,
        derivation_provenance: EvidenceDerivationProvenance,
        known_limitation_codes: tuple[EvidenceLimitationCode, ...],
        author_provenance: AuthorProvenance,
        recorded_by: str,
        recorded_at: datetime | None = None,
    ) -> LedgerRef:
        source_entry, snapshot_entry, source, snapshot = self._validated_source_material(
            source_candidate_ref, source_snapshot_ref
        )
        if not source_entry.synthetic or not snapshot_entry.synthetic:
            raise KnowledgeOpsPolicyError(
                "P1 EvidenceCandidate staging accepts synthetic fixture lineage only"
            )
        policy = self._bundle.source_policy(
            source.policy.policy_id, source.policy.policy_version
        )
        if policy.status != "active":
            raise KnowledgeOpsPolicyError("retired SourcePolicy cannot stage evidence")
        canonical = validate_url_against_policy(str(source.canonical_url), policy)
        if validate_url_against_policy(str(snapshot.canonical_url), policy) != canonical:
            raise KnowledgeOpsPolicyError("source and snapshot locators differ")
        if policy.decision_for(SourceOperation.DISCOVER_METADATA) == PolicyDecision.DENY:
            raise KnowledgeOpsPolicyError("SourcePolicy denies metadata discovery")
        limitations = set(known_limitation_codes)
        if policy.license_posture in {
            LicensePosture.NEEDS_VERIFICATION.value,
            LicensePosture.REGISTRATION_REQUIRED.value,
            LicensePosture.LICENSE_REQUIRED.value,
        } and EvidenceLimitationCode.RIGHTS_UNRESOLVED.value not in limitations:
            raise KnowledgeOpsPolicyError(
                "unverified SourcePolicy requires a rights-unresolved limitation"
            )
        profile = next(
            (
                item
                for item in self._bundle.coverage_profiles
                if item.profile_id == source.validation_profile_id
            ),
            None,
        )
        if profile is None or proposed_scope != profile.scope:
            raise KnowledgeOpsPolicyError(
                "EvidenceCandidate scope must equal its acquisition validation profile"
            )
        if proposed_knowledge_layer not in profile.layers:
            raise KnowledgeOpsPolicyError(
                "proposed knowledge layer is outside the validation profile"
            )
        if author_provenance.synthetic is not True:
            raise KnowledgeOpsPolicyError("P1 machine author must be explicitly synthetic")

        head = self._ledger.head(LedgerCollection.EVIDENCE_CANDIDATE, candidate_id)
        next_version = 1 if head is None else head.record_version + 1
        timestamp = recorded_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or author_provenance.authored_at > timestamp:
            raise KnowledgeOpsPolicyError(
                "EvidenceCandidate timestamp must follow timezone-aware author provenance"
            )
        candidate = EvidenceCandidate(
            candidate_id=candidate_id,
            candidate_version=next_version,
            source_candidate_ref=source_candidate_ref,
            source_snapshot_ref=source_snapshot_ref,
            connector_id=source.connector_id,
            connector_version=connector_version,
            parser_id=parser_id,
            parser_version=parser_version,
            whole_record_sha256=snapshot.content_sha256,
            locator=EvidenceLocator(
                canonical_url=canonical,
                stable_source_key=source.stable_source_key,
                document_version=source.document_version,
            ),
            benchmark_symptom_refs=benchmark_symptom_refs,
            proposed_knowledge_layer=proposed_knowledge_layer,
            proposed_claim_type=proposed_claim_type,
            proposed_scope=proposed_scope,
            derivation_provenance=derivation_provenance,
            known_limitation_codes=known_limitation_codes,
            author_provenance=author_provenance,
            synthetic=True,
        )
        assert_no_sensitive_data(
            candidate.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.EVIDENCE_CANDIDATE,
        )
        return self._ledger.append(
            LedgerCollection.EVIDENCE_CANDIDATE,
            candidate.candidate_id,
            payload_type="evidence_candidate_v2",
            payload=candidate,
            recorded_by=recorded_by,
            recorded_at=timestamp,
            synthetic=True,
            expected_record_version=next_version,
        ).ref

    def _validated_source_material(
        self,
        source_candidate_ref: LedgerRef,
        source_snapshot_ref: LedgerRef,
    ) -> tuple[LedgerEntry, LedgerEntry, SourceCandidate, SourceSnapshot]:
        source_entry = self._ledger.get(source_candidate_ref)
        snapshot_entry = self._ledger.get(source_snapshot_ref)
        if (
            source_entry.collection != LedgerCollection.CANDIDATE.value
            or source_entry.payload_type != "source_candidate"
        ):
            raise KnowledgeOpsPolicyError("exact SourceCandidate payload is required")
        if (
            snapshot_entry.collection != LedgerCollection.SNAPSHOT.value
            or snapshot_entry.payload_type != "source_snapshot"
        ):
            raise KnowledgeOpsPolicyError("exact SourceSnapshot payload is required")
        if self._ledger.head(source_candidate_ref.collection, source_candidate_ref.record_id).ref != source_candidate_ref:
            raise KnowledgeOpsPolicyError("stale SourceCandidate cannot stage evidence")
        if self._ledger.head(source_snapshot_ref.collection, source_snapshot_ref.record_id).ref != source_snapshot_ref:
            raise KnowledgeOpsPolicyError("stale SourceSnapshot cannot stage evidence")
        source = SourceCandidate.model_validate(source_entry.payload)
        snapshot = SourceSnapshot.model_validate(snapshot_entry.payload)
        if (
            source.candidate_id != source_candidate_ref.record_id
            or snapshot.snapshot_id != source_snapshot_ref.record_id
            or snapshot.candidate_ref != source_candidate_ref
        ):
            raise KnowledgeOpsPolicyError("SourceSnapshot does not belong to SourceCandidate")
        if source.synthetic != source_entry.synthetic or snapshot.synthetic != snapshot_entry.synthetic:
            raise KnowledgeOpsPolicyError("source payload and ledger synthetic status differ")
        assert_no_sensitive_data(source.model_dump(mode="json"))
        assert_no_sensitive_data(
            snapshot.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
        )
        return source_entry, snapshot_entry, source, snapshot


class EvidenceCandidatePromotionService:
    """Promotes one exact synthetic EvidenceCandidate to a non-authoritative draft."""

    def __init__(self, *, ledger: AppendOnlyLedger) -> None:
        self._ledger = ledger

    def promote_to_draft_claim(
        self,
        *,
        evidence_candidate_ref: LedgerRef,
        claim_id: str,
        author_provenance: AuthorProvenance,
        created_by: str,
        created_at: datetime | None = None,
    ) -> LedgerRef:
        candidate_entry = self._ledger.get(evidence_candidate_ref)
        if (
            candidate_entry.collection != LedgerCollection.EVIDENCE_CANDIDATE.value
            or candidate_entry.payload_type != "evidence_candidate_v2"
        ):
            raise KnowledgeOpsPolicyError(
                "draft Claim promotion requires an exact EvidenceCandidate payload"
            )
        if self._ledger.head(
            evidence_candidate_ref.collection, evidence_candidate_ref.record_id
        ).ref != evidence_candidate_ref:
            raise KnowledgeOpsPolicyError("stale EvidenceCandidate cannot be promoted")
        candidate = EvidenceCandidate.model_validate(candidate_entry.payload)
        if candidate.candidate_id != evidence_candidate_ref.record_id:
            raise KnowledgeOpsPolicyError("EvidenceCandidate ledger identity differs")
        if not candidate.synthetic or not candidate_entry.synthetic:
            raise KnowledgeOpsPolicyError(
                "P1 promotion accepts synthetic EvidenceCandidate lineage only"
            )
        if (
            author_provenance.author_identity_id
            != candidate.author_provenance.author_identity_id
            or author_provenance.author_principal_id
            != candidate.author_provenance.author_principal_id
            or not author_provenance.synthetic
        ):
            raise KnowledgeOpsPolicyError(
                "machine draft author must preserve the candidate author principal"
            )
        snapshot_entry = self._ledger.get(candidate.source_snapshot_ref)
        source_entry = self._ledger.get(candidate.source_candidate_ref)
        if (
            snapshot_entry.collection != LedgerCollection.SNAPSHOT.value
            or source_entry.collection != LedgerCollection.CANDIDATE.value
            or snapshot_entry.payload_type != "source_snapshot"
            or source_entry.payload_type != "source_candidate"
            or not snapshot_entry.synthetic
            or not source_entry.synthetic
        ):
            raise KnowledgeOpsPolicyError("draft Claim source lineage is invalid")
        snapshot = SourceSnapshot.model_validate(snapshot_entry.payload)
        source = SourceCandidate.model_validate(source_entry.payload)
        if (
            source.candidate_id != candidate.source_candidate_ref.record_id
            or snapshot.snapshot_id != candidate.source_snapshot_ref.record_id
            or snapshot.candidate_ref != candidate.source_candidate_ref
        ):
            raise KnowledgeOpsPolicyError("draft Claim source lineage is invalid")
        assert_no_sensitive_data(source.model_dump(mode="json"))
        if snapshot.content_sha256 != candidate.whole_record_sha256:
            raise KnowledgeOpsPolicyError("EvidenceCandidate whole-record digest changed")
        assert_no_sensitive_data(
            snapshot.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT,
        )
        assert_no_sensitive_data(
            candidate.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.EVIDENCE_CANDIDATE,
        )

        head = self._ledger.head(LedgerCollection.CLAIM, claim_id)
        next_version = 1 if head is None else head.record_version + 1
        if head is not None:
            if head.payload_type != "machine_draft_claim_v2" or not head.synthetic:
                raise KnowledgeOpsPolicyError(
                    "draft Claim successor must preserve typed synthetic lineage"
                )
            previous = MachineDraftClaim.model_validate(head.payload)
            if (
                previous.evidence_candidate_ref != evidence_candidate_ref
                or previous.source_candidate_ref != candidate.source_candidate_ref
                or previous.source_snapshot_ref != candidate.source_snapshot_ref
            ):
                raise KnowledgeOpsPolicyError(
                    "draft Claim successor cannot substitute its EvidenceCandidate"
                )
            assert_no_sensitive_data(
                previous.model_dump(mode="json"),
                digest_trust_profile=DigestTrustProfile.MACHINE_DRAFT_CLAIM,
            )
        timestamp = created_at or datetime.now(timezone.utc)
        claim = MachineDraftClaim(
            claim_id=claim_id,
            claim_version=next_version,
            evidence_candidate_ref=evidence_candidate_ref,
            source_candidate_ref=candidate.source_candidate_ref,
            source_snapshot_ref=candidate.source_snapshot_ref,
            draft_assertion_code=candidate.proposed_claim_type,
            knowledge_layer=candidate.proposed_knowledge_layer,
            scope=candidate.proposed_scope,
            benchmark_symptom_refs=candidate.benchmark_symptom_refs,
            limitation_codes=candidate.known_limitation_codes,
            author_provenance=author_provenance,
            created_at=timestamp,
            created_by=created_by,
            synthetic=True,
        )
        assert_no_sensitive_data(
            claim.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.MACHINE_DRAFT_CLAIM,
        )
        return self._ledger.append(
            LedgerCollection.CLAIM,
            claim.claim_id,
            payload_type="machine_draft_claim_v2",
            payload=claim,
            recorded_by=created_by,
            recorded_at=timestamp,
            synthetic=True,
            expected_record_version=next_version,
        ).ref

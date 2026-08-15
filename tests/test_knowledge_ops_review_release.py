from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from continucare.knowledge.ops import (
    AcquisitionRequest,
    AcquisitionService,
    AppendOnlyLedger,
    AuthorProvenance,
    GovernanceGate,
    GovernedSourceV2,
    InMemoryReviewerDirectory,
    KnowledgeGap,
    KnowledgeOpsIntegrityError,
    KnowledgeOpsPolicyError,
    KnowledgeReleaseBlocked,
    KnowledgeReleaseCandidate,
    LedgerCollection,
    OfflineFixtureConnector,
    QuarantineBlobStore,
    ReadinessBlockerCode,
    ReleaseArtifact,
    ReleaseReadinessReport,
    ReleaseReadinessService,
    ReviewCheck,
    ReviewDecisionPayload,
    ReviewEvent,
    ReviewEventAttestation,
    ReviewEventService,
    ReviewLedgerDecisionProvider,
    ReviewPacket,
    ReviewPacketBuilder,
    ReviewerIdentity,
    ReviewSubjectKind,
    SourceCandidate,
    SourceOperationReview,
    SourcePolicyRef,
    SourceSnapshot,
    SourcePromotionService,
    assert_no_sensitive_data,
    load_builtin_ops_bundle,
    load_builtin_ops_read_model,
)
from continucare.knowledge.ops.security import digest_derived_internal_id
from continucare.knowledge.ops import review as review_module


FIXTURE_CATALOG_SHA256 = (
    "e711994018bb783236e050d890783502eaee91e7345e4d4e9808fdf542764a3f"
)
PHONE_BEARING_REVIEW_CANDIDATE_SHA256 = (
    "5e06fcf272eadac5481501b2a120de788abbb5f6b6dcd9fb30cf15804119261b"
)
PHONE_BEARING_REVIEW_CANDIDATE_NUMBER = "15804119261"


def _fixture_root() -> Path:
    return Path(__file__).parent / "fixtures" / "knowledge_ops"


def _synthetic_reviewer(role: str) -> ReviewerIdentity:
    scope = next(
        item.scope
        for item in load_builtin_ops_bundle().coverage_profiles
        if item.profile_id == "fixture-medication-followup"
    )
    return ReviewerIdentity(
        identity_id=f"synthetic-{role}",
        principal_id=f"synthetic-principal-{role}",
        display_name=f"Synthetic {role} fixture",
        roles=(role,),
        authorized_jurisdictions=scope.jurisdictions,
        authorized_scopes=(scope,),
        authorization_valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        authorization_valid_until=datetime(2100, 1, 1, tzinfo=timezone.utc),
        assurance="synthetic_test",
        synthetic=True,
    )


def _reviewers(*roles: str) -> InMemoryReviewerDirectory:
    return InMemoryReviewerDirectory(tuple(_synthetic_reviewer(role) for role in roles))


class _FormalReviewerVerifierFixture:
    """Explicit verifier-mechanism fixture; never a production identity source."""

    def __init__(self, *reviewers: ReviewerIdentity) -> None:
        self._reviewers = {item.identity_id: item for item in reviewers}
        self._key = b"continucare-formal-verifier-mechanism-fixture-only"

    def resolve(self, identity_id: str) -> ReviewerIdentity | None:
        return self._reviewers.get(identity_id)

    def replace(self, identity: ReviewerIdentity) -> None:
        if identity.identity_id not in self._reviewers:
            raise KeyError(identity.identity_id)
        self._reviewers[identity.identity_id] = identity

    def verify_identity_authorization(
        self, identity, *, role, scope, at
    ) -> bool:
        return (
            self.resolve(identity.identity_id) == identity
            and identity.assurance == "formally_verified"
            and identity.verification_reference
            == "urn:continucare:test:formal-reviewer-verifier-fixture"
            and identity.authorizes(role=role, scope=scope, at=at)
        )

    def issue_review_attestation(
        self,
        identity,
        *,
        role,
        scope,
        event_claim_sha256,
        issued_at,
    ):
        if not self.verify_identity_authorization(
            identity, role=role, scope=scope, at=issued_at
        ):
            return None
        attestation_id = digest_derived_internal_id(
            "fixture", event_claim_sha256, digest_characters=32
        )
        verifier_reference = "urn:continucare:test:review-attestation-fixture"
        valid_until = identity.authorization_valid_until
        return ReviewEventAttestation(
            attestation_id=attestation_id,
            event_claim_sha256=event_claim_sha256,
            issued_at=issued_at,
            valid_until=valid_until,
            verifier_reference=verifier_reference,
            attestation_sha256=self._digest(
                identity.identity_id,
                attestation_id,
                event_claim_sha256,
                issued_at,
                valid_until,
                verifier_reference,
            ),
            synthetic=False,
        )

    def verify_review_attestation(
        self,
        identity,
        *,
        role,
        scope,
        event_claim_sha256,
        attestation,
        evaluated_at,
    ) -> bool:
        if (
            not self.verify_identity_authorization(
                identity, role=role, scope=scope, at=evaluated_at
            )
            or attestation.synthetic
            or attestation.event_claim_sha256 != event_claim_sha256
            or attestation.issued_at > evaluated_at
            or attestation.valid_until <= evaluated_at
            or attestation.valid_until > identity.authorization_valid_until
        ):
            return False
        expected = self._digest(
            identity.identity_id,
            attestation.attestation_id,
            event_claim_sha256,
            attestation.issued_at,
            attestation.valid_until,
            attestation.verifier_reference,
        )
        return hmac.compare_digest(expected, attestation.attestation_sha256)

    def _digest(
        self,
        identity_id,
        attestation_id,
        event_claim_sha256,
        issued_at,
        valid_until,
        verifier_reference,
    ) -> str:
        payload = "|".join(
            (
                identity_id,
                attestation_id,
                event_claim_sha256,
                issued_at.isoformat(),
                valid_until.isoformat(),
                verifier_reference,
            )
        ).encode("utf-8")
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()


def _formal_reviewer(role, scope, *, identity_suffix=None) -> ReviewerIdentity:
    now = datetime.now(timezone.utc)
    suffix = identity_suffix or role
    return ReviewerIdentity(
        identity_id=f"formal-fixture-{suffix}",
        principal_id=f"formal-fixture-principal-{suffix}",
        display_name=f"Formal verifier mechanism fixture {suffix}",
        roles=(role,),
        authorized_jurisdictions=scope.jurisdictions,
        authorized_scopes=(scope,),
        authorization_valid_from=now - timedelta(days=365),
        authorization_valid_until=now + timedelta(days=365),
        assurance="formally_verified",
        synthetic=False,
        verification_reference=(
            "urn:continucare:test:formal-reviewer-verifier-fixture"
        ),
        verification_evidence_sha256=hashlib.sha256(
            f"formal-fixture:{suffix}".encode("utf-8")
        ).hexdigest(),
        verified_by="test:formal-reviewer-verifier-fixture",
        verified_at=now,
    )


def _manifest_evidence(bundle):
    return tuple(
        {
            "file_id": item.ref.file_id,
            "file_version": item.ref.file_version,
            "manifest_sha256": item.manifest_sha256,
        }
        for item in bundle.index.files
    )


def _release_author(
    *,
    synthetic: bool,
    suffix: str,
    provenance_reference: str | None = None,
) -> AuthorProvenance:
    return AuthorProvenance(
        author_identity_id=f"author-{suffix}",
        author_principal_id=f"author-principal-{suffix}",
        authored_at=datetime.now(timezone.utc),
        provenance_reference=(
            provenance_reference
            or f"urn:continucare:test:author-provenance:{suffix}"
        ),
        synthetic=synthetic,
    )


def _acquisition_context(tmp_path: Path):
    bundle = load_builtin_ops_bundle()
    profile = next(
        item
        for item in bundle.coverage_profiles
        if item.profile_id == "fixture-medication-followup"
    )
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    quarantine = QuarantineBlobStore(tmp_path / "quarantine")
    connector = OfflineFixtureConnector(
        _fixture_root(), catalog_sha256=FIXTURE_CATALOG_SHA256
    )
    request = AcquisitionRequest(
        request_id="review-readiness",
        validation_profile_id=profile.profile_id,
        trigger="scheduled",
        policy_ids=("nmpa-cn-regulatory-metadata",),
        topic_codes=(
            {
                "system": "urn:continucare:synthetic-topic",
                "version": "1",
                "code": "review-readiness",
            },
        ),
        query_terms=("medication followup",),
        scope=profile.scope,
        created_at=datetime.now(timezone.utc),
        created_by="system:test",
    )
    acquisition = AcquisitionService(
        bundle=bundle,
        ledger=ledger,
        quarantine=quarantine,
        connector=connector,
    ).run(request)
    assert acquisition.status == "completed"
    return bundle, profile, ledger, acquisition


def _payload(
    *,
    scope=None,
    operation: str | None = None,
    operation_decision: str = "approved",
    result: str = "pass",
    evidence_refs=(),
) -> ReviewDecisionPayload:
    operation_reviews = ()
    if operation is not None:
        operation_reviews = (
            SourceOperationReview(
                operation=operation,
                decision=operation_decision,
                conditions=("Synthetic fixture only; no production rights conclusion.",),
            ),
        )
    return ReviewDecisionPayload(
        checklist=(
            ReviewCheck(
                check_id="synthetic-check",
                result=result,
                evidence_refs=evidence_refs,
                note="Mechanism-only synthetic review fixture.",
            ),
        ),
        source_operation_decisions=operation_reviews,
        confirmed_scope=scope,
        limitations=(
            "Synthetic reviewer evidence never counts toward KnowledgeRelease.",
        ),
    )


def _formal_payload(*, scope=None) -> ReviewDecisionPayload:
    return ReviewDecisionPayload(
        checklist=(
            ReviewCheck(
                check_id="formal-verifier-mechanism-check",
                result="pass",
                note=(
                    "Verifier mechanism fixture only; this is not a clinical "
                    "approval or a production reviewer assertion."
                ),
            ),
        ),
        confirmed_scope=scope,
        limitations=(
            "Formal identity is supplied only by an explicit test verifier fixture.",
        ),
    )


def _updated_identity(identity: ReviewerIdentity, **updates) -> ReviewerIdentity:
    return ReviewerIdentity.model_validate(
        {**identity.model_dump(mode="python"), **updates}
    )


def _formal_claim_review_context(
    tmp_path,
    *,
    author_identity_id="fixture-author-identity",
    author_principal_id="fixture-author-principal",
    record_curator=True,
):
    bundle = load_builtin_ops_bundle()
    profile = next(
        item
        for item in bundle.coverage_profiles
        if item.profile_id == "fixture-medication-followup"
    )
    ledger = AppendOnlyLedger(tmp_path / "formal-review-ledger")
    clinical = _formal_reviewer("clinical_reviewer", profile.scope)
    curator = _formal_reviewer("knowledge_curator", profile.scope)
    authored_at = datetime.now(timezone.utc)
    claim_ref = ledger.append(
        LedgerCollection.CLAIM,
        "formal-verifier-mechanism-claim",
        payload_type="claim_review_mechanism_fixture",
        payload={
            "claim_id": "formal-verifier-mechanism-claim",
            "statement": (
                "No clinical assertion; identity and gate verification mechanism only."
            ),
            "author_provenance": {
                "author_identity_id": author_identity_id,
                "author_principal_id": author_principal_id,
                "authored_at": authored_at.isoformat(),
                "provenance_reference": "urn:continucare:test:fixture-author",
                "synthetic": False,
            },
        },
        recorded_by="test:formal-verifier-mechanism",
        recorded_at=authored_at,
        synthetic=False,
    ).ref
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="clinical_claim",
        subject_ref=claim_ref,
        gate="clinical_claim_approval",
        scope=profile.scope,
        generated_by="test:formal-verifier-mechanism",
        known_limitations=(
            "Identity verifier mechanism fixture only; no clinical conclusion.",
        ),
    )
    verifier = _FormalReviewerVerifierFixture(clinical, curator)
    service = ReviewEventService(
        bundle=bundle, ledger=ledger, reviewers=verifier
    )
    clinical_ref = service.record(
        packet_ref=packet_ref,
        reviewer_identity_id=clinical.identity_id,
        reviewer_role="clinical_reviewer",
        axis="clinical",
        decision="approved",
        rationale="Formal verifier mechanism fixture clinical-axis event.",
        decision_payload=_formal_payload(scope=profile.scope),
    )
    curator_ref = None
    if record_curator:
        curator_ref = service.record(
            packet_ref=packet_ref,
            reviewer_identity_id=curator.identity_id,
            reviewer_role="knowledge_curator",
            axis="metadata_quality",
            decision="approved",
            rationale="Formal verifier mechanism fixture curator-axis event.",
            decision_payload=_formal_payload(),
        )
    provider = ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=verifier
    )
    return {
        "bundle": bundle,
        "profile": profile,
        "ledger": ledger,
        "claim_ref": claim_ref,
        "packet_ref": packet_ref,
        "clinical_ref": clinical_ref,
        "curator_ref": curator_ref,
        "clinical": clinical,
        "curator": curator,
        "verifier": verifier,
        "provider": provider,
    }


def _append_direct_formal_successor(
    context,
    event_ref,
    *,
    counts_toward_release: bool,
    valid_attestation: bool,
):
    ledger = context["ledger"]
    event = ReviewEvent.model_validate(ledger.get(event_ref).payload)
    identity = context["verifier"].resolve(event.reviewer_identity_id)
    assert identity is not None
    timestamp = event.decided_at + timedelta(microseconds=1)
    payload = event.model_dump(mode="json")
    payload.update(
        {
            "event_id": digest_derived_internal_id(
                "direct",
                hashlib.sha256(timestamp.isoformat().encode()).hexdigest(),
                digest_characters=20,
            ),
            "decided_at": timestamp.isoformat().replace("+00:00", "Z"),
            "expected_predecessor_sha256": event_ref.entry_sha256,
            "counts_toward_release": counts_toward_release,
        }
    )
    event_claim_sha256 = review_module._review_event_claim_sha256(payload)
    attestation = context["verifier"].issue_review_attestation(
        identity,
        role=event.reviewer_role,
        scope=context["profile"].scope,
        event_claim_sha256=event_claim_sha256,
        issued_at=timestamp,
    )
    assert attestation is not None
    if not valid_attestation:
        attestation = attestation.model_copy(
            update={"attestation_sha256": "0" * 64}
        )
    payload["review_attestation"] = attestation.model_dump(mode="json")
    direct_event = ReviewEvent.model_validate(payload)
    return ledger.append(
        LedgerCollection.REVIEW_EVENT,
        event_ref.record_id,
        payload_type="review_event_v3",
        payload=direct_event,
        recorded_by=event.reviewer_identity_id,
        recorded_at=timestamp,
        synthetic=False,
    ).ref


def _append_signed_formal_event_direct(
    context,
    *,
    identity: ReviewerIdentity,
    role,
    axis,
    decision_payload,
):
    ledger = context["ledger"]
    packet_ref = context["packet_ref"]
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)
    timestamp = datetime.now(timezone.utc)
    record_id = review_module._chain_id(
        "review", packet.subject.object_ref, str(role)
    )
    predecessor = ledger.head(LedgerCollection.REVIEW_EVENT, record_id)
    fields = dict(
        event_id=review_module._event_id(record_id, timestamp),
        subject=packet.subject,
        packet_ref=packet_ref,
        gate=packet.gate,
        axis=axis,
        decision="approved",
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
            review_module._reviewer_identity_assertion_sha256(identity)
        ),
        decision_payload=decision_payload,
        rationale="Direct signed self-review bypass probe.",
        decided_at=timestamp,
        expected_predecessor_sha256=(
            None if predecessor is None else predecessor.entry_sha256
        ),
        counts_toward_release=True,
        synthetic=False,
        knowledge_effect="informational_only",
        runtime_authority="none",
    )
    event_claim_sha256 = review_module._review_event_claim_sha256(fields)
    attestation = context["verifier"].issue_review_attestation(
        identity,
        role=role,
        scope=packet.scope,
        event_claim_sha256=event_claim_sha256,
        issued_at=timestamp,
    )
    assert attestation is not None
    event = ReviewEvent(**fields, review_attestation=attestation)
    return ledger.append(
        LedgerCollection.REVIEW_EVENT,
        record_id,
        payload_type="review_event_v3",
        payload=event,
        recorded_by=identity.identity_id,
        recorded_at=timestamp,
        synthetic=False,
    ).ref


def _source_review_packet(
    bundle, profile, ledger, acquisition, *, generated_at=None
):
    return ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind=ReviewSubjectKind.SOURCE_CANDIDATE,
        subject_ref=acquisition.candidate_refs[0],
        gate=GovernanceGate.SOURCE_PROMOTION,
        scope=profile.scope,
        generated_by="system:test-packet-builder",
        known_limitations=(
            "Candidate and snapshot are synthetic and cannot establish production rights.",
        ),
        evidence_refs=(acquisition.snapshot_refs[0],),
        open_gap_refs=acquisition.gap_refs,
        generated_at=generated_at,
    )


def _phone_bearing_review_evidence_context(tmp_path: Path):
    bundle = load_builtin_ops_bundle()
    profile = next(
        item
        for item in bundle.coverage_profiles
        if item.profile_id == "fixture-medication-followup"
    )
    ledger = AppendOnlyLedger(tmp_path / "phone-bearing-review-ledger")
    candidate = SourceCandidate(
        candidate_id="phone-bearing-review-candidate",
        request_id="phone-bearing-review-request",
        validation_profile_id=profile.profile_id,
        policy=SourcePolicyRef(
            policy_id="nmpa-cn-regulatory-metadata",
            policy_version=1,
        ),
        connector_id="offline-fixture",
        stable_source_key="phone-bearing-review-source",
        canonical_url=(
            "https://www.nmpa.gov.cn/synthetic-offline-fixture/"
            "phone-bearing-review-source"
        ),
        title="Synthetic phone-bearing digest review source",
        issuing_authority="ContinuCare synthetic fixture",
        source_type="regulatory_product_information",
        jurisdictions=({"system": "iso3166_1", "code": "CN"},),
        languages=("zh-CN",),
        document_version="synthetic-v1",
        metadata={"fixture_domain": "typed_review_digest"},
        discovered_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        synthetic=True,
    )
    candidate_ref = ledger.append(
        LedgerCollection.CANDIDATE,
        candidate.candidate_id,
        payload_type="source_candidate",
        payload=candidate,
        recorded_by="test:phone-digest-20",
        recorded_at=datetime(2026, 1, 2, 3, 4, 6, tzinfo=timezone.utc),
        synthetic=True,
    ).ref
    assert candidate_ref.entry_sha256 == PHONE_BEARING_REVIEW_CANDIDATE_SHA256
    assert PHONE_BEARING_REVIEW_CANDIDATE_NUMBER in candidate_ref.entry_sha256

    snapshot = SourceSnapshot(
        snapshot_id="phone-bearing-review-snapshot",
        candidate_ref=candidate_ref,
        canonical_url=candidate.canonical_url,
        retrieved_at=datetime(2026, 1, 2, 3, 4, 7, tzinfo=timezone.utc),
        content_type="text/html",
        content_size=0,
        content_sha256=hashlib.sha256(b"").hexdigest(),
        metadata_sha256=hashlib.sha256(
            b"typed-review-evidence-metadata"
        ).hexdigest(),
        storage="metadata_only",
        synthetic=True,
    )
    snapshot_ref = ledger.append(
        LedgerCollection.SNAPSHOT,
        snapshot.snapshot_id,
        payload_type="source_snapshot",
        payload=snapshot,
        recorded_by="test:typed-review-evidence",
        recorded_at=datetime(2026, 1, 2, 3, 4, 8, tzinfo=timezone.utc),
        synthetic=True,
    ).ref
    return bundle, profile, ledger, candidate, candidate_ref, snapshot, snapshot_ref


def _append_legacy_phone_evidence(ledger: AppendOnlyLedger, record_id: str):
    return ledger.append(
        LedgerCollection.CLAIM,
        record_id,
        payload_type="legacy_review_evidence",
        payload={
            "metadata": {
                "entry_sha256": PHONE_BEARING_REVIEW_CANDIDATE_SHA256,
            }
        },
        recorded_by="test:legacy-review-evidence",
        recorded_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
        synthetic=True,
    ).ref


def _approve_source_synthetically(
    bundle, profile, ledger, acquisition, directory=None
):
    fixture_clock = datetime.now(timezone.utc) - timedelta(seconds=2)
    packet_ref = _source_review_packet(
        bundle,
        profile,
        ledger,
        acquisition,
        generated_at=fixture_clock,
    )
    decision_time = fixture_clock + timedelta(seconds=1)
    directory = directory or _reviewers("knowledge_curator", "rights_officer")
    service = ReviewEventService(
        bundle=bundle, ledger=ledger, reviewers=directory
    )
    curator_ref = service.record(
        packet_ref=packet_ref,
        reviewer_identity_id="synthetic-knowledge_curator",
        reviewer_role="knowledge_curator",
        axis="metadata_quality",
        decision="approved",
        rationale="Synthetic metadata mechanism check passed.",
        decision_payload=_payload(),
        decided_at=decision_time,
    )
    rights_ref = service.record(
        packet_ref=packet_ref,
        reviewer_identity_id="synthetic-rights_officer",
        reviewer_role="rights_officer",
        axis="rights",
        decision="approved",
        rationale="Synthetic rights mechanism check passed only for fixture flow.",
        decision_payload=_payload(operation="register_link_metadata"),
        decided_at=decision_time,
    )
    decisions = ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=directory
    )
    gate_decision = decisions.resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    )
    assert gate_decision is not None
    assert gate_decision.synthetic is True
    assert gate_decision.production_eligible is False
    source_ref = SourcePromotionService(
        bundle=bundle,
        ledger=ledger,
        decisions=decisions,
    ).promote(
        source_id="synthetic-reviewed-source",
        candidate_ref=acquisition.candidate_refs[0],
        snapshot_ref=acquisition.snapshot_refs[0],
        promoted_by="system:synthetic-review-flow",
    )
    return packet_ref, (curator_ref, rights_ref), decisions, source_ref, directory


def test_review_packet_pins_exact_subject_scope_gaps_and_safety_boundary(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)

    assert packet.subject.subject_kind == "source_candidate"
    assert packet.subject.object_ref == acquisition.candidate_refs[0]
    assert packet.subject_entry_sha256 == acquisition.candidate_refs[0].entry_sha256
    assert packet.governance_bundle_id == bundle.index.bundle_id
    assert packet.governance_bundle_version == bundle.index.bundle_version
    assert packet.governance_index_sha256 == bundle.index_sha256()
    assert packet.governance_manifests == bundle.manifest_evidence()
    assert packet.gate == "source_promotion"
    assert packet.requested_roles == ("knowledge_curator", "rights_officer")
    assert packet.requested_source_operations == ("register_link_metadata",)
    assert packet.source_policy is not None
    assert packet.source_policy.policy_id == "nmpa-cn-regulatory-metadata"
    assert packet.scope == profile.scope
    assert packet.evidence_refs == (acquisition.snapshot_refs[0],)
    assert packet.open_gap_refs == acquisition.gap_refs
    assert packet.synthetic is True
    assert packet.contains_patient_data is False
    assert packet.knowledge_effect == "informational_only"
    assert packet.runtime_authority == "none"


def test_phone_bearing_typed_snapshot_evidence_builds_replays_and_resolves(
    tmp_path,
):
    (
        bundle,
        profile,
        ledger,
        _,
        candidate_ref,
        snapshot,
        snapshot_ref,
    ) = _phone_bearing_review_evidence_context(tmp_path)
    with pytest.raises(
        KnowledgeOpsPolicyError,
        match="appears to contain personal data",
    ):
        assert_no_sensitive_data(snapshot.model_dump(mode="json"))
    with pytest.raises(
        KnowledgeOpsPolicyError,
        match="appears to contain personal data",
    ):
        assert_no_sensitive_data(
            {"metadata": {"entry_sha256": PHONE_BEARING_REVIEW_CANDIDATE_SHA256}}
        )

    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="source_candidate",
        subject_ref=candidate_ref,
        gate="source_promotion",
        scope=profile.scope,
        generated_by="test:typed-review-evidence",
        known_limitations=("Synthetic typed evidence verifier fixture only.",),
        evidence_refs=(snapshot_ref,),
        generated_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
    )
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)
    subject_entry, material_entries = review_module._resolve_packet_material(
        bundle=bundle,
        ledger=ledger,
        packet=packet,
    )
    assert subject_entry.ref == candidate_ref
    assert tuple(item.ref for item in material_entries) == (snapshot_ref,)

    reviewers = _reviewers("knowledge_curator", "rights_officer")
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=reviewers,
    )
    decision_time = datetime(2026, 1, 2, 3, 5, 1, tzinfo=timezone.utc)
    service.record(
        packet_ref=packet_ref,
        reviewer_identity_id="synthetic-knowledge_curator",
        reviewer_role="knowledge_curator",
        axis="metadata_quality",
        decision="approved",
        rationale="Synthetic typed evidence replay for curator axis.",
        decision_payload=_payload(evidence_refs=(snapshot_ref,)),
        decided_at=decision_time,
    )
    service.record(
        packet_ref=packet_ref,
        reviewer_identity_id="synthetic-rights_officer",
        reviewer_role="rights_officer",
        axis="rights",
        decision="approved",
        rationale="Synthetic typed evidence replay for rights axis.",
        decision_payload=_payload(
            operation="register_link_metadata",
            evidence_refs=(snapshot_ref,),
        ),
        decided_at=decision_time,
    )
    decision = ReviewLedgerDecisionProvider(
        bundle=bundle,
        ledger=ledger,
        reviewers=reviewers,
    ).resolve_gate(
        candidate_ref,
        "source_promotion",
        evaluated_at=decision_time + timedelta(seconds=1),
    )
    assert decision is not None
    assert decision.synthetic is True
    assert decision.production_eligible is False
    assert ledger.verify_all() == 5


@pytest.mark.parametrize(
    "failure_mode",
    (
        "wrong_collection",
        "wrong_payload_type",
        "strict_model",
        "record_identity",
        "missing_candidate",
        "lineage_mismatch",
        "legacy_open_metadata",
    ),
)
def test_typed_review_evidence_failures_precede_packet_append(
    tmp_path,
    failure_mode,
):
    (
        bundle,
        profile,
        ledger,
        candidate,
        candidate_ref,
        snapshot,
        _,
    ) = _phone_bearing_review_evidence_context(tmp_path)
    snapshot_payload = snapshot.model_dump(mode="json")
    recorded_at = datetime(2026, 1, 2, 3, 5, 1, tzinfo=timezone.utc)

    if failure_mode == "wrong_collection":
        record_id = "wrong-collection-snapshot"
        snapshot_payload["snapshot_id"] = record_id
        evidence_ref = ledger.append(
            LedgerCollection.CLAIM,
            record_id,
            payload_type="source_snapshot",
            payload=snapshot_payload,
            recorded_by="test:wrong-typed-evidence-pair",
            recorded_at=recorded_at,
            synthetic=True,
        ).ref
    elif failure_mode == "wrong_payload_type":
        record_id = "wrong-payload-snapshot"
        snapshot_payload["snapshot_id"] = record_id
        evidence_ref = ledger.append(
            LedgerCollection.SNAPSHOT,
            record_id,
            payload_type="legacy_snapshot",
            payload=snapshot_payload,
            recorded_by="test:wrong-typed-evidence-pair",
            recorded_at=recorded_at,
            synthetic=True,
        ).ref
    elif failure_mode == "strict_model":
        record_id = "invalid-model-snapshot"
        snapshot_payload["snapshot_id"] = record_id
        snapshot_payload["unexpected_open_field"] = "must fail strict validation"
        evidence_ref = ledger.append(
            LedgerCollection.SNAPSHOT,
            record_id,
            payload_type="source_snapshot",
            payload=snapshot_payload,
            recorded_by="test:invalid-typed-evidence-model",
            recorded_at=recorded_at,
            synthetic=True,
        ).ref
    elif failure_mode == "record_identity":
        evidence_ref = ledger.append(
            LedgerCollection.SNAPSHOT,
            "different-snapshot-ledger-identity",
            payload_type="source_snapshot",
            payload=snapshot_payload,
            recorded_by="test:wrong-snapshot-identity",
            recorded_at=recorded_at,
            synthetic=True,
        ).ref
    elif failure_mode == "missing_candidate":
        record_id = "missing-candidate-snapshot"
        snapshot_payload["snapshot_id"] = record_id
        snapshot_payload["candidate_ref"] = {
            "collection": "candidate",
            "record_id": "missing-review-candidate",
            "record_version": 1,
            "entry_sha256": "a" * 64,
        }
        evidence_ref = ledger.append(
            LedgerCollection.SNAPSHOT,
            record_id,
            payload_type="source_snapshot",
            payload=snapshot_payload,
            recorded_by="test:missing-candidate-lineage",
            recorded_at=recorded_at,
            synthetic=True,
        ).ref
    elif failure_mode == "lineage_mismatch":
        lineage_candidate = SourceCandidate.model_validate(
            {
                **candidate.model_dump(mode="json"),
                "candidate_id": "non-synthetic-lineage-candidate",
                "synthetic": False,
            }
        )
        lineage_ref = ledger.append(
            LedgerCollection.CANDIDATE,
            lineage_candidate.candidate_id,
            payload_type="source_candidate",
            payload=lineage_candidate,
            recorded_by="test:lineage-mismatch",
            recorded_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
            synthetic=False,
        ).ref
        record_id = "lineage-mismatch-snapshot"
        snapshot_payload["snapshot_id"] = record_id
        snapshot_payload["candidate_ref"] = lineage_ref.model_dump(mode="json")
        evidence_ref = ledger.append(
            LedgerCollection.SNAPSHOT,
            record_id,
            payload_type="source_snapshot",
            payload=snapshot_payload,
            recorded_by="test:lineage-mismatch",
            recorded_at=recorded_at,
            synthetic=True,
        ).ref
    else:
        evidence_ref = _append_legacy_phone_evidence(
            ledger,
            "legacy-open-metadata-evidence",
        )

    entry_count = ledger.verify_all()
    packet_heads = ledger.list_heads(LedgerCollection.REVIEW_PACKET)
    with pytest.raises(
        (KnowledgeOpsIntegrityError, KnowledgeOpsPolicyError, ValidationError)
    ):
        ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
            subject_kind="source_candidate",
            subject_ref=candidate_ref,
            gate="source_promotion",
            scope=profile.scope,
            generated_by="test:typed-review-evidence-failure",
            known_limitations=("Failure must precede packet append.",),
            evidence_refs=(evidence_ref,),
            generated_at=datetime(2026, 1, 2, 3, 6, tzinfo=timezone.utc),
        )
    assert ledger.verify_all() == entry_count
    assert ledger.list_heads(LedgerCollection.REVIEW_PACKET) == packet_heads == ()


def test_invalid_supplemental_evidence_precedes_review_event_append(tmp_path):
    (
        bundle,
        profile,
        ledger,
        _,
        candidate_ref,
        _,
        snapshot_ref,
    ) = _phone_bearing_review_evidence_context(tmp_path)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="source_candidate",
        subject_ref=candidate_ref,
        gate="source_promotion",
        scope=profile.scope,
        generated_by="test:typed-review-evidence",
        known_limitations=("Supplemental failure ordering fixture.",),
        evidence_refs=(snapshot_ref,),
        generated_at=datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
    )
    bad_ref = _append_legacy_phone_evidence(
        ledger,
        "legacy-supplemental-evidence",
    )
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=_reviewers("knowledge_curator"),
    )
    entry_count = ledger.verify_all()
    with pytest.raises(
        KnowledgeOpsPolicyError,
        match="appears to contain personal data",
    ):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id="synthetic-knowledge_curator",
            reviewer_role="knowledge_curator",
            axis="metadata_quality",
            decision="approved",
            rationale="Invalid supplemental evidence must fail before append.",
            decision_payload=_payload(evidence_refs=(bad_ref,)),
            decided_at=datetime(2026, 1, 2, 3, 5, 1, tzinfo=timezone.utc),
        )
    assert ledger.verify_all() == entry_count
    assert ledger.list_heads(LedgerCollection.REVIEW_EVENT) == ()


def test_review_packet_contract_rejects_subject_digest_mismatch(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    payload = ledger.get(packet_ref).payload
    payload["subject_entry_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="subject digest"):
        ReviewPacket.model_validate(payload)


def test_high_risk_review_packet_pins_structured_author_provenance(tmp_path):
    bundle = load_builtin_ops_bundle()
    profile = bundle.coverage_profiles[0]
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    author = AuthorProvenance(
        author_identity_id="synthetic-claim-author",
        author_principal_id="synthetic-claim-author-principal",
        authored_at=datetime.now(timezone.utc),
        provenance_reference="urn:continucare:test:synthetic-claim-author",
        synthetic=True,
    )
    claim_ref = ledger.append(
        LedgerCollection.CLAIM,
        "synthetic-author-pinned-claim",
        payload_type="claim_review_mechanism_fixture",
        payload={
            "claim_id": "synthetic-author-pinned-claim",
            "author_provenance": author.model_dump(mode="json"),
        },
        recorded_by=author.author_identity_id,
        recorded_at=author.authored_at,
        synthetic=True,
    ).ref
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="clinical_claim",
        subject_ref=claim_ref,
        gate="clinical_claim_approval",
        scope=profile.scope,
        generated_by="test:packet-builder",
        known_limitations=("Synthetic authorship pin mechanism only.",),
    )
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)

    assert packet.author_provenance == author


def test_high_risk_packet_builder_rejects_missing_author_provenance(tmp_path):
    bundle = load_builtin_ops_bundle()
    profile = bundle.coverage_profiles[0]
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    claim_ref = ledger.append(
        LedgerCollection.CLAIM,
        "missing-author-claim",
        payload_type="claim_review_mechanism_fixture",
        payload={"claim_id": "missing-author-claim"},
        recorded_by="test-missing-author",
        synthetic=True,
    ).ref

    with pytest.raises(KnowledgeOpsPolicyError, match="author provenance"):
        ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
            subject_kind="clinical_claim",
            subject_ref=claim_ref,
            gate="clinical_claim_approval",
            scope=profile.scope,
            generated_by="test:packet-builder",
            known_limitations=("Missing author must fail closed.",),
        )


def test_packet_builder_adds_default_operation_to_explicit_operations(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="source_candidate",
        subject_ref=acquisition.candidate_refs[0],
        gate="source_promotion",
        scope=profile.scope,
        generated_by="system:test",
        known_limitations=("Synthetic expanded operation check only.",),
        requested_source_operations=("persist_snapshot",),
    )
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)

    assert packet.requested_source_operations == (
        "register_link_metadata",
        "persist_snapshot",
    )


def test_review_packet_discovers_related_open_gaps_when_caller_omits_them(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind=ReviewSubjectKind.SOURCE_CANDIDATE,
        subject_ref=acquisition.candidate_refs[0],
        gate=GovernanceGate.SOURCE_PROMOTION,
        scope=profile.scope,
        generated_by="system:test-packet-builder",
        known_limitations=(
            "Machine-created gaps must be carried even when omitted by the caller.",
        ),
        evidence_refs=(acquisition.snapshot_refs[0],),
    )
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)

    assert packet.open_gap_refs == acquisition.gap_refs


def test_snapshot_content_packet_derives_policy_and_persistence_operation(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="source_snapshot",
        subject_ref=acquisition.snapshot_refs[0],
        gate="content_persistence",
        scope=profile.scope,
        generated_by="system:test",
        known_limitations=("Synthetic blob only.",),
        evidence_refs=(acquisition.candidate_refs[0],),
    )
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)

    assert packet.source_policy is not None
    assert packet.source_policy.policy_id == "nmpa-cn-regulatory-metadata"
    assert packet.requested_source_operations == ("persist_snapshot",)
    assert packet.requested_roles == ("rights_officer", "knowledge_curator")


def test_change_set_packet_uses_content_persistence_gate_and_carries_gaps(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="change_set",
        subject_ref=acquisition.change_set_refs[0],
        gate="content_persistence",
        scope=profile.scope,
        generated_by="system:test",
        known_limitations=("Synthetic change observation only.",),
        evidence_refs=(acquisition.snapshot_refs[0],),
    )
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)

    assert packet.source_policy is not None
    assert packet.requested_source_operations == ("persist_snapshot",)
    assert packet.requested_roles == ("rights_officer", "knowledge_curator")
    assert packet.open_gap_refs == acquisition.gap_refs


def test_packet_builder_rejects_stale_subject(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    connector = OfflineFixtureConnector(
        _fixture_root(), catalog_sha256=FIXTURE_CATALOG_SHA256
    )
    # A second run appends a successor for the same logical candidate.
    request = AcquisitionRequest(
        request_id="review-readiness-second",
        validation_profile_id=profile.profile_id,
        trigger="scheduled",
        policy_ids=("nmpa-cn-regulatory-metadata",),
        topic_codes=(
            {
                "system": "urn:continucare:synthetic-topic",
                "version": "1",
                "code": "review-readiness",
            },
        ),
        query_terms=("medication followup",),
        scope=profile.scope,
        created_at=datetime.now(timezone.utc),
        created_by="system:test",
    )
    AcquisitionService(
        bundle=bundle,
        ledger=ledger,
        quarantine=QuarantineBlobStore(tmp_path / "quarantine"),
        connector=connector,
    ).run(request)

    with pytest.raises(KnowledgeOpsPolicyError, match="stale subject"):
        _source_review_packet(bundle, profile, ledger, acquisition)


def test_synthetic_review_events_are_append_only_and_never_count_toward_release(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref, event_refs, decisions, _, _ = _approve_source_synthetically(
        bundle, profile, ledger, acquisition
    )

    for event_ref in event_refs:
        event = ReviewEvent.model_validate(ledger.get(event_ref).payload)
        assert event.decision == "approved"
        assert event.reviewer_assurance == "synthetic_test"
        assert event.synthetic is True
        assert event.counts_toward_release is False
        assert event.knowledge_effect == "informational_only"
        assert event.runtime_authority == "none"
    decision = decisions.resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    )
    assert decision is not None
    assert decision.production_eligible is False
    assert ledger.get(packet_ref).synthetic is True


def test_gate_decision_rejects_packet_from_different_governance_bundle(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    _, _, _, _, directory = _approve_source_synthetically(
        bundle, profile, ledger, acquisition
    )
    changed_bundle = replace(
        bundle,
        index=bundle.index.model_copy(
            update={"bundle_version": bundle.index.bundle_version + 1}
        ),
    )

    assert ReviewLedgerDecisionProvider(
        bundle=changed_bundle, ledger=ledger, reviewers=directory
    ).resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is None


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"requested_roles": ("knowledge_curator",)}, "required role"),
        ({"requested_source_operations": ()}, "required Source operation"),
    ],
)
def test_review_event_revalidates_direct_packet_bypass(tmp_path, updates, message):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    packet = ReviewPacket.model_validate(ledger.get(packet_ref).payload)
    bypass_packet = packet.model_copy(update=updates)
    bypass_ref = ledger.append(
        LedgerCollection.REVIEW_PACKET,
        packet.packet_id,
        payload_type="review_packet",
        payload=bypass_packet,
        recorded_by="system:test-bypass-attempt",
        synthetic=True,
    ).ref
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=_reviewers("knowledge_curator"),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match=message):
        service.record(
            packet_ref=bypass_ref,
            reviewer_identity_id="synthetic-knowledge_curator",
            reviewer_role="knowledge_curator",
            axis="metadata_quality",
            decision="approved",
            rationale="Synthetic direct-ledger bypass must fail closed.",
            decision_payload=_payload(),
        )
    assert ledger.list_heads(LedgerCollection.REVIEW_EVENT) == ()


def test_gate_decision_revalidates_direct_event_payload_bypass(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    _, event_refs, decisions, _, _ = _approve_source_synthetically(
        bundle, profile, ledger, acquisition
    )
    rights_entry = ledger.get(event_refs[1])
    rights_event = ReviewEvent.model_validate(rights_entry.payload)
    bypass_event = rights_event.model_copy(update={"decision_payload": _payload()})
    ledger.append(
        LedgerCollection.REVIEW_EVENT,
        event_refs[1].record_id,
        payload_type="review_event_v2",
        payload=bypass_event,
        recorded_by="system:test-bypass-attempt",
        synthetic=True,
    )

    assert decisions.resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is None


def test_review_event_rejects_personal_data_in_rationale(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=_reviewers("knowledge_curator"),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="personal data"):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id="synthetic-knowledge_curator",
            reviewer_role="knowledge_curator",
            axis="metadata_quality",
            decision="revision_requested",
            rationale="Contact user@example.org for this review.",
        )
    assert ledger.list_heads(LedgerCollection.REVIEW_EVENT) == ()


def test_latest_review_event_head_controls_gate_without_history_rewrite(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    directory = _reviewers("knowledge_curator", "rights_officer")
    service = ReviewEventService(bundle=bundle, ledger=ledger, reviewers=directory)
    first = service.record(
        packet_ref=packet_ref,
        reviewer_identity_id="synthetic-knowledge_curator",
        reviewer_role="knowledge_curator",
        axis="metadata_quality",
        decision="approved",
        rationale="First synthetic decision.",
        decision_payload=_payload(),
    )
    second = service.record(
        packet_ref=packet_ref,
        reviewer_identity_id="synthetic-knowledge_curator",
        reviewer_role="knowledge_curator",
        axis="metadata_quality",
        decision="revision_requested",
        rationale="Synthetic change request supersedes the decision head.",
    )
    service.record(
        packet_ref=packet_ref,
        reviewer_identity_id="synthetic-rights_officer",
        reviewer_role="rights_officer",
        axis="rights",
        decision="approved",
        rationale="Synthetic rights decision.",
        decision_payload=_payload(operation="register_link_metadata"),
    )
    decisions = ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=directory
    )

    assert first.record_version == 1
    assert second.record_version == 2
    assert second.entry_sha256 != first.entry_sha256
    assert decisions.resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is None
    third = service.record(
        packet_ref=packet_ref,
        reviewer_identity_id="synthetic-knowledge_curator",
        reviewer_role="knowledge_curator",
        axis="metadata_quality",
        decision="approved",
        rationale="Synthetic corrected decision.",
        decision_payload=_payload(),
    )
    assert third.record_version == 3
    assert decisions.resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is not None
    assert ledger.history(first.collection, first.record_id)[0].ref == first


def test_new_related_gap_invalidates_existing_gate_decision(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    _, _, decisions, _, _ = _approve_source_synthetically(
        bundle, profile, ledger, acquisition
    )
    assert decisions.resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is not None

    observed_at = datetime.now(timezone.utc)
    gap = KnowledgeGap(
        gap_id="late-scope-gap",
        gap_kind="clinical_scope_missing",
        scope=profile.scope,
        subject_ref=acquisition.candidate_refs[0],
        reason="A later machine observation requires a new review packet.",
        blocks=("source_promotion", "knowledge_release"),
        observed_at=observed_at,
        synthetic=True,
    )
    ledger.append(
        LedgerCollection.GAP,
        gap.gap_id,
        payload_type="knowledge_gap",
        payload=gap,
        recorded_by="system:test",
        recorded_at=observed_at,
        synthetic=True,
    )

    assert decisions.resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is None


def test_new_packet_invalidates_events_on_older_packet(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    first_packet = _source_review_packet(bundle, profile, ledger, acquisition)
    second_packet = _source_review_packet(bundle, profile, ledger, acquisition)
    assert second_packet.record_version == 2
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=_reviewers("knowledge_curator"),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="stale Review Packet"):
        service.record(
            packet_ref=first_packet,
            reviewer_identity_id="synthetic-knowledge_curator",
            reviewer_role="knowledge_curator",
            axis="metadata_quality",
            decision="approved",
            rationale="Must not use stale packet.",
            decision_payload=_payload(),
        )


def test_review_service_rejects_role_axis_mismatch(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=_reviewers("rights_officer"),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="axis"):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id="synthetic-rights_officer",
            reviewer_role="rights_officer",
            axis="clinical",
            decision="approved",
            rationale="Invalid synthetic role/axis pair.",
            decision_payload=_payload(operation="register_link_metadata"),
        )


def test_multi_role_gate_requires_distinct_reviewer_identities(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    base_reviewer = _synthetic_reviewer("knowledge_curator")
    reviewer = ReviewerIdentity.model_validate(
        {
            **base_reviewer.model_dump(),
            "identity_id": "synthetic-multi-role",
            "principal_id": "synthetic-principal-multi-role",
            "display_name": "Synthetic multi-role fixture",
            "roles": ("knowledge_curator", "rights_officer"),
        }
    )
    directory = InMemoryReviewerDirectory((reviewer,))
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=directory,
    )
    service.record(
        packet_ref=packet_ref,
        reviewer_identity_id=reviewer.identity_id,
        reviewer_role="knowledge_curator",
        axis="metadata_quality",
        decision="approved",
        rationale="Synthetic curator check.",
        decision_payload=_payload(),
    )
    service.record(
        packet_ref=packet_ref,
        reviewer_identity_id=reviewer.identity_id,
        reviewer_role="rights_officer",
        axis="rights",
        decision="approved",
        rationale="Synthetic rights check.",
        decision_payload=_payload(operation="register_link_metadata"),
    )

    assert ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=directory
    ).resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is None


def test_multi_role_gate_rejects_distinct_accounts_for_same_principal(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    curator = _synthetic_reviewer("knowledge_curator")
    rights_base = _synthetic_reviewer("rights_officer")
    rights = ReviewerIdentity.model_validate(
        {
            **rights_base.model_dump(mode="python"),
            "principal_id": curator.principal_id,
        }
    )
    directory = InMemoryReviewerDirectory((curator, rights))
    service = ReviewEventService(
        bundle=bundle, ledger=ledger, reviewers=directory
    )
    service.record(
        packet_ref=packet_ref,
        reviewer_identity_id=curator.identity_id,
        reviewer_role="knowledge_curator",
        axis="metadata_quality",
        decision="approved",
        rationale="Synthetic curator account fixture.",
        decision_payload=_payload(),
    )
    service.record(
        packet_ref=packet_ref,
        reviewer_identity_id=rights.identity_id,
        reviewer_role="rights_officer",
        axis="rights",
        decision="approved",
        rationale="Synthetic second account for the same principal fixture.",
        decision_payload=_payload(operation="register_link_metadata"),
    )

    assert ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=directory
    ).resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is None


def test_unverified_real_identity_cannot_approve_or_resolve_direct_event(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    reviewer = ReviewerIdentity(
        identity_id="unverified-curator",
        principal_id="unverified-principal-curator",
        display_name="Unverified curator",
        roles=("knowledge_curator",),
        authorized_jurisdictions=profile.scope.jurisdictions,
        authorized_scopes=(profile.scope,),
        authorization_valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        authorization_valid_until=datetime(2100, 1, 1, tzinfo=timezone.utc),
        assurance="identity_unverified",
        synthetic=False,
    )
    rights = _synthetic_reviewer("rights_officer")
    directory = InMemoryReviewerDirectory((reviewer, rights))
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=directory,
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="cannot approve"):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id=reviewer.identity_id,
            reviewer_role="knowledge_curator",
            axis="metadata_quality",
            decision="approved",
            rationale="Unverified identity cannot approve.",
            decision_payload=_payload(),
        )

    revision_ref = service.record(
        packet_ref=packet_ref,
        reviewer_identity_id=reviewer.identity_id,
        reviewer_role="knowledge_curator",
        axis="metadata_quality",
        decision="revision_requested",
        rationale="Unverified reviewer may request revision but cannot approve.",
    )
    service.record(
        packet_ref=packet_ref,
        reviewer_identity_id=rights.identity_id,
        reviewer_role="rights_officer",
        axis="rights",
        decision="approved",
        rationale="Synthetic rights mechanism fixture.",
        decision_payload=_payload(operation="register_link_metadata"),
    )
    revision = ReviewEvent.model_validate(ledger.get(revision_ref).payload)
    timestamp = revision.decided_at + timedelta(microseconds=1)
    direct_payload = revision.model_dump(mode="json")
    direct_payload.update(
        {
            "event_id": review_module._event_id(revision_ref.record_id, timestamp),
            "decision": "approved",
            "decision_payload": _payload().model_dump(mode="json"),
            "rationale": "Direct signed unverified approval bypass probe.",
            "decided_at": timestamp.isoformat().replace("+00:00", "Z"),
            "expected_predecessor_sha256": revision_ref.entry_sha256,
            "counts_toward_release": False,
        }
    )
    claim_sha256 = review_module._review_event_claim_sha256(direct_payload)
    attestation = directory.issue_review_attestation(
        reviewer,
        role="knowledge_curator",
        scope=profile.scope,
        event_claim_sha256=claim_sha256,
        issued_at=timestamp,
    )
    assert attestation is not None
    direct_payload["review_attestation"] = attestation.model_dump(mode="json")
    direct_event = ReviewEvent.model_validate(direct_payload)
    ledger.append(
        LedgerCollection.REVIEW_EVENT,
        revision_ref.record_id,
        payload_type="review_event_v3",
        payload=direct_event,
        recorded_by=reviewer.identity_id,
        recorded_at=timestamp,
        synthetic=True,
    )

    assert ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=directory
    ).resolve_gate(
        acquisition.candidate_refs[0], GovernanceGate.SOURCE_PROMOTION
    ) is None


def test_formal_identity_contract_requires_auditable_verification_evidence():
    scope = load_builtin_ops_bundle().coverage_profiles[0].scope
    with pytest.raises(ValidationError, match="formal reviewer requires"):
        ReviewerIdentity(
            identity_id="not-a-formal-reviewer",
            principal_id="not-a-formal-principal",
            display_name="No formal reviewer available",
            roles=("clinical_reviewer",),
            authorized_jurisdictions=scope.jurisdictions,
            authorized_scopes=(scope,),
            authorization_valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            authorization_valid_until=datetime(2100, 1, 1, tzinfo=timezone.utc),
            assurance="formally_verified",
            synthetic=False,
        )


def test_readiness_directory_cannot_assert_a_formal_production_reviewer():
    scope = load_builtin_ops_bundle().coverage_profiles[0].scope
    reviewer = ReviewerIdentity(
        identity_id="formal-mechanism-fixture",
        principal_id="formal-mechanism-principal",
        display_name="Mechanism-only formal identity fixture",
        roles=("clinical_reviewer",),
        authorized_jurisdictions=scope.jurisdictions,
        authorized_scopes=(scope,),
        authorization_valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        authorization_valid_until=datetime(2100, 1, 1, tzinfo=timezone.utc),
        assurance="formally_verified",
        synthetic=False,
        verification_reference="urn:continucare:test:identity-proof",
        verification_evidence_sha256="1" * 64,
        verified_by="test:identity-authority",
        verified_at=datetime.now(timezone.utc),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="cannot assert formal"):
        InMemoryReviewerDirectory((reviewer,))


def test_decision_provider_requires_a_trusted_reviewer_verifier(tmp_path):
    bundle = load_builtin_ops_bundle()
    ledger = AppendOnlyLedger(tmp_path / "ledger")

    with pytest.raises(TypeError, match="reviewers"):
        ReviewLedgerDecisionProvider(bundle=bundle, ledger=ledger)


def test_fake_formal_event_appended_directly_cannot_resolve_gate(tmp_path):
    context = _formal_claim_review_context(tmp_path)
    initial = context["provider"].resolve_gate(
        context["claim_ref"], "clinical_claim_approval"
    )
    assert initial is not None
    assert initial.production_eligible is True

    _append_direct_formal_successor(
        context,
        context["clinical_ref"],
        counts_toward_release=True,
        valid_attestation=False,
    )

    assert context["provider"].resolve_gate(
        context["claim_ref"], "clinical_claim_approval"
    ) is None


def test_event_identity_fields_must_match_current_trusted_identity(tmp_path):
    context = _formal_claim_review_context(tmp_path)
    clinical = context["clinical"]
    changed = _updated_identity(
        clinical,
        verification_evidence_sha256=hashlib.sha256(
            b"changed-current-formal-evidence"
        ).hexdigest(),
    )
    context["verifier"].replace(changed)

    assert context["provider"].resolve_gate(
        context["claim_ref"], "clinical_claim_approval"
    ) is None


def test_current_inactive_reviewer_invalidates_prior_decision(tmp_path):
    context = _formal_claim_review_context(tmp_path)
    context["verifier"].replace(
        _updated_identity(context["clinical"], active=False)
    )

    assert context["provider"].resolve_gate(
        context["claim_ref"], "clinical_claim_approval"
    ) is None


def test_expired_reviewer_authorization_fails_closed_at_resolution(tmp_path):
    context = _formal_claim_review_context(tmp_path)
    expiration = context["clinical"].authorization_valid_until

    assert context["provider"].resolve_gate(
        context["claim_ref"],
        "clinical_claim_approval",
        evaluated_at=expiration,
    ) is None


def test_current_reviewer_role_mismatch_invalidates_prior_decision(tmp_path):
    context = _formal_claim_review_context(tmp_path)
    context["verifier"].replace(
        _updated_identity(context["clinical"], roles=("rights_officer",))
    )

    assert context["provider"].resolve_gate(
        context["claim_ref"], "clinical_claim_approval"
    ) is None


def test_current_reviewer_jurisdiction_and_scope_mismatch_fails_closed(tmp_path):
    context = _formal_claim_review_context(tmp_path)
    scope_payload = context["profile"].scope.model_dump(mode="python")
    other_scope = type(context["profile"].scope).model_validate(
        {
            **scope_payload,
            "jurisdictions": ({"system": "iso3166_1", "code": "US"},),
        }
    )
    context["verifier"].replace(
        _updated_identity(
            context["clinical"],
            authorized_jurisdictions=other_scope.jurisdictions,
            authorized_scopes=(other_scope,),
        )
    )

    assert context["provider"].resolve_gate(
        context["claim_ref"], "clinical_claim_approval"
    ) is None


def test_provider_recomputes_production_eligibility_and_ignores_event_count(tmp_path):
    context = _formal_claim_review_context(tmp_path)
    successor_ref = _append_direct_formal_successor(
        context,
        context["clinical_ref"],
        counts_toward_release=False,
        valid_attestation=True,
    )
    successor = ReviewEvent.model_validate(
        context["ledger"].get(successor_ref).payload
    )
    decision = context["provider"].resolve_gate(
        context["claim_ref"], "clinical_claim_approval"
    )

    assert successor.counts_toward_release is False
    assert decision is not None
    assert decision.production_eligible is True


def test_review_service_rejects_author_self_review(tmp_path):
    bundle = load_builtin_ops_bundle()
    profile = bundle.coverage_profiles[0]
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    reviewer = _synthetic_reviewer("clinical_reviewer")
    author = AuthorProvenance(
        author_identity_id=reviewer.identity_id,
        author_principal_id=reviewer.principal_id,
        authored_at=datetime.now(timezone.utc),
        provenance_reference="urn:continucare:test:self-review-probe",
        synthetic=True,
    )
    claim_ref = ledger.append(
        LedgerCollection.CLAIM,
        "synthetic-self-review-claim",
        payload_type="claim_review_mechanism_fixture",
        payload={
            "claim_id": "synthetic-self-review-claim",
            "author_provenance": author.model_dump(mode="json"),
        },
        recorded_by=author.author_identity_id,
        recorded_at=author.authored_at,
        synthetic=True,
    ).ref
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="clinical_claim",
        subject_ref=claim_ref,
        gate="clinical_claim_approval",
        scope=profile.scope,
        generated_by="test:packet-builder",
        known_limitations=("Self-review rejection mechanism fixture.",),
    )
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=InMemoryReviewerDirectory((reviewer,)),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="different identities"):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id=reviewer.identity_id,
            reviewer_role="clinical_reviewer",
            axis="clinical",
            decision="approved",
            rationale="Synthetic self-review attempt must fail.",
            decision_payload=_payload(scope=profile.scope),
        )
    assert ledger.list_heads(LedgerCollection.REVIEW_EVENT) == ()


def test_provider_rejects_signed_direct_ledger_author_self_review(tmp_path):
    context = _formal_claim_review_context(
        tmp_path,
        author_identity_id="formal-fixture-knowledge_curator",
        author_principal_id="formal-fixture-principal-knowledge_curator",
        record_curator=False,
    )
    direct_ref = _append_signed_formal_event_direct(
        context,
        identity=context["curator"],
        role="knowledge_curator",
        axis="metadata_quality",
        decision_payload=_formal_payload(),
    )
    assert ReviewEvent.model_validate(
        context["ledger"].get(direct_ref).payload
    ).counts_toward_release is True

    assert context["provider"].resolve_gate(
        context["claim_ref"], "clinical_claim_approval"
    ) is None


def test_rights_approval_requires_exact_operation_decisions(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=_reviewers("rights_officer"),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="explicitly approve"):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id="synthetic-rights_officer",
            reviewer_role="rights_officer",
            axis="rights",
            decision="approved",
            rationale="Missing exact operation decision.",
            decision_payload=_payload(),
        )
    with pytest.raises(KnowledgeOpsPolicyError, match="explicitly approve"):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id="synthetic-rights_officer",
            reviewer_role="rights_officer",
            axis="rights",
            decision="approved",
            rationale="Operation remains needs verification.",
            decision_payload=_payload(
                operation="register_link_metadata",
                operation_decision="needs_verification",
            ),
        )


def test_approved_checklist_cannot_contain_failure(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    packet_ref = _source_review_packet(bundle, profile, ledger, acquisition)
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=_reviewers("knowledge_curator"),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="no failures"):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id="synthetic-knowledge_curator",
            reviewer_role="knowledge_curator",
            axis="metadata_quality",
            decision="approved",
            rationale="Failing checklist cannot approve.",
            decision_payload=_payload(result="fail"),
        )


def test_synthetic_review_provider_drives_only_synthetic_source_promotion(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    _, _, _, source_ref, _ = _approve_source_synthetically(
        bundle, profile, ledger, acquisition
    )
    source = GovernedSourceV2.model_validate(ledger.get(source_ref).payload)

    assert source.registry_status == "synthetic_fixture"
    assert source.production_eligible is False
    assert source.synthetic is True
    assert source.runtime_authority == "none"
    assert source.unresolved_gap_refs == acquisition.gap_refs
    assert ledger.head(LedgerCollection.CLAIM, "any") is None
    assert ledger.head(LedgerCollection.BINDING, "any") is None


def _synthetic_release_context(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    reviewers = _reviewers(
        "knowledge_curator", "rights_officer", "clinical_reviewer", "pharmacist"
    )
    _, _, decisions, source_ref, _ = _approve_source_synthetically(
        bundle, profile, ledger, acquisition, reviewers
    )
    author = _release_author(
        synthetic=True, suffix="synthetic-release-candidate"
    )
    candidate = KnowledgeReleaseCandidate(
        release_candidate_id="synthetic-cn-zh-release",
        intended_uses=(
            "internal_knowledge_operations",
            "acquisition_basis_explanation",
            "informational_display",
        ),
        governance_bundle_id=bundle.index.bundle_id,
        governance_bundle_version=bundle.index.bundle_version,
        governance_index_sha256=bundle.index_sha256(),
        governance_manifests=_manifest_evidence(bundle),
        artifacts=(
            ReleaseArtifact(
                artifact_kind="source",
                object_ref=source_ref,
                validation_profile_id=profile.profile_id,
                scope=profile.scope,
            ),
        ),
        blocking_gap_refs=acquisition.gap_refs,
        created_at=author.authored_at,
        created_by=author.author_identity_id,
        author_provenance=author,
        synthetic=True,
    )
    readiness = ReleaseReadinessService(
        bundle=bundle, ledger=ledger, decisions=decisions
    )
    candidate_ref = readiness.stage_candidate(candidate)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="knowledge_release",
        subject_ref=candidate_ref,
        gate="knowledge_release",
        scope=profile.scope,
        generated_by="system:test",
        known_limitations=(
            "All artifacts and reviewers in this packet are synthetic fixtures.",
        ),
        additional_required_roles=("pharmacist",),
        open_gap_refs=acquisition.gap_refs,
    )
    event_service = ReviewEventService(
        bundle=bundle, ledger=ledger, reviewers=reviewers
    )
    reviews = (
        ("knowledge_curator", "release", _payload()),
        ("rights_officer", "release", _payload()),
        ("clinical_reviewer", "release", _payload(scope=profile.scope)),
        ("pharmacist", "applicability", _payload(scope=profile.scope)),
    )
    for role, axis, payload in reviews:
        event_service.record(
            packet_ref=packet_ref,
            reviewer_identity_id=f"synthetic-{role}",
            reviewer_role=role,
            axis=axis,
            decision="approved",
            rationale=f"Synthetic {role} release-readiness mechanism test.",
            decision_payload=payload,
        )
    return bundle, profile, ledger, acquisition, readiness, candidate_ref, source_ref


def test_release_readiness_blocks_synthetic_reviews_sources_and_open_gaps(tmp_path):
    _, _, ledger, acquisition, readiness, candidate_ref, source_ref = (
        _synthetic_release_context(tmp_path)
    )
    report_ref = readiness.assess(candidate_ref)
    report = ReleaseReadinessReport.model_validate(ledger.get(report_ref).payload)
    codes = {item.code for item in report.blockers}

    assert report.ready is False
    assert report.inspected_artifact_count == 1
    assert report.production_review_count == 0
    assert "synthetic_release_candidate" in codes
    assert "governance_release_intent_blocked" in codes
    assert "governance_readiness_gap_open" in codes
    assert "synthetic_artifact" in codes
    assert "nonproduction_source" in codes
    assert "artifact_review_nonproduction" in codes
    assert "open_blocking_gap" in codes
    assert "release_review_nonproduction" in codes
    assert "release_review_missing" not in codes
    assert "release_required_role_missing" not in codes
    assert len(
        [item for item in report.blockers if item.code == "open_blocking_gap"]
    ) == len(acquisition.gap_refs)
    assert any(item.subject_ref == source_ref for item in report.blockers)
    assert report.knowledge_effect == "informational_only"
    assert report.runtime_authority == "none"


def test_release_readiness_classifies_wrong_gap_payload_type_as_invalid(tmp_path):
    _, _, ledger, _, readiness, candidate_ref, _ = _synthetic_release_context(
        tmp_path
    )
    invalid_gap_ref = ledger.append(
        LedgerCollection.GAP,
        "wrong-gap-payload-type",
        payload_type="unexpected_gap_payload",
        payload={"synthetic": True},
        recorded_by="system:invalid-gap-fixture",
        synthetic=True,
    ).ref

    report_ref = readiness.assess(candidate_ref)
    report = ReleaseReadinessReport.model_validate(ledger.get(report_ref).payload)

    assert any(
        blocker.code == "invalid_gap_reference"
        and blocker.subject_ref == invalid_gap_ref
        for blocker in report.blockers
    )


def test_finalize_records_blocked_report_but_never_creates_release(tmp_path):
    _, _, ledger, _, readiness, candidate_ref, _ = _synthetic_release_context(tmp_path)

    with pytest.raises(KnowledgeReleaseBlocked) as captured:
        readiness.finalize(candidate_ref, finalized_by="system:test")

    report = ReleaseReadinessReport.model_validate(
        ledger.get(captured.value.readiness_report_ref).payload
    )
    assert report.ready is False
    assert ledger.head(LedgerCollection.RELEASE, "synthetic-cn-zh-release") is None


def test_empty_release_intent_is_explicitly_not_ready(tmp_path):
    bundle = load_builtin_ops_bundle()
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    decisions = ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=_reviewers()
    )
    readiness = ReleaseReadinessService(
        bundle=bundle, ledger=ledger, decisions=decisions
    )
    author = _release_author(synthetic=False, suffix="empty-release")
    candidate = KnowledgeReleaseCandidate(
        release_candidate_id="empty-release",
        intended_uses=("internal_knowledge_operations",),
        governance_bundle_id=bundle.index.bundle_id,
        governance_bundle_version=bundle.index.bundle_version,
        governance_index_sha256=bundle.index_sha256(),
        governance_manifests=_manifest_evidence(bundle),
        artifacts=(),
        blocking_gap_refs=(),
        created_at=author.authored_at,
        created_by=author.author_identity_id,
        author_provenance=author,
        synthetic=False,
    )
    candidate_ref = readiness.stage_candidate(candidate)
    report_ref = readiness.assess(candidate_ref)
    report = ReleaseReadinessReport.model_validate(ledger.get(report_ref).payload)

    assert report.ready is False
    assert {item.code for item in report.blockers} == {
        "governance_release_intent_blocked",
        "governance_readiness_gap_open",
        "empty_release",
        "release_review_missing",
    }
    assert len(
        [
            item
            for item in report.blockers
            if item.code == "governance_readiness_gap_open"
        ]
    ) == 11


def test_release_cannot_hide_gaps_carried_by_promoted_source(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    _, _, decisions, source_ref, _ = _approve_source_synthetically(
        bundle, profile, ledger, acquisition
    )
    readiness = ReleaseReadinessService(
        bundle=bundle, ledger=ledger, decisions=decisions
    )
    observed_at = datetime.now(timezone.utc)
    late_gap = KnowledgeGap(
        gap_id="late-release-gap",
        gap_kind="production_evidence_missing",
        scope=profile.scope,
        subject_ref=source_ref,
        reason="A later current-head Gap must be discovered independently.",
        blocks=("knowledge_release",),
        observed_at=observed_at,
        synthetic=True,
    )
    ledger.append(
        LedgerCollection.GAP,
        late_gap.gap_id,
        payload_type="knowledge_gap",
        payload=late_gap,
        recorded_by="system:test",
        recorded_at=observed_at,
        synthetic=True,
    )
    author = _release_author(synthetic=True, suffix="omitted-gap-release")
    candidate = KnowledgeReleaseCandidate(
        release_candidate_id="omitted-gap-release",
        intended_uses=("informational_display",),
        governance_bundle_id=bundle.index.bundle_id,
        governance_bundle_version=bundle.index.bundle_version,
        governance_index_sha256=bundle.index_sha256(),
        governance_manifests=_manifest_evidence(bundle),
        artifacts=(
            ReleaseArtifact(
                artifact_kind="source",
                object_ref=source_ref,
                validation_profile_id=profile.profile_id,
                scope=profile.scope,
            ),
        ),
        blocking_gap_refs=(),
        created_at=author.authored_at,
        created_by=author.author_identity_id,
        author_provenance=author,
        synthetic=True,
    )
    candidate_ref = readiness.stage_candidate(candidate)
    report = ReleaseReadinessReport.model_validate(
        ledger.get(readiness.assess(candidate_ref)).payload
    )

    assert len(
        [item for item in report.blockers if item.code == "open_blocking_gap"]
    ) == len(acquisition.gap_refs) + 1


def test_release_rejects_source_profile_substitution(tmp_path):
    bundle, profile, ledger, acquisition = _acquisition_context(tmp_path)
    _, _, decisions, source_ref, _ = _approve_source_synthetically(
        bundle, profile, ledger, acquisition
    )
    substituted_profile = next(
        item
        for item in bundle.coverage_profiles
        if item.profile_id == "fixture-acute-high-risk"
    )
    readiness = ReleaseReadinessService(
        bundle=bundle, ledger=ledger, decisions=decisions
    )
    author = _release_author(
        synthetic=True, suffix="profile-substitution-release"
    )
    candidate = KnowledgeReleaseCandidate(
        release_candidate_id="profile-substitution-release",
        intended_uses=("informational_display",),
        governance_bundle_id=bundle.index.bundle_id,
        governance_bundle_version=bundle.index.bundle_version,
        governance_index_sha256=bundle.index_sha256(),
        governance_manifests=_manifest_evidence(bundle),
        artifacts=(
            ReleaseArtifact(
                artifact_kind="source",
                object_ref=source_ref,
                validation_profile_id=substituted_profile.profile_id,
                scope=substituted_profile.scope,
            ),
        ),
        created_at=author.authored_at,
        created_by=author.author_identity_id,
        author_provenance=author,
        synthetic=True,
    )
    candidate_ref = readiness.stage_candidate(candidate)
    report = ReleaseReadinessReport.model_validate(
        ledger.get(readiness.assess(candidate_ref)).payload
    )

    assert "artifact_profile_scope_mismatch" in {
        blocker.code for blocker in report.blockers
    }


def test_release_stage_rejects_mismatched_governance_manifest_evidence(tmp_path):
    bundle = load_builtin_ops_bundle()
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    readiness = ReleaseReadinessService(
        bundle=bundle,
        ledger=ledger,
        decisions=ReviewLedgerDecisionProvider(
            bundle=bundle, ledger=ledger, reviewers=_reviewers()
        ),
    )
    evidence = list(_manifest_evidence(bundle))
    evidence[0] = {**evidence[0], "manifest_sha256": "0" * 64}
    author = _release_author(synthetic=False, suffix="manifest-mismatch")
    candidate = KnowledgeReleaseCandidate(
        release_candidate_id="manifest-mismatch",
        intended_uses=("internal_knowledge_operations",),
        governance_bundle_id=bundle.index.bundle_id,
        governance_bundle_version=bundle.index.bundle_version,
        governance_index_sha256=bundle.index_sha256(),
        governance_manifests=tuple(evidence),
        created_at=author.authored_at,
        created_by=author.author_identity_id,
        author_provenance=author,
        synthetic=False,
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="does not match"):
        readiness.stage_candidate(candidate)
    assert ledger.verify_all() == 0

    direct_ref = ledger.append(
        LedgerCollection.RELEASE_CANDIDATE,
        candidate.release_candidate_id,
        payload_type="knowledge_release_candidate",
        payload=candidate,
        recorded_by="system:test-bypass-attempt",
        synthetic=False,
    ).ref
    report = ReleaseReadinessReport.model_validate(
        ledger.get(readiness.assess(direct_ref)).payload
    )
    assert "governance_manifest_mismatch" in {
        blocker.code for blocker in report.blockers
    }


def test_release_candidate_personal_data_fails_stage_and_direct_bypass(tmp_path):
    bundle = load_builtin_ops_bundle()
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    decisions = ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=_reviewers()
    )
    readiness = ReleaseReadinessService(
        bundle=bundle,
        ledger=ledger,
        decisions=decisions,
    )
    author = _release_author(
        synthetic=True,
        suffix="personal-data-release",
        provenance_reference="Contact user@example.org for authorship evidence.",
    )
    candidate = KnowledgeReleaseCandidate(
        release_candidate_id="personal-data-release",
        intended_uses=("internal_knowledge_operations",),
        governance_bundle_id=bundle.index.bundle_id,
        governance_bundle_version=bundle.index.bundle_version,
        governance_index_sha256=bundle.index_sha256(),
        governance_manifests=_manifest_evidence(bundle),
        created_at=author.authored_at,
        created_by=author.author_identity_id,
        author_provenance=author,
        synthetic=True,
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="personal data"):
        readiness.stage_candidate(candidate)
    direct_ref = ledger.append(
        LedgerCollection.RELEASE_CANDIDATE,
        candidate.release_candidate_id,
        payload_type="knowledge_release_candidate",
        payload=candidate,
        recorded_by="system:test-bypass-attempt",
        synthetic=True,
    ).ref
    report = ReleaseReadinessReport.model_validate(
        ledger.get(readiness.assess(direct_ref)).payload
    )

    assert "patient_data_risk" in {blocker.code for blocker in report.blockers}


def test_synthetic_reviewer_cannot_attach_to_nonsynthetic_release_packet(tmp_path):
    bundle = load_builtin_ops_bundle()
    profile = bundle.coverage_profiles[0]
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    readiness = ReleaseReadinessService(
        bundle=bundle,
        ledger=ledger,
        decisions=ReviewLedgerDecisionProvider(
            bundle=bundle, ledger=ledger, reviewers=_reviewers()
        ),
    )
    author = _release_author(synthetic=False, suffix="nonsynthetic-empty")
    candidate = KnowledgeReleaseCandidate(
        release_candidate_id="nonsynthetic-empty",
        intended_uses=("internal_knowledge_operations",),
        governance_bundle_id=bundle.index.bundle_id,
        governance_bundle_version=bundle.index.bundle_version,
        governance_index_sha256=bundle.index_sha256(),
        governance_manifests=_manifest_evidence(bundle),
        created_at=author.authored_at,
        created_by=author.author_identity_id,
        author_provenance=author,
        synthetic=False,
    )
    candidate_ref = readiness.stage_candidate(candidate)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="knowledge_release",
        subject_ref=candidate_ref,
        gate="knowledge_release",
        scope=profile.scope,
        generated_by="system:test",
        known_limitations=("No artifacts and no formal reviewers.",),
    )
    service = ReviewEventService(
        bundle=bundle,
        ledger=ledger,
        reviewers=_reviewers("knowledge_curator"),
    )

    with pytest.raises(KnowledgeOpsPolicyError, match="non-synthetic subject"):
        service.record(
            packet_ref=packet_ref,
            reviewer_identity_id="synthetic-knowledge_curator",
            reviewer_role="knowledge_curator",
            axis="release",
            decision="approved",
            rationale="Synthetic identity must not review real release subject.",
            decision_payload=_payload(),
        )


def test_builtin_release_intent_contains_no_fake_approval_or_artifact():
    bundle = load_builtin_ops_bundle()
    read_model = load_builtin_ops_read_model()

    assert bundle.release_intent.status == "readiness_only_blocked"
    assert bundle.release_intent.selected_artifact_count == 0
    assert bundle.release_intent.formal_reviewers_available is False
    assert bundle.release_intent.formal_license_decisions_available is False
    assert bundle.release_intent.release_ready is False
    assert bundle.release_intent.knowledge_effect == "informational_only"
    assert bundle.release_intent.runtime_authority == "none"
    assert read_model.release_intent == bundle.release_intent
    assert read_model.production_releases == ()


def test_release_contract_rejects_runtime_authority_and_clinical_rules():
    bundle = load_builtin_ops_bundle()
    authored_at = datetime.now(timezone.utc)
    payload = {
        "release_candidate_id": "invalid-runtime-release",
        "intended_uses": ["informational_display"],
        "governance_bundle_id": bundle.index.bundle_id,
        "governance_bundle_version": bundle.index.bundle_version,
        "governance_index_sha256": bundle.index_sha256(),
        "governance_manifests": _manifest_evidence(bundle),
        "artifacts": [],
        "blocking_gap_refs": [],
        "created_at": authored_at.isoformat(),
        "created_by": "runtime-safety-author",
        "author_provenance": {
            "author_identity_id": "runtime-safety-author",
            "author_principal_id": "runtime-safety-author-principal",
            "authored_at": authored_at.isoformat(),
            "provenance_reference": "urn:continucare:test:runtime-safety-author",
            "synthetic": False,
        },
        "synthetic": False,
        "runtime_authority": "clinical",
    }
    with pytest.raises(ValidationError) as runtime_error:
        KnowledgeReleaseCandidate.model_validate(payload)
    assert "runtime_authority" in str(runtime_error.value)
    payload["runtime_authority"] = "none"
    payload["clinical_rule_refs"] = [None]
    with pytest.raises(ValidationError) as clinical_rule_error:
        KnowledgeReleaseCandidate.model_validate(payload)
    assert "clinical_rule_refs" in str(clinical_rule_error.value)


def test_readiness_report_cannot_claim_ready_with_blockers():
    with pytest.raises(ValidationError, match="exactly when blockers are empty"):
        ReleaseReadinessReport.model_validate(
            {
                "report_id": "invalid-report",
                "release_candidate_ref": {
                    "collection": "release_candidate",
                    "record_id": "candidate",
                    "record_version": 1,
                    "entry_sha256": "0" * 64,
                },
                "ready": True,
                "blockers": [
                    {
                        "code": ReadinessBlockerCode.EMPTY_RELEASE,
                        "message": "Blocked.",
                    }
                ],
                "assessed_at": datetime.now(timezone.utc).isoformat(),
                "assessed_by": "system:test",
                "inspected_artifact_count": 0,
                "production_review_count": 0,
            }
        )

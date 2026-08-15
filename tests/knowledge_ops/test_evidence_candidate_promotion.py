from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from continucare.knowledge.models import PayloadEnvelope as V1PayloadEnvelope
from continucare.knowledge.ops import (
    AcquisitionRequest,
    AcquisitionService,
    AppendOnlyLedger,
    AuthorProvenance,
    EvidenceCandidate,
    EvidenceCandidatePromotionService,
    EvidenceCandidateService,
    EvidenceDerivationProvenance,
    EvidenceLimitationCode,
    InMemoryReviewerDirectory,
    KnowledgeOpsIntegrityError,
    LedgerCollection,
    LedgerRef,
    MachineDraftClaim,
    OfflineFixtureConnector,
    QuarantineBlobStore,
    ReviewAxis,
    ReviewCheck,
    ReviewDecision,
    ReviewDecisionPayload,
    ReviewEvent,
    ReviewEventService,
    ReviewLedgerDecisionProvider,
    ReviewPacketBuilder,
    ReviewerIdentity,
    ReviewSubjectKind,
    SourceCandidate,
    SourceSnapshot,
    load_builtin_ops_bundle,
)
from continucare.knowledge.ops.models import (
    GovernanceGate,
    KnowledgeOpsPolicyError,
)


FIXTURE_CATALOG_SHA256 = (
    "e711994018bb783236e050d890783502eaee91e7345e4d4e9808fdf542764a3f"
)
NOW = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
LIMITATIONS = (
    EvidenceLimitationCode.SYNTHETIC_FIXTURE_ONLY,
    EvidenceLimitationCode.RIGHTS_UNRESOLVED,
    EvidenceLimitationCode.METADATA_ONLY,
    EvidenceLimitationCode.CLINICAL_INTERPRETATION_UNREVIEWED,
)
PHONE_BEARING_HEX64 = "a" * 8 + "13800138000" + "b" * 45
VERIFIED_DIGEST_PAYLOAD = b"verified-digest-fixture-318"
VERIFIED_DIGEST_SHA256 = (
    "0801d1a49c36a5ca5dff318001ec48a16ebcb2603b0bbc3ab17707581220db7b"
)


def _fixture_root() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "knowledge_ops"


def _acquire(tmp_path: Path):
    bundle = load_builtin_ops_bundle()
    profile = next(
        item
        for item in bundle.coverage_profiles
        if item.profile_id == "fixture-medication-followup"
    )
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    service = AcquisitionService(
        bundle=bundle,
        ledger=ledger,
        quarantine=QuarantineBlobStore(tmp_path / "quarantine"),
        connector=OfflineFixtureConnector(
            _fixture_root(), catalog_sha256=FIXTURE_CATALOG_SHA256
        ),
        environment="synthetic_test",
    )
    result = service.run(
        AcquisitionRequest(
            request_id="evidence-promotion-fixture",
            validation_profile_id=profile.profile_id,
            trigger="scheduled",
            policy_ids=("nmpa-cn-regulatory-metadata",),
            topic_codes=(
                {
                    "system": "urn:continucare:synthetic-topic",
                    "version": "1",
                    "code": "medication-followup",
                },
            ),
            query_terms=("medication followup",),
            scope=profile.scope,
            created_at=NOW,
            created_by="system:synthetic-acquisition",
        )
    )
    return bundle, profile, ledger, result


def _author(*, identity: str = "synthetic-machine-author", principal: str = "synthetic-author-principal"):
    return AuthorProvenance(
        author_identity_id=identity,
        author_principal_id=principal,
        authored_at=NOW + timedelta(minutes=1),
        provenance_reference="urn:continucare:synthetic:machine-derivation",
        provenance_evidence_sha256="a" * 64,
        synthetic=True,
    )


def _ledger_state(ledger: AppendOnlyLedger):
    return (
        ledger.verify_all(),
        tuple(
            (
                collection.value,
                tuple(entry.ref for entry in ledger.list_heads(collection)),
            )
            for collection in LedgerCollection
        ),
    )


def _stage(tmp_path: Path):
    bundle, profile, ledger, acquisition = _acquire(tmp_path)
    author = _author()
    evidence_ref = EvidenceCandidateService(bundle=bundle, ledger=ledger).stage(
        candidate_id="evc-medication-nausea-v1",
        source_candidate_ref=acquisition.candidate_refs[0],
        source_snapshot_ref=acquisition.snapshot_refs[0],
        connector_version="1.0.0",
        parser_id="synthetic-metadata-record-parser",
        parser_version="1.0.0",
        benchmark_symptom_refs=("core-symptom-nausea",),
        proposed_knowledge_layer="L1_terminology",
        proposed_claim_type="metadata-support-candidate",
        proposed_scope=profile.scope,
        derivation_provenance=EvidenceDerivationProvenance(
            fixture_set_id="knowledge-ops-five-domain-fixtures",
            extraction_profile_id="synthetic-metadata-only-v1",
            selected_metadata_fields=("stable_source_key", "document_version"),
        ),
        known_limitation_codes=LIMITATIONS,
        author_provenance=author,
        recorded_by=author.author_identity_id,
        recorded_at=NOW + timedelta(minutes=2),
    )
    return bundle, profile, ledger, acquisition, author, evidence_ref


def _promote(tmp_path: Path):
    bundle, profile, ledger, acquisition, author, evidence_ref = _stage(tmp_path)
    claim_ref = EvidenceCandidatePromotionService(ledger=ledger).promote_to_draft_claim(
        evidence_candidate_ref=evidence_ref,
        claim_id="dcl-medication-nausea-v1",
        author_provenance=author.model_copy(
            update={"authored_at": NOW + timedelta(minutes=3)}
        ),
        created_by=author.author_identity_id,
        created_at=NOW + timedelta(minutes=4),
    )
    return bundle, profile, ledger, acquisition, author, evidence_ref, claim_ref


def _reviewer(profile, *, identity="synthetic-clinical-reviewer", principal="synthetic-reviewer-principal"):
    return ReviewerIdentity(
        identity_id=identity,
        principal_id=principal,
        display_name="Synthetic independent clinical reviewer",
        roles=("clinical_reviewer",),
        authorized_jurisdictions=profile.scope.jurisdictions,
        authorized_scopes=(profile.scope,),
        authorization_valid_from=NOW - timedelta(days=1),
        authorization_valid_until=NOW + timedelta(days=1),
        assurance="synthetic_test",
        synthetic=True,
    )


def test_evidence_candidate_is_independently_typed_and_digest_only(tmp_path: Path) -> None:
    _bundle, _profile, ledger, acquisition, _author_value, ref = _stage(tmp_path)
    entry = ledger.get(ref)
    candidate = EvidenceCandidate.model_validate(entry.payload)

    assert ref.collection == LedgerCollection.EVIDENCE_CANDIDATE
    assert entry.payload_type == "evidence_candidate_v2"
    assert candidate.record_type == "evidence_candidate"
    assert candidate.candidate_id.startswith("evc-")
    assert candidate.whole_record_sha256 == ledger.get(
        acquisition.snapshot_refs[0]
    ).payload["content_sha256"]
    assert candidate.contains_source_text is False
    assert candidate.derivation_provenance.fingerprint_policy == "whole_record_digest_only"
    assert candidate.derivation_provenance.reconstructive_fingerprints_stored is False
    assert not any(
        key in entry.payload
        for key in ("snippet", "quote", "source_text", "token_hashes", "ngrams", "length_distribution")
    )

    with pytest.raises(ValidationError):
        EvidenceCandidate.model_validate({**entry.payload, "snippet": "forbidden"})


def test_verified_phone_bearing_snapshot_digest_survives_machine_lineage(
    tmp_path: Path,
) -> None:
    bundle, profile, ledger, acquisition = _acquire(tmp_path)
    digest = hashlib.sha256(VERIFIED_DIGEST_PAYLOAD).hexdigest()
    assert digest == VERIFIED_DIGEST_SHA256
    snapshot = SourceSnapshot.model_validate(
        ledger.get(acquisition.snapshot_refs[0]).payload
    )
    assert snapshot.quarantine_blob is not None
    verified_snapshot = snapshot.model_copy(
        update={
            "content_sha256": digest,
            "quarantine_blob": snapshot.quarantine_blob.model_copy(
                update={
                    "content_sha256": digest,
                    "relative_path": f"blobs/{digest}.bin",
                }
            ),
        }
    )
    snapshot_ref = ledger.append(
        LedgerCollection.SNAPSHOT,
        snapshot.snapshot_id,
        payload_type="source_snapshot",
        payload=verified_snapshot,
        recorded_by="system:verified-digest-fixture",
        recorded_at=NOW + timedelta(minutes=1),
        synthetic=True,
        expected_record_version=2,
    ).ref
    author = _author()
    evidence_ref = EvidenceCandidateService(bundle=bundle, ledger=ledger).stage(
        candidate_id="evc-phone-bearing-derived-digest",
        source_candidate_ref=acquisition.candidate_refs[0],
        source_snapshot_ref=snapshot_ref,
        connector_version="1.0.0",
        parser_id="synthetic-metadata-record-parser",
        parser_version="1.0.0",
        benchmark_symptom_refs=("core-symptom-nausea",),
        proposed_knowledge_layer="L1_terminology",
        proposed_claim_type="metadata-support-candidate",
        proposed_scope=profile.scope,
        derivation_provenance=EvidenceDerivationProvenance(
            fixture_set_id="knowledge-ops-five-domain-fixtures",
            extraction_profile_id="synthetic-metadata-only-v1",
            selected_metadata_fields=("stable_source_key", "document_version"),
        ),
        known_limitation_codes=LIMITATIONS,
        author_provenance=author,
        recorded_by=author.author_identity_id,
        recorded_at=NOW + timedelta(minutes=2),
    )
    candidate = EvidenceCandidate.model_validate(ledger.get(evidence_ref).payload)
    assert candidate.whole_record_sha256 == digest

    claim_ref = EvidenceCandidatePromotionService(
        ledger=ledger
    ).promote_to_draft_claim(
        evidence_candidate_ref=evidence_ref,
        claim_id="dcl-phone-bearing-derived-digest",
        author_provenance=author.model_copy(
            update={"authored_at": NOW + timedelta(minutes=3)}
        ),
        created_by=author.author_identity_id,
        created_at=NOW + timedelta(minutes=4),
    )
    assert MachineDraftClaim.model_validate(ledger.get(claim_ref).payload)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="clinical_claim",
        subject_ref=claim_ref,
        gate="clinical_claim_approval",
        scope=profile.scope,
        generated_by="system:test",
        known_limitations=("Synthetic digest trust regression.",),
        evidence_refs=(evidence_ref,),
        generated_at=NOW + timedelta(minutes=5),
    )
    assert ledger.get(packet_ref).payload_type == "review_packet"


def test_digest_profile_does_not_trust_an_unreplayable_nested_ledger_ref(
    tmp_path: Path,
) -> None:
    _bundle, _profile, ledger, _acquisition, author, evidence_ref = _stage(tmp_path)
    candidate = EvidenceCandidate.model_validate(ledger.get(evidence_ref).payload)
    digest = hashlib.sha256(VERIFIED_DIGEST_PAYLOAD).hexdigest()
    assert digest == VERIFIED_DIGEST_SHA256
    forged = candidate.model_copy(
        update={
            "candidate_version": 2,
            "source_snapshot_ref": LedgerRef(
                collection=LedgerCollection.SNAPSHOT,
                record_id="missing-phone-bearing-snapshot",
                record_version=1,
                entry_sha256=digest,
            ),
        }
    )
    forged_ref = ledger.append(
        LedgerCollection.EVIDENCE_CANDIDATE,
        candidate.candidate_id,
        payload_type="evidence_candidate_v2",
        payload=forged,
        recorded_by=author.author_identity_id,
        recorded_at=NOW + timedelta(minutes=8),
        synthetic=True,
        expected_record_version=2,
    ).ref
    before = _ledger_state(ledger)

    with pytest.raises(KnowledgeOpsIntegrityError, match="unknown"):
        EvidenceCandidatePromotionService(ledger=ledger).promote_to_draft_claim(
            evidence_candidate_ref=forged_ref,
            claim_id="dcl-unreplayable-nested-ref",
            author_provenance=author.model_copy(
                update={"authored_at": NOW + timedelta(minutes=9)}
            ),
            created_by=author.author_identity_id,
            created_at=NOW + timedelta(minutes=10),
        )

    assert _ledger_state(ledger) == before


@pytest.mark.parametrize(
    "field",
    ["document_sha256", "entry_sha256", "attestation_sha256"],
)
def test_source_candidate_open_metadata_never_inherits_digest_trust(
    tmp_path: Path,
    field: str,
) -> None:
    _bundle, _profile, ledger, acquisition = _acquire(tmp_path)
    before = _ledger_state(ledger)
    payload = dict(ledger.get(acquisition.candidate_refs[0]).payload)
    payload["metadata"] = {field: PHONE_BEARING_HEX64}

    with pytest.raises(
        KnowledgeOpsPolicyError, match="appears to contain personal data"
    ):
        SourceCandidate.model_validate(payload)

    assert _ledger_state(ledger) == before


def test_unbound_author_provenance_digest_is_rejected_before_stage_write(
    tmp_path: Path,
) -> None:
    bundle, profile, ledger, acquisition = _acquire(tmp_path)
    author = _author().model_copy(
        update={"provenance_evidence_sha256": PHONE_BEARING_HEX64}
    )
    before = _ledger_state(ledger)

    with pytest.raises(
        KnowledgeOpsPolicyError, match="appears to contain personal data"
    ):
        EvidenceCandidateService(bundle=bundle, ledger=ledger).stage(
            candidate_id="evc-untrusted-provenance-digest",
            source_candidate_ref=acquisition.candidate_refs[0],
            source_snapshot_ref=acquisition.snapshot_refs[0],
            connector_version="1.0.0",
            parser_id="synthetic-metadata-record-parser",
            parser_version="1.0.0",
            benchmark_symptom_refs=("core-symptom-nausea",),
            proposed_knowledge_layer="L1_terminology",
            proposed_claim_type="metadata-support-candidate",
            proposed_scope=profile.scope,
            derivation_provenance=EvidenceDerivationProvenance(
                fixture_set_id="knowledge-ops-five-domain-fixtures",
                extraction_profile_id="synthetic-metadata-only-v1",
                selected_metadata_fields=("stable_source_key", "document_version"),
            ),
            known_limitation_codes=LIMITATIONS,
            author_provenance=author,
            recorded_by=author.author_identity_id,
            recorded_at=NOW + timedelta(minutes=2),
        )

    assert _ledger_state(ledger) == before


def test_evidence_candidate_revisions_are_contiguous_append_only_history(
    tmp_path: Path,
) -> None:
    bundle, _profile, ledger, _acquisition, author, first_ref = _stage(tmp_path)
    first = EvidenceCandidate.model_validate(ledger.get(first_ref).payload)
    second_ref = EvidenceCandidateService(bundle=bundle, ledger=ledger).stage(
        candidate_id=first.candidate_id,
        source_candidate_ref=first.source_candidate_ref,
        source_snapshot_ref=first.source_snapshot_ref,
        connector_version=first.connector_version,
        parser_id=first.parser_id,
        parser_version=first.parser_version,
        benchmark_symptom_refs=first.benchmark_symptom_refs,
        proposed_knowledge_layer=first.proposed_knowledge_layer,
        proposed_claim_type=first.proposed_claim_type,
        proposed_scope=first.proposed_scope,
        derivation_provenance=first.derivation_provenance,
        known_limitation_codes=first.known_limitation_codes,
        author_provenance=author,
        recorded_by=author.author_identity_id,
        recorded_at=NOW + timedelta(minutes=8),
    )
    history = ledger.history(LedgerCollection.EVIDENCE_CANDIDATE, first.candidate_id)
    assert second_ref.record_version == 2
    assert EvidenceCandidate.model_validate(history[1].payload).candidate_version == 2
    assert history[1].supersedes_entry_sha256 == history[0].entry_sha256


def test_evidence_candidate_collection_type_record_type_and_id_are_all_enforced(
    tmp_path: Path,
) -> None:
    _bundle, _profile, ledger, _acquisition, _author_value, ref = _stage(tmp_path)
    candidate = ledger.get(ref).payload

    with pytest.raises(KnowledgeOpsPolicyError, match="only be stored"):
        ledger.append(
            LedgerCollection.CLAIM,
            ref.record_id,
            payload_type="evidence_candidate_v2",
            payload=candidate,
            recorded_by="system:test",
            synthetic=True,
        )
    with pytest.raises(ValidationError):
        EvidenceCandidate.model_validate({**candidate, "record_type": "claim"})
    with pytest.raises(ValidationError):
        EvidenceCandidate.model_validate({**candidate, "candidate_id": "dcl-wrong-namespace"})
    with pytest.raises(KnowledgeOpsPolicyError, match="only be stored in CLAIM"):
        ledger.append(
            LedgerCollection.EVIDENCE_CANDIDATE,
            "evc-wrong-payload",
            payload_type="machine_draft_claim_v2",
            payload={"record_type": "machine_draft_claim", "claim_id": "dcl-wrong", "claim_version": 1},
            recorded_by="system:test",
            synthetic=True,
        )


def test_evidence_candidate_cannot_be_sent_directly_to_claim_reviewer_or_v1_loader(
    tmp_path: Path,
) -> None:
    bundle, profile, ledger, _acquisition, _author_value, ref = _stage(tmp_path)
    with pytest.raises(KnowledgeOpsPolicyError, match="promoted"):
        ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
            subject_kind=ReviewSubjectKind.CLINICAL_CLAIM,
            subject_ref=ref,
            gate=GovernanceGate.CLINICAL_CLAIM_APPROVAL,
            scope=profile.scope,
            generated_by="system:test",
            known_limitations=("Synthetic EvidenceCandidate is not a Claim.",),
        )

    with pytest.raises(ValidationError):
        TypeAdapter(V1PayloadEnvelope).validate_python(ledger.get(ref).payload)


def test_promotion_creates_only_machine_authored_synthetic_draft_claim(tmp_path: Path) -> None:
    _bundle, _profile, ledger, _acquisition, _author_value, evidence_ref, claim_ref = _promote(tmp_path)
    entry = ledger.get(claim_ref)
    claim = MachineDraftClaim.model_validate(entry.payload)

    assert claim_ref.collection == LedgerCollection.CLAIM
    assert entry.payload_type == "machine_draft_claim_v2"
    assert claim.record_type == "machine_draft_claim"
    assert claim.lifecycle == "draft"
    assert claim.machine_generated is True
    assert claim.evidence_candidate_ref == evidence_ref
    assert claim.synthetic is True
    assert claim.formal_citation_count == 0
    assert claim.binding_created is False
    assert claim.clinical_rule_created is False
    assert claim.patient_content_created is False
    assert claim.production_eligible is False
    assert claim.release_ready is False
    assert claim.knowledge_effect == "informational_only"
    assert claim.runtime_authority == "none"


def test_synthetic_lineage_cannot_be_laundered_by_successor(tmp_path: Path) -> None:
    _bundle, _profile, ledger, _acquisition, _author_value, _evidence_ref, claim_ref = _promote(tmp_path)
    payload = dict(ledger.get(claim_ref).payload)
    payload.update({"claim_version": 2, "synthetic": False})
    payload["author_provenance"] = {
        **payload["author_provenance"],
        "synthetic": False,
    }
    with pytest.raises(KnowledgeOpsPolicyError, match="cannot be changed"):
        ledger.append(
            LedgerCollection.CLAIM,
            claim_ref.record_id,
            payload_type="machine_draft_claim_v2",
            payload=payload,
            recorded_by="system:laundering-probe",
            synthetic=False,
            expected_record_version=2,
        )

    payload["synthetic"] = False
    with pytest.raises(KnowledgeOpsPolicyError, match="synthetic status"):
        ledger.append(
            LedgerCollection.CLAIM,
            claim_ref.record_id,
            payload_type="machine_draft_claim_v2",
            payload=payload,
            recorded_by="system:payload-laundering-probe",
            synthetic=True,
            expected_record_version=2,
        )

    payload["synthetic"] = True
    with pytest.raises(KnowledgeOpsPolicyError, match="author provenance"):
        ledger.append(
            LedgerCollection.CLAIM,
            claim_ref.record_id,
            payload_type="machine_draft_claim_v2",
            payload=payload,
            recorded_by="system:author-laundering-probe",
            synthetic=True,
            expected_record_version=2,
        )


def test_machine_draft_record_type_cannot_hide_behind_another_payload_type(
    tmp_path: Path,
) -> None:
    ledger = AppendOnlyLedger(tmp_path / "ledger")
    with pytest.raises(KnowledgeOpsPolicyError, match="requires machine_draft_claim_v2"):
        ledger.append(
            LedgerCollection.CLAIM,
            "dcl-type-confusion",
            payload_type="other_claim_type",
            payload={
                "record_type": "machine_draft_claim",
                "claim_id": "dcl-type-confusion",
                "claim_version": 1,
            },
            recorded_by="system:type-confusion-probe",
            synthetic=True,
        )


def test_synthetic_review_uses_same_packet_event_verifier_and_attestation_path(
    tmp_path: Path,
) -> None:
    bundle, profile, ledger, _acquisition, author, evidence_ref, claim_ref = _promote(tmp_path)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind=ReviewSubjectKind.CLINICAL_CLAIM,
        subject_ref=claim_ref,
        gate=GovernanceGate.CLINICAL_CLAIM_APPROVAL,
        scope=profile.scope,
        generated_by="system:synthetic-review-ops",
        known_limitations=(
            "Synthetic fixture only; no formal clinical approval or release authority.",
        ),
        evidence_refs=(evidence_ref,),
        generated_at=NOW + timedelta(minutes=5),
    )
    reviewer = _reviewer(profile)
    assert reviewer.identity_id != author.author_identity_id
    assert reviewer.principal_id != author.author_principal_id
    directory = InMemoryReviewerDirectory((reviewer,))
    event_ref = ReviewEventService(
        bundle=bundle, ledger=ledger, reviewers=directory
    ).record(
        packet_ref=packet_ref,
        reviewer_identity_id=reviewer.identity_id,
        reviewer_role="clinical_reviewer",
        axis=ReviewAxis.CLINICAL,
        decision=ReviewDecision.APPROVED,
        rationale="Synthetic path validation only; not a production clinical decision.",
        decision_payload=ReviewDecisionPayload(
            checklist=(
                ReviewCheck(
                    check_id="synthetic-structure-check",
                    result="pass",
                    evidence_refs=(evidence_ref,),
                    note="Structured synthetic fixture lineage is internally consistent.",
                ),
            ),
            confirmed_scope=profile.scope,
            limitations=("Synthetic approval cannot count toward release.",),
        ),
        decided_at=NOW + timedelta(minutes=6),
    )
    event = ReviewEvent.model_validate(ledger.get(event_ref).payload)
    assert event.review_attestation.synthetic is True
    assert event.review_attestation.event_claim_sha256
    assert event.counts_toward_release is False
    assert event.synthetic is True
    assert event.runtime_authority == "none"
    assert ReviewLedgerDecisionProvider(
        bundle=bundle, ledger=ledger, reviewers=directory
    ).resolve_gate(
        claim_ref,
        GovernanceGate.CLINICAL_CLAIM_APPROVAL,
        evaluated_at=NOW + timedelta(minutes=7),
    ) is None


@pytest.mark.parametrize(
    ("identity", "principal"),
    [
        ("synthetic-machine-author", "other-principal"),
        ("other-reviewer-account", "synthetic-author-principal"),
    ],
)
def test_same_author_identity_or_principal_is_rejected(
    tmp_path: Path, identity: str, principal: str
) -> None:
    bundle, profile, ledger, _acquisition, _author_value, evidence_ref, claim_ref = _promote(tmp_path)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="clinical_claim",
        subject_ref=claim_ref,
        gate="clinical_claim_approval",
        scope=profile.scope,
        generated_by="system:test",
        known_limitations=("Synthetic separation test.",),
        evidence_refs=(evidence_ref,),
        generated_at=NOW + timedelta(minutes=5),
    )
    reviewer = _reviewer(profile, identity=identity, principal=principal)
    with pytest.raises(KnowledgeOpsPolicyError, match="different identities and principals"):
        ReviewEventService(
            bundle=bundle,
            ledger=ledger,
            reviewers=InMemoryReviewerDirectory((reviewer,)),
        ).record(
            packet_ref=packet_ref,
            reviewer_identity_id=reviewer.identity_id,
            reviewer_role="clinical_reviewer",
            axis="clinical",
            decision="in_review",
            rationale="Author/reviewer separation probe.",
            decided_at=NOW + timedelta(minutes=6),
        )


def test_forged_formal_event_and_formal_in_memory_reviewer_are_rejected(tmp_path: Path) -> None:
    bundle, profile, ledger, _acquisition, _author_value, evidence_ref, claim_ref = _promote(tmp_path)
    packet_ref = ReviewPacketBuilder(bundle=bundle, ledger=ledger).build(
        subject_kind="clinical_claim",
        subject_ref=claim_ref,
        gate="clinical_claim_approval",
        scope=profile.scope,
        generated_by="system:test",
        known_limitations=("Synthetic formal-forgery test.",),
        evidence_refs=(evidence_ref,),
        generated_at=NOW + timedelta(minutes=5),
    )
    reviewer = _reviewer(profile)
    directory = InMemoryReviewerDirectory((reviewer,))
    event_ref = ReviewEventService(
        bundle=bundle, ledger=ledger, reviewers=directory
    ).record(
        packet_ref=packet_ref,
        reviewer_identity_id=reviewer.identity_id,
        reviewer_role="clinical_reviewer",
        axis="clinical",
        decision="in_review",
        rationale="Synthetic event used only for forgery regression.",
        decided_at=NOW + timedelta(minutes=6),
    )
    forged = dict(ledger.get(event_ref).payload)
    forged.update(
        {
            "synthetic": False,
            "reviewer_synthetic": False,
            "reviewer_assurance": "formally_verified",
            "counts_toward_release": True,
        }
    )
    with pytest.raises(ValidationError):
        ReviewEvent.model_validate(forged)

    with pytest.raises(KnowledgeOpsPolicyError, match="cannot assert formal"):
        InMemoryReviewerDirectory(
            (
                reviewer.model_copy(
                    update={
                        "assurance": "formally_verified",
                        "synthetic": False,
                        "verification_reference": "urn:forged",
                        "verification_evidence_sha256": "f" * 64,
                        "verified_by": "forged",
                        "verified_at": NOW,
                    }
                ),
            )
        )

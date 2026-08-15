"""Quarantined, offline-first source acquisition and change staging."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from continucare.knowledge.ops.connectors import (
    AcquisitionConnector,
    FetchedDocument,
    OfflineFixtureConnector,
)
from continucare.knowledge.ops.manifests import KnowledgeOpsBundle
from continucare.knowledge.ops.models import (
    ClinicalContextScope,
    Jurisdiction,
    KnowledgeOpsIntegrityError,
    KnowledgeOpsPolicyError,
    LanguageCode,
    NonBlank,
    PolicyDecision,
    SafeId,
    Sha256,
    SourceOperation,
    SourcePolicyRef,
    StrictModel,
    safe_relative_parts,
)
from continucare.knowledge.ops.security import (
    DigestTrustProfile,
    assert_deidentified_query_terms,
    assert_no_sensitive_data,
    digest_derived_internal_id,
    validate_url_against_policy,
)
from continucare.knowledge.ops.store import (
    AppendOnlyLedger,
    LedgerCollection,
    LedgerRef,
)


class AcquisitionEnvironment(StrEnum):
    SYNTHETIC_TEST = "synthetic_test"
    PRODUCTION = "production"


class AcquisitionTrigger(StrEnum):
    SCHEDULED = "scheduled"
    COVERAGE_GAP = "coverage_gap"
    CURATOR_REQUEST = "curator_request"


class NormalizedTopicCode(StrictModel):
    system: NonBlank
    version: NonBlank
    code: NonBlank
    display: NonBlank | None = None


class AcquisitionRequest(StrictModel):
    request_id: SafeId
    validation_profile_id: SafeId
    trigger: AcquisitionTrigger
    policy_ids: tuple[SafeId, ...] = Field(min_length=1)
    topic_codes: tuple[NormalizedTopicCode, ...] = Field(min_length=1)
    query_terms: tuple[NonBlank, ...] = Field(min_length=1)
    scope: ClinicalContextScope
    created_at: datetime
    created_by: NonBlank
    contains_patient_data: Literal[False] = False
    live_network_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_request(self) -> "AcquisitionRequest":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        if len(self.policy_ids) != len(set(self.policy_ids)):
            raise ValueError("policy_ids must be unique")
        topic_keys = [(item.system, item.version, item.code) for item in self.topic_codes]
        if len(topic_keys) != len(set(topic_keys)):
            raise ValueError("topic_codes must be unique")
        assert_deidentified_query_terms(self.query_terms)
        assert_no_sensitive_data(
            {
                "topic_codes": [item.model_dump(mode="json") for item in self.topic_codes],
                "query_terms": list(self.query_terms),
            }
        )
        return self


class SourceCandidate(StrictModel):
    candidate_id: SafeId
    request_id: SafeId
    validation_profile_id: SafeId
    policy: SourcePolicyRef
    connector_id: SafeId
    stable_source_key: SafeId
    canonical_url: AnyHttpUrl
    title: NonBlank
    issuing_authority: NonBlank
    source_type: NonBlank
    jurisdictions: tuple[Jurisdiction, ...] = Field(min_length=1)
    languages: tuple[LanguageCode, ...] = Field(min_length=1)
    document_version: NonBlank
    metadata: dict[str, object] = Field(default_factory=dict)
    discovered_at: datetime
    machine_generated: Literal[True] = True
    synthetic: bool
    contains_patient_data: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_candidate(self) -> "SourceCandidate":
        if self.discovered_at.tzinfo is None:
            raise ValueError("discovered_at must include a timezone")
        assert_no_sensitive_data(
            {
                "title": self.title,
                "issuing_authority": self.issuing_authority,
                "document_version": self.document_version,
                "metadata": self.metadata,
            }
        )
        return self


class SnapshotStorage(StrEnum):
    METADATA_ONLY = "metadata_only"
    QUARANTINED_SYNTHETIC_FIXTURE = "quarantined_synthetic_fixture"


class QuarantineBlobRef(StrictModel):
    content_sha256: Sha256
    size: int = Field(ge=0)
    relative_path: NonBlank

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        safe_relative_parts(value)
        return value


class SourceSnapshot(StrictModel):
    snapshot_id: SafeId
    candidate_ref: LedgerRef
    canonical_url: AnyHttpUrl
    retrieved_at: datetime
    content_type: NonBlank
    content_size: int = Field(ge=0)
    content_sha256: Sha256
    metadata_sha256: Sha256
    storage: SnapshotStorage
    quarantine_blob: QuarantineBlobRef | None = None
    synthetic: bool
    contains_patient_data: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_snapshot(self) -> "SourceSnapshot":
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must include a timezone")
        if self.storage == SnapshotStorage.QUARANTINED_SYNTHETIC_FIXTURE:
            if not self.synthetic or self.quarantine_blob is None:
                raise ValueError("quarantined fixture snapshot requires synthetic blob")
            if (
                self.quarantine_blob.content_sha256 != self.content_sha256
                or self.quarantine_blob.size != self.content_size
            ):
                raise ValueError("quarantine blob must match snapshot bytes")
        elif self.quarantine_blob is not None:
            raise ValueError("metadata-only snapshot cannot reference a blob")
        return self


class ChangeKind(StrEnum):
    NEW_SOURCE = "new_source"
    UNCHANGED = "unchanged"
    METADATA_CHANGED = "metadata_changed"
    CONTENT_CHANGED = "content_changed"
    METADATA_AND_CONTENT_CHANGED = "metadata_and_content_changed"


class ChangeSet(StrictModel):
    change_set_id: SafeId
    candidate_ref: LedgerRef
    previous_snapshot_ref: LedgerRef | None = None
    current_snapshot_ref: LedgerRef
    change_kind: ChangeKind
    changed_fields: tuple[NonBlank, ...]
    observed_at: datetime
    requires_review: bool
    machine_generated: Literal[True] = True
    synthetic: bool
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_change_set(self) -> "ChangeSet":
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if len(self.changed_fields) != len(set(self.changed_fields)):
            raise ValueError("changed_fields must be unique")
        if self.change_kind == ChangeKind.UNCHANGED:
            if self.changed_fields or self.requires_review:
                raise ValueError("unchanged snapshot cannot declare changes or review")
        elif not self.changed_fields or not self.requires_review:
            raise ValueError("changed snapshot requires fields and review")
        if self.change_kind == ChangeKind.NEW_SOURCE and self.previous_snapshot_ref is not None:
            raise ValueError("new source cannot reference a previous snapshot")
        return self


class GapKind(StrEnum):
    SOURCE_MISSING = "source_missing"
    RIGHTS_REVIEW_MISSING = "rights_review_missing"
    SOURCE_REVIEW_MISSING = "source_review_missing"
    CONTENT_CHANGE_REVIEW_MISSING = "content_change_review_missing"
    CLINICAL_SCOPE_MISSING = "clinical_scope_missing"
    TERMINOLOGY_MAPPING_REVIEW_MISSING = "terminology_mapping_review_missing"
    TRANSLATION_PERMISSION_MISSING = "translation_permission_missing"
    CLINICAL_REVIEW_MISSING = "clinical_review_missing"
    PHARMACY_REVIEW_MISSING = "pharmacy_review_missing"
    CONNECTOR_FAILURE = "connector_failure"
    PRODUCTION_EVIDENCE_MISSING = "production_evidence_missing"


class KnowledgeGap(StrictModel):
    gap_id: SafeId
    gap_kind: GapKind
    scope: ClinicalContextScope
    subject_ref: LedgerRef | None = None
    reason: NonBlank
    blocks: tuple[NonBlank, ...] = Field(min_length=1)
    lifecycle: Literal["open", "resolved", "superseded"] = "open"
    observed_at: datetime
    machine_generated: Literal[True] = True
    synthetic: bool
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @field_validator("observed_at")
    @classmethod
    def observed_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class AcquisitionRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AcquisitionRun(StrictModel):
    run_id: SafeId
    request_id: SafeId
    validation_profile_id: SafeId
    connector_id: SafeId
    environment: AcquisitionEnvironment
    status: AcquisitionRunStatus
    started_at: datetime
    finished_at: datetime | None = None
    candidate_refs: tuple[LedgerRef, ...] = ()
    snapshot_refs: tuple[LedgerRef, ...] = ()
    change_set_refs: tuple[LedgerRef, ...] = ()
    gap_refs: tuple[LedgerRef, ...] = ()
    failure_code: SafeId | None = None
    contains_patient_data: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_run(self) -> "AcquisitionRun":
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must include a timezone")
        if self.status == AcquisitionRunStatus.RUNNING:
            if self.finished_at is not None or self.failure_code is not None:
                raise ValueError("running acquisition cannot be finished")
        else:
            if self.finished_at is None or self.finished_at.tzinfo is None:
                raise ValueError("terminal acquisition requires timezone-aware finished_at")
        if self.status == AcquisitionRunStatus.FAILED and self.failure_code is None:
            raise ValueError("failed acquisition requires a stable failure_code")
        if self.status != AcquisitionRunStatus.FAILED and self.failure_code is not None:
            raise ValueError("only failed acquisition can include failure_code")
        return self


class AcquisitionResult(StrictModel):
    run_ref: LedgerRef
    status: AcquisitionRunStatus
    candidate_refs: tuple[LedgerRef, ...]
    snapshot_refs: tuple[LedgerRef, ...]
    change_set_refs: tuple[LedgerRef, ...]
    gap_refs: tuple[LedgerRef, ...]


class QuarantineBlobStore:
    """Content-addressed storage only for explicitly synthetic fixture bytes."""

    def __init__(self, root: Path) -> None:
        requested = Path(root)
        requested.mkdir(parents=True, exist_ok=True)
        if requested.is_symlink():
            raise KnowledgeOpsIntegrityError("quarantine root cannot be a symlink")
        self._root = requested.resolve(strict=True)
        self._blob_root = self._root / "blobs"
        self._blob_root.mkdir(mode=0o700, exist_ok=True)
        if self._blob_root.is_symlink():
            raise KnowledgeOpsIntegrityError("quarantine blob root cannot be a symlink")

    @property
    def root(self) -> Path:
        return self._root

    def put_fixture(
        self, document: FetchedDocument, *, policy_decision: str
    ) -> QuarantineBlobRef:
        if not document.synthetic:
            raise KnowledgeOpsPolicyError("quarantine fixture store rejects real content")
        if policy_decision != PolicyDecision.OFFLINE_FIXTURE_ONLY.value:
            raise KnowledgeOpsPolicyError(
                "fixture persistence requires offline_fixture_only SourcePolicy"
            )
        digest = hashlib.sha256(document.body).hexdigest()
        if digest != document.content_sha256:
            raise KnowledgeOpsIntegrityError("fetched document digest mismatch")
        target = self._blob_root / f"{digest}.bin"
        if target.is_symlink():
            raise KnowledgeOpsIntegrityError("quarantine blob cannot be a symlink")
        if target.exists():
            if target.is_symlink() or target.read_bytes() != document.body:
                raise KnowledgeOpsIntegrityError("quarantine digest collision or tampering")
        else:
            _atomic_blob_create(target, document.body)
        return QuarantineBlobRef(
            content_sha256=digest,
            size=len(document.body),
            relative_path=f"blobs/{digest}.bin",
        )

    def read_verified(self, blob: QuarantineBlobRef) -> bytes:
        target = self._root / blob.relative_path
        if target.is_symlink():
            raise KnowledgeOpsIntegrityError("quarantine blob cannot be a symlink")
        try:
            resolved = target.resolve(strict=True)
        except OSError as exc:
            raise KnowledgeOpsIntegrityError("quarantine blob is unavailable") from exc
        if self._root not in resolved.parents or not resolved.is_file():
            raise KnowledgeOpsIntegrityError("quarantine blob escaped its root")
        payload = resolved.read_bytes()
        if len(payload) != blob.size or hashlib.sha256(payload).hexdigest() != blob.content_sha256:
            raise KnowledgeOpsIntegrityError("quarantine blob integrity mismatch")
        return payload


class AcquisitionService:
    def __init__(
        self,
        *,
        bundle: KnowledgeOpsBundle,
        ledger: AppendOnlyLedger,
        quarantine: QuarantineBlobStore,
        connector: AcquisitionConnector,
        environment: AcquisitionEnvironment = AcquisitionEnvironment.SYNTHETIC_TEST,
    ) -> None:
        self._bundle = bundle
        self._ledger = ledger
        self._quarantine = quarantine
        self._connector = connector
        self._environment = AcquisitionEnvironment(environment)

    def run(self, request: AcquisitionRequest) -> AcquisitionResult:
        profile = next(
            (
                item
                for item in self._bundle.coverage_profiles
                if item.profile_id == request.validation_profile_id
            ),
            None,
        )
        if profile is None:
            raise KnowledgeOpsPolicyError("unknown coverage validation profile")
        if request.scope != profile.scope:
            raise KnowledgeOpsPolicyError(
                "acquisition scope must equal the exact frozen validation profile scope"
            )
        policies = tuple(self._bundle.source_policy(policy_id) for policy_id in request.policy_ids)
        if any(policy.status != "active" for policy in policies):
            raise KnowledgeOpsPolicyError("acquisition cannot use a retired SourcePolicy")
        if self._environment == AcquisitionEnvironment.PRODUCTION:
            if isinstance(self._connector, OfflineFixtureConnector):
                raise KnowledgeOpsPolicyError(
                    "synthetic offline connector cannot run in production environment"
                )
            raise KnowledgeOpsPolicyError(
                "Knowledge Ops v2.0 has no production network acquisition authority"
            )

        started_at = datetime.now(timezone.utc)
        run_id = _derived_id("run", request.request_id)
        running = AcquisitionRun(
            run_id=run_id,
            request_id=request.request_id,
            validation_profile_id=request.validation_profile_id,
            connector_id=self._connector.connector_id,
            environment=self._environment,
            status=AcquisitionRunStatus.RUNNING,
            started_at=started_at,
        )
        assert_no_sensitive_data(
            running.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_RUN,
        )
        self._ledger.append(
            LedgerCollection.ACQUISITION_RUN,
            run_id,
            payload_type="acquisition_run",
            payload=running,
            recorded_by="system:knowledge-acquisition",
            recorded_at=started_at,
            synthetic=True,
        )

        candidate_refs: list[LedgerRef] = []
        snapshot_refs: list[LedgerRef] = []
        change_set_refs: list[LedgerRef] = []
        gap_refs: list[LedgerRef] = []
        try:
            for policy in policies:
                resources = self._connector.discover(request, policy)
                if not resources:
                    gap_refs.append(
                        self._append_gap(
                            gap_id=_derived_id(
                                "gap-source-missing",
                                f"{request.validation_profile_id}-{policy.policy_id}",
                            ),
                            gap_kind=GapKind.SOURCE_MISSING,
                            scope=request.scope,
                            subject_ref=None,
                            reason="Offline validation fixture produced no Source candidate.",
                            blocks=("source_promotion", "knowledge_release"),
                            observed_at=started_at,
                        )
                    )
                    continue
                for resource in resources:
                    self._validate_discovered_resource(request, policy, resource)
                    logical_source_key = (
                        f"{resource.connector_id}-{policy.policy_id}-{resource.stable_id}"
                    )
                    candidate = self._candidate(request, policy, resource, started_at)
                    candidate_entry = self._ledger.append(
                        LedgerCollection.CANDIDATE,
                        candidate.candidate_id,
                        payload_type="source_candidate",
                        payload=candidate,
                        recorded_by="system:knowledge-acquisition",
                        recorded_at=started_at,
                        synthetic=True,
                    )
                    candidate_refs.append(candidate_entry.ref)

                    previous_snapshot_entry = self._ledger.head(
                        LedgerCollection.SNAPSHOT,
                        _derived_id("snapshot", logical_source_key),
                    )
                    if previous_snapshot_entry is None:
                        previous_snapshot = None
                    else:
                        if previous_snapshot_entry.payload_type != "source_snapshot":
                            raise KnowledgeOpsIntegrityError(
                                "snapshot ledger head has an unexpected payload type"
                            )
                        previous_snapshot = SourceSnapshot.model_validate(
                            previous_snapshot_entry.payload
                        )
                        self._ledger.get(previous_snapshot.candidate_ref)
                        assert_no_sensitive_data(
                            previous_snapshot.model_dump(mode="json"),
                            digest_trust_profile=(
                                DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT
                            ),
                        )
                    document = self._connector.fetch(resource, policy)
                    self._validate_fetched_document(resource, policy, document)
                    persist_decision = policy.decision_for(SourceOperation.PERSIST_SNAPSHOT)
                    blob = self._quarantine.put_fixture(
                        document, policy_decision=persist_decision
                    )
                    snapshot = SourceSnapshot(
                        snapshot_id=_derived_id("snapshot", logical_source_key),
                        candidate_ref=candidate_entry.ref,
                        canonical_url=document.canonical_url,
                        retrieved_at=started_at,
                        content_type=document.content_type,
                        content_size=len(document.body),
                        content_sha256=document.content_sha256,
                        metadata_sha256=_metadata_digest(candidate),
                        storage=SnapshotStorage.QUARANTINED_SYNTHETIC_FIXTURE,
                        quarantine_blob=blob,
                        synthetic=True,
                    )
                    assert_no_sensitive_data(
                        snapshot.model_dump(mode="json"),
                        digest_trust_profile=(
                            DigestTrustProfile.ACQUISITION_SOURCE_SNAPSHOT
                        ),
                    )
                    snapshot_entry = self._ledger.append(
                        LedgerCollection.SNAPSHOT,
                        snapshot.snapshot_id,
                        payload_type="source_snapshot",
                        payload=snapshot,
                        recorded_by="system:knowledge-acquisition",
                        recorded_at=started_at,
                        synthetic=True,
                    )
                    snapshot_refs.append(snapshot_entry.ref)
                    change = _build_change_set(
                        stable_id=logical_source_key,
                        candidate_ref=candidate_entry.ref,
                        previous_entry=previous_snapshot_entry,
                        previous=previous_snapshot,
                        current_entry=snapshot_entry.ref,
                        current=snapshot,
                        observed_at=started_at,
                    )
                    assert_no_sensitive_data(
                        change.model_dump(mode="json"),
                        digest_trust_profile=DigestTrustProfile.ACQUISITION_CHANGE_SET,
                    )
                    change_entry = self._ledger.append(
                        LedgerCollection.CHANGE_SET,
                        change.change_set_id,
                        payload_type="change_set",
                        payload=change,
                        recorded_by="system:knowledge-acquisition",
                        recorded_at=started_at,
                        synthetic=True,
                    )
                    change_set_refs.append(change_entry.ref)
                    gap_refs.extend(
                        self._default_source_gaps(
                            request=request,
                            stable_id=logical_source_key,
                            candidate_ref=candidate_entry.ref,
                            change=change,
                            observed_at=started_at,
                        )
                    )

            finished_at = datetime.now(timezone.utc)
            terminal = AcquisitionRun(
                run_id=run_id,
                request_id=request.request_id,
                validation_profile_id=request.validation_profile_id,
                connector_id=self._connector.connector_id,
                environment=self._environment,
                status=AcquisitionRunStatus.COMPLETED,
                started_at=started_at,
                finished_at=finished_at,
                candidate_refs=tuple(candidate_refs),
                snapshot_refs=tuple(snapshot_refs),
                change_set_refs=tuple(change_set_refs),
                gap_refs=tuple(gap_refs),
            )
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            failure_code = _stable_failure_code(exc)
            failure_gap = self._append_gap(
                gap_id=_derived_id("gap-connector-failure", request.request_id),
                gap_kind=GapKind.CONNECTOR_FAILURE,
                scope=request.scope,
                subject_ref=None,
                reason=f"Acquisition stopped with stable failure code {failure_code}.",
                blocks=("source_promotion", "knowledge_release"),
                observed_at=finished_at,
            )
            gap_refs.append(failure_gap)
            terminal = AcquisitionRun(
                run_id=run_id,
                request_id=request.request_id,
                validation_profile_id=request.validation_profile_id,
                connector_id=self._connector.connector_id,
                environment=self._environment,
                status=AcquisitionRunStatus.FAILED,
                started_at=started_at,
                finished_at=finished_at,
                candidate_refs=tuple(candidate_refs),
                snapshot_refs=tuple(snapshot_refs),
                change_set_refs=tuple(change_set_refs),
                gap_refs=tuple(gap_refs),
                failure_code=failure_code,
            )
        assert_no_sensitive_data(
            terminal.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_RUN,
        )
        terminal_entry = self._ledger.append(
            LedgerCollection.ACQUISITION_RUN,
            run_id,
            payload_type="acquisition_run",
            payload=terminal,
            recorded_by="system:knowledge-acquisition",
            recorded_at=finished_at,
            synthetic=True,
        )
        return AcquisitionResult(
            run_ref=terminal_entry.ref,
            status=terminal.status,
            candidate_refs=terminal.candidate_refs,
            snapshot_refs=terminal.snapshot_refs,
            change_set_refs=terminal.change_set_refs,
            gap_refs=terminal.gap_refs,
        )

    def _candidate(self, request, policy, resource, observed_at) -> SourceCandidate:
        canonical_url = validate_url_against_policy(str(resource.canonical_url), policy)
        logical_source_key = (
            f"{resource.connector_id}-{policy.policy_id}-{resource.stable_id}"
        )
        return SourceCandidate(
            candidate_id=_derived_id("candidate", logical_source_key),
            request_id=request.request_id,
            validation_profile_id=request.validation_profile_id,
            policy=SourcePolicyRef(
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
            ),
            connector_id=resource.connector_id,
            stable_source_key=resource.stable_id,
            canonical_url=canonical_url,
            title=resource.title,
            issuing_authority=resource.issuing_authority,
            source_type=resource.source_type,
            jurisdictions=resource.jurisdictions,
            languages=resource.languages,
            document_version=resource.document_version,
            metadata=resource.metadata,
            discovered_at=observed_at,
            synthetic=resource.synthetic,
        )

    def _validate_discovered_resource(self, request, policy, resource) -> None:
        if (
            resource.connector_id != self._connector.connector_id
            or resource.validation_profile_id != request.validation_profile_id
            or resource.policy_id != policy.policy_id
        ):
            raise KnowledgeOpsPolicyError(
                "connector returned a resource outside the exact acquisition request"
            )
        if not resource.synthetic:
            raise KnowledgeOpsPolicyError(
                "Knowledge Ops v2.0 acquisition accepts synthetic resources only"
            )
        if resource.source_type not in policy.source_types:
            raise KnowledgeOpsPolicyError(
                "connector resource source_type is outside SourcePolicy"
            )
        policy_jurisdictions = {
            (item.system, item.code) for item in policy.source_jurisdictions
        }
        resource_jurisdictions = {
            (item.system, item.code) for item in resource.jurisdictions
        }
        if not resource_jurisdictions.issubset(policy_jurisdictions):
            raise KnowledgeOpsPolicyError(
                "connector resource jurisdiction is outside SourcePolicy"
            )
        if not set(resource.languages).issubset(set(policy.languages)):
            raise KnowledgeOpsPolicyError(
                "connector resource language is outside SourcePolicy"
            )
        assert_no_sensitive_data(resource.model_dump(mode="json"))

    def _validate_fetched_document(self, resource, policy, document) -> None:
        if (
            document.connector_id != self._connector.connector_id
            or document.connector_id != resource.connector_id
            or document.stable_id != resource.stable_id
        ):
            raise KnowledgeOpsPolicyError(
                "connector returned bytes outside the exact discovered resource"
            )
        if not document.synthetic:
            raise KnowledgeOpsPolicyError(
                "synthetic acquisition run received non-synthetic bytes"
            )
        expected_url = validate_url_against_policy(
            str(resource.canonical_url), policy
        )
        fetched_url = validate_url_against_policy(document.canonical_url, policy)
        if fetched_url != expected_url:
            raise KnowledgeOpsPolicyError(
                "fetched document URL differs from the discovered resource"
            )
        if document.content_type not in policy.allowed_content_types:
            raise KnowledgeOpsPolicyError(
                "fetched document content type is outside SourcePolicy"
            )
        if len(document.body) > policy.maximum_response_bytes:
            raise KnowledgeOpsPolicyError(
                "fetched document exceeds SourcePolicy byte limit"
            )
        if hashlib.sha256(document.body).hexdigest() != document.content_sha256:
            raise KnowledgeOpsIntegrityError("fetched document digest mismatch")
        try:
            fixture_text = document.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise KnowledgeOpsPolicyError(
                "synthetic fixture bytes must be UTF-8 inspectable"
            ) from exc
        assert_no_sensitive_data({"synthetic_fixture_body": fixture_text})
        if document.redirect_urls or document.peer_ips:
            raise KnowledgeOpsPolicyError(
                "synthetic fixture fetch cannot carry live transport attestations"
            )

    def _default_source_gaps(
        self,
        *,
        request: AcquisitionRequest,
        stable_id: str,
        candidate_ref: LedgerRef,
        change: ChangeSet,
        observed_at: datetime,
    ) -> tuple[LedgerRef, ...]:
        refs = [
            self._append_gap(
                gap_id=_derived_id("gap-rights", stable_id),
                gap_kind=GapKind.RIGHTS_REVIEW_MISSING,
                scope=request.scope,
                subject_ref=candidate_ref,
                reason="No formal rights officer decision exists for this staged Source.",
                blocks=("source_promotion", "content_persistence", "knowledge_release"),
                observed_at=observed_at,
            ),
            self._append_gap(
                gap_id=_derived_id("gap-source-review", stable_id),
                gap_kind=GapKind.SOURCE_REVIEW_MISSING,
                scope=request.scope,
                subject_ref=candidate_ref,
                reason="No formal knowledge curator decision exists for this staged Source.",
                blocks=("source_promotion", "knowledge_release"),
                observed_at=observed_at,
            ),
        ]
        if change.change_kind not in {ChangeKind.NEW_SOURCE, ChangeKind.UNCHANGED}:
            refs.append(
                self._append_gap(
                    gap_id=_derived_id("gap-content-change", stable_id),
                    gap_kind=GapKind.CONTENT_CHANGE_REVIEW_MISSING,
                    scope=request.scope,
                    subject_ref=candidate_ref,
                    reason="A machine-detected metadata/content change requires human review.",
                    blocks=("source_promotion", "knowledge_release"),
                    observed_at=observed_at,
                )
            )
        return tuple(refs)

    def _append_gap(
        self,
        *,
        gap_id: str,
        gap_kind: GapKind,
        scope: ClinicalContextScope,
        subject_ref: LedgerRef | None,
        reason: str,
        blocks: tuple[str, ...],
        observed_at: datetime,
    ) -> LedgerRef:
        gap = KnowledgeGap(
            gap_id=gap_id,
            gap_kind=gap_kind,
            scope=scope,
            subject_ref=subject_ref,
            reason=reason,
            blocks=blocks,
            observed_at=observed_at,
            synthetic=True,
        )
        if gap.subject_ref is not None:
            self._ledger.get(gap.subject_ref)
        assert_no_sensitive_data(
            gap.model_dump(mode="json"),
            digest_trust_profile=DigestTrustProfile.ACQUISITION_KNOWLEDGE_GAP,
        )
        return self._ledger.append(
            LedgerCollection.GAP,
            gap_id,
            payload_type="knowledge_gap",
            payload=gap,
            recorded_by="system:knowledge-acquisition",
            recorded_at=observed_at,
            synthetic=True,
        ).ref


def _metadata_digest(candidate: SourceCandidate) -> str:
    payload = candidate.model_dump(
        mode="json",
        exclude={"discovered_at", "request_id", "candidate_id"},
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _build_change_set(
    *,
    stable_id: str,
    candidate_ref: LedgerRef,
    previous_entry,
    previous: SourceSnapshot | None,
    current_entry: LedgerRef,
    current: SourceSnapshot,
    observed_at: datetime,
) -> ChangeSet:
    if previous is None:
        kind = ChangeKind.NEW_SOURCE
        changed_fields = ("source",)
    else:
        metadata_changed = previous.metadata_sha256 != current.metadata_sha256
        content_changed = previous.content_sha256 != current.content_sha256
        if metadata_changed and content_changed:
            kind = ChangeKind.METADATA_AND_CONTENT_CHANGED
            changed_fields = ("metadata", "content")
        elif metadata_changed:
            kind = ChangeKind.METADATA_CHANGED
            changed_fields = ("metadata",)
        elif content_changed:
            kind = ChangeKind.CONTENT_CHANGED
            changed_fields = ("content",)
        else:
            kind = ChangeKind.UNCHANGED
            changed_fields = ()
    return ChangeSet(
        change_set_id=_derived_id("change", stable_id),
        candidate_ref=candidate_ref,
        previous_snapshot_ref=None if previous_entry is None else previous_entry.ref,
        current_snapshot_ref=current_entry,
        change_kind=kind,
        changed_fields=changed_fields,
        observed_at=observed_at,
        requires_review=kind != ChangeKind.UNCHANGED,
        synthetic=True,
    )


def _stable_failure_code(exc: Exception) -> str:
    if isinstance(exc, KnowledgeOpsPolicyError):
        return "policy_error"
    if isinstance(exc, KnowledgeOpsIntegrityError):
        return "integrity_error"
    return "connector_error"


def _derived_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(f"{prefix}|{value}".encode("utf-8")).hexdigest()
    return digest_derived_internal_id(prefix, digest, digest_characters=32)


def _atomic_blob_create(target: Path, payload: bytes) -> None:
    temporary = target.parent / f".{target.name}.{secrets.token_hex(12)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink():
                raise KnowledgeOpsIntegrityError("quarantine blob cannot be a symlink")
            if target.read_bytes() != payload:
                raise KnowledgeOpsIntegrityError("quarantine digest collision")
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

"""Hash-pinned, atomic loading for Knowledge Operations v2 manifests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from continucare.knowledge.ops.models import (
    CORE_SYMPTOM_REUSED_BENCHMARK_KEYS,
    CoreSymptomAliasAudit,
    CoreSymptomAliasAuditManifest,
    CoverageProfileManifest,
    CoverageValidationProfile,
    FileRef,
    GovernanceManifestEvidence,
    GovernanceGate,
    KnowledgeOpsBundleIndex,
    KnowledgeReleaseIntent,
    KnowledgeOpsManifestError,
    LicensePosture,
    PayloadEnvelope,
    PolicyDecision,
    ReadinessBlock,
    ReadinessGap,
    ReadinessGapKind,
    ReadinessGapRegistryManifest,
    ReviewGatePolicy,
    ReviewPolicyManifest,
    ReleaseIntentManifest,
    SafetyBoundary,
    SafetyBoundaryManifest,
    SourcePolicy,
    SourcePolicyGapSubject,
    SourcePolicyManifest,
    ValidationDomain,
    safe_relative_parts,
)


LEGACY_INDEX_PATH = "bundle_index_v2.json"
BUILTIN_INDEX_PATH = "bundle_index_v2_4.json"
_PAYLOAD_ADAPTER = TypeAdapter(PayloadEnvelope)
_P1_READINESS_SOURCE_POLICY_REFS = frozenset(
    {
        ("source-dailymed", 1),
        ("source-ema-website-data", 1),
        ("source-medlineplus", 1),
        ("nlm-pubmed-metadata", 2),
        ("source-pmc-open-access", 1),
    }
)


class BundleSource(Protocol):
    def read_bytes(self, relative_path: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class DirectoryBundleSource:
    root: Path

    def read_bytes(self, relative_path: str) -> bytes:
        parts = safe_relative_parts(relative_path)
        root = self.root.resolve(strict=True)
        if self.root.is_symlink():
            raise ValueError("bundle root cannot be a symlink")
        current = root
        for part in parts:
            candidate = current / part
            if candidate.is_symlink():
                raise ValueError("bundle path cannot traverse a symlink")
            current = candidate
        resolved = current.resolve(strict=True)
        if root not in resolved.parents:
            raise ValueError("bundle path escaped its root")
        if not resolved.is_file():
            raise ValueError("bundle resource is not a regular file")
        return resolved.read_bytes()


@dataclass(frozen=True, slots=True)
class TraversableBundleSource:
    root: object

    def read_bytes(self, relative_path: str) -> bytes:
        parts = safe_relative_parts(relative_path)
        resource = self.root
        for part in parts:
            resource = resource.joinpath(part)  # type: ignore[attr-defined]
        if not resource.is_file():  # type: ignore[attr-defined]
            raise ValueError("bundle resource is not a file")
        payload = resource.read_bytes()  # type: ignore[attr-defined]
        if not isinstance(payload, bytes):
            raise TypeError("bundle resource must return bytes")
        return payload


@dataclass(frozen=True, slots=True)
class KnowledgeOpsBundle:
    index: KnowledgeOpsBundleIndex
    boundary: SafetyBoundary
    source_policies: tuple[SourcePolicy, ...]
    coverage_profiles: tuple[CoverageValidationProfile, ...]
    review_gates: tuple[ReviewGatePolicy, ...]
    release_intent: KnowledgeReleaseIntent
    manifest_digests: Mapping[tuple[str, int], str]
    readiness_gaps: tuple[ReadinessGap, ...] = ()
    core_symptom_alias_audit: CoreSymptomAliasAudit | None = None

    def source_policy(self, policy_id: str, policy_version: int = 1) -> SourcePolicy:
        for policy in self.source_policies:
            if (policy.policy_id, policy.policy_version) == (policy_id, policy_version):
                return policy
        raise KeyError(f"unknown SourcePolicy {policy_id}@{policy_version}")

    def review_gate(self, gate: GovernanceGate | str) -> ReviewGatePolicy:
        gate_value = str(gate)
        for policy in self.review_gates:
            if str(policy.gate) == gate_value:
                return policy
        raise KeyError(f"unknown review gate {gate_value}")

    def manifest_evidence(self) -> tuple[GovernanceManifestEvidence, ...]:
        return tuple(
            GovernanceManifestEvidence(
                file_id=pinned.ref.file_id,
                file_version=pinned.ref.file_version,
                manifest_sha256=pinned.manifest_sha256,
            )
            for pinned in self.index.files
        )

    def index_sha256(self) -> str:
        payload = json.dumps(
            self.index.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def load_builtin_ops_bundle() -> KnowledgeOpsBundle:
    source = TraversableBundleSource(files("continucare.knowledge.manifests_v2"))
    return load_ops_bundle(source, index_path=BUILTIN_INDEX_PATH)


def load_ops_bundle(
    source: BundleSource,
    *,
    index_path: str = LEGACY_INDEX_PATH,
) -> KnowledgeOpsBundle:
    """Load a complete v2 governance bundle or return no partial state."""

    try:
        index_bytes = source.read_bytes(index_path)
        index = KnowledgeOpsBundleIndex.model_validate_json(index_bytes)
    except (OSError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise KnowledgeOpsManifestError(f"invalid Knowledge Ops index: {exc}") from exc

    envelopes: list[PayloadEnvelope] = []
    digests: dict[tuple[str, int], str] = {}
    for pinned in index.files:
        try:
            payload = source.read_bytes(pinned.relative_path)
        except Exception as exc:
            raise KnowledgeOpsManifestError(
                f"cannot read pinned file {pinned.relative_path}: {exc}"
            ) from exc
        if len(payload) != pinned.size:
            raise KnowledgeOpsManifestError(
                f"pinned file {pinned.relative_path} size mismatch"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != pinned.manifest_sha256:
            raise KnowledgeOpsManifestError(
                f"pinned file {pinned.relative_path} SHA-256 mismatch"
            )
        try:
            envelope = _PAYLOAD_ADAPTER.validate_json(payload)
        except ValidationError as exc:
            raise KnowledgeOpsManifestError(
                f"invalid pinned file {pinned.relative_path}: {exc}"
            ) from exc
        if envelope.ref != pinned.ref:
            raise KnowledgeOpsManifestError(
                f"pinned ref does not match payload {pinned.relative_path}"
            )
        envelopes.append(envelope)
        digests[pinned.ref.key()] = digest

    _validate_file_versions(envelopes, index.current_file_refs)
    current_keys = {item.key() for item in index.current_file_refs}
    current_envelopes = [
        item for item in envelopes if item.ref.key() in current_keys
    ]
    boundary_files = [
        item for item in current_envelopes if isinstance(item, SafetyBoundaryManifest)
    ]
    source_files = [
        item for item in current_envelopes if isinstance(item, SourcePolicyManifest)
    ]
    profile_files = [
        item for item in current_envelopes if isinstance(item, CoverageProfileManifest)
    ]
    review_files = [
        item for item in current_envelopes if isinstance(item, ReviewPolicyManifest)
    ]
    release_files = [
        item for item in current_envelopes if isinstance(item, ReleaseIntentManifest)
    ]
    readiness_files = [
        item
        for item in current_envelopes
        if isinstance(item, ReadinessGapRegistryManifest)
    ]
    alias_audit_files = [
        item
        for item in current_envelopes
        if isinstance(item, CoreSymptomAliasAuditManifest)
    ]
    if not all(
        len(items) == 1
        for items in (
            boundary_files,
            source_files,
            profile_files,
            review_files,
            release_files,
        )
    ):
        raise KnowledgeOpsManifestError(
            "current v2 bundle requires exactly one boundary, source policy, "
            "coverage profile, review policy, and release intent file"
        )
    if index.bundle_version >= 3:
        if len(readiness_files) != 1:
            raise KnowledgeOpsManifestError(
                "Knowledge Ops bundle v3+ requires exactly one readiness Gap registry"
            )
    elif readiness_files:
        raise KnowledgeOpsManifestError(
            "legacy Knowledge Ops bundles cannot claim a readiness Gap registry"
        )
    if index.bundle_version >= 4:
        if len(alias_audit_files) != 1:
            raise KnowledgeOpsManifestError(
                "Knowledge Ops bundle v4+ requires exactly one Core Symptom alias audit"
            )
    elif alias_audit_files:
        raise KnowledgeOpsManifestError(
            "legacy Knowledge Ops bundles cannot claim a Core Symptom alias audit"
        )

    source_policies = _materialize_source_policies(
        current=source_files[0],
        envelopes=envelopes,
    )
    coverage_profiles = profile_files[0].profiles
    review_gates = review_files[0].gates
    _validate_unique_versions(
        source_policies,
        lambda item: (item.policy_id, item.policy_version),
        "SourcePolicy",
    )
    _validate_source_policy_versions(source_policies)
    _validate_unique_versions(
        coverage_profiles,
        lambda item: (item.profile_id, item.profile_version),
        "coverage profile",
    )
    _validate_unique_versions(review_gates, lambda item: str(item.gate), "review gate")
    if {str(item.domain) for item in coverage_profiles} != {
        item.value for item in ValidationDomain
    }:
        raise KnowledgeOpsManifestError(
            "coverage profiles must contain exactly the five frozen validation domains"
        )
    if {str(item.gate) for item in review_gates} != {item.value for item in GovernanceGate}:
        raise KnowledgeOpsManifestError(
            "review policy must contain every governance gate exactly once"
        )
    if not source_policies:
        raise KnowledgeOpsManifestError("source policy registry cannot be empty")
    readiness_gaps = () if not readiness_files else readiness_files[0].gaps
    _validate_readiness_gaps(readiness_gaps, source_policies)
    alias_audit = None if not alias_audit_files else alias_audit_files[0].audit
    if alias_audit is not None:
        _validate_core_symptom_alias_audit(alias_audit)

    return KnowledgeOpsBundle(
        index=index,
        boundary=boundary_files[0].boundary,
        source_policies=source_policies,
        coverage_profiles=coverage_profiles,
        review_gates=review_gates,
        release_intent=release_files[0].intent,
        manifest_digests=MappingProxyType(digests),
        readiness_gaps=readiness_gaps,
        core_symptom_alias_audit=alias_audit,
    )


def _validate_file_versions(
    envelopes: list[PayloadEnvelope], current_refs: tuple[FileRef, ...]
) -> None:
    by_id: dict[str, list[int]] = {}
    for item in envelopes:
        by_id.setdefault(item.file_id, []).append(item.file_version)
    for file_id, versions in by_id.items():
        ordered = sorted(versions)
        if ordered != list(range(1, ordered[-1] + 1)):
            raise KnowledgeOpsManifestError(
                f"manifest {file_id} versions must be append-only and contiguous"
            )
    current = {item.key() for item in current_refs}
    expected = {(file_id, max(versions)) for file_id, versions in by_id.items()}
    if current != expected:
        raise KnowledgeOpsManifestError("current_file_refs must select each exact head")


def _validate_unique_versions(items, key, label: str) -> None:
    keys = [key(item) for item in items]
    if len(keys) != len(set(keys)):
        raise KnowledgeOpsManifestError(f"duplicate {label} identity")


def _materialize_source_policies(
    *,
    current: SourcePolicyManifest,
    envelopes: list[PayloadEnvelope],
) -> tuple[SourcePolicy, ...]:
    manifests = {
        item.ref.key(): item
        for item in envelopes
        if isinstance(item, SourcePolicyManifest)
    }
    chain: list[SourcePolicyManifest] = []
    cursor = current
    seen: set[tuple[str, int]] = set()
    while True:
        key = cursor.ref.key()
        if key in seen:
            raise KnowledgeOpsManifestError("source policy manifest extension cycle")
        seen.add(key)
        chain.append(cursor)
        if cursor.extends is None:
            break
        predecessor = manifests.get(cursor.extends.key())
        if predecessor is None:
            raise KnowledgeOpsManifestError(
                "source policy manifest predecessor is not pinned"
            )
        cursor = predecessor
    policies: list[SourcePolicy] = []
    for manifest in reversed(chain):
        policies.extend(manifest.policies)
    return tuple(policies)


def _validate_source_policy_versions(policies: tuple[SourcePolicy, ...]) -> None:
    by_id: dict[str, list[int]] = {}
    for policy in policies:
        by_id.setdefault(policy.policy_id, []).append(policy.policy_version)
    for policy_id, versions in by_id.items():
        ordered = sorted(versions)
        if ordered != list(range(1, ordered[-1] + 1)):
            raise KnowledgeOpsManifestError(
                f"SourcePolicy {policy_id} versions must be contiguous"
            )


def _validate_readiness_gaps(
    gaps: tuple[ReadinessGap, ...],
    policies: tuple[SourcePolicy, ...],
) -> None:
    if not gaps:
        return
    by_policy = {
        (item.policy_id, item.policy_version): item for item in policies
    }
    live_refs: set[tuple[str, int]] = set()
    rights_refs: set[tuple[str, int]] = set()
    for gap in gaps:
        if not isinstance(gap.subject, SourcePolicyGapSubject):
            continue
        reference = gap.subject.source_policy.key()
        try:
            policy = by_policy[reference]
        except KeyError as exc:
            raise KnowledgeOpsManifestError(
                f"readiness Gap {gap.gap_id} references an unknown SourcePolicy"
            ) from exc
        if policy.status != "active":
            raise KnowledgeOpsManifestError(
                f"readiness Gap {gap.gap_id} references a retired SourcePolicy"
            )
        if gap.gap_kind == ReadinessGapKind.LIVE_VALIDATION_NOT_ATTEMPTED.value:
            live_refs.add(reference)
            if policy.live_network_enabled is not False:
                raise KnowledgeOpsManifestError(
                    "live-validation Gap conflicts with SourcePolicy live posture"
                )
        elif gap.gap_kind == ReadinessGapKind.RIGHTS_UNRESOLVED.value:
            rights_refs.add(reference)
            if policy.license_posture == LicensePosture.VERIFIED_OPEN.value:
                raise KnowledgeOpsManifestError(
                    "rights-unresolved Gap conflicts with verified-open SourcePolicy"
                )
            metadata_only_operations = {
                "register_link_metadata",
                "discover_metadata",
            }
            if any(
                rule.decision == PolicyDecision.ALLOW.value
                and rule.operation not in metadata_only_operations
                for rule in policy.operation_rules
            ):
                raise KnowledgeOpsManifestError(
                    "rights-unresolved SourcePolicy allows reuse beyond metadata/link-only"
                )
            if ReadinessBlock.REUSE_BEYOND_METADATA_LINK_ONLY.value not in gap.blocks:
                raise KnowledgeOpsManifestError(
                    "rights-unresolved Gap must enforce metadata/link-only reuse"
                )
    if live_refs != _P1_READINESS_SOURCE_POLICY_REFS:
        raise KnowledgeOpsManifestError(
            "readiness registry must cover the frozen five live-validation policies"
        )
    if rights_refs != _P1_READINESS_SOURCE_POLICY_REFS:
        raise KnowledgeOpsManifestError(
            "readiness registry must cover the frozen five rights policies"
        )

    catalog_gap = next(
        (
            item
            for item in gaps
            if item.gap_kind
            == ReadinessGapKind.TERMINOLOGY_ALIAS_REVIEW_PENDING.value
        ),
        None,
    )
    if catalog_gap is None:
        raise KnowledgeOpsManifestError("readiness registry omits the catalog Gap")
    from continucare.terminology.core_catalog import load_core_symptom_catalog_v2

    catalog = load_core_symptom_catalog_v2()
    expected_refs = tuple(
        item.existing_concept_ref
        for item in catalog.records
        if item.concept_status == "reused_concept"
        and item.existing_concept_ref is not None
    )
    subject = catalog_gap.subject
    if (
        subject.subject_kind != "core_symptom_catalog"
        or subject.catalog_id != catalog.catalog_id
        or subject.catalog_version != catalog.catalog_version
        or subject.concept_refs != expected_refs
    ):
        raise KnowledgeOpsManifestError(
            "catalog readiness Gap differs from the current reused concept refs"
        )


def _validate_core_symptom_alias_audit(audit: CoreSymptomAliasAudit) -> None:
    """Bind the technical audit to exact catalog bytes and their full alias sets."""

    from continucare.terminology.catalog import DATA_FILE, load_glp1_symptom_catalog
    from continucare.terminology.core_catalog import (
        CORE_CATALOG_V2_FILE,
        load_core_symptom_catalog_v2,
    )

    source_bytes = DATA_FILE.read_bytes()
    catalog_bytes = CORE_CATALOG_V2_FILE.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != audit.source_catalog_sha256:
        raise KnowledgeOpsManifestError(
            "Core Symptom alias audit source catalog SHA-256 mismatch"
        )
    if hashlib.sha256(catalog_bytes).hexdigest() != audit.catalog_sha256:
        raise KnowledgeOpsManifestError(
            "Core Symptom alias audit v2 catalog SHA-256 mismatch"
        )

    source_catalog = load_glp1_symptom_catalog()
    catalog = load_core_symptom_catalog_v2()
    if (
        source_catalog.catalog_id != audit.source_catalog_id
        or source_catalog.version != audit.source_catalog_version
        or catalog.catalog_id != audit.catalog_id
        or catalog.catalog_version != audit.catalog_version
    ):
        raise KnowledgeOpsManifestError(
            "Core Symptom alias audit catalog identity or version mismatch"
        )

    reused_records = tuple(
        item for item in catalog.records if item.concept_status == "reused_concept"
    )
    if tuple(item.benchmark_key for item in reused_records) != (
        CORE_SYMPTOM_REUSED_BENCHMARK_KEYS
    ):
        raise KnowledgeOpsManifestError(
            "Core Symptom alias audit reused concept set differs from v2 catalog"
        )
    if len(reused_records) != len(audit.concept_audits):
        raise KnowledgeOpsManifestError(
            "Core Symptom alias audit concept count differs from v2 catalog"
        )

    for record, concept_audit in zip(
        reused_records,
        audit.concept_audits,
        strict=True,
    ):
        if (
            record.benchmark_id != concept_audit.benchmark_id
            or record.benchmark_key != concept_audit.benchmark_key
            or record.existing_concept_ref != concept_audit.existing_concept_ref
            or record.preferred_zh != concept_audit.preferred_zh
            or record.preferred_en != concept_audit.benchmark_label_en
        ):
            raise KnowledgeOpsManifestError(
                "Core Symptom alias audit differs from its exact v2 record"
            )
        source_concept = source_catalog.concept(concept_audit.existing_concept_ref)
        if (
            source_concept.preferred_zh != concept_audit.preferred_zh
            or tuple(source_concept.aliases_zh)
            != tuple(item.alias_zh for item in concept_audit.aliases)
        ):
            raise KnowledgeOpsManifestError(
                "Core Symptom alias audit does not cover the exact v1 alias sequence"
            )
        expected_roles = (
            "v1_preferred_display_label",
            *("inherited_v1_alias" for _ in source_concept.aliases_zh[1:]),
        )
        if tuple(item.source_role for item in concept_audit.aliases) != expected_roles:
            raise KnowledgeOpsManifestError(
                "Core Symptom alias audit preferred/inherited partition differs"
            )

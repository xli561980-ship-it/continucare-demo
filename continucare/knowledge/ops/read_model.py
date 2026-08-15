"""Incremental, UI-independent v2 read model for knowledge operations."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from continucare.knowledge.ops.manifests import KnowledgeOpsBundle, load_builtin_ops_bundle
from continucare.knowledge.ops.models import (
    KNOWLEDGE_OPS_CONTRACT_VERSION,
    CoverageValidationProfile,
    KnowledgeReleaseIntent,
    ReadinessBlock,
    ReadinessGap,
    ReadinessGapKind,
    ReviewGatePolicy,
    SafeId,
    SafetyBoundary,
    SourcePolicy,
    SourcePolicyGapSubject,
    SourcePolicyRef,
    StrictModel,
)


class SourceGovernanceReadiness(StrictModel):
    source_policy: SourcePolicyRef
    open_gap_ids: tuple[SafeId, ...]
    persistent_validation_status: Literal["not_attempted", "not_recorded"]
    maximum_reuse: Literal["metadata_link_only", "source_policy_default_deny"]
    production_eligible: bool
    release_ready: bool


class GovernanceReadinessView(StrictModel):
    registry_present: bool
    registry_file_version: int | None = Field(default=None, ge=1)
    open_gaps: tuple[ReadinessGap, ...]
    source_readiness: tuple[SourceGovernanceReadiness, ...]
    production_blocking_gap_ids: tuple[SafeId, ...]
    release_blocking_gap_ids: tuple[SafeId, ...]
    production_eligible: bool
    release_ready: bool
    consumer_integration_ready: bool
    persistent_source_validation_claimed: Literal[False] = False
    wrote_knowledge_state: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"


class KnowledgeOpsReadModel(StrictModel):
    contract_version: Literal[KNOWLEDGE_OPS_CONTRACT_VERSION]
    bundle_id: SafeId
    bundle_version: int = Field(ge=1)
    bundle_index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary: SafetyBoundary
    source_policies: tuple[SourcePolicy, ...]
    validation_profiles: tuple[CoverageValidationProfile, ...]
    review_gates: tuple[ReviewGatePolicy, ...]
    release_intent: KnowledgeReleaseIntent
    readiness_gaps: tuple[ReadinessGap, ...] = ()
    governance_readiness: GovernanceReadinessView
    operational_state: Literal["readiness_only"] = "readiness_only"
    production_releases: tuple[None, ...] = Field(default_factory=tuple, max_length=0)


def build_ops_read_model(bundle: KnowledgeOpsBundle) -> KnowledgeOpsReadModel:
    readiness = build_governance_readiness(bundle)
    return KnowledgeOpsReadModel(
        contract_version=KNOWLEDGE_OPS_CONTRACT_VERSION,
        bundle_id=bundle.index.bundle_id,
        bundle_version=bundle.index.bundle_version,
        bundle_index_sha256=bundle.index_sha256(),
        boundary=bundle.boundary,
        source_policies=bundle.source_policies,
        validation_profiles=bundle.coverage_profiles,
        review_gates=bundle.review_gates,
        release_intent=bundle.release_intent,
        readiness_gaps=bundle.readiness_gaps,
        governance_readiness=readiness,
    )


def build_governance_readiness(
    bundle: KnowledgeOpsBundle,
) -> GovernanceReadinessView:
    gaps = bundle.readiness_gaps
    production_blockers = tuple(
        item.gap_id
        for item in gaps
        if ReadinessBlock.PRODUCTION_ELIGIBILITY.value in item.blocks
    )
    release_blockers = tuple(
        item.gap_id
        for item in gaps
        if ReadinessBlock.KNOWLEDGE_RELEASE.value in item.blocks
    )
    source_gap_order: dict[tuple[str, int], list[ReadinessGap]] = {}
    for gap in gaps:
        if isinstance(gap.subject, SourcePolicyGapSubject):
            source_gap_order.setdefault(gap.subject.source_policy.key(), []).append(gap)

    release_ready = bundle.release_intent.release_ready and not release_blockers
    production_eligible = (
        bundle.release_intent.formal_reviewers_available
        and bundle.release_intent.formal_license_decisions_available
        and not production_blockers
    )
    sources: list[SourceGovernanceReadiness] = []
    for (policy_id, policy_version), source_gaps in source_gap_order.items():
        gap_kinds = {item.gap_kind for item in source_gaps}
        sources.append(
            SourceGovernanceReadiness(
                source_policy=SourcePolicyRef(
                    policy_id=policy_id,
                    policy_version=policy_version,
                ),
                open_gap_ids=tuple(item.gap_id for item in source_gaps),
                persistent_validation_status=(
                    "not_attempted"
                    if ReadinessGapKind.LIVE_VALIDATION_NOT_ATTEMPTED.value
                    in gap_kinds
                    else "not_recorded"
                ),
                maximum_reuse=(
                    "metadata_link_only"
                    if ReadinessGapKind.RIGHTS_UNRESOLVED.value in gap_kinds
                    else "source_policy_default_deny"
                ),
                production_eligible=production_eligible,
                release_ready=release_ready,
            )
        )
    registry_ref = next(
        (
            item
            for item in bundle.index.current_file_refs
            if item.file_id == "knowledge-ops-readiness-gaps"
        ),
        None,
    )
    return GovernanceReadinessView(
        registry_present=registry_ref is not None,
        registry_file_version=(
            None if registry_ref is None else registry_ref.file_version
        ),
        open_gaps=gaps,
        source_readiness=tuple(sources),
        production_blocking_gap_ids=production_blockers,
        release_blocking_gap_ids=release_blockers,
        production_eligible=production_eligible,
        release_ready=release_ready,
        consumer_integration_ready=(
            registry_ref is not None
            and not any(
                ReadinessBlock.CONSUMER_INTEGRATION.value in item.blocks
                for item in gaps
            )
        ),
    )


def load_builtin_ops_read_model() -> KnowledgeOpsReadModel:
    return build_ops_read_model(load_builtin_ops_bundle())

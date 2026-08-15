"""Frozen, fail-closed Core Symptom Catalog v2 consumer read API.

This module projects only hash-pinned repository manifests and catalogs.  It
does not read SQLite, perform network access, match patient text, or grant any
clinical/runtime authority.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator

from continucare.knowledge.ops.manifests import (
    KnowledgeOpsBundle,
    load_builtin_ops_bundle,
)
from continucare.knowledge.ops.models import (
    AliasAuditReason,
    CoreSymptomAliasAudit,
    GovernanceGate,
    KnowledgeOpsManifestError,
    NonBlank,
    ReadinessBlock,
    ReadinessGap,
    ReadinessGapKind,
    ReviewerRole,
    SafeId,
    StrictModel,
)
from continucare.knowledge.ops.read_model import build_governance_readiness
from continucare.terminology.core_catalog import (
    BENCHMARK_KEYS,
    CORE_CATALOG_V2_FILE,
    CoreSymptomCatalogV2,
    load_core_symptom_catalog_v2,
)


CORE_SYMPTOM_ALIAS_GAP_ID = (
    "gap-core-symptom-catalog-terminology-alias-review-pending"
)
_REQUIRED_ALIAS_REVIEW_ROLES = (
    ReviewerRole.TERMINOLOGIST,
    ReviewerRole.RIGHTS_OFFICER,
    ReviewerRole.KNOWLEDGE_CURATOR,
)
_MISSING_FORMAL_EVIDENCE = (
    "formal_review_packet_for_exact_alias_audit",
    "non_synthetic_formally_verified_terminologist_review_event",
    "non_synthetic_formally_verified_rights_officer_review_event",
    "non_synthetic_formally_verified_knowledge_curator_review_event",
    "distinct_reviewer_identities_and_principals",
    "reviewer_verifier_attestation_for_each_formal_decision",
    "hash_pinned_successor_readiness_manifest",
    "independent_post_resolution_consumer_review",
)


class CoreSymptomDisplayLabels(StrictModel):
    preferred_zh: NonBlank
    preferred_en: NonBlank
    zh_label_status: Literal[
        "v1_preferred_display_only_not_formal_patient_expression_review",
        "benchmark_display_only_pending_formal_terminology_review",
    ]
    en_label_status: Literal[
        "benchmark_display_only_pending_formal_translation_review"
    ] = "benchmark_display_only_pending_formal_translation_review"
    matchable: Literal[False] = False


class WithheldAliasReadiness(StrictModel):
    alias_zh: NonBlank
    source_alias_index: int = Field(ge=1)
    status: Literal["withheld_pending_formal_terminology_review"] = (
        "withheld_pending_formal_terminology_review"
    )
    boundary_reason: AliasAuditReason
    matchable: Literal[False] = False
    semantic_equivalence_status: Literal["not_established"] = "not_established"


class CoreSymptomRecordReadDTO(StrictModel):
    catalog_id: Literal["continucare-core-symptom-catalog"]
    catalog_version: Literal["2.0.0"]
    benchmark_id: SafeId
    benchmark_key: SafeId
    preferred_zh: NonBlank
    preferred_en: NonBlank
    concept_status: Literal["reused_concept", "alias_candidate", "internal_candidate"]
    existing_concept_ref: SafeId | None = None
    candidate_target_ref: SafeId | None = None
    mapping_status: Literal[
        "inherited_v1_reference_unverified",
        "pending_unverified",
    ]
    semantic_boundary_codes: tuple[SafeId, ...] = Field(min_length=1)
    display_labels: CoreSymptomDisplayLabels
    withheld_alias_count: int = Field(ge=0)
    withheld_aliases: tuple[WithheldAliasReadiness, ...]
    approved_match_aliases: tuple[NonBlank, ...] = Field(
        default_factory=tuple,
        max_length=0,
    )
    terminology_review_status: Literal["pending_formal_terminology_review"] = (
        "pending_formal_terminology_review"
    )
    open_gap_ids: tuple[SafeId, ...]
    consumer_integration_ready: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_alias_counts(self) -> "CoreSymptomRecordReadDTO":
        if self.withheld_alias_count != len(self.withheld_aliases):
            raise ValueError("withheld alias count differs from immutable details")
        if self.concept_status == "reused_concept" and not self.open_gap_ids:
            raise ValueError("reused concept read DTO must expose its open alias Gap")
        return self


class CoreSymptomAliasReadinessDTO(StrictModel):
    audit_id: Literal["core-symptom-alias-technical-boundary-audit"]
    audit_version: Literal[1]
    audit_kind: Literal["technical_boundary_audit"]
    catalog_id: Literal["continucare-core-symptom-catalog"]
    catalog_version: Literal["2.0.0"]
    audited_benchmark_keys: tuple[SafeId, ...] = Field(min_length=9, max_length=9)
    audited_alias_count: Literal[35]
    preferred_display_label_count: Literal[9]
    withheld_alias_count: Literal[26]
    approved_match_alias_count: Literal[0] = 0
    terminology_review_status: Literal["pending_formal_terminology_review"]
    formal_terminologist_review_completed: Literal[False] = False
    clinical_patient_expression_validation_completed: Literal[False] = False
    contains_patient_data: Literal[False] = False
    open_gap_ids: tuple[SafeId, ...] = Field(min_length=1)
    consumer_integration_ready: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"


class AliasGapResolutionReadinessDTO(StrictModel):
    gap_id: Literal[
        "gap-core-symptom-catalog-terminology-alias-review-pending"
    ]
    lifecycle: Literal["open"]
    required_gate: Literal["terminology_mapping_promotion"]
    required_roles: tuple[ReviewerRole, ...] = Field(min_length=3)
    formal_decision_present: Literal[False] = False
    valid_attestations_present: Literal[False] = False
    successor_manifest_present: Literal[False] = False
    resolution_permitted: Literal[False] = False
    consumer_integration_ready: Literal[False] = False
    synthetic_review_events_sufficient: Literal[False] = False
    same_identity_or_principal_sufficient: Literal[False] = False
    model_output_accepted_as_reviewer_evidence: Literal[False] = False
    local_boolean_override_available: Literal[False] = False
    missing_formal_evidence: tuple[SafeId, ...] = Field(min_length=8)
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_frozen_gate(self) -> "AliasGapResolutionReadinessDTO":
        if self.required_roles != tuple(item.value for item in _REQUIRED_ALIAS_REVIEW_ROLES):
            raise ValueError("alias Gap readiness cannot weaken required reviewer roles")
        if self.missing_formal_evidence != _MISSING_FORMAL_EVIDENCE:
            raise ValueError("alias Gap readiness must expose the complete missing evidence")
        return self


class CoreSymptomCatalogReadModel(StrictModel):
    catalog_id: Literal["continucare-core-symptom-catalog"]
    catalog_version: Literal["2.0.0"]
    records: tuple[CoreSymptomRecordReadDTO, ...] = Field(
        min_length=12,
        max_length=12,
    )
    alias_readiness: CoreSymptomAliasReadinessDTO
    gap_resolution_readiness: AliasGapResolutionReadinessDTO
    release_ready: Literal[False] = False
    consumer_integration_ready: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_ordered_catalog(self) -> "CoreSymptomCatalogReadModel":
        if tuple(item.benchmark_key for item in self.records) != BENCHMARK_KEYS:
            raise ValueError("Core Symptom read model record order differs")
        if any(item.approved_match_aliases for item in self.records):
            raise ValueError("unreviewed Core Symptom aliases cannot be matchable")
        return self

    def record(self, benchmark_key: str) -> CoreSymptomRecordReadDTO:
        record = next(
            (item for item in self.records if item.benchmark_key == benchmark_key),
            None,
        )
        if record is None:
            raise LookupError(f"unknown Core Symptom benchmark {benchmark_key!r}")
        return record


def build_core_symptom_catalog_read_model(
    bundle: KnowledgeOpsBundle,
    catalog: CoreSymptomCatalogV2,
) -> CoreSymptomCatalogReadModel:
    """Build from verified immutable inputs; no caller readiness override exists."""

    audit = bundle.core_symptom_alias_audit
    if audit is None:
        raise KnowledgeOpsManifestError(
            "Core Symptom consumer read model requires the hash-pinned alias audit"
        )
    catalog = _require_hash_pinned_canonical_catalog(catalog, audit)
    gap = _resolve_exact_alias_gap(bundle, catalog)
    gate = bundle.review_gate(GovernanceGate.TERMINOLOGY_MAPPING_PROMOTION)
    if gate.required_roles != tuple(item.value for item in _REQUIRED_ALIAS_REVIEW_ROLES):
        raise KnowledgeOpsManifestError(
            "terminology mapping review policy differs from the frozen three-role gate"
        )
    governance = build_governance_readiness(bundle)
    if governance.consumer_integration_ready:
        raise KnowledgeOpsManifestError(
            "open Core Symptom alias Gap cannot yield consumer readiness"
        )

    audit_by_key = {item.benchmark_key: item for item in audit.concept_audits}
    records: list[CoreSymptomRecordReadDTO] = []
    for record in catalog.records:
        concept_audit = audit_by_key.get(record.benchmark_key)
        if record.concept_status == "reused_concept" and concept_audit is None:
            raise KnowledgeOpsManifestError(
                "reused Core Symptom concept is absent from the alias audit"
            )
        if record.concept_status != "reused_concept" and concept_audit is not None:
            raise KnowledgeOpsManifestError(
                "alias audit cannot attach inherited aliases to a candidate concept"
            )
        withheld = ()
        zh_status = "benchmark_display_only_pending_formal_terminology_review"
        open_gap_ids: tuple[str, ...] = ()
        if concept_audit is not None:
            withheld = tuple(
                WithheldAliasReadiness(
                    alias_zh=item.alias_zh,
                    source_alias_index=item.source_alias_index,
                    boundary_reason=item.boundary_reason,
                )
                for item in concept_audit.aliases
                if item.source_role == "inherited_v1_alias"
            )
            zh_status = (
                "v1_preferred_display_only_not_formal_patient_expression_review"
            )
            open_gap_ids = (gap.gap_id,)
        records.append(
            CoreSymptomRecordReadDTO(
                catalog_id=catalog.catalog_id,
                catalog_version=catalog.catalog_version,
                benchmark_id=record.benchmark_id,
                benchmark_key=record.benchmark_key,
                preferred_zh=record.preferred_zh,
                preferred_en=record.preferred_en,
                concept_status=record.concept_status,
                existing_concept_ref=record.existing_concept_ref,
                candidate_target_ref=record.candidate_target_ref,
                mapping_status=record.mapping_status,
                semantic_boundary_codes=record.semantic_boundary_codes,
                display_labels=CoreSymptomDisplayLabels(
                    preferred_zh=record.preferred_zh,
                    preferred_en=record.preferred_en,
                    zh_label_status=zh_status,
                ),
                withheld_alias_count=len(withheld),
                withheld_aliases=withheld,
                approved_match_aliases=(),
                open_gap_ids=open_gap_ids,
            )
        )

    alias_readiness = _build_alias_readiness(audit, gap)
    gap_readiness = AliasGapResolutionReadinessDTO(
        gap_id=gap.gap_id,
        lifecycle=gap.lifecycle,
        required_gate=gate.gate,
        required_roles=gate.required_roles,
        missing_formal_evidence=_MISSING_FORMAL_EVIDENCE,
    )
    return CoreSymptomCatalogReadModel(
        catalog_id=catalog.catalog_id,
        catalog_version=catalog.catalog_version,
        records=tuple(records),
        alias_readiness=alias_readiness,
        gap_resolution_readiness=gap_readiness,
    )


def _require_hash_pinned_canonical_catalog(
    catalog: CoreSymptomCatalogV2,
    audit: CoreSymptomAliasAudit,
) -> CoreSymptomCatalogV2:
    """Reject every caller catalog that differs from the pinned repository bytes."""

    canonical_bytes = CORE_CATALOG_V2_FILE.read_bytes()
    if hashlib.sha256(canonical_bytes).hexdigest() != audit.catalog_sha256:
        raise KnowledgeOpsManifestError(
            "Core Symptom read model canonical catalog SHA-256 differs from alias audit"
        )
    canonical_catalog = load_core_symptom_catalog_v2()
    if not isinstance(catalog, CoreSymptomCatalogV2) or catalog.model_dump(
        mode="json"
    ) != canonical_catalog.model_dump(mode="json"):
        raise KnowledgeOpsManifestError(
            "caller-supplied Core Symptom catalog differs from hash-pinned canonical catalog"
        )
    return canonical_catalog


def _resolve_exact_alias_gap(
    bundle: KnowledgeOpsBundle,
    catalog: CoreSymptomCatalogV2,
) -> ReadinessGap:
    matches = tuple(
        item
        for item in bundle.readiness_gaps
        if item.gap_id == CORE_SYMPTOM_ALIAS_GAP_ID
    )
    if len(matches) != 1:
        raise KnowledgeOpsManifestError(
            "Core Symptom consumer read model requires the exact open alias Gap"
        )
    gap = matches[0]
    expected_refs = tuple(
        item.existing_concept_ref
        for item in catalog.records
        if item.concept_status == "reused_concept"
        and item.existing_concept_ref is not None
    )
    if (
        gap.gap_kind != ReadinessGapKind.TERMINOLOGY_ALIAS_REVIEW_PENDING.value
        or gap.lifecycle != "open"
        or gap.blocks != (ReadinessBlock.CONSUMER_INTEGRATION.value,)
        or gap.subject.subject_kind != "core_symptom_catalog"
        or gap.subject.catalog_id != catalog.catalog_id
        or gap.subject.catalog_version != catalog.catalog_version
        or gap.subject.concept_refs != expected_refs
    ):
        raise KnowledgeOpsManifestError(
            "Core Symptom alias Gap differs from the exact catalog boundary"
        )
    return gap


def _build_alias_readiness(
    audit: CoreSymptomAliasAudit,
    gap: ReadinessGap,
) -> CoreSymptomAliasReadinessDTO:
    aliases = tuple(
        alias
        for concept in audit.concept_audits
        for alias in concept.aliases
    )
    withheld = tuple(
        alias
        for alias in aliases
        if alias.disposition == "withheld_pending_formal_terminology_review"
    )
    display = tuple(alias for alias in aliases if alias.display_label)
    if len(aliases) != 35 or len(withheld) != 26 or len(display) != 9:
        raise KnowledgeOpsManifestError(
            "Core Symptom alias audit totals differ from the exact source catalogs"
        )
    return CoreSymptomAliasReadinessDTO(
        audit_id=audit.audit_id,
        audit_version=audit.audit_version,
        audit_kind=audit.audit_kind,
        catalog_id=audit.catalog_id,
        catalog_version=audit.catalog_version,
        audited_benchmark_keys=tuple(
            item.benchmark_key for item in audit.concept_audits
        ),
        audited_alias_count=len(aliases),
        preferred_display_label_count=len(display),
        withheld_alias_count=len(withheld),
        terminology_review_status=audit.terminology_review_status,
        open_gap_ids=(gap.gap_id,),
    )


@lru_cache(maxsize=1)
def load_builtin_core_symptom_catalog_read_model() -> CoreSymptomCatalogReadModel:
    return build_core_symptom_catalog_read_model(
        load_builtin_ops_bundle(),
        load_core_symptom_catalog_v2(),
    )


def list_core_symptom_records() -> tuple[CoreSymptomRecordReadDTO, ...]:
    return load_builtin_core_symptom_catalog_read_model().records


def get_core_symptom_record(benchmark_key: str) -> CoreSymptomRecordReadDTO:
    return load_builtin_core_symptom_catalog_read_model().record(benchmark_key)


def get_core_symptom_alias_readiness() -> CoreSymptomAliasReadinessDTO:
    return load_builtin_core_symptom_catalog_read_model().alias_readiness


def get_core_symptom_gap_resolution_readiness() -> AliasGapResolutionReadinessDTO:
    return load_builtin_core_symptom_catalog_read_model().gap_resolution_readiness

"""Deterministic developer-facing rendering of registered Knowledge Evidence."""

from __future__ import annotations

import json

from continucare.knowledge.models import (
    CoverageGapKind,
    SourcedClinicalClaim,
    WorkflowDesignDecision,
    artifact_key,
)
from continucare.knowledge.registry import (
    LoadMode,
    PathwayKnowledgeView,
    SymptomKnowledgeView,
)


KNOWLEDGE_DISCLAIMER = (
    "本页是已登记知识与审核状态的只读视图，不处理患者数据。",
    "知识登记本身不批准、不启用、也不执行任何 clinical rule 或其他 artifact。",
    "未审核 / not_assessed 不表示安全、有效或可用于临床；runtime 资格由 artifact 自身的独立治理决定。",
)


def render_pathway_knowledge(view: PathwayKnowledgeView) -> str:
    """Render only controlled registry text and derived governance status."""

    lines = [
        f"Knowledge Evidence — {view.pathway.pathway_code} v{view.pathway.pathway_version}",
        f"mode={view.mode.value.upper()}",
    ]
    if view.mode == LoadMode.HISTORICAL:
        lines.append("HISTORICAL INSPECTION：失效或未解析记录仅供审计，不是 current 关系。")
    lines.extend(
        [
            "",
            *KNOWLEDGE_DISCLAIMER,
            "",
            f"Coverage: unique_artifacts={view.unique_artifact_count}; "
            f"registered_relationships={view.registered_relationship_count}; "
            f"explicit_gaps={view.explicit_gap_count}; "
            "verified_citation_relationships="
            f"{view.verified_citation_relationship_count}; "
            "claim_review_approved_relationships="
            f"{view.claim_review_approved_relationship_count}",
            "",
            "Registered relationships",
        ]
    )
    for binding in sorted(
        view.bindings, key=lambda item: (item.binding_id, item.binding_version)
    ):
        claim = view.claims[binding.claim.key()]
        summary = view.review_summaries[claim.ref.key()]
        resolution = view.artifact_resolutions[
            ("binding", binding.binding_id, binding.binding_version)
        ]
        lines.extend(
            [
                f"- {binding.binding_id}@{binding.binding_version}",
                f"  artifact={_artifact_label(binding.artifact)}",
                f"  resolution={'resolved' if resolution.resolved else 'unresolved'}",
                f"  claim={claim.claim_id}@{claim.claim_version}",
                f"  statement={claim.statement}",
                f"  supports={'；'.join(claim.supports)}",
                f"  does_not_support={'；'.join(claim.does_not_support)}",
                "  applicable_scope="
                + json.dumps(
                    claim.applicable_scope.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                f"  review={summary.aggregate}",
                "  review_axes="
                + json.dumps(dict(summary.axes), ensure_ascii=False, sort_keys=True),
                f"  pharmacy_review={summary.pharmacy or 'not_assessed'}",
                f"  claim_lifecycle={claim.lifecycle}",
                f"  binding_lifecycle={binding.lifecycle}",
                "  selection="
                + (
                    "current"
                    if binding.ref.key() in view.current_binding_keys
                    else "historical"
                ),
                "  knowledge_effect=informational_only; runtime_authority=none",
                "  required_independent_approvals="
                + ",".join(binding.required_independent_approvals),
            ]
        )
        if isinstance(claim, SourcedClinicalClaim):
            for citation in sorted(claim.citations, key=lambda item: item.citation_id):
                source = view.sources[citation.source.key()]
                lines.append(
                    "  source="
                    f"{source.title} [{source.source_id}@{source.record_version}]; "
                    f"document_version={source.document_version or 'not_available'}; "
                    f"url={source.canonical_url or source.access_urls[0].url}; "
                    f"locator={json.dumps(citation.locator.model_dump(mode='json'), ensure_ascii=False, sort_keys=True)}; "
                    f"source_registry_status={source.registry_status}; "
                    "source_selection="
                    + (
                        "current"
                        if source.ref.key() in view.current_source_keys
                        else "historical"
                    )
                    + "; "
                    "source_internal_consistency_review="
                    f"{view.source_review_status[source.ref.key()]}; "
                    f"source_integrity={view.source_content_status[source.ref.key()]}"
                )
        elif isinstance(claim, WorkflowDesignDecision):
            lines.extend(
                [
                    f"  owner_role={claim.owner_role}",
                    f"  decision_status={claim.decision_status}",
                    "  source_integrity=not_applicable",
                ]
            )
    lines.extend(["", "Explicit unbound gaps"])
    for gap in sorted(view.gaps, key=lambda item: (item.gap_id, item.gap_version)):
        resolution = view.artifact_resolutions[("gap", gap.gap_id, gap.gap_version)]
        source_integrity = (
            "not_applicable"
            if gap.gap_kind == CoverageGapKind.DESIGN_GOVERNANCE_METADATA
            else "not_assessed"
        )
        lines.extend(
            [
                f"- {gap.gap_id}@{gap.gap_version}",
                f"  artifact={_artifact_label(gap.artifact)}",
                f"  resolution={'resolved' if resolution.resolved else 'unresolved'}",
                f"  gap_kind={gap.gap_kind}",
                f"  gap_lifecycle={gap.lifecycle}",
                "  selection="
                + (
                    "current"
                    if gap.ref.key() in view.current_gap_keys
                    else "historical"
                ),
                f"  reason={gap.reason}",
                f"  source_integrity={source_integrity}",
            ]
        )
    return "\n".join(lines) + "\n"


def render_symptom_knowledge(view: SymptomKnowledgeView) -> str:
    """Render one reference-only symptom index without minting display facts."""

    record = view.record
    resolution = view.catalog_resolution
    lines = [
        f"Symptom Knowledge — {record.symptom_index_id}@{record.record_version}",
        f"mode={view.mode.value.upper()}",
        "reference_only=true; runtime_authority=none",
        f"catalog_ref={record.catalog_term.catalog_id}|"
        f"{record.catalog_term.catalog_version}|{record.catalog_term.concept_id}",
        f"catalog_resolution={'resolved' if resolution.resolved else 'unresolved'}",
    ]
    if resolution.resolved:
        assert resolution.concept is not None
        concept = resolution.concept
        lines.extend(
            [
                f"preferred_zh={concept.preferred_zh}",
                "coding="
                f"{concept.coding.system}|{concept.coding.version or 'not_available'}|"
                f"{concept.coding.code}|{concept.coding.display or 'not_available'}",
            ]
        )
    else:
        lines.append(f"unresolved_detail={resolution.detail}")
    lines.extend(["", "Exact claims"])
    for claim in view.claims:
        summary = view.review_summaries[claim.ref.key()]
        lines.extend(
            [
                f"- {claim.ref}",
                f"  statement={claim.statement}",
                f"  supports={'；'.join(claim.supports)}",
                f"  does_not_support={'；'.join(claim.does_not_support)}",
                "  applicable_scope="
                + json.dumps(
                    claim.applicable_scope.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                f"  review={summary.aggregate}",
            ]
        )
        if isinstance(claim, SourcedClinicalClaim):
            sources = {item.ref.key(): item for item in view.sources}
            for citation in claim.citations:
                source = sources[citation.source.key()]
                lines.append(
                    f"  source={source.ref}; authority="
                    f"{source.issuing_authority or 'not_available'}; "
                    f"document_version={source.document_version or 'not_available'}; "
                    f"locator={json.dumps(citation.locator.model_dump(mode='json'), ensure_ascii=False, sort_keys=True)}; "
                    f"access={source.access.mode}; "
                    f"integrity={view.source_content_status[source.ref.key()]}"
                )
    lines.extend(["", "Exact bindings"])
    for binding in view.bindings:
        lines.append(
            f"- {binding.ref}; pathway={binding.pathway.pathway_code}|"
            f"{binding.pathway.pathway_version}; artifact={_artifact_label(binding.artifact)}"
        )
    lines.extend(["", "Exact coverage gaps"])
    for gap in view.gaps:
        lines.append(
            f"- {gap.ref}; pathway={gap.pathway.pathway_code}|"
            f"{gap.pathway.pathway_version}; gap_kind={gap.gap_kind}; reason={gap.reason}"
        )
    return "\n".join(lines) + "\n"


def _artifact_label(artifact) -> str:
    return "|".join(artifact_key(artifact))

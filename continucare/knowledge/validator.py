"""Cross-file and source-integrity checks for an L1 release."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from continucare.knowledge.models import KnowledgeRelease
from continucare.pathways import load_builtin_pathways


class KnowledgeValidationError(ValueError):
    pass


DATA_FILES = {
    "source_registry_sha256": "source_registry.json",
    "product_registry_sha256": "product_registry.json",
    "evidence_claims_sha256": "evidence_claims.json",
    "metric_definitions_sha256": "metric_definitions.json",
    "terminology_manifest_sha256": "terminology_manifest.json",
    "patient_content_sha256": "patient_content.zh-CN.json",
    "data_quality_rules_sha256": "data_quality_rules.json",
    "clinical_rules_sha256": "clinical_rules.json",
    "coverage_report_sha256": "coverage_report.json",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_release(
    release: KnowledgeRelease,
    *,
    data_dir: Path | None = None,
    repository_root: Path | None = None,
) -> list[str]:
    errors: list[str] = []

    if release.manifest.status != "engineering_validated":
        errors.append("runtime release manifest is not engineering_validated")

    def unique(items: list, attribute: str, label: str) -> set[str]:
        values = [getattr(item, attribute) for item in items]
        if len(values) != len(set(values)):
            errors.append(f"duplicate {label}")
        return set(values)

    source_ids = unique(release.sources, "source_id", "source_id")
    product_ids = unique(release.products, "product_id", "product_id")
    claim_ids = unique(release.evidence_claims, "claim_id", "claim_id")
    metric_ids = unique(release.metrics, "metric_id", "metric_id")
    sources_by_id = {item.source_id: item for item in release.sources}
    claims_by_id = {item.claim_id: item for item in release.evidence_claims}
    unique(release.patient_content, "content_id", "content_id")
    link_ids = [item.link_id for item in release.patient_content]
    if len(link_ids) != len(set(link_ids)):
        errors.append("duplicate patient-content link_id")

    for source in release.sources:
        if source.superseded_by is not None:
            errors.append(f"superseded source {source.source_id} is in current release")
        if source.runtime_eligible and source.verification_status != "verified":
            errors.append(f"runtime source {source.source_id} is not verified")
        if source.runtime_eligible and source.jurisdiction != "CN":
            errors.append(f"non-CN source {source.source_id} is runtime eligible")

    for product in release.products:
        if product.label_source_id and product.label_source_id not in source_ids:
            errors.append(f"product {product.product_id} has unknown label source")
        unknown_approval_sources = set(product.approval_source_ids) - source_ids
        if unknown_approval_sources:
            errors.append(f"product {product.product_id} has unknown approval source")
        if product.approval_status == "approved" and not product.approval_numbers:
            errors.append(f"approved product {product.product_id} lacks approval number")

    for claim in release.evidence_claims:
        if claim.source_id not in source_ids:
            errors.append(f"claim {claim.claim_id} has unknown source")
            continue
        if set(claim.product_ids) - product_ids:
            errors.append(f"claim {claim.claim_id} has unknown product")
        if claim.product_scope_kind == "product_specific" and not claim.product_ids:
            errors.append(f"claim {claim.claim_id} lacks product scope")
        source = next(item for item in release.sources if item.source_id == claim.source_id)
        if claim.runtime_eligible and not source.runtime_eligible:
            errors.append(f"claim {claim.claim_id} exceeds source runtime permission")
        if claim.runtime_eligible and claim.review_status != "engineering_reviewed":
            errors.append(f"runtime claim {claim.claim_id} is not engineering reviewed")
        if not claim.indications or not claim.populations:
            errors.append(f"claim {claim.claim_id} lacks indication/population scope")

    for metric in release.metrics:
        unknown = set(metric.evidence_claim_ids) - claim_ids
        if unknown:
            errors.append(f"metric {metric.metric_id} has unknown evidence claims")
        if metric.runtime_eligible:
            usable = [
                claim
                for claim in release.evidence_claims
                if claim.claim_id in metric.evidence_claim_ids and claim.runtime_eligible
            ]
            if not usable:
                errors.append(f"runtime metric {metric.metric_id} lacks runtime evidence")
            cn_usable = [
                claim
                for claim in usable
                if next(
                    source
                    for source in release.sources
                    if source.source_id == claim.source_id
                ).jurisdiction
                == "CN"
            ]
            if not cn_usable:
                errors.append(f"runtime metric {metric.metric_id} lacks CN evidence")
        if metric.product_scope:
            unknown_products = set(metric.product_scope) - product_ids
            if unknown_products:
                errors.append(f"metric {metric.metric_id} has unknown product scope")
            scoped_claims = [
                claim
                for claim in release.evidence_claims
                if claim.claim_id in metric.evidence_claim_ids and claim.runtime_eligible
            ]
            if not any(set(claim.product_ids) & set(metric.product_scope) for claim in scoped_claims):
                errors.append(f"metric {metric.metric_id} lacks product-scoped evidence")
            for product_id in metric.product_scope:
                product = next(item for item in release.products if item.product_id == product_id)
                if (metric.indication_scope or metric.population_scope) and not any(
                    (
                        not metric.indication_scope
                        or scope.indication in set(metric.indication_scope)
                    )
                    and (
                        not metric.population_scope
                        or bool(set(scope.populations) & set(metric.population_scope))
                    )
                    for scope in product.indication_population_scopes
                ):
                    errors.append(
                        f"metric {metric.metric_id} conflicts with product "
                        "indication-population scope"
                    )

    for entry in release.terminology:
        if entry.metric_id not in metric_ids:
            errors.append(f"terminology entry has unknown metric {entry.metric_id}")

    for content in release.patient_content:
        if content.metric_id and content.metric_id not in metric_ids:
            errors.append(f"content {content.content_id} has unknown metric")
        if set(content.evidence_claim_ids) - claim_ids:
            errors.append(f"content {content.content_id} has unknown evidence")
        restricted_claims = [
            claims_by_id[claim_id]
            for claim_id in content.evidence_claim_ids
            if claim_id in claims_by_id
            and claims_by_id[claim_id].source_id in sources_by_id
            and sources_by_id[claims_by_id[claim_id].source_id].license_status
            == "restricted"
        ]
        if restricted_claims:
            errors.append(
                f"content {content.content_id} embeds restricted-source content"
            )

    if release.clinical_rules:
        errors.append("clinical_rules.json must remain empty before clinical approval")

    coverage = release.coverage
    expected_coverage = {
        "source_count": len(release.sources),
        "verified_source_count": sum(
            source.verification_status == "verified" for source in release.sources
        ),
        "cn_source_count": sum(source.jurisdiction == "CN" for source in release.sources),
        "product_record_count": len(release.products),
        "verified_product_record_count": sum(
            product.verification_status == "verified" for product in release.products
        ),
        "incomplete_product_record_count": sum(
            product.verification_status != "verified" for product in release.products
        ),
        "evidence_claim_count": len(release.evidence_claims),
        "runtime_evidence_claim_count": sum(
            claim.runtime_eligible for claim in release.evidence_claims
        ),
        "metric_count": len(release.metrics),
        "runtime_metric_count": sum(metric.runtime_eligible for metric in release.metrics),
        "clinical_rule_count": len(release.clinical_rules),
    }
    if coverage.release_id != release.manifest.release_id:
        errors.append("coverage report release_id mismatch")
    if datetime.fromisoformat(coverage.generated_at) != datetime.fromisoformat(
        release.manifest.created_at
    ):
        errors.append("coverage report build timestamp mismatch")
    for field, expected in expected_coverage.items():
        if getattr(coverage, field) != expected:
            errors.append(f"coverage report {field} is stale")
    if {item.product_id for item in coverage.products} != product_ids:
        errors.append("coverage report products are stale")
    if {item.metric_id for item in coverage.metrics} != metric_ids:
        errors.append("coverage report metrics are stale")
    patient_link_ids = {item.link_id for item in release.patient_content}
    for metric_coverage in coverage.metrics:
        unknown_links = set(metric_coverage.questionnaire_link_ids) - patient_link_ids
        if unknown_links:
            errors.append(
                f"coverage metric {metric_coverage.metric_id} references unpublished "
                "patient content"
            )
    expected_public_artifacts = {
        "Questionnaire/glp1-14d-followup-v1",
        "PlanDefinition/glp1-14d-followup-plan-v1",
        "Contract/glp1_14d_observation_mapping.json",
    }
    if set(coverage.compiled_artifacts) != expected_public_artifacts:
        errors.append("coverage report public artifacts are stale")

    if data_dir:
        for manifest_field, file_name in DATA_FILES.items():
            actual = sha256_file(data_dir / file_name)
            expected = getattr(release.manifest, manifest_field)
            if actual != expected:
                errors.append(f"manifest digest mismatch for {file_name}")

    if repository_root:
        for source in release.sources:
            if not source.local_path:
                continue
            path = repository_root / source.local_path
            if not path.is_file():
                errors.append(f"local source missing: {source.local_path}")
            elif sha256_file(path) != source.sha256:
                errors.append(f"local source digest mismatch: {source.local_path}")

    pathways = [
        pathway
        for pathway in load_builtin_pathways().list()
        if pathway.knowledge_release_id == release.manifest.release_id
    ]
    for pathway in pathways:
        if set(pathway.product_scope) - product_ids:
            errors.append(f"pathway {pathway.code} has unknown product scope")
        scoped_products = [
            product for product in release.products if product.product_id in pathway.product_scope
        ]
        if (pathway.indication_scope or pathway.population_scope) and not all(
            any(
                (
                    not pathway.indication_scope
                    or scope.indication in set(pathway.indication_scope)
                )
                and (
                    not pathway.population_scope
                    or bool(set(scope.populations) & set(pathway.population_scope))
                )
                for scope in product.indication_population_scopes
            )
            for product in scoped_products
        ):
            errors.append(
                f"pathway {pathway.code} conflicts with product "
                "indication-population scopes"
            )

    if errors:
        raise KnowledgeValidationError("; ".join(errors))
    return [
        f"release {release.manifest.release_id} is cross-file valid",
        f"{len(release.sources)} sources registered",
        f"{len(release.metrics)} metrics registered",
        "clinical rules are fail-closed (empty)",
    ]


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def validate_runtime_artifacts(release: KnowledgeRelease) -> list[str]:
    """Recompile the runtime contracts and verify their release-manifest digests."""

    from continucare.knowledge.compiler import (
        compile_observation_mappings,
        compile_plan_definition,
        compile_questionnaire,
    )

    artifacts = {
        "questionnaire_sha256": compile_questionnaire(release),
        "plan_definition_sha256": compile_plan_definition(release),
        "observation_mapping_sha256": compile_observation_mappings(release),
    }
    errors = []
    for field, artifact in artifacts.items():
        actual = hashlib.sha256(canonical_json_bytes(artifact)).hexdigest()
        if actual != getattr(release.manifest, field):
            errors.append(f"manifest digest mismatch for compiled {field}")
    if errors:
        raise KnowledgeValidationError("; ".join(errors))
    return ["compiled runtime artifact digests match release manifest"]


def validate_packaged_release(release: KnowledgeRelease) -> list[str]:
    """Fail closed on packaged JSON drift before a CareSession can start."""

    data_dir = Path(str(files("continucare.knowledge.data.cn_glp1.v1")))
    messages = validate_release(release, data_dir=data_dir)
    messages.extend(validate_runtime_artifacts(release))
    return messages

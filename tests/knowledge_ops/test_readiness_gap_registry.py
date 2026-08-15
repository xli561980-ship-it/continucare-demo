from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from continucare.knowledge.ops.manifests import (
    DirectoryBundleSource,
    load_builtin_ops_bundle,
    load_ops_bundle,
)
from continucare.knowledge.ops.models import (
    KnowledgeOpsManifestError,
    ReadinessGap,
    ReadinessGapKind,
)
from continucare.knowledge.ops.read_model import build_ops_read_model
from continucare.knowledge.ops.source_connectors import OFFICIAL_ENDPOINT_POLICIES
from continucare.knowledge.ops.source_connectors.live_validation import (
    LiveValidationRecord,
    LiveValidationReport,
)
from continucare.terminology.core_catalog import load_core_symptom_catalog_v2


MANIFEST_ROOT = Path(__file__).parents[2] / "continucare" / "knowledge" / "manifests_v2"
EXPECTED_SOURCE_REFS = {
    ("source-dailymed", 1),
    ("source-ema-website-data", 1),
    ("source-medlineplus", 1),
    ("nlm-pubmed-metadata", 2),
    ("source-pmc-open-access", 1),
}
EXPECTED_GAP_IDS = {
    "gap-p1a-dailymed-live-validation-not-attempted",
    "gap-p1a-ema-live-validation-not-attempted",
    "gap-p1a-medlineplus-live-validation-not-attempted",
    "gap-p1a-pubmed-live-validation-not-attempted",
    "gap-p1a-pmc-live-validation-not-attempted",
    "gap-p1a-dailymed-rights-unresolved",
    "gap-p1a-ema-rights-unresolved",
    "gap-p1a-medlineplus-rights-unresolved",
    "gap-p1a-pubmed-rights-unresolved",
    "gap-p1a-pmc-rights-unresolved",
    "gap-p1b-cold-import-socket-proof-pending",
    "gap-core-symptom-catalog-terminology-alias-review-pending",
}


def test_legacy_bundle_indexes_load_with_no_backfilled_readiness_gaps() -> None:
    source = DirectoryBundleSource(MANIFEST_ROOT)
    for index_path, version in (
        ("bundle_index_v2.json", 1),
        ("bundle_index_v2_2.json", 2),
    ):
        bundle = load_ops_bundle(source, index_path=index_path)
        read_model = build_ops_read_model(bundle)
        assert bundle.index.bundle_version == version
        assert bundle.readiness_gaps == ()
        assert read_model.readiness_gaps == ()
        assert read_model.governance_readiness.registry_present is False
        assert read_model.governance_readiness.production_eligible is False
        assert read_model.governance_readiness.release_ready is False
        assert read_model.governance_readiness.consumer_integration_ready is False


def test_builtin_bundle_v4_loads_exact_frozen_12_open_readiness_gaps() -> None:
    bundle = load_builtin_ops_bundle()
    assert bundle.index.bundle_version == 4
    assert len(bundle.readiness_gaps) == 12
    assert {item.gap_id for item in bundle.readiness_gaps} == EXPECTED_GAP_IDS
    assert {item.lifecycle for item in bundle.readiness_gaps} == {"open"}
    counts = {
        kind.value: sum(item.gap_kind == kind.value for item in bundle.readiness_gaps)
        for kind in ReadinessGapKind
    }
    assert counts == {
        "live_validation_not_attempted": 5,
        "rights_unresolved": 5,
        "cold_import_socket_proof_pending": 1,
        "terminology_alias_review_pending": 1,
    }

    for kind in ("live_validation_not_attempted", "rights_unresolved"):
        refs = {
            item.subject.source_policy.key()
            for item in bundle.readiness_gaps
            if item.gap_kind == kind
        }
        assert refs == EXPECTED_SOURCE_REFS
    cold = next(
        item
        for item in bundle.readiness_gaps
        if item.gap_kind == "cold_import_socket_proof_pending"
    )
    assert cold.subject.gate == "cold_import_socket_proof"


def test_catalog_gap_reads_the_current_nine_reused_concept_refs() -> None:
    catalog = load_core_symptom_catalog_v2()
    expected = tuple(
        item.existing_concept_ref
        for item in catalog.records
        if item.concept_status == "reused_concept"
    )
    gap = next(
        item
        for item in load_builtin_ops_bundle().readiness_gaps
        if item.gap_kind == "terminology_alias_review_pending"
    )
    assert expected == (
        "nausea",
        "vomiting",
        "diarrhea",
        "abdominal-pain",
        "constipation",
        "decreased-appetite",
        "fatigue",
        "dizziness",
        "dyspnea",
    )
    assert gap.subject.catalog_id == catalog.catalog_id
    assert gap.subject.catalog_version == catalog.catalog_version
    assert gap.subject.concept_refs == expected
    assert gap.blocks == ("consumer_integration",)
    assert "production_eligibility" not in gap.blocks
    assert "knowledge_release" not in gap.blocks


def test_readiness_registry_references_policies_without_copying_policy_facts() -> None:
    raw = json.loads((MANIFEST_ROOT / "readiness_gaps_v1.json").read_bytes())
    serialized = json.dumps(raw, ensure_ascii=False)
    for prohibited_copy in (
        "license_posture",
        "live_network_enabled",
        "operation_rules",
        "maximum_response_bytes",
    ):
        assert prohibited_copy not in serialized


def test_current_open_only_gap_schema_cannot_express_resolved() -> None:
    gap = load_builtin_ops_bundle().readiness_gaps[0]
    with pytest.raises(ValidationError, match="lifecycle"):
        ReadinessGap.model_validate(
            {**gap.model_dump(mode="json"), "lifecycle": "resolved"}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ReadinessGap.model_validate(
            {
                **gap.model_dump(mode="json"),
                "resolved_by_principal_id": "synthetic-resolver",
                "resolution_evidence_ref": "urn:synthetic",
            }
        )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _repin(root: Path, relative_path: str) -> None:
    index_path = root / "bundle_index_v2_3.json"
    index = json.loads(index_path.read_bytes())
    payload = (root / relative_path).read_bytes()
    pinned = next(item for item in index["files"] if item["relative_path"] == relative_path)
    pinned["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    pinned["size"] = len(payload)
    _write_json(index_path, index)


@pytest.mark.parametrize(
    "inconsistency",
    [
        "rights_claims_verified_open",
        "live_policy_enabled",
        "rights_allows_nonmetadata_operation",
        "unknown_source_policy_ref",
    ],
)
def test_policy_gap_inconsistencies_fail_closed(
    tmp_path: Path, inconsistency: str
) -> None:
    root = tmp_path / "manifests_v2"
    shutil.copytree(MANIFEST_ROOT, root)
    if inconsistency == "unknown_source_policy_ref":
        path = root / "readiness_gaps_v1.json"
        payload = json.loads(path.read_bytes())
        payload["gaps"][0]["subject"]["source_policy"]["policy_id"] = "unknown-policy"
        _write_json(path, payload)
        _repin(root, path.name)
    else:
        path = root / "source_policies_v2_2.json"
        payload = json.loads(path.read_bytes())
        policy = next(
            item for item in payload["policies"] if item["policy_id"] == "source-dailymed"
        )
        if inconsistency == "rights_claims_verified_open":
            policy["license_posture"] = "verified_open"
        elif inconsistency == "live_policy_enabled":
            policy["live_network_enabled"] = True
        else:
            rule = next(
                item
                for item in policy["operation_rules"]
                if item["operation"] == "fetch_for_change_detection"
            )
            rule["decision"] = "allow"
        _write_json(path, payload)
        _repin(root, path.name)
    with pytest.raises(KnowledgeOpsManifestError):
        load_ops_bundle(
            DirectoryBundleSource(root), index_path="bundle_index_v2_3.json"
        )


def test_governance_readiness_is_default_deny_and_source_scoped() -> None:
    view = build_ops_read_model(load_builtin_ops_bundle()).governance_readiness
    assert view.registry_present is True
    assert view.registry_file_version == 1
    assert len(view.production_blocking_gap_ids) == 11
    assert len(view.release_blocking_gap_ids) == 11
    assert view.production_eligible is False
    assert view.release_ready is False
    assert view.consumer_integration_ready is False
    assert view.persistent_source_validation_claimed is False
    assert view.wrote_knowledge_state is False
    assert view.knowledge_effect == "informational_only"
    assert view.runtime_authority == "none"
    assert len(view.source_readiness) == 5
    for source in view.source_readiness:
        assert source.source_policy.key() in EXPECTED_SOURCE_REFS
        assert source.persistent_validation_status == "not_attempted"
        assert source.maximum_reuse == "metadata_link_only"
        assert source.production_eligible is False
        assert source.release_ready is False


def test_transient_validated_report_cannot_mutate_persistent_readiness() -> None:
    bundle = load_builtin_ops_bundle()
    before = build_ops_read_model(bundle).governance_readiness
    timestamp = datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc)
    records = tuple(
        LiveValidationRecord(
            source=endpoint.source_id,
            status="validated",
            official_documentation_url=endpoint.official_documentation_url,
            endpoint_origin=f"https://{endpoint.hostname}",
            endpoint_path_template=endpoint.path_template,
            timestamp=timestamp,
            http_status=200,
            normalized_mime=endpoint.allowed_media_types[0],
            byte_count=2,
            whole_response_sha256=hashlib.sha256(b"{}").hexdigest(),
            parsed_metadata_record_count=1,
            limitations=("Transient synthetic report fixture only.",),
        )
        for endpoint in OFFICIAL_ENDPOINT_POLICIES
    )
    report = LiveValidationReport(
        validator_id="continucare-knowledge-live-contract-validator-v1",
        generated_at=timestamp,
        request_count=5,
        records=records,
    )
    after = build_ops_read_model(load_builtin_ops_bundle()).governance_readiness
    assert {item.status for item in report.records} == {"validated"}
    assert report.wrote_knowledge_state is False
    assert report.release_ready is False
    assert before == after
    assert after.production_eligible is False
    assert after.release_ready is False
    assert {
        item.persistent_validation_status for item in after.source_readiness
    } == {"not_attempted"}

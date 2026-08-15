from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from continucare.knowledge.ops.manifests import (
    DirectoryBundleSource,
    load_builtin_ops_bundle,
    load_ops_bundle,
)
from continucare.knowledge.ops.models import (
    FileRef,
    KnowledgeOpsBundleIndex,
    PayloadEnvelope,
    SourceOperation,
)
from continucare.knowledge.ops.source_connectors import OFFICIAL_ENDPOINT_POLICIES


MANIFEST_ROOT = Path(__file__).parents[2] / "continucare" / "knowledge" / "manifests_v2"
OLD_CURRENT_REFS = (
    FileRef(file_id="knowledge-ops-safety-boundary", file_version=1),
    FileRef(file_id="knowledge-ops-source-policies", file_version=1),
    FileRef(file_id="knowledge-ops-coverage-profiles", file_version=1),
    FileRef(file_id="knowledge-ops-review-policy", file_version=1),
    FileRef(file_id="knowledge-ops-release-intent", file_version=1),
)
HISTORICAL_FILES = {
    "bundle_index_v2.json": ("03702a13464d0032677e88d433793ed4720820547cea59567201157b4fff4ddb", 2008),
    "bundle_index_v2_2.json": ("05993fa2418a4a52e06d3e83370ca042f9e8089862f0a52a08fcd2092fd38909", 2054),
    "bundle_index_v2_3.json": ("45f2d604277fb23604f6752b48dbcd5228356377d3cf48497648ef287038457e", 2373),
    "coverage_profiles_v2.json": ("ef939e3330b37cf8791b80a3942846fe7a2bda8c35670ca38122a99df787c298", 8217),
    "release_intent_v2.json": ("edc2fcd5d191d1fd6ccf2bd44b626fc4a82ff2f81dff783e5d39e38f8a517c6f", 908),
    "review_policy_v2.json": ("e6013664d1b126b60dec78522d988e296755ebe096c108f49d5862455a30709f", 3261),
    "readiness_gaps_v1.json": ("9dea836f1d987b38d0cd76342bc10c053c2969c57d1c0ff8f8e7ff90f8475025", 7632),
    "safety_boundary_v2.json": ("64fb14f5562389cbd1a78bde579b702810b40b654a864c147c6b0ea98b69ad43", 846),
    "source_policies_v2.json": ("273dbdc2d50023b6687edbb56a8ceef818daa907837bd9b066cd748300a487bf", 23732),
    "source_policies_v2_2.json": ("6914b875081741ca039ef18cea89e1338d19dda7f78a6c51a0d64e56c5e8bccd", 19327),
}


def test_every_bundle_index_replays_hash_size_ref_current_head_and_contiguity() -> None:
    source = DirectoryBundleSource(MANIFEST_ROOT)
    adapter = TypeAdapter(PayloadEnvelope)
    indexes = sorted(MANIFEST_ROOT.glob("bundle_index*.json"))
    assert [item.name for item in indexes] == [
        "bundle_index_v2.json",
        "bundle_index_v2_2.json",
        "bundle_index_v2_3.json",
        "bundle_index_v2_4.json",
    ]
    for path in indexes:
        index = KnowledgeOpsBundleIndex.model_validate_json(path.read_bytes())
        versions: dict[str, list[int]] = {}
        for pinned in index.files:
            payload = source.read_bytes(pinned.relative_path)
            assert len(payload) == pinned.size
            assert hashlib.sha256(payload).hexdigest() == pinned.manifest_sha256
            envelope = adapter.validate_json(payload)
            assert envelope.ref == pinned.ref
            versions.setdefault(pinned.ref.file_id, []).append(pinned.ref.file_version)
        for values in versions.values():
            assert sorted(values) == list(range(1, max(values) + 1))
        assert {item.key() for item in index.current_file_refs} == {
            (file_id, max(values)) for file_id, values in versions.items()
        }
        loaded = load_ops_bundle(source, index_path=path.name)
        assert loaded.index == index


def test_original_v2_files_and_old_index_current_refs_are_byte_stable() -> None:
    for name, (expected_sha, expected_size) in HISTORICAL_FILES.items():
        payload = (MANIFEST_ROOT / name).read_bytes()
        assert len(payload) == expected_size
        assert hashlib.sha256(payload).hexdigest() == expected_sha
    old = load_ops_bundle(
        DirectoryBundleSource(MANIFEST_ROOT), index_path="bundle_index_v2.json"
    )
    assert old.index.bundle_version == 1
    assert old.index.current_file_refs == OLD_CURRENT_REFS
    assert len(old.source_policies) == 8


def test_builtin_v2_4_materializes_incremental_policy_gap_and_alias_history() -> None:
    bundle = load_builtin_ops_bundle()
    assert bundle.index.bundle_version == 4
    assert len(bundle.source_policies) == 13
    assert len(bundle.readiness_gaps) == 12
    assert bundle.core_symptom_alias_audit is not None
    assert bundle.source_policy("nlm-pubmed-metadata", 1).policy_version == 1
    assert bundle.source_policy("nlm-pubmed-metadata", 2).policy_version == 2
    assert bundle.source_policy("nmpa-cn-regulatory-metadata").policy_version == 1


def test_endpoint_contracts_exactly_match_new_default_deny_source_policies() -> None:
    bundle = load_builtin_ops_bundle()
    assert len(OFFICIAL_ENDPOINT_POLICIES) == 5
    for endpoint in OFFICIAL_ENDPOINT_POLICIES:
        policy = bundle.source_policy(
            endpoint.source_policy_id, endpoint.source_policy_version
        )
        allowed_hosts = {
            urlsplit(str(origin)).hostname for origin in policy.allowed_origins
        }
        assert allowed_hosts == {endpoint.hostname}
        assert endpoint.path_template in policy.allowed_path_templates
        assert set(endpoint.allowed_query_keys) == set(policy.allowed_query_parameters)
        assert set(endpoint.allowed_media_types) == set(policy.allowed_content_types)
        assert endpoint.maximum_response_bytes == policy.maximum_response_bytes
        assert policy.live_network_enabled is False
        assert policy.license_posture != "verified_open"
        assert len(policy.rights_evidence) == 1
        evidence = policy.rights_evidence[0]
        assert evidence.formal_rights_review_completed is False
        assert evidence.reviewed_by == "none-formal-rights-officer-unavailable"
        assert evidence.conclusion == "metadata_discovery_only_rights_unresolved"
        assert evidence.known_limitations


def test_rights_boundaries_allow_metadata_but_not_high_risk_reuse() -> None:
    bundle = load_builtin_ops_bundle()
    new_policy_keys = {
        (item.source_policy_id, item.source_policy_version)
        for item in OFFICIAL_ENDPOINT_POLICIES
    }
    high_risk = (
        SourceOperation.PERSIST_FULL_TEXT,
        SourceOperation.TRANSLATE,
        SourceOperation.ADAPT,
        SourceOperation.REDISTRIBUTE,
        SourceOperation.COMMERCIAL_USE,
        SourceOperation.MODEL_TRAINING,
        SourceOperation.VECTOR_INDEX,
    )
    for key in new_policy_keys:
        policy = bundle.source_policy(*key)
        assert policy.decision_for(SourceOperation.DISCOVER_METADATA) == "allow"
        assert all(
            policy.decision_for(operation) in {"deny", "review_required"}
            for operation in high_risk
        )
    pubmed = bundle.source_policy("nlm-pubmed-metadata", 2)
    pmc = bundle.source_policy("source-pmc-open-access", 1)
    assert pubmed.policy_id != pmc.policy_id
    assert pubmed.source_types != pmc.source_types
    assert "Abstract" in " ".join(
        pubmed.rights_evidence[0].known_limitations
    )
    assert "item-specific" in " ".join(
        pmc.rights_evidence[0].known_limitations
    )


def test_all_operational_source_policies_remain_live_network_disabled() -> None:
    assert all(
        policy.live_network_enabled is False
        for policy in load_builtin_ops_bundle().source_policies
    )

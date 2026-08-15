from __future__ import annotations

import hashlib
import http.client
import inspect
import json
import shutil
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path
from urllib import request as urllib_request

import pytest
from pydantic import ValidationError

from continucare.knowledge.ops.catalog_read_model import (
    CORE_SYMPTOM_ALIAS_GAP_ID,
    AliasGapResolutionReadinessDTO,
    CoreSymptomRecordReadDTO,
    build_core_symptom_catalog_read_model,
    get_core_symptom_alias_readiness,
    get_core_symptom_gap_resolution_readiness,
    get_core_symptom_record,
    list_core_symptom_records,
    load_builtin_core_symptom_catalog_read_model,
)
from continucare.knowledge.ops.manifests import (
    DirectoryBundleSource,
    load_builtin_ops_bundle,
    load_ops_bundle,
)
from continucare.knowledge.ops.models import KnowledgeOpsManifestError
from continucare.terminology.core_catalog import (
    BENCHMARK_KEYS,
    CoreSymptomCatalogV2,
    load_core_symptom_catalog_v2,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_ROOT = REPOSITORY_ROOT / "continucare" / "knowledge" / "manifests_v2"
KNOWN_WITHHELD = {
    "胃里难受",
    "胃痛",
    "大便稀",
    "稀便",
    "大便干",
    "解不出来",
    "没精神",
    "晕乎乎",
    "头昏",
    "不想吃东西",
    "气短",
}


def test_list_and_get_api_are_deterministic_frozen_dtos() -> None:
    first = list_core_symptom_records()
    second = list_core_symptom_records()
    assert isinstance(first, tuple)
    assert first == second
    assert tuple(item.benchmark_key for item in first) == BENCHMARK_KEYS
    assert len(first) == 12
    assert get_core_symptom_record("nausea") == first[0]

    record = get_core_symptom_record("nausea")
    with pytest.raises(ValidationError, match="frozen"):
        record.preferred_zh = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs"):
        CoreSymptomRecordReadDTO.model_validate(
            {**record.model_dump(mode="json"), "raw_manifest": {}}
        )


def test_unknown_benchmark_fails_closed() -> None:
    with pytest.raises(LookupError, match="unknown Core Symptom benchmark"):
        get_core_symptom_record("unknown-benchmark")


def test_all_matchable_aliases_are_empty_and_inherited_aliases_are_withheld() -> None:
    records = list_core_symptom_records()
    assert all(item.approved_match_aliases == () for item in records)
    reused = tuple(item for item in records if item.concept_status == "reused_concept")
    assert len(reused) == 9
    assert sum(item.withheld_alias_count for item in reused) == 26
    withheld = {
        alias.alias_zh
        for record in reused
        for alias in record.withheld_aliases
    }
    assert KNOWN_WITHHELD.issubset(withheld)
    assert all(
        alias.status == "withheld_pending_formal_terminology_review"
        and alias.matchable is False
        and alias.semantic_equivalence_status == "not_established"
        for record in reused
        for alias in record.withheld_aliases
    )
    for record in reused:
        assert record.display_labels.preferred_zh == record.preferred_zh
        assert record.display_labels.zh_label_status == (
            "v1_preferred_display_only_not_formal_patient_expression_review"
        )
        assert record.display_labels.matchable is False
        assert record.open_gap_ids == (CORE_SYMPTOM_ALIAS_GAP_ID,)


def test_candidate_boundaries_and_english_labels_remain_display_only() -> None:
    records = {item.benchmark_key: item for item in list_core_symptom_records()}
    assert records["bloating"].candidate_target_ref == "abdominal-distension"
    assert "not-equivalent-to-flatulence" in records[
        "bloating"
    ].semantic_boundary_codes
    assert records["rash"].candidate_target_ref == "skin-eruption"
    assert {
        "not-equivalent-to-urticaria",
        "not-equivalent-to-pruritus",
        "not-equivalent-to-angioedema",
        "not-equivalent-to-anaphylaxis",
    }.issubset(records["rash"].semantic_boundary_codes)
    assert records["chest-pain"].concept_status == "internal_candidate"
    assert records["chest-pain"].existing_concept_ref is None
    for record in records.values():
        assert record.display_labels.preferred_en == record.preferred_en
        assert record.display_labels.en_label_status == (
            "benchmark_display_only_pending_formal_translation_review"
        )
        assert record.consumer_integration_ready is False
        assert record.knowledge_effect == "informational_only"
        assert record.runtime_authority == "none"


def test_alias_and_gap_readiness_are_derived_from_current_pinned_manifests() -> None:
    alias = get_core_symptom_alias_readiness()
    gap = get_core_symptom_gap_resolution_readiness()
    assert alias.audited_alias_count == 35
    assert alias.preferred_display_label_count == 9
    assert alias.withheld_alias_count == 26
    assert alias.approved_match_alias_count == 0
    assert alias.open_gap_ids == (CORE_SYMPTOM_ALIAS_GAP_ID,)
    assert alias.formal_terminologist_review_completed is False
    assert alias.clinical_patient_expression_validation_completed is False
    assert alias.consumer_integration_ready is False

    assert gap.gap_id == CORE_SYMPTOM_ALIAS_GAP_ID
    assert gap.lifecycle == "open"
    assert gap.required_gate == "terminology_mapping_promotion"
    assert gap.required_roles == (
        "terminologist",
        "rights_officer",
        "knowledge_curator",
    )
    assert gap.formal_decision_present is False
    assert gap.valid_attestations_present is False
    assert gap.successor_manifest_present is False
    assert gap.resolution_permitted is False
    assert gap.consumer_integration_ready is False
    assert gap.synthetic_review_events_sufficient is False
    assert gap.same_identity_or_principal_sufficient is False
    assert gap.model_output_accepted_as_reviewer_evidence is False
    assert gap.local_boolean_override_available is False
    assert set(gap.missing_formal_evidence) == {
        "formal_review_packet_for_exact_alias_audit",
        "non_synthetic_formally_verified_terminologist_review_event",
        "non_synthetic_formally_verified_rights_officer_review_event",
        "non_synthetic_formally_verified_knowledge_curator_review_event",
        "distinct_reviewer_identities_and_principals",
        "reviewer_verifier_attestation_for_each_formal_decision",
        "hash_pinned_successor_readiness_manifest",
        "independent_post_resolution_consumer_review",
    }


def test_no_boolean_or_caller_parameter_can_force_readiness() -> None:
    signature = inspect.signature(build_core_symptom_catalog_read_model)
    assert tuple(signature.parameters) == ("bundle", "catalog")
    assert not any("ready" in name for name in signature.parameters)
    current = get_core_symptom_gap_resolution_readiness()
    with pytest.raises(ValidationError):
        AliasGapResolutionReadinessDTO.model_validate(
            {
                **current.model_dump(mode="json"),
                "formal_decision_present": True,
                "resolution_permitted": True,
                "consumer_integration_ready": True,
            }
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "preferred_zh",
        "preferred_en",
        "semantic_boundary_codes",
        "alias_value",
        "alias_review_status",
        "concept_mapping_contract",
    ),
)
def test_builder_rejects_every_schema_valid_caller_catalog_drift(
    mutation: str,
) -> None:
    bundle = load_builtin_ops_bundle()
    canonical = load_core_symptom_catalog_v2()
    payload = canonical.model_dump(mode="json")
    record = payload["records"][0]

    if mutation == "preferred_zh":
        record["preferred_zh"] = "恶心（caller篡改）"
    elif mutation == "preferred_en":
        record["preferred_en"] = "caller-altered nausea"
    elif mutation == "semantic_boundary_codes":
        record["semantic_boundary_codes"] = ["caller-only-boundary"]
    elif mutation == "alias_value":
        record["aliases"][0]["value"] = "恶心（caller alias）"
    elif mutation == "alias_review_status":
        record["aliases"][0]["review_status"] = "pending_terminologist_review"
    else:
        record["concept_status"] = "internal_candidate"
        record["existing_concept_ref"] = None
        record["candidate_target_ref"] = None
        record["mapping_status"] = "pending_unverified"

    altered = CoreSymptomCatalogV2.model_validate(payload)
    assert altered != canonical
    with pytest.raises(
        KnowledgeOpsManifestError,
        match="differs from hash-pinned canonical catalog",
    ):
        build_core_symptom_catalog_read_model(bundle, altered)

    projected = build_core_symptom_catalog_read_model(bundle, canonical)
    assert len(projected.records) == 12
    assert all(item.approved_match_aliases == () for item in projected.records)
    assert projected.gap_resolution_readiness.lifecycle == "open"
    assert projected.consumer_integration_ready is False


def test_builder_rechecks_canonical_catalog_digest_against_audit_pin() -> None:
    bundle = load_builtin_ops_bundle()
    audit = bundle.core_symptom_alias_audit
    assert audit is not None
    wrong_pin = audit.model_copy(update={"catalog_sha256": "0" * 64})
    with pytest.raises(
        KnowledgeOpsManifestError,
        match="canonical catalog SHA-256 differs from alias audit",
    ):
        build_core_symptom_catalog_read_model(
            replace(bundle, core_symptom_alias_audit=wrong_pin),
            load_core_symptom_catalog_v2(),
        )


def test_missing_audit_or_current_gap_fails_readiness_closed() -> None:
    bundle = load_builtin_ops_bundle()
    catalog = load_core_symptom_catalog_v2()
    without_audit = replace(bundle, core_symptom_alias_audit=None)
    with pytest.raises(KnowledgeOpsManifestError, match="hash-pinned alias audit"):
        build_core_symptom_catalog_read_model(without_audit, catalog)

    without_gap = replace(
        bundle,
        readiness_gaps=tuple(
            item for item in bundle.readiness_gaps if item.gap_id != CORE_SYMPTOM_ALIAS_GAP_ID
        ),
    )
    with pytest.raises(KnowledgeOpsManifestError, match="exact open alias Gap"):
        build_core_symptom_catalog_read_model(without_gap, catalog)


def test_deleting_gap_from_re_pinned_manifest_still_fails_bundle_load(
    tmp_path: Path,
) -> None:
    root = tmp_path / "manifests_v2"
    shutil.copytree(MANIFEST_ROOT, root)
    gap_path = root / "readiness_gaps_v1.json"
    gaps = json.loads(gap_path.read_bytes())
    gaps["gaps"] = [
        item for item in gaps["gaps"] if item["gap_id"] != CORE_SYMPTOM_ALIAS_GAP_ID
    ]
    gap_path.write_text(
        json.dumps(gaps, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    index_path = root / "bundle_index_v2_4.json"
    index = json.loads(index_path.read_bytes())
    gap_pin = next(
        item for item in index["files"] if item["relative_path"] == gap_path.name
    )
    gap_bytes = gap_path.read_bytes()
    gap_pin["manifest_sha256"] = hashlib.sha256(gap_bytes).hexdigest()
    gap_pin["size"] = len(gap_bytes)
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeOpsManifestError):
        load_ops_bundle(
            DirectoryBundleSource(root),
            index_path="bundle_index_v2_4.json",
        )


def test_read_api_performs_no_sqlite_network_dns_http_or_urlopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"sqlite": 0, "socket": 0, "dns": 0, "http": 0, "api": 0}

    def reject(kind: str):
        def _reject(*_args, **_kwargs):
            calls[kind] += 1
            raise AssertionError(f"Core Symptom read API attempted {kind}")

        return _reject

    monkeypatch.setattr(sqlite3, "connect", reject("sqlite"))
    monkeypatch.setattr(socket, "socket", reject("socket"))
    monkeypatch.setattr(socket, "create_connection", reject("socket"))
    monkeypatch.setattr(socket, "getaddrinfo", reject("dns"))
    monkeypatch.setattr(http.client.HTTPConnection, "request", reject("http"))
    monkeypatch.setattr(http.client.HTTPSConnection, "request", reject("http"))
    monkeypatch.setattr(urllib_request, "urlopen", reject("api"))
    load_builtin_core_symptom_catalog_read_model.cache_clear()

    model = load_builtin_core_symptom_catalog_read_model()
    assert len(model.records) == 12
    assert model.consumer_integration_ready is False
    assert calls == {"sqlite": 0, "socket": 0, "dns": 0, "http": 0, "api": 0}


def test_ui_render_pathway_and_runtime_do_not_import_new_read_api() -> None:
    protected = [REPOSITORY_ROOT / "app.py"]
    protected.extend((REPOSITORY_ROOT / "pages").rglob("*.py"))
    for namespace in (
        "pathways",
        "layer4",
        "services",
        "agents",
        "care_agent",
        "care_engine",
    ):
        protected.extend((REPOSITORY_ROOT / "continucare" / namespace).rglob("*.py"))
    protected.extend(
        [
            REPOSITORY_ROOT / "continucare" / "ui.py",
            REPOSITORY_ROOT / "continucare" / "knowledge" / "render.py",
        ]
    )
    for path in protected:
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            assert "catalog_read_model" not in content, path
            assert "get_core_symptom_record" not in content, path


def test_current_schema_has_no_resolved_successor_alias_gap_manifest() -> None:
    readiness_manifests = sorted(
        item.name for item in MANIFEST_ROOT.glob("readiness_gaps*.json")
    )
    assert readiness_manifests == ["readiness_gaps_v1.json"]
    assert get_core_symptom_gap_resolution_readiness().successor_manifest_present is False

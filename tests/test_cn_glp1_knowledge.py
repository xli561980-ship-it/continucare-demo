from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from continucare.knowledge import (
    KnowledgeValidationError,
    compile_observation_mappings,
    compile_pro_ctcae_questionnaire,
    compile_questionnaire,
    load_cn_glp1_release,
    validate_release,
)
from continucare.knowledge import compiler as knowledge_compiler
from continucare.pathways import load_builtin_pathways


DATA_DIR = Path("continucare/knowledge/data/cn_glp1/v1")


def test_release_is_hash_locked_cross_file_valid_and_fail_closed():
    release = load_cn_glp1_release().release

    validate_release(release, data_dir=DATA_DIR)
    assert release.manifest.status == "engineering_validated"
    assert release.manifest.synthetic_only is True
    assert release.coverage.product_record_count == len(release.products) == 15
    by_brand = {}
    for item in release.products:
        by_brand.setdefault(item.brand_name_zh, []).append(item)
    assert {brand: len(items) for brand, items in by_brand.items()} == {
        "诺和盈": 5,
        "穆峰达": 8,
        "度易达": 2,
    }
    assert all(item.verification_status == "incomplete" for item in by_brand["诺和盈"])
    assert all(item.label_source_id is None for item in by_brand["诺和盈"])
    prefilled = [
        item for item in by_brand["穆峰达"] if "prefilled" in item.product_id
    ]
    multidose = [
        item for item in by_brand["穆峰达"] if "multidose" in item.product_id
    ]
    assert all(item.verification_status == "incomplete" for item in prefilled)
    assert all(item.verification_status == "verified" for item in multidose)
    assert all(item.agonist_type == "dual_gip_glp1_agonist" for item in by_brand["穆峰达"])
    assert all(item.verification_status == "verified" for item in by_brand["度易达"])
    assert release.clinical_rules == []
    required_sources = {
        "nhc-obesity-guideline-2024",
        "hl7-fhir-r4-4.0.1-json-schema",
        "loinc-2.82",
        "lilly-cn-mounjaro-prefilled-label-2026-02-10",
        "lilly-cn-mounjaro-multidose-label-2026-05-19",
        "lilly-cn-trulicity-label-2025-04-14",
    }
    assert required_sources <= {item.source_id for item in release.sources}
    assert release.coverage.source_count == len(release.sources)
    assert release.coverage.verified_product_record_count == 6
    assert release.coverage.incomplete_product_record_count == 9
    assert all(
        {scope.indication for scope in item.indication_population_scopes}
        == set(item.approved_indications)
        for item in release.products
    )


def test_foreign_label_grading_and_signal_sources_are_not_runtime_eligible():
    release = load_cn_glp1_release().release
    by_id = {item.source_id: item for item in release.sources}

    assert by_id["fda-wegovy-label-2026-03"].runtime_eligible is False
    assert by_id["nci-ctcae-v6-2025"].runtime_eligible is False
    assert by_id["fda-aems-faers-2026q2"].runtime_eligible is False
    assert by_id["loinc-2.82"].runtime_eligible is False

    pathway = load_builtin_pathways().get("GLP1-14D")
    background = {
        item.source_id: item for item in pathway.knowledge_sources
        if item.usage == "background_only"
    }
    assert {"ema-wegovy-epar", "fda-wegovy-2026", "jcm-glp1-gi-consensus-2022"} <= set(background)
    assert all(item.runtime_eligible is False for item in background.values())
    assert all(item.runtime_eligible is False for item in pathway.knowledge_sources)


def test_every_evidence_claim_has_an_explicit_product_scope():
    release = load_cn_glp1_release().release

    assert all(claim.product_scope_kind for claim in release.evidence_claims)
    assert all(
        bool(claim.product_ids) == (claim.product_scope_kind == "product_specific")
        for claim in release.evidence_claims
    )


def test_operational_questionnaire_and_mapping_trace_to_release_evidence():
    questionnaire = compile_questionnaire()
    mappings = compile_observation_mappings()

    release_values = {
        extension["valueString"]
        for extension in questionnaire["extension"]
        if extension["url"].endswith("knowledge-release-id")
    }
    assert "cn-glp1-l1-v1.0.3" in release_values
    assert mappings["knowledge_release_id"] == "cn-glp1-l1-v1.0.3"
    assert all(item["metric_id"] for item in mappings["mappings"])
    assert all(item["evidence_claim_ids"] for item in mappings["mappings"])
    claim_ids = {
        extension["valueString"]
        for item in questionnaire["item"]
        for extension in item.get("extension", [])
        if extension["url"].endswith("evidence-claim-id")
    }
    assert "pro-ctcae-glp1-question-wording-001" not in claim_ids
    assert "fda-wegovy-safety-background-001" not in claim_ids


def test_mapping_compiler_rejects_empty_traceability_fields(monkeypatch):
    policy = knowledge_compiler.load_glp1_observation_mapping().model_copy(deep=True)
    policy.mappings[0].metric_id = None
    policy.mappings[0].evidence_claim_ids = []
    monkeypatch.setattr(
        knowledge_compiler, "load_glp1_observation_mapping", lambda: policy
    )

    with pytest.raises(ValueError, match="mapping metric drift"):
        compile_observation_mappings()


def test_mapping_compiler_rejects_metric_semantic_drift():
    release = load_cn_glp1_release().release.model_copy(deep=True)
    metric = next(
        item for item in release.metrics if item.metric_id == "vomiting_count_24h"
    )
    metric.time_window = "current"

    with pytest.raises(ValueError, match="mapping time window drift"):
        compile_observation_mappings(release)


def test_prepare_release_refuses_to_mutate_a_published_release(tmp_path):
    manifest = DATA_DIR / "release_manifest.json"
    coverage = DATA_DIR / "coverage_report.json"
    before = (manifest.read_bytes(), coverage.read_bytes())

    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_cn_glp1_knowledge.py",
            "--prepare-release",
            "2099-01-01T00:00:00+00:00",
            "--candidate-release-id",
            "cn-glp1-l1-v1.0.3",
            "--output",
            str(tmp_path / "cn-glp1-l1-v1.0.3"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "engineering_validated release" in result.stderr
    assert (manifest.read_bytes(), coverage.read_bytes()) == before


def test_prepare_release_requires_explicit_new_release_id_when_output_is_absent(
    tmp_path,
):
    manifest = DATA_DIR / "release_manifest.json"
    coverage = DATA_DIR / "coverage_report.json"
    before = (manifest.read_bytes(), coverage.read_bytes())

    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_cn_glp1_knowledge.py",
            "--prepare-release",
            "2099-01-01T00:00:00+00:00",
            "--output",
            str(tmp_path / "cn-glp1-l1-v1.0.3"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "requires a new --candidate-release-id" in result.stderr
    assert (manifest.read_bytes(), coverage.read_bytes()) == before


def test_restricted_pro_ctcae_text_is_not_in_the_public_release():
    release = load_cn_glp1_release().release
    pro_metrics = [
        metric for metric in release.metrics if metric.metric_id.startswith("pro_")
    ]

    assert len(pro_metrics) == 11
    assert all(metric.runtime_eligible is False for metric in pro_metrics)
    assert not any(
        item.link_id.startswith("pro-") for item in release.patient_content
    )
    with pytest.raises(ValueError, match="restricted PRO-CTCAE content"):
        compile_pro_ctcae_questionnaire(release)


def test_validator_rejects_restricted_source_content_in_public_patient_content():
    release = load_cn_glp1_release().release.model_copy(deep=True)
    release.patient_content[0].evidence_claim_ids = [
        "pro-ctcae-glp1-question-wording-001"
    ]

    with pytest.raises(
        KnowledgeValidationError, match="embeds restricted-source content"
    ):
        validate_release(release)


def test_runtime_claim_cannot_depend_on_background_only_source():
    release = load_cn_glp1_release().release.model_copy(deep=True)
    claim = next(
        item
        for item in release.evidence_claims
        if item.claim_id == "fda-wegovy-safety-background-001"
    )
    claim.runtime_eligible = True

    with pytest.raises(KnowledgeValidationError, match="exceeds source runtime permission"):
        validate_release(release)

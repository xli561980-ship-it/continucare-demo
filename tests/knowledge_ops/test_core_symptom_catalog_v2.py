from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from continucare.terminology.catalog import load_glp1_symptom_catalog
from continucare.terminology.core_catalog import (
    BENCHMARK_KEYS,
    CORE_CATALOG_V2_FILE,
    CoreSymptomCatalogV2,
    CoreSymptomRecord,
    load_core_symptom_catalog_v2,
)


REUSED = {
    "nausea": "nausea",
    "vomiting": "vomiting",
    "diarrhea": "diarrhea",
    "abdominal-pain": "abdominal-pain",
    "constipation": "constipation",
    "decreased-appetite": "decreased-appetite",
    "fatigue": "fatigue",
    "dizziness": "dizziness",
    "dyspnea": "dyspnea",
}


def test_catalog_contains_exact_ordered_12_item_shared_benchmark() -> None:
    catalog = load_core_symptom_catalog_v2()
    assert tuple(item.benchmark_key for item in catalog.records) == BENCHMARK_KEYS
    assert len(catalog.records) == 12
    assert catalog.shared_owner == "continucare-shared-terminology"
    assert catalog.pathway_owned is False
    assert catalog.ui_runtime_enabled is False
    assert catalog.release_ready is False
    assert catalog.knowledge_effect == "informational_only"
    assert catalog.runtime_authority == "none"


def test_nine_existing_concepts_are_references_not_new_mappings() -> None:
    catalog = load_core_symptom_catalog_v2()
    v1_ids = {item.concept_id for item in load_glp1_symptom_catalog().concepts}
    assert len(v1_ids) == 49
    for benchmark, reference in REUSED.items():
        record = catalog.symptom(benchmark)
        assert record.concept_status == "reused_concept"
        assert record.existing_concept_ref == reference
        assert reference in v1_ids
        assert record.mapping_status == "inherited_v1_reference_unverified"


def test_bloating_and_rash_remain_alias_candidates_and_chest_pain_internal() -> None:
    catalog = load_core_symptom_catalog_v2()
    bloating = catalog.symptom("bloating")
    rash = catalog.symptom("rash")
    chest = catalog.symptom("chest-pain")

    assert (bloating.concept_status, bloating.candidate_target_ref) == (
        "alias_candidate",
        "abdominal-distension",
    )
    assert "not-equivalent-to-flatulence" in bloating.semantic_boundary_codes
    assert (rash.concept_status, rash.candidate_target_ref) == (
        "alias_candidate",
        "skin-eruption",
    )
    assert {
        "not-equivalent-to-urticaria",
        "not-equivalent-to-pruritus",
        "not-equivalent-to-angioedema",
        "not-equivalent-to-anaphylaxis",
    }.issubset(rash.semantic_boundary_codes)
    assert chest.concept_status == "internal_candidate"
    assert chest.existing_concept_ref is None
    assert chest.candidate_target_ref is None
    assert "no-external-code-claimed" in chest.semantic_boundary_codes


def test_ambiguous_semantics_are_explicitly_not_collapsed() -> None:
    catalog = load_core_symptom_catalog_v2()
    assert {
        "not-equivalent-to-vertigo",
        "not-equivalent-to-presyncope",
        "not-equivalent-to-syncope",
    }.issubset(catalog.symptom("dizziness").semantic_boundary_codes)
    assert "not-equivalent-to-reduced-intake" in catalog.symptom(
        "decreased-appetite"
    ).semantic_boundary_codes
    for key in ("dyspnea", "chest-pain"):
        assert "no-emergency-or-red-flag-inference" in catalog.symptom(
            key
        ).semantic_boundary_codes


def test_every_zh_alias_has_source_and_review_status() -> None:
    catalog = load_core_symptom_catalog_v2()
    aliases = [
        alias
        for record in catalog.records
        for alias in record.aliases
        if alias.language == "zh-CN"
    ]
    assert len(aliases) == 12
    assert all(alias.alias_source for alias in aliases)
    assert all(alias.review_status for alias in aliases)
    assert all(
        record.terminology_review_status == "pending_terminologist_review"
        for record in catalog.records
    )


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "red_flag",
        "triage",
        "severity",
        "severity_tier",
        "risk_tier",
        "diagnosis",
        "treatment",
        "recommendation",
        "clinical_rule",
    ],
)
def test_record_schema_rejects_prohibited_clinical_fields(forbidden_field: str) -> None:
    record = load_core_symptom_catalog_v2().records[0]
    with pytest.raises(ValidationError):
        CoreSymptomRecord.model_validate(
            {**record.model_dump(mode="json"), forbidden_field: "forbidden"}
        )


def test_fixed_alias_targets_and_internal_candidate_cannot_be_relabelled() -> None:
    catalog = load_core_symptom_catalog_v2()
    raw = catalog.model_dump(mode="json")
    bloating = next(item for item in raw["records"] if item["benchmark_key"] == "bloating")
    bloating["candidate_target_ref"] = "flatulence"
    with pytest.raises(ValidationError, match="bloating"):
        CoreSymptomCatalogV2.model_validate(raw)


def test_catalog_contains_no_new_external_code_claims() -> None:
    raw = json.loads(CORE_CATALOG_V2_FILE.read_text(encoding="utf-8"))
    serialized = json.dumps(raw, ensure_ascii=False)
    assert '"coding"' not in serialized
    assert '"code_system"' not in serialized
    assert "snomed.info" not in serialized
    assert "icd" not in serialized.lower()
    assert "loinc" not in serialized.lower()
    assert "meddra" not in serialized.lower()


def test_ui_pathway_and_runtime_modules_do_not_import_v2_catalog() -> None:
    root = Path(__file__).parents[2]
    candidates = [root / "app.py"]
    candidates.extend((root / "pages").glob("*.py"))
    candidates.extend((root / "continucare" / "pathways").rglob("*.py"))
    candidates.extend(
        [
            root / "continucare" / "ui.py",
            root / "continucare" / "knowledge" / "render.py",
            root / "continucare" / "agents" / "runtime.py",
            root / "continucare" / "layer4" / "states.py",
        ]
    )
    for path in candidates:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        assert "core_catalog" not in content, path
        assert "core_symptom_catalog_v2" not in content, path

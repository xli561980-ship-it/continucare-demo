from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from continucare.knowledge.ops.manifests import (
    DirectoryBundleSource,
    load_builtin_ops_bundle,
    load_ops_bundle,
)
from continucare.knowledge.ops.models import (
    CORE_SYMPTOM_REUSED_BENCHMARK_KEYS,
    KnowledgeOpsManifestError,
)
from continucare.terminology.catalog import DATA_FILE, load_glp1_symptom_catalog
from continucare.terminology.core_catalog import (
    CORE_CATALOG_V2_FILE,
    load_core_symptom_catalog_v2,
)


MANIFEST_ROOT = (
    Path(__file__).parents[2] / "continucare" / "knowledge" / "manifests_v2"
)
V1_CATALOG_SHA256 = "34c67ac92f24fcebf6e43c6a2d0dc27d6963922af84a37741a6f6930abf35e7a"
V2_CATALOG_SHA256 = "e7a1694aa7468aa236584f104acbd60e6d0d4c0edf2cc2991ef8771bbaf2e7cf"
AUDIT_SHA256 = "b1c78809fabcf4ba2974a0190d1a9dfc57db8ec7dc935f6f93644b38ef2a637d"
AUDIT_SIZE = 12_839
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


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _repin_v4(root: Path, relative_path: str) -> None:
    index_path = root / "bundle_index_v2_4.json"
    index = json.loads(index_path.read_bytes())
    payload = (root / relative_path).read_bytes()
    pinned = next(
        item for item in index["files"] if item["relative_path"] == relative_path
    )
    pinned["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    pinned["size"] = len(payload)
    _write_json(index_path, index)


def test_audit_covers_exact_nine_reused_concepts_and_every_v1_alias_once() -> None:
    bundle = load_builtin_ops_bundle()
    audit = bundle.core_symptom_alias_audit
    assert audit is not None
    assert tuple(item.benchmark_key for item in audit.concept_audits) == (
        CORE_SYMPTOM_REUSED_BENCHMARK_KEYS
    )
    assert len(audit.concept_audits) == 9

    source_catalog = load_glp1_symptom_catalog()
    seen: list[tuple[str, str]] = []
    for concept_audit in audit.concept_audits:
        source = source_catalog.concept(concept_audit.existing_concept_ref)
        audited_values = tuple(item.alias_zh for item in concept_audit.aliases)
        assert audited_values == tuple(source.aliases_zh)
        assert len(audited_values) == len(set(audited_values))
        seen.extend((concept_audit.benchmark_key, value) for value in audited_values)
    assert len(seen) == 35
    assert len(seen) == len(set(seen))


def test_preferred_display_labels_are_separate_and_all_other_aliases_withheld() -> None:
    audit = load_builtin_ops_bundle().core_symptom_alias_audit
    assert audit is not None
    withheld: set[str] = set()
    for concept in audit.concept_audits:
        preferred, *inherited = concept.aliases
        assert preferred.alias_zh == concept.preferred_zh
        assert preferred.source_role == "v1_preferred_display_label"
        assert preferred.disposition == (
            "display_label_only_pending_formal_terminology_review"
        )
        assert preferred.display_label is True
        assert preferred.matchable is False
        assert preferred.semantic_equivalence_status == "not_established"
        assert concept.approved_match_aliases == ()
        assert inherited
        for alias in inherited:
            assert alias.source_role == "inherited_v1_alias"
            assert alias.disposition == (
                "withheld_pending_formal_terminology_review"
            )
            assert alias.display_label is False
            assert alias.matchable is False
            assert alias.semantic_equivalence_status == "not_established"
            assert alias.formal_terminology_review_completed is False
            withheld.add(alias.alias_zh)
    assert len(withheld) == 26
    assert KNOWN_WITHHELD.issubset(withheld)


def test_audit_contains_no_positive_alias_or_translation_review_claim() -> None:
    audit = load_builtin_ops_bundle().core_symptom_alias_audit
    assert audit is not None
    assert audit.audit_kind == "technical_boundary_audit"
    assert audit.technical_audit_only is True
    assert audit.formal_terminologist_review_completed is False
    assert audit.clinical_patient_expression_validation_completed is False
    assert audit.contains_patient_data is False
    assert audit.release_ready is False
    assert audit.knowledge_effect == "informational_only"
    assert audit.runtime_authority == "none"
    for concept in audit.concept_audits:
        assert concept.formal_translation_review_completed is False
        assert concept.english_label_disposition == (
            "benchmark_display_only_pending_formal_translation_review"
        )
        assert concept.approved_match_aliases == ()
        for alias in concept.aliases:
            assert alias.matchable is False
            assert alias.semantic_equivalence_status == "not_established"


def test_catalog_bytes_and_new_manifest_pin_are_exact() -> None:
    assert hashlib.sha256(DATA_FILE.read_bytes()).hexdigest() == V1_CATALOG_SHA256
    assert (
        hashlib.sha256(CORE_CATALOG_V2_FILE.read_bytes()).hexdigest()
        == V2_CATALOG_SHA256
    )
    audit_bytes = (MANIFEST_ROOT / "core_symptom_alias_audit_v1.json").read_bytes()
    assert len(audit_bytes) == AUDIT_SIZE
    assert hashlib.sha256(audit_bytes).hexdigest() == AUDIT_SHA256
    index = json.loads((MANIFEST_ROOT / "bundle_index_v2_4.json").read_bytes())
    pin = next(
        item
        for item in index["files"]
        if item["relative_path"] == "core_symptom_alias_audit_v1.json"
    )
    assert pin["manifest_sha256"] == AUDIT_SHA256
    assert pin["size"] == AUDIT_SIZE


def test_old_bundle_indexes_remain_loadable_without_backfilled_alias_audit() -> None:
    source = DirectoryBundleSource(MANIFEST_ROOT)
    for index_path, version in (
        ("bundle_index_v2.json", 1),
        ("bundle_index_v2_2.json", 2),
        ("bundle_index_v2_3.json", 3),
    ):
        bundle = load_ops_bundle(source, index_path=index_path)
        assert bundle.index.bundle_version == version
        assert bundle.core_symptom_alias_audit is None


@pytest.mark.parametrize(
    "mutation",
    ["wrong_source_digest", "missing_inherited_alias", "changed_english_label"],
)
def test_re_pinned_alias_audit_semantic_tampering_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / "manifests_v2"
    shutil.copytree(MANIFEST_ROOT, root)
    path = root / "core_symptom_alias_audit_v1.json"
    payload = json.loads(path.read_bytes())
    if mutation == "wrong_source_digest":
        payload["audit"]["source_catalog_sha256"] = "0" * 64
    elif mutation == "missing_inherited_alias":
        payload["audit"]["concept_audits"][0]["aliases"].pop()
    else:
        payload["audit"]["concept_audits"][0]["benchmark_label_en"] = "queasiness"
    _write_json(path, payload)
    _repin_v4(root, path.name)

    with pytest.raises(KnowledgeOpsManifestError):
        load_ops_bundle(
            DirectoryBundleSource(root),
            index_path="bundle_index_v2_4.json",
        )


def test_core_catalog_reused_set_is_still_exact_and_candidates_are_unchanged() -> None:
    catalog = load_core_symptom_catalog_v2()
    reused = tuple(
        item.benchmark_key
        for item in catalog.records
        if item.concept_status == "reused_concept"
    )
    assert reused == CORE_SYMPTOM_REUSED_BENCHMARK_KEYS
    assert catalog.symptom("bloating").candidate_target_ref == "abdominal-distension"
    assert catalog.symptom("rash").candidate_target_ref == "skin-eruption"
    chest = catalog.symptom("chest-pain")
    assert chest.concept_status == "internal_candidate"
    assert chest.existing_concept_ref is None

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from collections import Counter
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from streamlit.testing.v1 import AppTest

from continucare.knowledge import (
    KNOWLEDGE_DISCLAIMER,
    KnowledgeArtifactUnresolved,
    KnowledgeAuthorityError,
    KnowledgeCurrentSelectionError,
    KnowledgePinnedFileError,
    KnowledgeReferenceError,
    KnowledgeSchemaError,
    KnowledgeSourceArtifactError,
    LoadMode,
    load_builtin_bundle,
    load_bundle,
    render_pathway_knowledge,
    render_symptom_knowledge,
)
from continucare.knowledge.__main__ import main
from continucare.knowledge.models import (
    AnswerListLocator,
    PageLocator,
    PURPOSE_REQUIREMENTS,
    ReviewEvent,
    SectionLocator,
    SourceRecord,
    SymptomIndexRecord,
    TableOrFigureLocator,
    TerminologyConceptLocator,
    UrlFragmentLocator,
    required_approvals,
)
from continucare.knowledge.resolvers import (
    ArtifactResolution,
    CatalogTermResolution,
    FilesystemBundleSource,
    FilesystemSourceArtifactResolver,
    MappingAuthorityResolver,
    RepositoryArtifactResolver,
    ResolvedAuthority,
    TraversableBundleSource,
    safe_relative_parts,
)
from continucare.pathways import load_builtin_pathways
from continucare.terminology.catalog import load_glp1_symptom_catalog


MANIFEST_NAMES = (
    "sources_v1.json",
    "claims_v1.json",
    "bindings_glp1_14d_v1.json",
    "governance_v1.json",
    "symptom_index_v1.json",
)
BUILTIN_MANIFESTS = Path(str(files("continucare.knowledge.manifests")))
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _ArtifactResolver:
    def __init__(self, resolved: bool = True):
        self.resolved = resolved

    def resolve(self, pathway, artifact) -> ArtifactResolution:
        del pathway, artifact
        return ArtifactResolution(self.resolved, "synthetic test resolver")

    def resolve_catalog_term(self, term) -> CatalogTermResolution:
        del term
        return CatalogTermResolution(self.resolved, "synthetic test catalog resolver")


class _CountingSourceArtifactResolver:
    def __init__(self, payload: bytes = b"synthetic bytes"):
        self.payload = payload
        self.calls: list[str] = []

    def read_bytes(self, relative_posix_path: str) -> bytes:
        self.calls.append(relative_posix_path)
        return self.payload


def _copy_builtin_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    root.mkdir(parents=True)
    for name in (*MANIFEST_NAMES, "bundle_index_v1.json"):
        shutil.copyfile(BUILTIN_MANIFESTS / name, root / name)
    return root


def _read_json(root: Path, name: str) -> dict:
    return json.loads((root / name).read_text("utf-8"))


def _write_json(root: Path, name: str, value: dict) -> None:
    (root / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _repin(root: Path) -> None:
    index = _read_json(root, "bundle_index_v1.json")
    for pinned in index["files"]:
        payload = (root / pinned["relative_path"]).read_bytes()
        pinned["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
        pinned["size"] = len(payload)
    _write_json(root, "bundle_index_v1.json", index)


def _mutate(root: Path, name: str, update) -> dict:
    value = _read_json(root, name)
    update(value)
    _write_json(root, name, value)
    _repin(root)
    return value


def _load_test_bundle(
    root: Path,
    *,
    mode: LoadMode | str = LoadMode.CURRENT,
    artifact_resolver=None,
    authority_resolver=None,
    source_artifact_resolver=None,
):
    return load_bundle(
        FilesystemBundleSource(root),
        "bundle_index_v1.json",
        mode=mode,
        artifact_resolver=artifact_resolver or _ArtifactResolver(),
        authority_resolver=authority_resolver,
        source_artifact_resolver=source_artifact_resolver,
    )


def _clinical_event(
    *,
    event_id: str = "clinical-review-1",
    decision: str = "approved",
    claim_id: str = "glp1-nausea-collection-rationale",
    actor: str = "Practitioner/reviewer",
    supersedes_event_id: str | None = None,
) -> dict:
    return {
        "event_type": "clinical",
        "event_id": event_id,
        "supersedes_event_id": supersedes_event_id,
        "subject": {"claim_id": claim_id, "claim_version": 1},
        "decision": decision,
        "claimed_role": "clinician",
        "actor_reference": actor,
        "decided_at": "2026-08-13T01:00:00+00:00",
        "rationale": "Synthetic review event for contract testing.",
    }


def _add_events(root: Path, events: list[dict]) -> None:
    _mutate(
        root,
        "governance_v1.json",
        lambda value: value["review_events"].extend(events),
    )


def test_builtin_bundle_has_exact_frozen_inventory_and_review_posture():
    registry = load_builtin_bundle()
    view = registry.for_pathway("GLP1-14D", "1.0.0")

    assert registry.mode == LoadMode.CURRENT
    assert (len(registry.sources), len(registry.claims)) == (15, 8)
    assert (len(view.bindings), len(view.gaps)) == (11, 14)
    assert len(registry.symptom_indexes) == 4
    assert view.unique_artifact_count == 21
    assert view.registered_relationship_count == 11
    assert view.explicit_gap_count == 14
    assert view.verified_citation_relationship_count == 0
    assert view.claim_review_approved_relationship_count == 0
    assert Counter(
        item.artifact.artifact_kind for item in (*view.bindings, *view.gaps)
    ) == {
        "questionnaire_item": 6,
        "observation_mapping_item": 5,
        "questionnaire_terminology_binding": 5,
        "terminology_concept": 8,
        "plan_definition": 1,
    }
    assert Counter(item.gap_kind for item in view.gaps) == {
        "design_governance_metadata": 4,
        "exact_terminology_basis": 6,
        "patient_expression_evidence": 4,
    }
    assert len(registry.governance.legacy_source_aliases) == 13
    assert registry.governance.review_events == ()
    assert all(item.lifecycle == "draft" for item in registry.claims)
    assert all(
        citation.quote is None
        for claim in registry.claims
        for citation in claim.citations
    )
    assert all(summary.aggregate == "not_assessed" for summary in view.review_summaries.values())
    assert all(item.resolved for item in view.artifact_resolutions.values())
    assert set(view.source_content_status.values()) == {"not_content_fixed"}


def test_builtin_bindings_reuse_seven_claims_and_require_exact_approvals():
    view = load_builtin_bundle().for_pathway("GLP1-14D", "1.0.0")

    usage = Counter(item.claim.claim_id for item in view.bindings)
    assert len(usage) == 7
    assert sorted(usage.values()) == [1, 1, 1, 1, 2, 2, 3]
    for binding in view.bindings:
        assert binding.required_independent_approvals == required_approvals(
            binding.artifact.artifact_kind, binding.binding_purpose
        )
        assert not hasattr(binding, "approved_for_execution")
        assert not hasattr(binding, "activation_gate")


def test_builtin_sources_preserve_unknown_metadata_without_inventing_authority():
    registry = load_builtin_bundle()
    daily_med = [
        item for item in registry.sources if item.source_id.startswith("dailymed-")
    ]

    assert len(daily_med) == 4
    assert all(item.language == "und" for item in daily_med)
    assert all(item.issuing_authority is None for item in daily_med)
    assert all(item.authority_type is None for item in daily_med)
    assert all(item.jurisdictions == () for item in daily_med)
    assert all(
        item.authority_metadata_status == "not_available_in_repository"
        and item.jurisdiction_metadata_status == "not_available_in_repository"
        for item in daily_med
    )
    assert all(item.access.mode == "link_only" for item in registry.sources)
    assert all(item.manifestation_of is None for item in registry.sources)


def test_source_metadata_status_cannot_disagree_with_values():
    source = load_builtin_bundle().sources[0].model_dump(mode="json")
    source["authority_metadata_status"] = "not_available_in_repository"

    with pytest.raises(ValidationError, match="cannot include an authority"):
        SourceRecord.model_validate(source)


def test_builtin_resources_are_importable_from_the_tracked_manifest_package():
    resources = files("continucare.knowledge.manifests")

    assert resources.joinpath("__init__.py").is_file()
    for name in (*MANIFEST_NAMES, "bundle_index_v1.json"):
        assert resources.joinpath(name).is_file()
        assert resources.joinpath(name).read_bytes()


def test_renderer_is_read_only_explicit_and_uses_derived_coverage():
    view = load_builtin_bundle().for_pathway("GLP1-14D", "1.0.0")
    rendered = render_pathway_knowledge(view)

    for line in KNOWLEDGE_DISCLAIMER:
        assert rendered.count(line) == 1
    assert "mode=CURRENT" in rendered
    coverage = next(line for line in rendered.splitlines() if line.startswith("Coverage:"))
    assert coverage == (
        "Coverage: unique_artifacts=21; registered_relationships=11; "
        "explicit_gaps=14; verified_citation_relationships=0; "
        "claim_review_approved_relationships=0"
    )
    assert "/" not in coverage
    assert "target#" not in rendered
    assert "source_integrity=not_content_fixed" in rendered
    assert "required_independent_approvals=" in rendered
    assert "knowledge_effect=informational_only; runtime_authority=none" in rendered
    assert "approved_for_execution" not in rendered


def test_loaded_registry_and_policy_are_deeply_read_only():
    registry = load_builtin_bundle()
    view = registry.for_pathway("GLP1-14D", "1.0.0")
    before = render_pathway_knowledge(view)
    summary = registry.review_summaries[
        ("glp1-nausea-collection-rationale", 1)
    ]

    for mutation in (
        lambda: registry.sources[0].access_urls.clear(),
        lambda: registry.claims[0].citations.clear(),
        lambda: registry.bindings[0].required_independent_approvals.clear(),
        lambda: registry.governance.review_events.append(None),
        lambda: registry.artifact_resolutions.clear(),
        lambda: registry.event_verification.clear(),
        lambda: view.source_content_status.clear(),
        lambda: summary.axes.__setitem__("clinical", "approved"),
        lambda: PURPOSE_REQUIREMENTS["justifies_collection"].clear(),
    ):
        with pytest.raises((AttributeError, TypeError)):
            mutation()

    with pytest.raises(TypeError):
        registry.review_summaries[("fake", 1)] = summary
    with pytest.raises(TypeError):
        PURPOSE_REQUIREMENTS["justifies_collection"] = frozenset()
    assert render_pathway_knowledge(view) == before


@pytest.mark.parametrize(
    "value",
    ["N/A", "na", "none", "unknown", "whole document", "not available", "TBD"],
)
def test_exact_locator_contract_rejects_placeholder_text(value):
    with pytest.raises(ValidationError, match="placeholder"):
        SectionLocator(section_number=value)
    with pytest.raises(ValidationError, match="placeholder"):
        UrlFragmentLocator(fragment=value)


def test_every_locator_union_arm_rejects_placeholder_fields():
    invalid_builders = (
        lambda: PageLocator(page_number=1, page_label="N/A"),
        lambda: TableOrFigureLocator(kind="table", label="TBD 1"),
        lambda: TerminologyConceptLocator(
            code_system="unknown", code_system_release="N/A", code="TBD"
        ),
        lambda: AnswerListLocator(
            code_system="none", code_system_release="not available", list_code="unknown"
        ),
    )
    for build in invalid_builders:
        with pytest.raises(ValidationError, match="placeholder"):
            build()

    with pytest.raises(ValidationError, match="requires an exact section title"):
        SectionLocator(section_number="Warnings")


def test_cli_requires_exact_version_and_reports_unknown_pathway(capsys):
    assert main(["GLP1-14D"]) == 2
    missing = capsys.readouterr()
    assert missing.out == ""
    assert missing.err.splitlines() == [
        "error: --version is required for an exact Pathway lookup"
    ]

    assert main(["UNKNOWN", "--version", "1.0.0"]) == 2
    unknown = capsys.readouterr()
    assert unknown.out == ""
    assert unknown.err.startswith("error: knowledge pathway UNKNOWN version 1.0.0")


def test_cli_renders_current_and_explicit_historical_modes(capsys):
    assert main(["GLP1-14D", "--version", "1.0.0"]) == 0
    current = capsys.readouterr()
    assert current.err == ""
    assert "mode=CURRENT" in current.out

    assert main(["GLP1-14D", "--version", "1.0.0", "--historical"]) == 0
    historical = capsys.readouterr()
    assert historical.err == ""
    assert "mode=HISTORICAL" in historical.out
    assert "HISTORICAL INSPECTION" in historical.out


def test_glp1_pathway_still_has_no_clinical_rules():
    before = load_builtin_pathways().get("GLP1-14D", "1.0.0").model_dump(
        mode="json"
    )
    load_builtin_bundle()
    after = load_builtin_pathways().get("GLP1-14D", "1.0.0").model_dump(
        mode="json"
    )

    assert before == after
    assert after["clinical_rules"] == []


def test_symptom_index_resolves_four_exact_catalog_terms_without_copying_truth():
    registry = load_builtin_bundle()
    views = {item.record.symptom_index_id: item for item in registry.symptom_views()}

    assert set(views) == {"diarrhea", "nausea", "vomiting", "abdominal-pain"}
    assert {
        key: (
            value.catalog_resolution.concept.preferred_zh,
            value.catalog_resolution.concept.coding.code,
        )
        for key, value in views.items()
    } == {
        "diarrhea": ("腹泻", "62315008"),
        "nausea": ("恶心", "422587007"),
        "vomiting": ("呕吐", "249497008"),
        "abdominal-pain": ("腹痛", "21522001"),
    }
    allowed_fields = {
        "symptom_index_id",
        "record_version",
        "catalog_term",
        "claim_refs",
        "binding_refs",
        "coverage_gap_refs",
        "lifecycle",
        "supersedes",
        "registered_at",
    }
    forbidden = {
        "preferred_zh",
        "aliases",
        "aliases_zh",
        "coding",
        "severity",
        "grading",
        "red_flags",
        "review_status",
        "approval_status",
        "clinical_action",
        "risk_level",
    }
    for record in registry.symptom_indexes:
        assert set(record.model_dump()) == allowed_fields
        assert forbidden.isdisjoint(record.model_dump())


def test_symptom_index_unknown_catalog_term_fails_current_and_is_historical_unresolved(
    tmp_path,
):
    root = _copy_builtin_bundle(tmp_path)
    _mutate(
        root,
        "symptom_index_v1.json",
        lambda value: value["records"][0]["catalog_term"].update(
            concept_id="not-a-real-concept"
        ),
    )
    resolver = RepositoryArtifactResolver()

    with pytest.raises(KnowledgeArtifactUnresolved, match="exact catalog term"):
        _load_test_bundle(root, artifact_resolver=resolver)

    historical = _load_test_bundle(
        root,
        mode=LoadMode.HISTORICAL,
        artifact_resolver=resolver,
    )
    view = historical.for_symptom("diarrhea", 1)
    assert not view.catalog_resolution.resolved
    assert "unresolved" in render_symptom_knowledge(view)


def test_symptom_index_exact_refs_preserve_claim_scope_and_derived_review():
    registry = load_builtin_bundle()
    for view in registry.symptom_views():
        assert all(
            registry.review_summaries[claim.ref.key()].aggregate == "not_assessed"
            for claim in view.claims
        )
        for binding in view.bindings:
            assert binding.claim in view.record.claim_refs
            claim = next(item for item in view.claims if item.ref == binding.claim)
            if claim.applicable_scope.mode == "pathway_whitelist":
                assert binding.pathway in claim.applicable_scope.pathways
        rendered = render_symptom_knowledge(view)
        assert "supports=" in rendered
        assert "does_not_support=" in rendered
        assert "applicable_scope=" in rendered
        assert "runtime_authority=none" in rendered


def test_m5k_external_sources_are_link_only_unbound_and_do_not_change_snomed_runtime():
    before = load_glp1_symptom_catalog().model_dump(mode="json")
    registry = load_builtin_bundle()
    after = load_glp1_symptom_catalog().model_dump(mode="json")
    sources = {
        item.source_id: item
        for item in registry.sources
        if item.registered_by == "Organization/continucare-m5k-link-only-registration"
    }

    assert before == after
    assert set(sources) == {"hpo-v2026-06-23", "nci-pro-ctcae-official-site"}
    assert all(item.access.mode == "link_only" for item in sources.values())
    assert all(
        registry.source_content_status[item.ref.key()] == "not_content_fixed"
        for item in sources.values()
    )
    cited = {
        citation.source.key()
        for claim in registry.claims
        if hasattr(claim, "citations")
        for citation in claim.citations
    }
    assert all(item.ref.key() not in cited for item in sources.values())
    assert not any(
        item.source_id == "nci-pro-ctcae-official-site"
        for view in registry.symptom_views()
        for item in view.sources
    )


def test_no_instrument_body_synthetic_expression_or_fixed_coverage_contract():
    package_root = REPOSITORY_ROOT / "continucare" / "knowledge"
    text = "\n".join(
        path.read_text("utf-8")
        for path in package_root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".json"}
    )

    assert "In the last 7 days" not in text
    assert "target_number" not in text
    assert "PRO-CTCAE→" not in text
    assert not list(package_root.rglob("*.pdf"))
    registry = load_builtin_bundle()
    expression_gaps = [
        gap
        for gap in registry.gaps
        if gap.gap_kind == "patient_expression_evidence"
    ]
    assert len(expression_gaps) == 4
    assert all("不复制别名" in item.reason for item in expression_gaps)


def test_symptom_cli_requires_exact_version_and_supports_both_modes(capsys):
    assert main(["--symptom-index-id", "nausea"]) == 2
    error = capsys.readouterr()
    assert "--record-version" in error.err

    assert main(
        ["--symptom-index-id", "nausea", "--record-version", "1"]
    ) == 0
    current = capsys.readouterr()
    assert "mode=CURRENT" in current.out
    assert "catalog_resolution=resolved" in current.out

    assert main(
        [
            "--symptom-index-id",
            "nausea",
            "--record-version",
            "1",
            "--historical",
        ]
    ) == 0
    historical = capsys.readouterr()
    assert "mode=HISTORICAL" in historical.out


def test_knowledge_page_has_no_patient_store_or_runtime_side_effect_imports():
    page = REPOSITORY_ROOT / "pages" / "5_knowledge_evidence.py"
    tree = ast.parse(page.read_text("utf-8"), filename=str(page))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    forbidden = (
        "continucare.db",
        "continucare.adapters",
        "continucare.services",
        "continucare.layer4",
        "continucare.care_agent",
        "continucare.care_engine",
    )
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in imports
        for prefix in forbidden
    )
    assert "patient_id" not in page.read_text("utf-8")


def test_knowledge_page_renders_governed_ops_sections_offline():
    app = AppTest.from_file(
        str(REPOSITORY_ROOT / "pages" / "5_knowledge_evidence.py"),
        default_timeout=10,
    ).run()

    assert not app.exception
    assert app.radio[0].options == ["来源库", "术语治理", "审核流程", "发布状态"]
    expected_sections = {
        "sources": "监管与药品资料",
        "terminology": "Core Symptom Catalog",
        "review": "HUMAN REVIEW GATES",
        "release": "治理准备中 · 尚未正式发布",
    }
    for section_id, expected_text in expected_sections.items():
        app.radio[0].set_value(section_id).run()
        assert not app.exception
        rendered = "\n".join(item.value for item in app.markdown)
        assert expected_text in rendered
        assert "GLP1-14D" not in rendered
        assert "四个内置主题" not in rendered


def test_payload_hash_is_checked_before_json_parsing(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    path = root / "claims_v1.json"
    payload = path.read_bytes()
    path.write_bytes(b"[" + payload[1:])

    with pytest.raises(KnowledgePinnedFileError, match="SHA-256"):
        _load_test_bundle(root)


def test_rehashed_invalid_payload_is_a_schema_error(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    (root / "claims_v1.json").write_text("[\n", encoding="utf-8")
    _repin(root)

    with pytest.raises(KnowledgeSchemaError, match="invalid pinned payload"):
        _load_test_bundle(root)


@pytest.mark.parametrize(
    "value",
    ["", "/absolute.json", "../escape.json", "a/../b", "a/./b", "a//b", r"a\b"],
)
def test_safe_relative_paths_reject_ambiguous_or_escaping_values(value):
    with pytest.raises(ValueError):
        safe_relative_parts(value)


def test_filesystem_backed_sources_reject_symlink_traversal(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.json").write_text("{}", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    for source in (FilesystemBundleSource(root), TraversableBundleSource(root)):
        with pytest.raises(ValueError, match="symlink"):
            source.read_bytes("linked/secret.json")


def test_filesystem_and_traversable_sources_share_path_contract(tmp_path):
    (tmp_path / "ok.json").write_text("{}", encoding="utf-8")
    filesystem = FilesystemBundleSource(tmp_path)
    traversable = TraversableBundleSource(tmp_path)

    assert filesystem.read_bytes("ok.json") == b"{}"
    assert traversable.read_bytes("ok.json") == b"{}"
    for source in (filesystem, traversable):
        with pytest.raises(ValueError):
            source.read_bytes("../ok.json")


def test_index_cannot_pin_an_escaping_payload_path(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    index = _read_json(root, "bundle_index_v1.json")
    index["files"][0]["relative_path"] = "../sources_v1.json"
    _write_json(root, "bundle_index_v1.json", index)

    with pytest.raises(KnowledgeSchemaError, match="resource path"):
        _load_test_bundle(root)


def test_unknown_source_and_claim_refs_fail_atomically(tmp_path):
    source_root = _copy_builtin_bundle(tmp_path / "source-case")
    _mutate(
        source_root,
        "claims_v1.json",
        lambda value: value["records"][0]["citations"][0].update(
            source={"source_id": "missing", "record_version": 1}
        ),
    )
    with pytest.raises(KnowledgeReferenceError, match="unknown source"):
        _load_test_bundle(source_root)

    claim_root = _copy_builtin_bundle(tmp_path / "claim-case")
    _mutate(
        claim_root,
        "bindings_glp1_14d_v1.json",
        lambda value: value["records"][0].update(
            claim={"claim_id": "missing", "claim_version": 1}
        ),
    )
    with pytest.raises(KnowledgeReferenceError, match="unknown claim"):
        _load_test_bundle(claim_root)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        (
            "sources_v1.json",
            lambda value: value["records"][0].update(registry_status="withdrawn"),
            "current source",
        ),
        (
            "claims_v1.json",
            lambda value: value["records"][0].update(
                lifecycle="retracted", retraction_reason="synthetic withdrawal"
            ),
            "current claim",
        ),
        (
            "bindings_glp1_14d_v1.json",
            lambda value: value["records"][0].update(lifecycle="retracted"),
            "current binding",
        ),
        (
            "governance_v1.json",
            lambda value: value["coverage_gaps"][0].update(lifecycle="resolved"),
            "current coverage gap",
        ),
    ],
)
def test_current_selection_rejects_ineligible_heads(
    tmp_path, field, replacement, message
):
    root = _copy_builtin_bundle(tmp_path)
    _mutate(root, field, replacement)

    with pytest.raises(KnowledgeCurrentSelectionError, match=message):
        _load_test_bundle(root)


def test_current_selection_cannot_point_to_a_non_head(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    claims = _read_json(root, "claims_v1.json")
    successor = json.loads(json.dumps(claims["records"][0]))
    successor["claim_version"] = 2
    successor["supersedes"] = {
        "claim_id": successor["claim_id"],
        "claim_version": 1,
    }
    claims["records"].append(successor)
    _write_json(root, "claims_v1.json", claims)
    _repin(root)

    with pytest.raises(KnowledgeCurrentSelectionError, match="non-head"):
        _load_test_bundle(root)


def test_inactive_head_can_be_omitted_from_current_without_rewriting_history(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    _mutate(
        root,
        "governance_v1.json",
        lambda value: value["coverage_gaps"][0].update(lifecycle="resolved"),
    )
    index = _read_json(root, "bundle_index_v1.json")
    omitted = index["current_gap_refs"].pop(0)
    _write_json(root, "bundle_index_v1.json", index)

    registry = _load_test_bundle(root)
    view = registry.for_pathway("GLP1-14D", "1.0.0")
    assert omitted["gap_id"] not in {item.gap_id for item in view.gaps}
    assert any(
        item.gap_id == omitted["gap_id"] and item.lifecycle == "resolved"
        for item in registry.gaps
    )


def test_historical_mode_rejects_dangling_current_refs(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    index = _read_json(root, "bundle_index_v1.json")
    index["current_source_refs"][0] = {
        "source_id": "missing-source",
        "record_version": 99,
    }
    _write_json(root, "bundle_index_v1.json", index)

    with pytest.raises(KnowledgeCurrentSelectionError, match="unknown records"):
        _load_test_bundle(root, mode=LoadMode.HISTORICAL)


def test_append_only_successors_leave_old_records_readable(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    claims = _read_json(root, "claims_v1.json")
    original_claim = json.loads(json.dumps(claims["records"][0]))
    successor_claim = json.loads(json.dumps(original_claim))
    successor_claim.update(
        claim_version=2,
        statement="Synthetic successor for append-only contract testing.",
        supersedes={
            "claim_id": original_claim["claim_id"],
            "claim_version": 1,
        },
    )
    claims["records"].append(successor_claim)
    _write_json(root, "claims_v1.json", claims)

    bindings = _read_json(root, "bindings_glp1_14d_v1.json")
    original_binding = json.loads(json.dumps(bindings["records"][0]))
    successor_binding = json.loads(json.dumps(original_binding))
    successor_binding.update(
        binding_version=2,
        claim={"claim_id": original_claim["claim_id"], "claim_version": 2},
        supersedes={
            "binding_id": original_binding["binding_id"],
            "binding_version": 1,
        },
    )
    bindings["records"].append(successor_binding)
    _write_json(root, "bindings_glp1_14d_v1.json", bindings)

    index = _read_json(root, "bundle_index_v1.json")
    index["current_claim_refs"][0]["claim_version"] = 2
    index["current_binding_refs"][0]["binding_version"] = 2
    index["current_symptom_index_refs"] = []
    _write_json(root, "bundle_index_v1.json", index)
    _repin(root)

    registry = _load_test_bundle(root)
    view = registry.for_pathway("GLP1-14D", "1.0.0")
    assert _read_json(root, "claims_v1.json")["records"][0] == original_claim
    assert any(item.claim_version == 2 for item in registry.claims)
    assert any(item.binding_version == 2 for item in view.bindings)
    assert not any(
        item.binding_id == original_binding["binding_id"]
        and item.binding_version == 1
        for item in view.bindings
    )


def test_broken_successor_chain_is_rejected(tmp_path):
    root = _copy_builtin_bundle(tmp_path)

    def append_bad_gap(value):
        successor = json.loads(json.dumps(value["coverage_gaps"][0]))
        successor["gap_version"] = 2
        successor["supersedes"] = None
        value["coverage_gaps"].append(successor)

    _mutate(root, "governance_v1.json", append_bad_gap)

    with pytest.raises(KnowledgeSchemaError, match="version 2 must supersede"):
        _load_test_bundle(root)


def test_current_unresolved_artifact_fails_but_historical_stays_readable(tmp_path):
    root = _copy_builtin_bundle(tmp_path)

    with pytest.raises(KnowledgeArtifactUnresolved):
        _load_test_bundle(root, artifact_resolver=_ArtifactResolver(False))

    historical = _load_test_bundle(
        root,
        mode=LoadMode.HISTORICAL,
        artifact_resolver=_ArtifactResolver(False),
    )
    view = historical.for_pathway("GLP1-14D", "1.0.0")
    assert historical.mode == LoadMode.HISTORICAL
    assert len(view.artifact_resolutions) == 25
    assert not any(item.resolved for item in view.artifact_resolutions.values())


def test_unresolved_review_actor_fails_current_but_is_annotated_historically(
    tmp_path,
):
    root = _copy_builtin_bundle(tmp_path)
    _add_events(root, [_clinical_event()])

    with pytest.raises(KnowledgeAuthorityError, match="could not be resolved"):
        _load_test_bundle(root)

    historical = _load_test_bundle(root, mode=LoadMode.HISTORICAL)
    assert historical.event_verification == {
        "clinical-review-1": "unverified_assertion"
    }
    assert (
        historical.review_summaries[("glp1-nausea-collection-rationale", 1)].aggregate
        == "not_assessed"
    )


def test_trusted_rejected_review_blocks_current_selection(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    _add_events(root, [_clinical_event(decision="rejected")])
    authority = MappingAuthorityResolver(
        {"Practitioner/reviewer": frozenset({"clinician"})}
    )

    with pytest.raises(KnowledgeCurrentSelectionError, match="rejected"):
        _load_test_bundle(root, authority_resolver=authority)


def test_review_chain_head_determines_status_and_pharmacy_is_separate(tmp_path):
    root = _copy_builtin_bundle(tmp_path / "clinical")
    _add_events(
        root,
        [
            _clinical_event(event_id="clinical-root"),
            _clinical_event(
                event_id="clinical-head",
                decision="changes_requested",
                supersedes_event_id="clinical-root",
            ),
        ],
    )
    authority = MappingAuthorityResolver(
        {"Practitioner/reviewer": frozenset({"clinician"})}
    )
    registry = _load_test_bundle(root, authority_resolver=authority)
    summary = registry.review_summaries[("glp1-nausea-collection-rationale", 1)]
    assert summary.axes["clinical"] == "changes_requested"
    assert summary.aggregate == "changes_requested"

    pharmacy_root = _copy_builtin_bundle(tmp_path / "pharmacy")
    pharmacy_event = {
        **_clinical_event(event_id="pharmacy-review"),
        "event_type": "pharmacy",
        "claimed_role": "pharmacist",
    }
    _add_events(pharmacy_root, [pharmacy_event])
    pharmacy_authority = MappingAuthorityResolver(
        {"Practitioner/reviewer": frozenset({"pharmacist"})}
    )
    pharmacy_registry = _load_test_bundle(
        pharmacy_root, authority_resolver=pharmacy_authority
    )
    pharmacy_summary = pharmacy_registry.review_summaries[
        ("glp1-nausea-collection-rationale", 1)
    ]
    assert pharmacy_summary.pharmacy == "approved"
    assert pharmacy_summary.axes["clinical"] == "not_assessed"
    assert pharmacy_summary.aggregate == "not_assessed"
    assert "pharmacy_review=approved" in render_pathway_knowledge(
        pharmacy_registry.for_pathway("GLP1-14D", "1.0.0")
    )


def test_claim_review_approval_does_not_claim_artifact_approval(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    claim = {
        "claim_id": "glp1-nausea-collection-rationale",
        "claim_version": 1,
    }
    _add_events(
        root,
        [
            _clinical_event(),
            {
                "event_type": "internal_consistency",
                "event_id": "claim-internal-check",
                "supersedes_event_id": None,
                "subject": claim,
                "decision": "approved",
                "claimed_role": "knowledge_curator",
                "actor_reference": "Practitioner/reviewer",
                "decided_at": "2026-08-13T01:01:00+00:00",
                "rationale": "Synthetic claim consistency check.",
            },
            {
                "event_type": "citation_verification",
                "event_id": "citation-check",
                "supersedes_event_id": None,
                "subject": {
                    "claim": claim,
                    "citation_id": "fda-section-6-1",
                },
                "decision": "approved",
                "claimed_role": "librarian",
                "actor_reference": "Practitioner/reviewer",
                "decided_at": "2026-08-13T01:02:00+00:00",
                "rationale": "Synthetic exact-citation check.",
            },
        ],
    )
    authority = MappingAuthorityResolver(
        {
            "Practitioner/reviewer": frozenset(
                {"clinician", "knowledge_curator", "librarian"}
            )
        }
    )
    view = _load_test_bundle(root, authority_resolver=authority).for_pathway(
        "GLP1-14D", "1.0.0"
    )
    rendered = render_pathway_knowledge(view)

    assert view.claim_review_approved_relationship_count == 1
    assert "claim_review_approved_relationships=1" in rendered
    assert "fully_approved" not in rendered
    assert "questionnaire_publication" in rendered
    assert "runtime_authority=none" in rendered


@pytest.mark.parametrize("shape", ["branch", "cross_subject", "cycle"])
def test_review_chains_reject_branch_cross_subject_and_cycle(tmp_path, shape):
    root = _copy_builtin_bundle(tmp_path)
    root_event = _clinical_event(event_id="event-a")
    if shape == "branch":
        events = [
            root_event,
            _clinical_event(event_id="event-b", supersedes_event_id="event-a"),
            _clinical_event(event_id="event-c", supersedes_event_id="event-a"),
        ]
    elif shape == "cross_subject":
        events = [
            root_event,
            _clinical_event(
                event_id="event-b",
                claim_id="glp1-vomiting-collection-rationale",
                supersedes_event_id="event-a",
            ),
        ]
    else:
        events = [
            _clinical_event(event_id="event-a", supersedes_event_id="event-b"),
            _clinical_event(event_id="event-b", supersedes_event_id="event-a"),
        ]
    _add_events(root, events)

    with pytest.raises(KnowledgeSchemaError, match="review"):
        _load_test_bundle(root)


def test_review_event_union_accepts_only_domain_specific_subjects_and_roles():
    adapter = TypeAdapter(ReviewEvent)
    claim = {"claim_id": "claim-a", "claim_version": 1}
    source = {"source_id": "source-a", "record_version": 1}
    base = {
        "event_id": "event-a",
        "supersedes_event_id": None,
        "actor_reference": "Practitioner/test",
        "decided_at": "2026-08-13T00:00:00+00:00",
        "rationale": "Synthetic typed-event test.",
        "decision": "approved",
    }
    valid = [
        {**base, "event_type": "clinical", "subject": claim, "claimed_role": "clinician"},
        {**base, "event_type": "pharmacy", "subject": claim, "claimed_role": "pharmacist"},
        {**base, "event_type": "terminology", "subject": claim, "claimed_role": "terminologist"},
        {
            **base,
            "event_type": "internal_consistency",
            "subject": source,
            "claimed_role": "knowledge_curator",
        },
        {
            **base,
            "event_type": "citation_verification",
            "subject": {"claim": claim, "citation_id": "citation-a"},
            "claimed_role": "librarian",
        },
        {
            **base,
            "event_type": "license_decision",
            "subject": source,
            "claimed_role": "rights_officer",
            "payload": {"allowed_uses": ["local_copy"]},
        },
        {
            **base,
            "event_type": "equivalence",
            "subject": {"manifestation": source, "canonical": source},
            "claimed_role": "librarian",
        },
    ]

    assert [adapter.validate_python(item).event_type for item in valid] == [
        "clinical",
        "pharmacy",
        "terminology",
        "internal_consistency",
        "citation_verification",
        "license_decision",
        "equivalence",
    ]
    invalid = {
        **base,
        "event_type": "clinical",
        "subject": source,
        "claimed_role": "pharmacist",
    }
    with pytest.raises(ValidationError):
        adapter.validate_python(invalid)


def test_nonapproved_license_decision_cannot_smuggle_allowed_uses():
    event = {
        "event_type": "license_decision",
        "event_id": "license-a",
        "supersedes_event_id": None,
        "subject": {"source_id": "source-a", "record_version": 1},
        "decision": "rejected",
        "claimed_role": "rights_officer",
        "actor_reference": "Organization/rights",
        "decided_at": "2026-08-13T00:00:00+00:00",
        "rationale": "Synthetic rejected license.",
        "payload": {"allowed_uses": ["local_copy"]},
    }

    with pytest.raises(ValidationError, match="cannot include allowed_uses"):
        TypeAdapter(ReviewEvent).validate_python(event)


def _make_source_local(
    root: Path,
    *,
    source_id: str,
    relative_path: str,
    payload: bytes,
    digest: str | None = None,
) -> None:
    def update(value):
        source = next(
            item for item in value["records"] if item["source_id"] == source_id
        )
        source["access"] = {
            "mode": "local_artifact",
            "artifact": {
                "relative_path": relative_path,
                "third_party_content_sha256": digest
                or hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            },
        }

    _mutate(root, "sources_v1.json", update)


def _license_event(source_id: str, allowed_uses: list[str]) -> dict:
    return {
        "event_type": "license_decision",
        "event_id": f"license-{source_id}",
        "supersedes_event_id": None,
        "subject": {"source_id": source_id, "record_version": 1},
        "decision": "approved",
        "claimed_role": "rights_officer",
        "actor_reference": "Organization/rights",
        "decided_at": "2026-08-13T00:00:00+00:00",
        "rationale": "Synthetic local-copy license for safety-contract testing.",
        "payload": {"allowed_uses": allowed_uses},
    }


def test_current_local_artifact_requires_bytes_hash_and_trusted_license(tmp_path):
    payload = b"synthetic licensed bytes"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    (artifact_root / "document.bin").write_bytes(payload)
    authority = MappingAuthorityResolver(
        {"Organization/rights": frozenset({"rights_officer"})}
    )

    success_root = _copy_builtin_bundle(tmp_path / "success")
    _make_source_local(
        success_root,
        source_id="snomed-fhir",
        relative_path="document.bin",
        payload=payload,
    )
    _add_events(success_root, [_license_event("snomed-fhir", ["local_copy"])])
    loaded = _load_test_bundle(
        success_root,
        authority_resolver=authority,
        source_artifact_resolver=FilesystemSourceArtifactResolver(artifact_root),
    )
    assert loaded.source_content_status[("snomed-fhir", 1)] == "content_fixed"

    no_license_root = _copy_builtin_bundle(tmp_path / "no-license")
    _make_source_local(
        no_license_root,
        source_id="snomed-fhir",
        relative_path="document.bin",
        payload=payload,
    )
    no_license_resolver = _CountingSourceArtifactResolver(payload)
    with pytest.raises(KnowledgeSourceArtifactError, match="license"):
        _load_test_bundle(
            no_license_root,
            source_artifact_resolver=no_license_resolver,
        )
    assert no_license_resolver.calls == []

    wrong_hash_root = _copy_builtin_bundle(tmp_path / "wrong-hash")
    _make_source_local(
        wrong_hash_root,
        source_id="snomed-fhir",
        relative_path="document.bin",
        payload=payload,
        digest="0" * 64,
    )
    _add_events(wrong_hash_root, [_license_event("snomed-fhir", ["local_copy"])])
    with pytest.raises(KnowledgeSourceArtifactError, match="hash mismatch"):
        _load_test_bundle(
            wrong_hash_root,
            authority_resolver=authority,
            source_artifact_resolver=FilesystemSourceArtifactResolver(artifact_root),
        )


def test_historical_local_artifact_never_reads_registered_blob(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    payload = b"not read"
    _make_source_local(
        root,
        source_id="snomed-fhir",
        relative_path="document.bin",
        payload=payload,
    )
    resolver = _CountingSourceArtifactResolver(payload)

    historical = _load_test_bundle(
        root,
        mode=LoadMode.HISTORICAL,
        source_artifact_resolver=resolver,
    )
    assert resolver.calls == []
    assert (
        historical.source_content_status[("snomed-fhir", 1)]
        == "not_read_in_historical_mode"
    )


def test_local_artifact_path_is_rejected_by_schema_before_any_resolver(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    _make_source_local(
        root,
        source_id="snomed-fhir",
        relative_path="../../outside.bin",
        payload=b"not read",
    )
    resolver = _CountingSourceArtifactResolver()

    with pytest.raises(KnowledgeSchemaError, match="resource path"):
        _load_test_bundle(
            root,
            mode=LoadMode.HISTORICAL,
            source_artifact_resolver=resolver,
        )
    assert resolver.calls == []


def test_embedded_quote_requires_separate_trusted_short_quote_permission(tmp_path):
    payload = b"synthetic licensed terminology bytes"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    (artifact_root / "loinc.bin").write_bytes(payload)
    authority = MappingAuthorityResolver(
        {"Organization/rights": frozenset({"rights_officer"})}
    )
    root = _copy_builtin_bundle(tmp_path)
    _make_source_local(
        root,
        source_id="loinc-2.82",
        relative_path="loinc.bin",
        payload=payload,
    )
    _mutate(
        root,
        "claims_v1.json",
        lambda value: value["records"][1]["citations"][0].update(
            quote="Synthetic short excerpt."
        ),
    )
    _add_events(root, [_license_event("loinc-2.82", ["local_copy"])])

    with pytest.raises(KnowledgeSourceArtifactError, match="short_quote"):
        _load_test_bundle(
            root,
            authority_resolver=authority,
            source_artifact_resolver=FilesystemSourceArtifactResolver(artifact_root),
        )

    governance = _read_json(root, "governance_v1.json")
    governance["review_events"][0]["payload"]["allowed_uses"].append("short_quote")
    _write_json(root, "governance_v1.json", governance)
    _repin(root)
    registry = _load_test_bundle(
        root,
        authority_resolver=authority,
        source_artifact_resolver=FilesystemSourceArtifactResolver(artifact_root),
    )
    assert registry.source_content_status[("loinc-2.82", 1)] == "content_fixed"


def test_authority_identity_must_match_the_asserted_actor(tmp_path):
    class MismatchedAuthorityResolver:
        def resolve(self, actor_reference: str):
            del actor_reference
            return ResolvedAuthority(
                actor_reference="Practitioner/someone-else",
                roles=frozenset({"clinician"}),
            )

    root = _copy_builtin_bundle(tmp_path)
    _add_events(root, [_clinical_event()])

    with pytest.raises(KnowledgeAuthorityError, match="could not be resolved"):
        _load_test_bundle(root, authority_resolver=MismatchedAuthorityResolver())


def test_authority_resolver_wrong_return_type_is_fail_closed(tmp_path):
    class WrongTypeAuthorityResolver:
        def resolve(self, actor_reference: str):
            del actor_reference
            return object()

    root = _copy_builtin_bundle(tmp_path)
    _add_events(root, [_clinical_event()])
    with pytest.raises(KnowledgeAuthorityError, match="ResolvedAuthority"):
        _load_test_bundle(root, authority_resolver=WrongTypeAuthorityResolver())

    historical = _load_test_bundle(
        root,
        mode=LoadMode.HISTORICAL,
        authority_resolver=WrongTypeAuthorityResolver(),
    )
    assert historical.event_verification["clinical-review-1"] == (
        "unverified_assertion"
    )


def test_historical_authority_annotations_cover_superseded_events(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    _add_events(
        root,
        [
            _clinical_event(event_id="old-review", actor="Practitioner/old"),
            _clinical_event(
                event_id="current-review",
                actor="Practitioner/current",
                supersedes_event_id="old-review",
            ),
        ],
    )
    authority = MappingAuthorityResolver(
        {"Practitioner/current": frozenset({"clinician"})}
    )

    registry = _load_test_bundle(root, authority_resolver=authority)
    assert registry.event_verification == {
        "old-review": "unverified_assertion",
        "current-review": "trusted",
    }
    assert (
        registry.review_summaries[("glp1-nausea-collection-rationale", 1)].axes[
            "clinical"
        ]
        == "approved"
    )


def test_source_review_text_is_derived_from_trusted_events(tmp_path):
    root = _copy_builtin_bundle(tmp_path)
    event = {
        "event_type": "internal_consistency",
        "event_id": "source-check",
        "supersedes_event_id": None,
        "subject": {"source_id": "fda-wegovy-2026", "record_version": 1},
        "decision": "approved",
        "claimed_role": "knowledge_curator",
        "actor_reference": "Practitioner/curator",
        "decided_at": "2026-08-13T00:00:00+00:00",
        "rationale": "Synthetic internal source check.",
    }
    _add_events(root, [event])
    authority = MappingAuthorityResolver(
        {"Practitioner/curator": frozenset({"knowledge_curator"})}
    )

    registry = _load_test_bundle(root, authority_resolver=authority)
    rendered = render_pathway_knowledge(
        registry.for_pathway("GLP1-14D", "1.0.0")
    )
    assert "source_internal_consistency_review=approved" in rendered
    assert "source_review=unreviewed_public_source" not in rendered


def test_protocol_exceptions_are_wrapped_or_annotated_by_mode(tmp_path):
    class ExplodingArtifactResolver:
        def resolve(self, pathway, artifact):
            del pathway, artifact
            raise RuntimeError("artifact resolver exploded")

        def resolve_catalog_term(self, term):
            del term
            raise RuntimeError("artifact resolver exploded")

    class ExplodingAuthorityResolver:
        def resolve(self, actor_reference: str):
            del actor_reference
            raise RuntimeError("authority resolver exploded")

    class ExplodingBundleSource:
        def read_bytes(self, relative_posix_path: str):
            del relative_posix_path
            raise RuntimeError("bundle source exploded")

    class WrongTypeBundleSource:
        def read_bytes(self, relative_posix_path: str):
            del relative_posix_path
            return "not bytes"

    root = _copy_builtin_bundle(tmp_path / "artifact")
    with pytest.raises(KnowledgeArtifactUnresolved, match="exploded"):
        _load_test_bundle(root, artifact_resolver=ExplodingArtifactResolver())
    historical = _load_test_bundle(
        root,
        mode=LoadMode.HISTORICAL,
        artifact_resolver=ExplodingArtifactResolver(),
    )
    assert not any(item.resolved for item in historical.artifact_resolutions.values())

    authority_root = _copy_builtin_bundle(tmp_path / "authority")
    _add_events(authority_root, [_clinical_event()])
    with pytest.raises(KnowledgeAuthorityError, match="exploded"):
        _load_test_bundle(
            authority_root, authority_resolver=ExplodingAuthorityResolver()
        )
    historical_authority = _load_test_bundle(
        authority_root,
        mode=LoadMode.HISTORICAL,
        authority_resolver=ExplodingAuthorityResolver(),
    )
    assert historical_authority.event_verification["clinical-review-1"] == (
        "unverified_assertion"
    )

    with pytest.raises(KnowledgePinnedFileError, match="bundle source exploded"):
        load_bundle(
            ExplodingBundleSource(),
            "bundle_index_v1.json",
            artifact_resolver=_ArtifactResolver(),
        )
    with pytest.raises(KnowledgePinnedFileError, match="must return bytes"):
        load_bundle(
            WrongTypeBundleSource(),
            "bundle_index_v1.json",
            artifact_resolver=_ArtifactResolver(),
        )


def test_source_artifact_protocol_exception_is_wrapped_after_license(tmp_path):
    class ExplodingSourceArtifactResolver:
        def read_bytes(self, relative_posix_path: str):
            del relative_posix_path
            raise RuntimeError("source artifact exploded")

    payload = b"synthetic bytes"
    root = _copy_builtin_bundle(tmp_path)
    _make_source_local(
        root,
        source_id="snomed-fhir",
        relative_path="document.bin",
        payload=payload,
    )
    _add_events(root, [_license_event("snomed-fhir", ["local_copy"])])
    authority = MappingAuthorityResolver(
        {"Organization/rights": frozenset({"rights_officer"})}
    )

    with pytest.raises(KnowledgeSourceArtifactError, match="source artifact exploded"):
        _load_test_bundle(
            root,
            authority_resolver=authority,
            source_artifact_resolver=ExplodingSourceArtifactResolver(),
        )


def test_historical_equivalence_event_is_not_current_due_only_to_canonical_source(
    tmp_path,
):
    root = _copy_builtin_bundle(tmp_path)

    def add_manifestation_successor(value):
        child = next(
            item for item in value["records"] if item["source_id"] == "snomed-fhir"
        )
        canonical = next(
            item
            for item in value["records"]
            if item["source_id"] == "snomed-ct-clinical-finding"
        )
        shared_id = {"scheme": "urn", "value": "urn:synthetic:shared-document"}
        child["document_identifiers"].append(shared_id)
        canonical["document_identifiers"].append(shared_id)
        child["manifestation_of"] = {
            "source_id": canonical["source_id"],
            "record_version": 1,
        }
        successor = json.loads(json.dumps(child))
        successor["record_version"] = 2
        successor["supersedes"] = {
            "source_id": child["source_id"],
            "record_version": 1,
        }
        successor["manifestation_of"] = None
        value["records"].append(successor)

    _mutate(root, "sources_v1.json", add_manifestation_successor)
    event = {
        "event_type": "equivalence",
        "event_id": "historical-equivalence",
        "supersedes_event_id": None,
        "subject": {
            "manifestation": {"source_id": "snomed-fhir", "record_version": 1},
            "canonical": {
                "source_id": "snomed-ct-clinical-finding",
                "record_version": 1,
            },
        },
        "decision": "approved",
        "claimed_role": "librarian",
        "actor_reference": "Practitioner/unresolved-librarian",
        "decided_at": "2026-08-13T00:00:00+00:00",
        "rationale": "Synthetic historical equivalence edge.",
    }
    _add_events(root, [event])
    index = _read_json(root, "bundle_index_v1.json")
    current = next(
        item
        for item in index["current_source_refs"]
        if item["source_id"] == "snomed-fhir"
    )
    current["record_version"] = 2
    _write_json(root, "bundle_index_v1.json", index)
    _repin(root)

    registry = _load_test_bundle(root)
    assert registry.event_verification["historical-equivalence"] == (
        "unverified_assertion"
    )


def test_source_cannot_be_a_manifestation_of_itself(tmp_path):
    root = _copy_builtin_bundle(tmp_path)

    def add_self_edge(value):
        source = value["records"][0]
        source["manifestation_of"] = {
            "source_id": source["source_id"],
            "record_version": source["record_version"],
        }

    _mutate(root, "sources_v1.json", add_self_edge)
    with pytest.raises(KnowledgeSchemaError, match="manifestation of itself"):
        _load_test_bundle(root)


def test_source_manifestation_graph_must_be_acyclic(tmp_path):
    root = _copy_builtin_bundle(tmp_path)

    def add_two_node_cycle(value):
        left, right = value["records"][:2]
        shared = {"scheme": "urn", "value": "urn:synthetic:cycle-document"}
        left["document_identifiers"].append(shared)
        right["document_identifiers"].append(shared)
        left["manifestation_of"] = {
            "source_id": right["source_id"],
            "record_version": right["record_version"],
        }
        right["manifestation_of"] = {
            "source_id": left["source_id"],
            "record_version": left["record_version"],
        }

    _mutate(root, "sources_v1.json", add_two_node_cycle)
    with pytest.raises(KnowledgeReferenceError, match="contains a cycle"):
        _load_test_bundle(root)


def _write_synthetic_bundle(
    root: Path,
    *,
    allowed_pathway: tuple[str, str] | None = None,
) -> None:
    root.mkdir(parents=True)
    pathway_code = "SYNTHETIC-PW-B"
    pathway_version = "2.0.0"
    source = {
        "file_kind": "source_registry",
        "file_id": "synthetic-sources",
        "file_version": 1,
        "contract_version": "1.0.0",
        "records": [
            {
                "source_id": "synthetic-shared-standard",
                "record_version": 1,
                "title": "Synthetic interoperability standard",
                "language": "en",
                "source_type": "terminology_standard",
                "authority_metadata_status": "repository_declared_unverified",
                "issuing_authority": "Synthetic Standards Body",
                "authority_type": "standards_body",
                "jurisdiction_metadata_status": "repository_declared_unverified",
                "jurisdictions": [{"system": "global", "code": "INT"}],
                "document_identifiers": [
                    {"scheme": "urn", "value": "urn:synthetic:standard:v1"}
                ],
                "canonical_url": "https://example.invalid/synthetic-standard",
                "access_urls": [
                    {
                        "url": "https://example.invalid/synthetic-standard",
                        "url_role": "landing",
                    }
                ],
                "document_version": "1.0.0",
                "document_effective_date": None,
                "document_publication_date": None,
                "retrieval": {
                    "precision": "date",
                    "retrieved_on": "2026-08-13",
                    "retrieved_at": None,
                },
                "access": {"mode": "link_only", "integrity": "not_content_fixed"},
                "license_terms_uri": None,
                "license_terms_locator": None,
                "manifestation_of": None,
                "registry_status": "active",
                "supersedes": None,
                "registered_at": "2026-08-13T00:00:00+00:00",
                "registered_by": "Organization/synthetic-test",
            }
        ],
    }
    if allowed_pathway is None:
        scope = {"mode": "universal_nonclinical_standard", "domain": "terminology"}
    else:
        scope = {
            "mode": "pathway_whitelist",
            "pathways": [
                {
                    "pathway_code": allowed_pathway[0],
                    "pathway_version": allowed_pathway[1],
                }
            ],
            "conditions": {"mode": "any"},
            "products": {"mode": "any"},
            "jurisdictions": {"mode": "any"},
            "care_settings": {"mode": "any"},
            "age": {"mode": "any"},
            "population": "Synthetic non-clinical fixture population.",
        }
    claim = {
        "file_kind": "claim_registry",
        "file_id": "synthetic-claims",
        "file_version": 1,
        "contract_version": "1.0.0",
        "records": [
            {
                "claim_kind": "sourced_clinical_claim",
                "claim_id": "synthetic-terminology-support",
                "claim_version": 1,
                "claim_type": "terminology_support",
                "statement": "Synthetic standard representation candidate.",
                "supports": ["A synthetic representation for loader tests."],
                "does_not_support": ["No clinical behavior or real-world validity."],
                "applicable_scope": scope,
                "evidence_strength": "standards_body",
                "citations": [
                    {
                        "citation_id": "synthetic-code",
                        "source": {
                            "source_id": "synthetic-shared-standard",
                            "record_version": 1,
                        },
                        "locator": {
                            "locator_type": "terminology_concept",
                            "code_system": "urn:synthetic:codes",
                            "code_system_release": "1.0.0",
                            "code": "SYN-1",
                        },
                        "role": "terminology_definition",
                        "quote": None,
                    }
                ],
                "lifecycle": "draft",
                "supersedes": None,
                "retraction_reason": None,
                "registered_at": "2026-08-13T00:00:00+00:00",
            }
        ],
    }
    binding = {
        "file_kind": "binding_manifest",
        "file_id": "synthetic-bindings",
        "file_version": 1,
        "contract_version": "1.0.0",
        "pathway": {
            "pathway_code": pathway_code,
            "pathway_version": pathway_version,
        },
        "records": [
            {
                "binding_id": "synthetic-questionnaire-item-binding",
                "binding_version": 1,
                "pathway": {
                    "pathway_code": pathway_code,
                    "pathway_version": pathway_version,
                },
                "artifact": {
                    "artifact_kind": "questionnaire_item",
                    "questionnaire_canonical": "urn:synthetic:Questionnaire:pw-b",
                    "questionnaire_version": "2.0.0",
                    "link_id": "synthetic-item",
                },
                "claim": {
                    "claim_id": "synthetic-terminology-support",
                    "claim_version": 1,
                },
                "binding_purpose": "justifies_terminology_choice",
                "required_independent_approvals": [
                    "questionnaire_publication",
                    "terminology_approval",
                ],
                "lifecycle": "active",
                "supersedes": None,
                "note": "Synthetic fixture only.",
                "created_at": "2026-08-13T00:00:00+00:00",
            }
        ],
    }
    governance = {
        "file_kind": "governance_registry",
        "file_id": "synthetic-governance",
        "file_version": 1,
        "contract_version": "1.0.0",
        "review_events": [],
        "legacy_source_aliases": [],
        "artifact_ownership": [],
        "coverage_gaps": [],
    }
    payloads = {
        "sources.json": source,
        "claims.json": claim,
        "bindings.json": binding,
        "governance.json": governance,
    }
    for name, value in payloads.items():
        _write_json(root, name, value)
    pinned_files = []
    for name, file_id in (
        ("sources.json", "synthetic-sources"),
        ("claims.json", "synthetic-claims"),
        ("bindings.json", "synthetic-bindings"),
        ("governance.json", "synthetic-governance"),
    ):
        payload = (root / name).read_bytes()
        pinned_files.append(
            {
                "ref": {"file_id": file_id, "file_version": 1},
                "relative_path": name,
                "manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    index = {
        "file_kind": "bundle_index",
        "bundle_id": "synthetic-pathway-b-knowledge",
        "bundle_version": 1,
        "contract_version": "1.0.0",
        "files": pinned_files,
        "current_source_refs": [
            {"source_id": "synthetic-shared-standard", "record_version": 1}
        ],
        "current_claim_refs": [
            {"claim_id": "synthetic-terminology-support", "claim_version": 1}
        ],
        "current_binding_refs": [
            {
                "binding_id": "synthetic-questionnaire-item-binding",
                "binding_version": 1,
            }
        ],
        "current_gap_refs": [],
    }
    _write_json(root, "bundle_index_v1.json", index)


def test_second_pathway_fixture_uses_same_loader_without_glp_contract_constants(
    tmp_path,
):
    root = tmp_path / "synthetic"
    _write_synthetic_bundle(root)

    registry = _load_test_bundle(root)
    view = registry.for_pathway("SYNTHETIC-PW-B", "2.0.0")
    rendered = render_pathway_knowledge(view)

    assert view.unique_artifact_count == 1
    assert len(view.bindings) == 1
    assert view.bindings[0].claim.claim_id == "synthetic-terminology-support"
    coverage = next(line for line in rendered.splitlines() if line.startswith("Coverage:"))
    assert coverage == (
        "Coverage: unique_artifacts=1; registered_relationships=1; "
        "explicit_gaps=0; verified_citation_relationships=0; "
        "claim_review_approved_relationships=0"
    )
    with pytest.raises(KnowledgeReferenceError):
        load_builtin_bundle().for_pathway("SYNTHETIC-PW-B", "2.0.0")


@pytest.mark.parametrize(
    "allowed_pathway",
    [("GLP1-14D", "1.0.0"), ("SYNTHETIC-PW-B", "1.0.0")],
)
def test_pathway_specific_claim_cannot_cross_code_or_version_scope(
    tmp_path, allowed_pathway
):
    root = tmp_path / "synthetic"
    _write_synthetic_bundle(root, allowed_pathway=allowed_pathway)

    with pytest.raises(KnowledgeReferenceError, match="outside claim"):
        _load_test_bundle(root)


def test_shared_claim_still_requires_an_explicit_pathway_binding(tmp_path):
    root = tmp_path / "synthetic"
    _write_synthetic_bundle(root)
    registry = _load_test_bundle(root)

    with pytest.raises(KnowledgeReferenceError):
        registry.for_pathway("ANOTHER-PATHWAY", "1.0.0")


def test_dynamic_counts_allow_one_artifact_to_have_a_binding_and_a_gap(tmp_path):
    root = tmp_path / "synthetic"
    _write_synthetic_bundle(root)
    binding = _read_json(root, "bindings.json")["records"][0]

    governance = _read_json(root, "governance.json")
    governance["coverage_gaps"].append(
        {
            "gap_id": "synthetic-overlapping-gap",
            "gap_version": 1,
            "pathway": binding["pathway"],
            "artifact": binding["artifact"],
            "gap_kind": "design_governance_metadata",
            "reason": "Synthetic overlap proves counts are independently derived.",
            "lifecycle": "open",
            "supersedes": None,
            "recorded_at": "2026-08-13T00:00:00+00:00",
        }
    )
    _write_json(root, "governance.json", governance)
    index = _read_json(root, "bundle_index_v1.json")
    index["current_gap_refs"] = [
        {"gap_id": "synthetic-overlapping-gap", "gap_version": 1}
    ]
    _write_json(root, "bundle_index_v1.json", index)
    _repin(root)

    view = _load_test_bundle(root).for_pathway("SYNTHETIC-PW-B", "2.0.0")
    assert view.unique_artifact_count == 1
    assert view.registered_relationship_count == 1
    assert view.explicit_gap_count == 1
    assert view.unique_artifact_count < (
        view.registered_relationship_count + view.explicit_gap_count
    )


def test_design_decision_rendering_shows_governance_without_source_claims(tmp_path):
    root = tmp_path / "synthetic"
    _write_synthetic_bundle(root)
    claims = _read_json(root, "claims.json")
    claims["records"][0] = {
        "claim_kind": "workflow_design_decision",
        "claim_id": "synthetic-terminology-support",
        "claim_version": 1,
        "statement": "Synthetic display decision.",
        "supports": ["A deterministic synthetic display behavior."],
        "does_not_support": ["No clinical validity or execution authority."],
        "applicable_scope": {
            "mode": "pathway_whitelist",
            "pathways": [
                {"pathway_code": "SYNTHETIC-PW-B", "pathway_version": "2.0.0"}
            ],
            "conditions": {"mode": "any"},
            "products": {"mode": "any"},
            "jurisdictions": {"mode": "any"},
            "care_settings": {"mode": "any"},
            "age": {"mode": "any"},
            "population": "Synthetic design fixture.",
        },
        "owner_role": "synthetic_artifact_owner",
        "decision_rationale": "Exercise design-decision rendering.",
        "decision_status": "proposed",
        "citations": [],
        "evidence_strength": "not_evidence_based",
        "lifecycle": "draft",
        "supersedes": None,
        "retraction_reason": None,
        "registered_at": "2026-08-13T00:00:00+00:00",
    }
    _write_json(root, "claims.json", claims)
    bindings = _read_json(root, "bindings.json")
    bindings["records"][0]["binding_purpose"] = "documents_design_decision"
    bindings["records"][0]["required_independent_approvals"] = [
        "artifact_owner_approval",
        "questionnaire_publication",
    ]
    _write_json(root, "bindings.json", bindings)
    _repin(root)

    rendered = render_pathway_knowledge(
        _load_test_bundle(root).for_pathway("SYNTHETIC-PW-B", "2.0.0")
    )
    assert "owner_role=synthetic_artifact_owner" in rendered
    assert "decision_status=proposed" in rendered
    assert "source_integrity=not_applicable" in rendered


def test_knowledge_package_has_no_runtime_or_patient_store_dependencies():
    package_root = REPOSITORY_ROOT / "continucare" / "knowledge"
    forbidden = (
        "continucare.layer4",
        "continucare.db",
        "continucare.adapters",
        "continucare.services",
        "continucare.care_engine",
    )
    for path in package_root.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any(
            imported == prefix or imported.startswith(prefix + ".")
            for imported in imports
            for prefix in forbidden
        ), path
        assert "patient_id" not in path.read_text("utf-8")


def test_existing_runtime_modules_do_not_depend_on_knowledge_package():
    offenders = []
    for path in (REPOSITORY_ROOT / "continucare").rglob("*.py"):
        if "knowledge" in path.parts:
            continue
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            if any(name.startswith("continucare.knowledge") for name in names):
                offenders.append(path)
    assert offenders == []

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from continucare.knowledge import LoadMode, load_builtin_bundle
from continucare.knowledge.resolvers import CatalogTermResolution
from continucare.ui import project_knowledge_library


ROOT = Path(__file__).parents[1]
KNOWLEDGE_PAGE = ROOT / "pages" / "5_knowledge_evidence.py"
UI_SOURCE = ROOT / "continucare" / "ui.py"


def _registry_with_view(registry, view):
    return SimpleNamespace(
        mode=registry.mode,
        symptom_views=lambda: (view,),
        sources=registry.sources,
        source_content_status=registry.source_content_status,
    )


def test_current_library_has_four_catalog_resolved_topics_in_stable_registry_order():
    projection = project_knowledge_library(load_builtin_bundle())

    assert [item.topic_id for item in projection.topics] == [
        "diarrhea",
        "nausea",
        "vomiting",
        "abdominal-pain",
    ]
    assert [item.name for item in projection.topics] == ["腹泻", "恶心", "呕吐", "腹痛"]
    assert all(item.catalog_resolved for item in projection.topics)
    assert projection.selected_topic_id == "diarrhea"


def test_topic_names_and_codes_come_from_exact_catalog_resolution():
    registry = load_builtin_bundle()
    projection = project_knowledge_library(registry)

    for topic, view in zip(projection.topics, registry.symptom_views()):
        concept = view.catalog_resolution.concept
        assert concept is not None
        assert topic.name == concept.preferred_zh
        assert topic.catalog_system == concept.coding.system
        assert topic.catalog_code == concept.coding.code


def test_support_limit_claim_scope_and_review_are_kept_separate():
    projection = project_knowledge_library(
        load_builtin_bundle(),
        selected_topic_id="nausea",
    )
    topic = projection.selected_topic
    assert topic is not None

    assert len(topic.claims) == 2
    assert topic.supports
    assert topic.does_not_support
    assert all(claim.statement for claim in topic.claims)
    assert all(claim.scope_json.startswith("{") for claim in topic.claims)
    assert all(claim.review_aggregate == "not_assessed" for claim in topic.claims)
    assert not any("资料完整" in item for item in topic.supports)


def test_coverage_gap_present_and_absent_have_truthful_distinct_language():
    registry = load_builtin_bundle()
    view = registry.symptom_views()[0]
    with_gap = project_knowledge_library(
        _registry_with_view(registry, view),
    ).selected_topic
    no_gap = project_knowledge_library(
        _registry_with_view(registry, replace(view, gaps=())),
    ).selected_topic

    assert with_gap is not None and with_gap.gaps
    assert with_gap.coverage_message == "仍有未解决的资料缺口"
    assert no_gap is not None and not no_gap.gaps
    assert no_gap.coverage_message == "当前未登记覆盖缺口"


def test_no_claim_state_does_not_invent_support_or_scope():
    registry = load_builtin_bundle()
    view = replace(registry.symptom_views()[0], claims=(), sources=())
    topic = project_knowledge_library(
        _registry_with_view(registry, view)
    ).selected_topic

    assert topic is not None
    assert topic.claims == ()
    assert topic.supports == ()
    assert topic.does_not_support == ()


def test_unresolved_catalog_keeps_exact_state_without_inventing_a_name_or_code():
    registry = load_builtin_bundle()
    view = replace(
        registry.symptom_views()[0],
        catalog_resolution=CatalogTermResolution(
            resolved=False,
            detail="exact catalog term unavailable",
        ),
    )
    topic = project_knowledge_library(
        _registry_with_view(registry, view)
    ).selected_topic

    assert topic is not None
    assert not topic.catalog_resolved
    assert topic.name is None
    assert topic.catalog_code is None
    assert topic.catalog_detail == "exact catalog term unavailable"


def test_current_and_historical_are_explicit_technical_modes():
    current = project_knowledge_library(load_builtin_bundle(mode=LoadMode.CURRENT))
    historical = project_knowledge_library(
        load_builtin_bundle(mode=LoadMode.HISTORICAL)
    )

    assert current.mode == "CURRENT"
    assert historical.mode == "HISTORICAL"
    assert all(item.mode == "CURRENT" for item in current.topics)
    assert all(item.mode == "HISTORICAL" for item in historical.topics)


def test_official_sources_versions_locators_bindings_and_unbound_sources_are_preserved():
    projection = project_knowledge_library(
        load_builtin_bundle(),
        selected_topic_id="nausea",
    )
    topic = projection.selected_topic
    assert topic is not None

    assert topic.sources
    assert all(source.title for source in topic.sources)
    assert all(source.issuing_authority for source in topic.sources)
    assert all(source.document_version for source in topic.sources)
    assert all(source.url.startswith("http") for source in topic.sources)
    assert any(source.locators for source in topic.sources)
    assert topic.bindings
    assert all(binding.pathway_scope for binding in topic.bindings)
    assert projection.unbound_sources
    assert {item.source_ref.split("@", 1)[0] for item in projection.unbound_sources} >= {
        "hpo-v2026-06-23",
        "nci-pro-ctcae-official-site",
    }


def test_fixed_independence_contract_and_local_selection_do_not_change_library_facts():
    registry = load_builtin_bundle()
    diarrhea = project_knowledge_library(registry, selected_topic_id="diarrhea")
    nausea = project_knowledge_library(registry, selected_topic_id="nausea")

    assert diarrhea.independence_notice == "这里只说明采集依据，没有对这位患者做过评估。"
    assert diarrhea.readonly_notice == "本页只读，不读取患者故事，不创建记录，不参与本轮完成判定。"
    assert diarrhea.topics == nausea.topics
    assert diarrhea.unbound_sources == nausea.unbound_sources
    assert diarrhea.selected_topic_id == "diarrhea"
    assert nausea.selected_topic_id == "nausea"


def test_page_imports_only_offline_knowledge_and_has_no_patient_or_story_runtime_dependencies():
    source = KNOWLEDGE_PAGE.read_text("utf-8")
    tree = ast.parse(source, filename=str(KNOWLEDGE_PAGE))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    forbidden = (
        "continucare.config",
        "continucare.db",
        "continucare.adapters",
        "continucare.services",
        "continucare.layer4",
        "continucare.care_agent",
        "continucare.care_engine",
        "continucare.models",
        "urllib",
        "requests",
        "httpx",
    )

    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in imports
        for prefix in forbidden
    )
    assert "get_settings" not in source
    assert "SQLiteStore" not in source
    assert "competition_demo" not in source
    assert "render_competition_progress" not in source
    assert "render_integration_status" not in source
    assert "patient_id" not in source
    assert "DEMO_PATIENT" not in source
    assert "自动预选" not in source
    assert "continucare.knowledge.ops.read_model" in imports
    assert "catalog_read_model" not in source
    assert "load_builtin_bundle" not in source


def test_page_loads_ops_read_model_without_database_and_sections_are_local(monkeypatch, tmp_path):
    missing_db = tmp_path / "knowledge-must-not-create.db"
    monkeypatch.setenv("CONTINUCARE_DB_PATH", str(missing_db))

    app = AppTest.from_file(str(KNOWLEDGE_PAGE), default_timeout=10).run()

    assert not app.exception
    assert app.radio[0].value == "sources"
    assert app.radio[0].options == ["来源库", "术语治理", "审核流程", "发布状态"]
    visible = "\n".join(item.value for item in app.markdown)
    assert "让每条知识都能回答" in visible
    assert "监管与药品资料" in visible
    assert "医学标准与量表" in visible
    assert "患者教育与研究" in visible

    app.radio[0].set_value("terminology").run()
    assert not app.exception
    assert app.radio[0].value == "terminology"
    assert not missing_db.exists()
    visible = "\n".join(item.value for item in app.markdown)
    assert "具体症状不是一级分类" in visible
    assert "Core Symptom Catalog" in visible
    assert "治理缺口仍待处理" in visible
    assert "GLP1-14D" not in visible
    assert "四个内置主题" not in visible


def test_all_governed_knowledge_sections_render_without_alias_consumer_import():
    ui_source = UI_SOURCE.read_text("utf-8")
    app = AppTest.from_file(str(KNOWLEDGE_PAGE), default_timeout=10).run()
    page_source = KNOWLEDGE_PAGE.read_text("utf-8")

    source_visible = "\n".join(item.value for item in app.markdown)
    assert "SOURCE LIBRARY" in source_visible
    assert "真实网络：未启用" in source_visible

    app.radio[0].set_value("review").run()
    review_visible = "\n".join(item.value for item in app.markdown)
    assert "HUMAN REVIEW GATES" in review_visible
    assert "模型输出和测试事件都不能代替正式审核人" in review_visible
    assert "版本发布" in review_visible

    app.radio[0].set_value("release").run()
    release_visible = "\n".join(item.value for item in app.markdown)
    assert "治理准备中 · 尚未正式发布" in release_visible
    assert "不能把未审核患者表达用于自动匹配" in release_visible
    assert "患者表达匹配：未启用" in release_visible

    assert ".cc-kc-hero" in ui_source
    assert ".cc-kc-group-grid" in ui_source
    assert ".cc-kc-gate-grid" in ui_source
    assert ".cc-patient-quote" in ui_source
    assert "catalog_read_model" not in page_source
    assert "get_core_symptom_record" not in page_source

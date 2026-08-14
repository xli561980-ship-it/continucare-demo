from __future__ import annotations

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_engine import CareEngine
from continucare.db import reset_demo
from continucare.demo_data import DEMO_PATIENT_ID, STRUCTURED_SCENARIOS
from continucare.presentation import (
    L5_REQUIRED_DISCLAIMERS,
    build_l5_governance_for_patient,
    build_latest_l5_submission_view,
)


def _completed_store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "l5-presentation.db")
    reset_demo(store.db_path)
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    engine.complete(session.session_id, STRUCTURED_SCENARIOS["呕吐与摄入记录"])
    return store


def test_l5_governance_view_contains_required_release_scope_sources_and_review(
    tmp_path,
):
    store = _completed_store(tmp_path)

    view = build_l5_governance_for_patient(store, DEMO_PATIENT_ID)

    assert view.pathway_code == "GLP1-14D"
    assert view.pathway_version == "1.0.0"
    assert view.knowledge_release_id == "cn-glp1-l1-v1.0.3"
    assert len(view.products) == 5
    assert any("诺和盈" in item for item in view.products)
    assert "慢性体重管理" in view.indications
    assert any("诺和诺德" in item for item in view.data_sources)
    assert any(
        "数据契约标准，不作为中国临床依据" in item
        for item in view.data_sources
    )
    assert view.knowledge_status == "engineering_validated"
    assert view.pathway_status == "draft"
    assert view.review_status == "未完成临床审核"
    assert view.disclaimers == L5_REQUIRED_DISCLAIMERS == (
        "仅用于合成数据和工程验证",
        "未完成临床审核",
        "不提供诊断和治疗建议",
    )


def test_l5_submission_view_exposes_raw_answers_and_observation_trace(tmp_path):
    store = _completed_store(tmp_path)

    view = build_latest_l5_submission_view(store, DEMO_PATIENT_ID)

    assert view is not None
    raw_by_link = {item["linkId"]: item for item in view.raw_answer_rows}
    assert raw_by_link["nausea-present"]["FHIR value[x]"] == "valueBoolean"
    assert raw_by_link["nausea-present"]["原始答案"] == "false"
    assert raw_by_link["vomiting-count-24h"]["原始答案"] == "1"
    assert len(view.observation_rows) == 4
    assert all(item["FHIR状态"] == "final" for item in view.observation_rows)
    assert all(item["Evidence Claim"] != "—" for item in view.observation_rows)
    assert all(
        item["知识Release"] == "cn-glp1-l1-v1.0.3"
        for item in view.observation_rows
    )

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from continucare.services.competition_demo import (
    CompetitionDemoProgress,
    CompetitionDemoStage,
)
from continucare.ui import (
    PATIENT_CONSEQUENCE,
    PATIENT_DECISION_ACTIONS,
    PATIENT_DECISION_BOUNDARY,
    PATIENT_EMERGENCY_NOTICE,
    patient_recorded_meaning,
    project_patient_followup,
)


ROOT = Path(__file__).parents[1]
PATIENT_PAGE = ROOT / "pages" / "1_patient_followup.py"
UI_SOURCE = ROOT / "continucare" / "ui.py"


def _progress(stage: CompetitionDemoStage, **updates) -> CompetitionDemoProgress:
    return CompetitionDemoProgress(
        stage=stage,
        generation="session:run",
        run_id="run",
        **updates,
    )


def _visible_projection_text(projection) -> str:
    values = (
        projection.notice_title,
        projection.notice_detail,
        projection.original_quote,
        *projection.recorded_meanings,
        projection.question,
        projection.consequence,
        projection.boundary,
        *projection.produced,
        *projection.not_produced,
        *(label for _, label in projection.decision_actions),
    )
    return "\n".join(str(value) for value in values if value)


def test_real_candidate_projects_the_frozen_patient_meaning():
    candidate = {
        "answer": True,
        "evidence_text": "拉肚子",
        "effective_time": {"expression": "今天"},
        "terminology_match": {"preferred_zh": "腹泻"},
        "patient_message": "不应直接展示的术语目录和 SNOMED 技术说明",
    }

    assert patient_recorded_meaning(candidate) == "今天有腹泻"


def test_ready_projection_matches_the_frozen_first_viewport_contract():
    projection = project_patient_followup(
        _progress(CompetitionDemoStage.CANDIDATE_READY),
        original_quote="我今天拉肚子。",
        recorded_meanings=("今天有腹泻",),
    )

    assert projection.original_quote == "我今天拉肚子。"
    assert projection.recorded_meanings == ("今天有腹泻",)
    assert projection.question == "这和你的意思一致吗？"
    assert projection.consequence == PATIENT_CONSEQUENCE
    assert projection.decision_actions == PATIENT_DECISION_ACTIONS
    assert projection.boundary == PATIENT_DECISION_BOUNDARY
    assert projection.show_record_link
    assert not projection.show_nurse_demo_link
    assert PATIENT_EMERGENCY_NOTICE.startswith("这里不是急救通道")


def test_unsure_remains_open_to_acceptance_and_rejection():
    projection = project_patient_followup(
        _progress(CompetitionDemoStage.CANDIDATE_UNSURE),
        original_quote="我今天拉肚子。",
        recorded_meanings=("今天有腹泻",),
    )

    assert projection.notice_title == "这段记录还没有确认。"
    assert "仍可以选择“对，就是这个意思”或“不是这个意思”" in (
        projection.notice_detail or ""
    )
    assert projection.decision_actions == PATIENT_DECISION_ACTIONS
    assert projection.tone == "caution"


def test_rejected_projection_is_read_only_and_lists_exact_non_outputs():
    projection = project_patient_followup(
        _progress(CompetitionDemoStage.CANDIDATE_REJECTED),
        original_quote="我今天拉肚子。",
        recorded_meanings=("今天有腹泻",),
    )

    assert projection.notice_title == "这一轮到这里结束。"
    assert projection.notice_detail == "您选择了“不是这个意思”，系统没有形成患者确认记录。"
    assert projection.decision_actions == ()
    assert projection.produced == ("患者原话", "本次决定记录")
    assert projection.not_produced == (
        "患者确认记录",
        "护士核对任务",
        "医生速览",
        "任何临床评估或消息发送",
    )
    assert projection.show_record_link
    assert not projection.show_home_link


def test_confirmation_receipt_is_read_only_and_explicitly_demo_scoped():
    projection = project_patient_followup(
        _progress(CompetitionDemoStage.TASK_REQUESTED),
        original_quote="我今天拉肚子。",
        recorded_meanings=("今天有腹泻",),
    )

    assert projection.notice_title == "我们已经保存您的确认。"
    assert projection.notice_detail == "您确认的表述：今天有腹泻。下一步是由护士核对这条记录。"
    assert projection.boundary == "这不是诊断或风险判断，本演示不会发送消息。"
    assert projection.decision_actions == ()
    assert projection.show_nurse_demo_link


@pytest.mark.parametrize(
    "stage",
    [
        CompetitionDemoStage.TASK_REJECTED,
        CompetitionDemoStage.TASK_CANCELLED,
        CompetitionDemoStage.TASK_FAILED,
        CompetitionDemoStage.TASK_ENTERED_IN_ERROR,
        CompetitionDemoStage.STORY_COMPLETE,
    ],
)
def test_all_later_terminal_states_remove_patient_business_actions(stage):
    projection = project_patient_followup(
        _progress(stage),
        original_quote="我今天拉肚子。",
        recorded_meanings=("今天有腹泻",),
    )

    assert projection.decision_actions == ()
    assert projection.show_record_link
    assert not projection.show_nurse_demo_link
    assert "不会发送消息" in projection.boundary


def test_empty_and_integrity_error_fail_closed_without_business_actions():
    empty = project_patient_followup(CompetitionDemoProgress())
    broken = project_patient_followup(
        CompetitionDemoProgress(integrity_issue="raw sqlite detail")
    )

    assert empty.notice_title == "目前没有需要您确认的内容"
    assert "已完成" not in _visible_projection_text(empty)
    assert empty.decision_actions == ()
    assert empty.show_home_link
    assert broken.decision_actions == ()
    assert broken.notice_title == "这一轮记录暂时无法读取。"
    assert "raw sqlite detail" not in _visible_projection_text(broken)


def test_patient_visible_projection_never_exposes_internal_workflow_terms():
    prohibited = ("candidate", "ready-to-send", "Observation", "Provenance", "M5-D")
    for stage in CompetitionDemoStage:
        projection = project_patient_followup(
            _progress(stage),
            original_quote="我今天拉肚子。",
            recorded_meanings=("今天有腹泻",),
        )
        visible = _visible_projection_text(projection)
        assert all(term not in visible for term in prohibited)


def test_patient_page_keeps_full_group_guard_and_legacy_path_isolated():
    source = PATIENT_PAGE.read_text("utf-8")

    assert 'st.title("我的随访")' in source
    assert "render_competition_progress" not in source
    assert "start_or_resume(" not in source
    assert "expected_generation=pending[\"generation\"]" in source
    assert "tuple(item.candidate_id for item in semantic_result.candidates)" in source
    assert "review_service.accept_all(pending[\"run_id\"], list(candidate_ids))" in source
    assert "mark_candidates_unsure(\n                        pending[\"run_id\"], list(candidate_ids)" in source
    assert "reject_candidates(\n                        pending[\"run_id\"], list(candidate_ids)" in source
    assert 'st.toggle("其他填写方式"' in source
    assert 'st.button("确认并提交"' in source
    assert "if progress.run_id:" in source
    assert "这里不会开放完整问卷提交，也不会创建第二条语义故事" in source
    assert "show_other_methods = bool(projection.decision_actions or progress.run_id is None)" in source

    button_labels = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "button"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert not any("重新表述" in label for label in button_labels)


@pytest.mark.parametrize(
    "stage",
    [
        CompetitionDemoStage.CANDIDATE_REJECTED,
        CompetitionDemoStage.PATIENT_CONFIRMED,
        CompetitionDemoStage.TASK_REQUESTED,
        CompetitionDemoStage.NURSE_RECEIVED,
        CompetitionDemoStage.NURSE_IN_PROGRESS,
        CompetitionDemoStage.TASK_REJECTED,
        CompetitionDemoStage.TASK_CANCELLED,
        CompetitionDemoStage.TASK_FAILED,
        CompetitionDemoStage.TASK_ENTERED_IN_ERROR,
        CompetitionDemoStage.COMMUNICATION_PENDING,
        CompetitionDemoStage.DOCTOR_BRIEF_PENDING,
        CompetitionDemoStage.COMMUNICATION_READY,
        CompetitionDemoStage.DOCTOR_BRIEF_READY,
        CompetitionDemoStage.STORY_COMPLETE,
    ],
)
def test_post_decision_and_terminal_story_never_allows_other_methods(stage):
    progress = _progress(stage)
    projection = project_patient_followup(
        progress,
        original_quote="我今天拉肚子。",
        recorded_meanings=("今天有腹泻",),
    )

    show_other_methods = bool(projection.decision_actions or progress.run_id is None)

    assert progress.run_id is not None
    assert projection.decision_actions == ()
    assert not show_other_methods


def test_patient_styles_are_scoped_to_the_patient_marker():
    source = UI_SOURCE.read_text("utf-8")

    assert ".cc-patient-shell" in source
    assert '.stApp:has(.cc-patient-shell) [data-testid="stSidebar"]' in source
    assert ".st-key-cc_patient_decisions button" in source
    assert "min-height:3rem" in source
    assert 'font-family:"Songti SC", STSong' in source
    assert "@media (prefers-reduced-motion: reduce)" in source

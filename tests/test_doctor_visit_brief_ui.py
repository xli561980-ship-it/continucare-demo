from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from continucare.layer4.contracts import (
    DoctorReviewDecision,
    EvidenceReference,
    EvidenceRole,
    Layer4SummaryDraft,
    ResourceReference,
    SummaryDraftStatus,
    SummaryEvidenceItem,
)
from continucare.services.competition_demo import (
    CompetitionDemoProgress,
    CompetitionDemoStage,
)
from continucare.ui import (
    DOCTOR_DECISION_ACTIONS,
    DOCTOR_DECISION_BOUNDARY,
    DOCTOR_REJECT_BOUNDARY,
    build_doctor_modified_items,
    project_doctor_visit_brief,
    render_disclosure_controls,
)


ROOT = Path(__file__).parents[1]
DOCTOR_PAGE = ROOT / "pages" / "3_doctor_summary.py"
UI_SOURCE = ROOT / "continucare" / "ui.py"


def _progress(stage: CompetitionDemoStage, **updates) -> CompetitionDemoProgress:
    values = {
        "stage": stage,
        "generation": "session:run",
        "task_id": "task-1",
        "communication_readiness": "pending-approval",
    }
    values.update(updates)
    return CompetitionDemoProgress(**values)


def _task(status: str = "completed", *, note: str | None = None) -> dict:
    value = {
        "resourceType": "Task",
        "id": "task-1",
        "status": status,
        "authoredOn": "2026-08-14T09:00:00+00:00",
    }
    if note is not None:
        value["note"] = [{"text": note}]
    return value


def _evidence(evidence_id: str, reference: str) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=evidence_id,
        resource=ResourceReference(reference=reference, version_id="1"),
        role=EvidenceRole.SOURCE,
    )


def _summary(
    status: SummaryDraftStatus = SummaryDraftStatus.SAFETY_REVIEWED,
    *,
    version: str = "1",
    patient_text: str = "我今天拉肚子。",
    task_text: str = (
        "Task/task-1/_history/4：status=completed；受控处理结果=记录一致；"
        "临床评估=not_assessed。"
    ),
) -> Layer4SummaryDraft:
    return Layer4SummaryDraft(
        summary_id="summary-manual-review-test",
        version=version,
        patient_id="synthetic-patient-001",
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
        summary_kind="manual_review_brief",
        period_start="2026-08-14T09:00:00+00:00",
        period_end="2026-08-14T09:05:00+00:00",
        status=status,
        items=[
            SummaryEvidenceItem(
                item_id="patient-wording",
                section="overview",
                text=patient_text,
                evidence_refs=[
                    _evidence("patient-source", "QuestionnaireResponse/response-1")
                ],
            ),
            SummaryEvidenceItem(
                item_id="nurse-wording",
                section="tasks_and_actions",
                text=task_text,
                evidence_refs=[_evidence("task-source", "Task/task-1")],
            ),
        ],
        source_evidence_digest="a" * 64,
        created_at="2026-08-14T09:06:00+00:00",
    )


def _review(decision: DoctorReviewDecision, *, note: str | None = None):
    return SimpleNamespace(decision=decision, note=note)


def _project(stage=CompetitionDemoStage.DOCTOR_BRIEF_PENDING, **updates):
    values = {
        "progress": _progress(stage),
        "tasks": (_task(),),
        "summary": _summary(),
        "confirmed_statement": "今天有腹泻",
        "original_quote": "我今天拉肚子。",
        "nursing_detail": "受控处理结果：记录一致。沟通文字仍待护士核对。",
    }
    values.update(updates)
    return project_doctor_visit_brief(**values)


def _first_layer_text(projection) -> str:
    values = (
        projection.notice_title,
        projection.notice_detail,
        *(item.label for item in projection.facts),
        *(item.value for item in projection.facts),
        projection.summary_text,
        projection.primary_label,
        projection.decision_boundary,
        projection.reject_boundary,
        projection.recorded_decision,
        *(label for _, label in projection.decision_actions),
        *projection.produced,
        *projection.not_produced,
    )
    return "\n".join(str(value) for value in values if value)


class _DoctorDisclosureDOM(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[tuple[str, dict[str, str | None]]] = []
        self.controls: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append((tag, attributes))
        if tag == "a" and "aria-controls" in attributes:
            self.controls.append(attributes)


class _DoctorDisclosureRenderer:
    def __init__(self, query_params=None):
        self.query_params = query_params or {}
        self.fragments: list[str] = []

    def markdown(self, value, **_kwargs):
        self.fragments.append(str(value))

    def dom(self):
        parser = _DoctorDisclosureDOM()
        parser.feed("\n".join(self.fragments))
        return parser


def test_demo_not_started_has_no_write_action():
    projection = project_doctor_visit_brief(CompetitionDemoProgress())

    assert projection.notice_title == "还没有可生成速览的已完成记录核对。"
    assert projection.primary_action is None
    assert not projection.show_decisions
    assert projection.facts[2].value == "尚未提供临床评估"


def test_no_task_and_unfinished_task_explain_the_missing_upstream_step():
    no_task = project_doctor_visit_brief(
        _progress(CompetitionDemoStage.CANDIDATE_READY, task_id=None),
    )
    unfinished = project_doctor_visit_brief(
        _progress(CompetitionDemoStage.TASK_REQUESTED),
        tasks=(_task("requested"),),
        confirmed_statement="今天有腹泻",
        original_quote="我今天拉肚子。",
    )

    assert no_task.state == "waiting_for_task"
    assert no_task.primary_action is None
    assert no_task.facts[0].value == "尚未形成患者确认记录"
    assert no_task.facts[0].source_key is None
    assert unfinished.notice_title == "还没有可生成速览的已完成记录核对。"
    assert unfinished.facts[1].value == "等待护士接手记录核对"
    assert unfinished.primary_action is None
    assert unfinished.show_nurse_link


def test_completed_task_without_summary_has_one_explicit_generation_action():
    projection = _project(
        stage=CompetitionDemoStage.COMMUNICATION_PENDING,
        summary=None,
    )

    assert projection.state == "ready_to_generate"
    assert projection.notice_title == "还没有可生成的复诊速览"
    assert projection.primary_action == "generate"
    assert projection.primary_label == "按当前记录生成速览"
    assert not projection.show_decisions


def test_pending_safety_reviewed_summary_has_fixed_facts_and_unselected_choices():
    projection = _project()

    assert [item.label for item in projection.facts] == [
        "患者确认的表述",
        "护理动作",
        "当前边界",
    ]
    assert [item.value for item in projection.facts] == [
        "今天有腹泻",
        "护士已完成记录核对",
        "尚未提供临床评估",
    ]
    assert projection.summary_text == (
        "患者表示今天有腹泻；护士已完成记录核对。尚未提供临床评估。"
    )
    assert projection.show_decisions
    assert projection.decision_actions == DOCTOR_DECISION_ACTIONS
    assert projection.recorded_decision is None
    assert projection.decision_boundary == DOCTOR_DECISION_BOUNDARY
    assert projection.reject_boundary == DOCTOR_REJECT_BOUNDARY
    assert projection.show_nurse_link


@pytest.mark.parametrize(
    ("status", "decision", "state", "title"),
    [
        (
            SummaryDraftStatus.DOCTOR_REVIEWED,
            DoctorReviewDecision.ACCEPT,
            "reviewed",
            "已记录这版速览的措辞决定",
        ),
        (
            SummaryDraftStatus.REJECTED,
            DoctorReviewDecision.REJECT,
            "rejected",
            "未采用这版速览",
        ),
    ],
)
def test_reviewed_and_rejected_summaries_are_read_only(
    status, decision, state, title
):
    projection = _project(
        summary=_summary(status, version="2"),
        review=_review(decision, note="持久化说明。"),
        review_source_summary=_summary(version="1"),
    )

    assert projection.state == state
    assert projection.notice_title == title
    assert not projection.show_decisions
    assert projection.primary_action is None
    assert projection.recorded_decision == dict(DOCTOR_DECISION_ACTIONS)[decision.value]
    assert projection.decision_note == "持久化说明。"


def test_communication_ready_makes_the_old_summary_stale_and_blocks_review():
    projection = _project(
        stage=CompetitionDemoStage.COMMUNICATION_READY,
        progress=_progress(
            CompetitionDemoStage.COMMUNICATION_READY,
            communication_readiness="ready-to-send",
        ),
        stale=True,
    )

    assert projection.state == "stale"
    assert projection.notice_title == "这版速览基于较早记录。"
    assert projection.primary_action == "refresh"
    assert projection.primary_label == "按当前记录生成新版本"
    assert not projection.show_decisions


def test_story_complete_is_fully_read_only_and_not_a_clinical_conclusion():
    projection = _project(
        stage=CompetitionDemoStage.STORY_COMPLETE,
        progress=_progress(
            CompetitionDemoStage.STORY_COMPLETE,
            communication_readiness="ready-to-send",
            is_terminal=True,
        ),
    )

    assert projection.state == "story_complete"
    assert projection.primary_action is None
    assert not projection.show_decisions
    assert "9/9" in (projection.notice_detail or "")
    assert "不代表临床结论" in (projection.notice_detail or "")
    assert "真实消息发送" in projection.not_produced


@pytest.mark.parametrize(
    ("stage", "status", "tone"),
    [
        (CompetitionDemoStage.TASK_REJECTED, "rejected", "stopped"),
        (CompetitionDemoStage.TASK_CANCELLED, "cancelled", "stopped"),
        (CompetitionDemoStage.TASK_FAILED, "failed", "error"),
        (
            CompetitionDemoStage.TASK_ENTERED_IN_ERROR,
            "entered-in-error",
            "error",
        ),
    ],
)
def test_task_terminals_show_persisted_reason_and_never_write(stage, status, tone):
    projection = project_doctor_visit_brief(
        _progress(stage),
        tasks=(_task(status, note="持久化停止原因。"),),
        confirmed_statement="今天有腹泻",
        original_quote="我今天拉肚子。",
    )

    assert projection.state == stage.value
    assert projection.tone == tone
    assert "原因：持久化停止原因。" in (projection.notice_detail or "")
    assert projection.primary_action is None
    assert not projection.show_decisions


def test_missing_terminal_reason_is_explicitly_not_recorded():
    projection = project_doctor_visit_brief(
        _progress(CompetitionDemoStage.TASK_ENTERED_IN_ERROR),
        tasks=(_task("entered-in-error"),),
        confirmed_statement="今天有腹泻",
        original_quote="我今天拉肚子。",
    )

    assert "原因：未记录" in (projection.notice_detail or "")


def test_integrity_unknown_and_missing_source_states_fail_closed():
    integrity = _project(
        progress=_progress(
            CompetitionDemoStage.DOCTOR_BRIEF_PENDING,
            integrity_issue="raw sqlite detail",
        )
    )
    unknown = project_doctor_visit_brief(
        SimpleNamespace(
            stage="future-stage",
            generation="session:run",
            task_id=None,
            integrity_issue=None,
        )
    )
    missing = _project(source_error="exact QuestionnaireResponse source missing")

    for projection in (integrity, unknown, missing):
        assert projection.state == "error"
        assert projection.primary_action is None
        assert not projection.show_decisions
        assert "没有继续" in (projection.notice_detail or "")
    assert "raw sqlite detail" not in (integrity.notice_detail or "")
    assert "QuestionnaireResponse" not in (missing.notice_detail or "")


def test_degraded_unresolved_and_truncated_trace_is_disclosed_without_rewriting_body():
    projection = _project(
        trace_degraded=True,
        unresolved_references=("Task/task-1/_history/4",),
        trace_truncated=True,
    )

    assert projection.summary_text
    assert projection.source_notice == (
        "部分来源暂时无法读取；存在尚未解析的来源；技术来源达到展开上限。"
    )
    assert "Task/task-1" not in projection.source_notice


def test_modify_changes_one_existing_item_and_preserves_all_evidence_references():
    source = _summary()
    before = [
        [evidence.model_dump(mode="json") for evidence in item.evidence_refs]
        for item in source.items
    ]
    modified = build_doctor_modified_items(
        source,
        item_id="patient-wording",
        replacement="患者今天表示有腹泻",
        allowed_item_ids=("patient-wording", "nurse-wording"),
    )

    assert len(modified) == len(source.items)
    assert modified[0].text == "患者今天表示有腹泻"
    assert modified[0].section == source.items[0].section
    assert modified[1] == source.items[1]
    assert [
        [evidence.model_dump(mode="json") for evidence in item.evidence_refs]
        for item in modified
    ] == before


def test_modified_wording_is_projected_from_the_immutable_result_version():
    source = _summary()
    modified_items = build_doctor_modified_items(
        source,
        item_id="patient-wording",
        replacement="患者今天表示有腹泻",
        allowed_item_ids=("patient-wording", "nurse-wording"),
    )
    result = source.model_copy(
        update={
            "version": "2",
            "status": SummaryDraftStatus.DOCTOR_REVIEWED,
            "items": list(modified_items),
        }
    )
    projection = _project(
        summary=result,
        review=_review(DoctorReviewDecision.MODIFY, note="调整主语位置。"),
        review_source_summary=source,
    )

    assert projection.summary_text == (
        "患者今天表示有腹泻；护士已完成记录核对。尚未提供临床评估。"
    )


def test_knowledge_status_never_participates_in_the_doctor_projection():
    base = _progress(CompetitionDemoStage.DOCTOR_BRIEF_PENDING)
    with_knowledge = base.model_copy(
        update={
            "knowledge_available": True,
            "knowledge_error": "independent knowledge state",
        }
    )
    kwargs = {
        "tasks": (_task(),),
        "summary": _summary(),
        "confirmed_statement": "今天有腹泻",
        "original_quote": "我今天拉肚子。",
        "nursing_detail": "护士已完成记录核对。",
    }

    assert project_doctor_visit_brief(base, **kwargs) == project_doctor_visit_brief(
        with_knowledge, **kwargs
    )


def test_first_layer_never_exposes_internal_delivery_or_clinical_approval_terms():
    prohibited = (
        "ready-to-send",
        "pending-approval",
        "M5-C",
        "FHIR",
        "Alert",
        "ClinicalRule",
        "批准",
        "签署",
        "定稿",
        "写回病历",
    )
    projections = [
        _project(),
        _project(stale=True),
        _project(
            summary=_summary(SummaryDraftStatus.DOCTOR_REVIEWED, version="2"),
            review=_review(DoctorReviewDecision.ACCEPT),
            review_source_summary=_summary(),
        ),
    ]
    for projection in projections:
        visible = _first_layer_text(projection)
        assert all(term not in visible for term in prohibited)


def test_page_uses_pure_projection_guards_and_scoped_doctor_styles():
    page_source = DOCTOR_PAGE.read_text("utf-8")
    ui_source = UI_SOURCE.read_text("utf-8")

    assert 'st.title("复诊准备")' in page_source
    assert "30 秒速览" in page_source
    assert "证据链" in page_source
    assert "project_doctor_visit_brief" in page_source
    assert "DoctorWorkbenchService" in page_source
    assert "ManualReviewBriefService" in page_source
    assert "DoctorReviewService" in page_source
    assert "demo_write_guard" in page_source
    assert "expected_generation=progress.generation" in page_source
    assert "render_disclosure_controls" in page_source
    assert "sqlite3.Error" in page_source
    assert "index=None" in page_source
    assert "render_competition_progress" not in page_source
    assert ".metric(" not in page_source
    assert "SEND_ENABLED" not in page_source
    assert "Alert" not in page_source
    assert ".cc-doctor-shell" in ui_source
    assert '.stApp:has(.cc-doctor-shell) [data-testid="stSidebar"]' in ui_source
    assert ".cc-doctor-facts" in ui_source
    assert "min-height:44px" in ui_source
    assert "@media (prefers-reduced-motion: reduce)" in ui_source
    assert 'aria-expanded="{str(active).lower()}"' in ui_source
    assert 'panel_id="cc-doctor-source-panel"' in page_source


def test_doctor_disclosure_renders_a_safe_collapsed_target_and_one_expanded_panel():
    options = (("patient", "患者原话"), ("nursing", "护理动作"))
    collapsed = _DoctorDisclosureRenderer()

    assert render_disclosure_controls(
        collapsed,
        query_parameter="cc_doctor_source",
        page_path="/doctor_summary",
        options=options,
        aria_label="复诊速览来源",
        panel_id="cc-doctor-source-panel",
        stacked=True,
    ) is None
    collapsed_dom = collapsed.dom()
    assert [item["aria-expanded"] for item in collapsed_dom.controls] == [
        "false",
        "false",
    ]
    assert collapsed_dom.ids == [
        (
            "span",
            {
                "id": "cc-doctor-source-panel",
                "class": "cc-disclosure-anchor",
                "hidden": None,
                "aria-hidden": "true",
            },
        )
    ]

    unknown = _DoctorDisclosureRenderer({"cc_doctor_source": "future-value"})
    assert render_disclosure_controls(
        unknown,
        query_parameter="cc_doctor_source",
        page_path="/doctor_summary",
        options=options,
        aria_label="复诊速览来源",
        panel_id="cc-doctor-source-panel",
        stacked=True,
    ) is None
    assert all(
        item["aria-expanded"] == "false" for item in unknown.dom().controls
    )

    expanded = _DoctorDisclosureRenderer({"cc_doctor_source": "nursing"})
    assert render_disclosure_controls(
        expanded,
        query_parameter="cc_doctor_source",
        page_path="/doctor_summary",
        options=options,
        aria_label="复诊速览来源",
        panel_id="cc-doctor-source-panel",
        stacked=True,
    ) == "nursing"
    expanded.markdown(
        '<section id="cc-doctor-source-panel"><h2>护理动作</h2></section>',
        unsafe_allow_html=True,
    )
    expanded_dom = expanded.dom()
    assert [item["aria-expanded"] for item in expanded_dom.controls] == [
        "false",
        "true",
    ]
    assert len(expanded_dom.ids) == 1
    assert expanded_dom.ids[0][0] == "section"
    assert "hidden" not in expanded_dom.ids[0][1]

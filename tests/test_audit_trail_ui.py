from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from continucare.models import AuditEvent
from continucare.services.competition_demo import (
    CompetitionDemoProgress,
    CompetitionDemoStage,
)
from continucare.ui import project_audit_trail


ROOT = Path(__file__).parents[1]
AUDIT_PAGE = ROOT / "pages" / "4_audit_log.py"
UI_SOURCE = ROOT / "continucare" / "ui.py"


class _AuditDOM(HTMLParser):
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


def _rendered_dom(app) -> _AuditDOM:
    parser = _AuditDOM()
    parser.feed("\n".join(str(item.value) for item in app.markdown))
    return parser


TASK_STATUSES = {
    CompetitionDemoStage.TASK_REQUESTED: "requested",
    CompetitionDemoStage.NURSE_RECEIVED: "received",
    CompetitionDemoStage.NURSE_IN_PROGRESS: "in-progress",
    CompetitionDemoStage.TASK_REJECTED: "rejected",
    CompetitionDemoStage.TASK_CANCELLED: "cancelled",
    CompetitionDemoStage.TASK_FAILED: "failed",
    CompetitionDemoStage.TASK_ENTERED_IN_ERROR: "entered-in-error",
    CompetitionDemoStage.COMMUNICATION_PENDING: "completed",
    CompetitionDemoStage.DOCTOR_BRIEF_PENDING: "completed",
    CompetitionDemoStage.COMMUNICATION_READY: "completed",
    CompetitionDemoStage.DOCTOR_BRIEF_READY: "completed",
    CompetitionDemoStage.STORY_COMPLETE: "completed",
}


def _progress(stage: CompetitionDemoStage, **updates) -> CompetitionDemoProgress:
    has_story = stage != CompetitionDemoStage.NOT_STARTED
    has_confirmation = stage not in {
        CompetitionDemoStage.NOT_STARTED,
        CompetitionDemoStage.CANDIDATE_READY,
        CompetitionDemoStage.CANDIDATE_UNSURE,
        CompetitionDemoStage.CANDIDATE_REJECTED,
    }
    has_task = stage in TASK_STATUSES
    has_communication = stage in {
        CompetitionDemoStage.COMMUNICATION_PENDING,
        CompetitionDemoStage.DOCTOR_BRIEF_PENDING,
        CompetitionDemoStage.COMMUNICATION_READY,
        CompetitionDemoStage.DOCTOR_BRIEF_READY,
        CompetitionDemoStage.STORY_COMPLETE,
    }
    has_brief = stage in {
        CompetitionDemoStage.DOCTOR_BRIEF_PENDING,
        CompetitionDemoStage.COMMUNICATION_READY,
        CompetitionDemoStage.DOCTOR_BRIEF_READY,
        CompetitionDemoStage.STORY_COMPLETE,
    }
    decisions = {}
    if stage == CompetitionDemoStage.CANDIDATE_UNSURE:
        decisions = {"candidate-1": "unsure"}
    elif stage == CompetitionDemoStage.CANDIDATE_REJECTED:
        decisions = {"candidate-1": "rejected"}
    elif has_confirmation:
        decisions = {"candidate-1": "accepted"}
    values = {
        "stage": stage,
        "generation": "session:run" if has_story else None,
        "candidate_count": 1 if has_story else 0,
        "candidate_decisions": decisions,
        "questionnaire_response_count": 1 if has_confirmation else 0,
        "observation_count": 1 if has_confirmation else 0,
        "task_id": "task-1" if has_task else None,
        "task_status": TASK_STATUSES.get(stage),
        "manual_task_count": 1 if has_task else 0,
        "communication_count": 1 if has_communication else 0,
        "communication_readiness": (
            "ready-to-send"
            if stage
            in {
                CompetitionDemoStage.COMMUNICATION_READY,
                CompetitionDemoStage.DOCTOR_BRIEF_READY,
                CompetitionDemoStage.STORY_COMPLETE,
            }
            else "pending-approval"
            if has_communication
            else None
        ),
        "manual_brief_count": 1 if has_brief else 0,
        "audit_count": 1 if has_story else 0,
    }
    values.update(updates)
    return CompetitionDemoProgress(**values)


def _task(status: str, *, note: str | None = None, status_reason: str | None = None):
    task = {"resourceType": "Task", "id": "task-1", "status": status}
    if note:
        task["note"] = [{"text": note}]
    if status_reason:
        task["statusReason"] = {
            "coding": [{"code": status_reason, "display": status_reason}]
        }
    return task


def _event(
    event_type: str,
    *,
    event_id: str,
    created_at: str = "2026-08-14T09:00:00+00:00",
    actor_type: str = "deterministic_workflow",
    entity_type: str = "Task",
    entity_id: str = "task-1",
    details: dict | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        patient_id="synthetic-patient-001",
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor_type=actor_type,
        details_json=details or {},
        created_at=created_at,
    )


def _project(stage: CompetitionDemoStage, **updates):
    progress = _progress(stage)
    tasks = ()
    if stage in TASK_STATUSES:
        tasks = (_task(TASK_STATUSES[stage]),)
    return project_audit_trail(progress, tasks=tasks, **updates)


def _provenance(provenance_id: str, *targets: str) -> dict:
    return {
        "resourceType": "Provenance",
        "id": provenance_id,
        "target": [{"reference": target} for target in targets],
    }


def _only_action(event: AuditEvent, *provenances: dict):
    projection = project_audit_trail(
        _progress(CompetitionDemoStage.TASK_REQUESTED),
        events=(event,),
        tasks=(_task("requested"),),
        provenances=provenances,
    )
    assert len(projection.actions) == 1
    return projection.actions[0]


@pytest.mark.parametrize(
    ("stage", "title"),
    [
        (CompetitionDemoStage.NOT_STARTED, "这一轮还没有留下流程记录"),
        (CompetitionDemoStage.CANDIDATE_READY, "目前等待患者确认"),
        (CompetitionDemoStage.CANDIDATE_UNSURE, "目前等待患者明确决定"),
        (CompetitionDemoStage.CANDIDATE_REJECTED, "本轮已结束：患者没有确认这段记录"),
        (CompetitionDemoStage.PATIENT_CONFIRMED, "患者已确认，等待记录核对"),
        (CompetitionDemoStage.TASK_REQUESTED, "目前等待护士接手"),
        (CompetitionDemoStage.NURSE_RECEIVED, "护士已接手，等待开始核对"),
        (CompetitionDemoStage.NURSE_IN_PROGRESS, "护士正在核对"),
        (CompetitionDemoStage.COMMUNICATION_PENDING, "沟通文字仍待人工核对"),
        (CompetitionDemoStage.DOCTOR_BRIEF_PENDING, "沟通文字仍待人工核对"),
        (CompetitionDemoStage.COMMUNICATION_READY, "复诊速览需要按当前来源刷新"),
        (CompetitionDemoStage.STORY_COMPLETE, "演示记录链已走完"),
    ],
)
def test_all_main_story_states_have_a_truthful_human_conclusion(stage, title):
    projection = _project(stage)

    assert projection.title == title
    assert projection.reason
    assert projection.explanation
    assert projection.show_guide_link


@pytest.mark.parametrize(
    ("stage", "tone"),
    [
        (CompetitionDemoStage.CANDIDATE_REJECTED, "stopped"),
        (CompetitionDemoStage.TASK_REJECTED, "stopped"),
        (CompetitionDemoStage.TASK_CANCELLED, "stopped"),
        (CompetitionDemoStage.TASK_FAILED, "error"),
        (CompetitionDemoStage.TASK_ENTERED_IN_ERROR, "error"),
        (CompetitionDemoStage.STORY_COMPLETE, "complete"),
    ],
)
def test_three_terminal_families_have_distinct_tones_and_no_action(stage, tone):
    projection = _project(stage)

    assert projection.tone == tone
    assert "临床评估" in projection.not_produced
    assert "真实消息发送" in projection.not_produced


def test_story_complete_lists_only_persisted_products_and_explains_9_of_9():
    projection = _project(CompetitionDemoStage.STORY_COMPLETE)

    assert projection.reason == "合成演示 9/9 完成"
    assert "患者确认记录" in projection.produced
    assert "例行护士核对 Task 与处理历史" in projection.produced
    assert "未发送的沟通文字及人工核对记录" in projection.produced
    assert "按当前来源生成的复诊速览" in projection.produced
    assert "9/9 只代表合成本地持久化接力完成" in projection.explanation


def test_terminal_reason_uses_persisted_task_note_then_status_reason_and_never_template():
    progress = _progress(CompetitionDemoStage.TASK_ENTERED_IN_ERROR)
    from_note = project_audit_trail(
        progress,
        tasks=(
            _task(
                "entered-in-error",
                note="持久化说明",
                status_reason="持久化状态原因",
            ),
        ),
    )
    from_reason = project_audit_trail(
        progress,
        tasks=(_task("entered-in-error", status_reason="持久化状态原因"),),
    )
    missing = project_audit_trail(
        progress,
        tasks=(_task("entered-in-error"),),
    )

    assert from_note.reason == "持久化说明"
    assert from_reason.reason == "持久化状态原因"
    assert missing.reason == "未记录"


def test_actions_include_only_real_events_and_sort_forward_with_stable_ties():
    events = (
        _event("manual_review_task_created", event_id="event-task"),
        _event("demo_reset", event_id="event-reset", actor_type="demo_operator"),
        _event(
            "semantic_candidate_patient_decision",
            event_id="event-decision",
            actor_type="synthetic_patient",
            details={"decision": "accepted_for_manual_review"},
        ),
        _event(
            "patient_message_submitted",
            event_id="event-message",
            actor_type="synthetic_patient",
            created_at="2026-08-14T08:59:00+00:00",
        ),
    )
    projection = project_audit_trail(
        _progress(CompetitionDemoStage.TASK_REQUESTED),
        events=events,
        tasks=(_task("requested"),),
    )

    assert [item.event_id for item in projection.actions] == [
        "event-message",
        "event-reset",
        "event-decision",
        "event-task",
    ]
    assert [item.sequence for item in projection.actions] == [1, 2, 3, 4]
    assert [item.action for item in projection.actions] == [
        "提交原话",
        "准备新的合成演示记录",
        "选择确认",
        "创建例行记录核对",
    ]


def test_action_time_order_uses_real_instants_across_offsets():
    later = _event(
        "manual_review_task_created",
        event_id="later",
        created_at="2026-08-14T10:00:00+02:00",
    )
    earlier = _event(
        "patient_message_submitted",
        event_id="earlier",
        created_at="2026-08-14T07:30:00+00:00",
        actor_type="synthetic_patient",
    )

    projection = project_audit_trail(
        _progress(CompetitionDemoStage.TASK_REQUESTED),
        events=(later, earlier),
        tasks=(_task("requested"),),
    )

    assert [item.event_id for item in projection.actions] == ["earlier", "later"]


def test_task_resource_reference_does_not_match_longer_task_id_history():
    action = _only_action(
        _event("manual_review_task_created", event_id="task-created"),
        _provenance("task-10-v1", "Task/task-10/_history/1"),
    )

    assert action.provenance_refs == ()


@pytest.mark.parametrize("other_id", ("task-10", "task-100", "task-1-copy"))
def test_task_resource_reference_does_not_match_any_shared_prefix_id(other_id):
    action = _only_action(
        _event("manual_review_task_created", event_id="task-created"),
        _provenance("other-task", f"Task/{other_id}"),
        _provenance("other-task-history", f"Task/{other_id}/_history/2"),
    )

    assert action.provenance_refs == ()


def test_task_creation_links_only_patient_confirmation_resource_provenance():
    action = _only_action(
        _event("manual_review_task_created", event_id="task-created"),
        _provenance("confirmed", "Task/task-1"),
        _provenance("acknowledged", "Task/task-1/_history/2"),
        _provenance("future", "Task/task-1/_history/5"),
    )

    assert action.provenance_refs == ("Provenance/confirmed",)


@pytest.mark.parametrize(
    ("event_type", "version", "provenance_id"),
    (
        ("manual_review_task_acknowledged", "2", "acknowledged"),
        ("manual_review_task_started", "4", "started"),
        ("manual_review_outcome_recorded", "5", "outcome"),
        ("manual_review_communication_approved", "5", "approved"),
    ),
)
def test_task_lifecycle_event_with_direct_provenance_links_only_its_action(
    event_type, version, provenance_id
):
    action = _only_action(
        _event(
            event_type,
            event_id=f"event-{provenance_id}",
            details={
                "provenance_id": provenance_id,
                "task_ref": f"Task/task-1/_history/{version}",
                "task_version": version,
            },
        ),
        _provenance("confirmed", "Task/task-1"),
        _provenance("acknowledged", "Task/task-1/_history/2"),
        _provenance("started", "Task/task-1/_history/3", "Task/task-1/_history/4"),
        _provenance("outcome", "Task/task-1/_history/5", "Communication/message-1/_history/1"),
        _provenance("approved", "Communication/message-1/_history/2"),
    )

    assert action.provenance_refs == (f"Provenance/{provenance_id}",)


def test_task_v2_event_does_not_link_other_or_future_versions():
    action = _only_action(
        _event(
            "manual_review_task_acknowledged",
            event_id="task-v2",
            details={
                "provenance_id": "task-v2",
                "task_ref": "Task/task-1/_history/2",
                "task_version": "2",
            },
        ),
        _provenance("task-v1", "Task/task-1/_history/1"),
        _provenance("task-v2", "Task/task-1/_history/2"),
        _provenance("task-v3", "Task/task-1/_history/3"),
        _provenance("task-v4", "Task/task-1/_history/4"),
    )

    assert action.provenance_refs == ("Provenance/task-v2",)


@pytest.mark.parametrize("version", ("1", "2"))
def test_summary_generation_links_only_its_exact_summary_version(version):
    action = _only_action(
        _event(
            "manual_review_brief_generated",
            event_id=f"summary-v{version}",
            entity_type="Layer4SummaryDraft",
            entity_id="summary-1",
            details={
                "summary_ref": f"urn:continucare:summary:summary-1:version:{version}",
                "summary_version": version,
                "task_ref": "Task/task-1/_history/5",
                "communication_ref": f"Communication/message-1/_history/{version}",
            },
        ),
        _provenance("task-v5", "Task/task-1/_history/5"),
        _provenance("communication-v1", "Communication/message-1/_history/1"),
        _provenance("communication-v2", "Communication/message-1/_history/2"),
        _provenance("summary-v1", "urn:continucare:summary:summary-1:version:1"),
        _provenance("summary-v2", "urn:continucare:summary:summary-1:version:2"),
    )

    assert action.provenance_refs == (f"Provenance/summary-v{version}",)


def test_doctor_review_uses_result_summary_version_for_exact_provenance():
    action = _only_action(
        _event(
            "doctor_reviewed_summary",
            event_id="doctor-review",
            entity_type="Layer4SummaryDraft",
            entity_id="summary-1",
            actor_type="doctor",
            details={
                "source_summary_version": "1",
                "result_summary_version": "2",
            },
        ),
        _provenance("summary-v1", "urn:continucare:summary:summary-1:version:1"),
        _provenance("doctor-review", "urn:continucare:summary:summary-1:version:2"),
        _provenance("future-summary", "urn:continucare:summary:summary-1:version:3"),
    )

    assert action.provenance_refs == ("Provenance/doctor-review",)


def test_missing_exact_task_version_does_not_fail_open_to_all_history():
    action = _only_action(
        _event("future_task_event", event_id="missing-exact-ref"),
        _provenance("task-v1", "Task/task-1/_history/1"),
        _provenance("task-v2", "Task/task-1/_history/2"),
        _provenance("task-v3", "Task/task-1/_history/3"),
    )

    assert action.provenance_refs == ()


def test_direct_provenance_is_stable_and_never_duplicated():
    action = _only_action(
        _event(
            "manual_review_task_acknowledged",
            event_id="direct-deduplicated",
            details={
                "provenance_id": "task-v2",
                "task_ref": "Task/task-1/_history/2",
                "task_version": "2",
            },
        ),
        _provenance("task-v2", "Task/task-1/_history/2"),
        _provenance("task-v2", "Task/task-1/_history/2"),
    )

    assert action.provenance_refs == ("Provenance/task-v2",)


def test_unknown_event_and_mock_event_are_safe_in_first_layer():
    unknown = _event(
        "future_internal_event",
        event_id="unknown",
        actor_type="future_actor",
    )
    mock = _event(
        "notification_mock_sent",
        event_id="mock",
        actor_type="mock_notifier",
        created_at="2026-08-14T09:01:00+00:00",
    )
    projection = project_audit_trail(
        _progress(CompetitionDemoStage.CANDIDATE_READY),
        events=(unknown, mock),
    )
    visible = "\n".join(
        value
        for item in projection.actions
        for value in (
            item.participant,
            item.action,
            item.effect,
            item.before_state,
            item.after_state,
        )
        if value
    )

    assert projection.actions[0].action == "记录了一项流程动作"
    assert "future_internal_event" not in visible
    assert projection.actions[1].action == "模拟（未真实发送 / 未写入）"
    assert projection.actions[0].event_type == "future_internal_event"


def test_integrity_unknown_and_stage_task_mismatch_fail_closed_without_raw_detail():
    integrity = project_audit_trail(
        _progress(
            CompetitionDemoStage.TASK_REQUESTED,
            integrity_issue="raw sqlite detail",
        ),
        tasks=(_task("requested"),),
    )
    unknown = project_audit_trail(
        SimpleNamespace(stage="future-stage", integrity_issue=None),
    )
    mismatch = project_audit_trail(
        _progress(CompetitionDemoStage.NURSE_RECEIVED),
        tasks=(_task("requested"),),
    )
    wrong_task_id = project_audit_trail(
        _progress(CompetitionDemoStage.TASK_REQUESTED),
        tasks=({**_task("requested"), "id": "task-other"},),
    )

    for projection in (integrity, unknown, mismatch, wrong_task_id):
        assert projection.tone == "error"
        assert "后续业务动作已经停止" in projection.explanation
    assert "raw sqlite detail" not in integrity.reason


def test_audit_page_is_read_only_and_uses_scoped_progressive_disclosure(monkeypatch, tmp_path):
    db_path = tmp_path / "does-not-exist.db"
    monkeypatch.setenv("CONTINUCARE_DB_PATH", str(db_path))
    page_source = AUDIT_PAGE.read_text("utf-8")
    ui_source = UI_SOURCE.read_text("utf-8")

    app = AppTest.from_file(str(AUDIT_PAGE), default_timeout=10).run()

    assert not app.exception
    assert app.title[0].value == "记录追溯"
    assert not db_path.exists()
    assert not app.button
    assert all(
        label in page_source
        for label in ("为什么停在这里", "查看资源关系", "查看技术详情")
    )
    assert "project_audit_trail" in page_source
    assert "render_competition_progress" not in page_source
    assert "append_audit_event" not in page_source
    assert "start_competition_demo" not in page_source
    assert "demo_write_guard" not in page_source
    assert "Layer4SQLiteStore(settings.db_path, initialize=False)" in page_source
    assert "render_disclosure_controls" in page_source
    assert "重置" not in page_source
    assert ".cc-audit-shell" in ui_source
    assert ".cc-audit-table" in ui_source
    assert "min-height:44px" in ui_source
    assert 'aria-expanded="{str(active).lower()}"' in ui_source

    collapsed = _rendered_dom(app)
    assert len(collapsed.controls) == 3
    assert all(item["aria-expanded"] == "false" for item in collapsed.controls)
    targets = [
        item for item in collapsed.ids if item[1]["id"] == "cc-audit-disclosure-panel"
    ]
    assert len(targets) == 1
    assert targets[0][0] == "span"
    assert targets[0][1]["hidden"] is None
    assert targets[0][1]["aria-hidden"] == "true"
    assert "tabindex" not in targets[0][1]

    app.query_params["cc_audit_disclosure"] = "why"
    app.run()
    expanded = _rendered_dom(app)
    assert [item["aria-expanded"] for item in expanded.controls] == [
        "true",
        "false",
        "false",
    ]
    targets = [
        item for item in expanded.ids if item[1]["id"] == "cc-audit-disclosure-panel"
    ]
    assert len(targets) == 1
    assert targets[0][0] == "section"
    assert "hidden" not in targets[0][1]

    app.query_params["cc_audit_disclosure"] = "future-value"
    app.run()
    unknown = _rendered_dom(app)
    assert all(item["aria-expanded"] == "false" for item in unknown.controls)
    assert sum(
        item[1]["id"] == "cc-audit-disclosure-panel" for item in unknown.ids
    ) == 1

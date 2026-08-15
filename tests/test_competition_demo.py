from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_agent import CareAgentService
from continucare.care_agent.model_api import SemanticModelConfig, UnconfiguredModelAdapter
from continucare.care_engine import CareEngine
from continucare.db import connect, reset_demo
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.layer4 import (
    Layer4SQLiteStore,
    ManualReviewBriefService,
    TaskWorkflowService,
)
from continucare.layer4.manual_reviews import SEND_ENABLED
from continucare.navigation import APP_ROUTES, ROLE_ROUTES
from continucare.pathways import load_builtin_pathways
from continucare.services import competition_demo
from continucare.services.competition_demo import (
    CompetitionDemoConflict,
    CompetitionDemoProgress,
    CompetitionDemoStage,
    demo_write_guard,
    read_competition_demo,
    start_competition_demo,
)
from continucare.services.confirmed_review import ConfirmedReviewService
from continucare.services.manual_review_workflow import ManualReviewWorkflowService
from continucare.ui import (
    COMPETITION_STEP_LABELS,
    DEMO_GUIDE_STEPS,
    clear_demo_session_state,
    project_demo_guide,
    render_competition_progress,
    render_demo_guide,
    render_disclosure_controls,
)


def _after(resource, seconds: int = 1) -> str:
    return (
        datetime.fromisoformat(resource["meta"]["lastUpdated"])
        + timedelta(seconds=seconds)
    ).isoformat()


def _services(db_path):
    progress = read_competition_demo(db_path)
    store = SQLiteStore(db_path, initialize=False)
    engine = CareEngine(store)
    agent = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        patient_timezone="Asia/Shanghai",
    )
    confirmed = ConfirmedReviewService(store, care_agent=agent, care_engine=engine)
    repository = Layer4SQLiteStore(db_path, initialize=False)
    workflow = ManualReviewWorkflowService(store, layer4_store=repository)
    return progress, store, confirmed, repository, workflow


def _confirm(db_path):
    progress, store, confirmed, repository, workflow = _services(db_path)
    record = store.get_agent_run(progress.run_id)
    candidate_ids = [
        item["candidate_id"] for item in record.output_json["candidates"]
    ]
    with demo_write_guard(db_path, expected_generation=progress.generation):
        result = confirmed.accept_all(progress.run_id, candidate_ids)
    return store, repository, workflow, result


def _complete_nurse(db_path):
    store, repository, workflow, confirmed = _confirm(db_path)
    received = workflow.acknowledge(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        note="已收到合成任务。",
        occurred_at=_after(confirmed.task),
    )
    started = workflow.start(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        note="接受并开始核对合成证据。",
        occurred_at=_after(received.task),
    )
    outcome = workflow.record_outcome(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        outcome="evidence_consistent",
        note="已核对原话、确认结果与最终证据链。",
        occurred_at=_after(started.task),
    )
    return store, repository, workflow, outcome


def _briefs(store, repository):
    pathway = load_builtin_pathways().get("GLP1-14D", "1.0.0")
    return ManualReviewBriefService(
        store,
        repository,
        pathway_code=pathway.code,
        pathway_version=pathway.version,
    )


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_ids(db_path):
    progress, store, _, _, _ = _services(db_path)
    result = store.get_agent_run(progress.run_id).output_json
    return [item["candidate_id"] for item in result["candidates"]]


def _add_second_candidate(db_path):
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT run_id, output_json FROM agent_runs ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        output = json.loads(row["output_json"])
        second = json.loads(json.dumps(output["candidates"][0]))
        second["candidate_id"] = f"{second['candidate_id']}-second"
        second["patient_message"] = "我还整理了另一项合成候选，仍须患者确认。"
        output["candidates"].append(second)
        connection.execute(
            "UPDATE agent_runs SET output_json=? WHERE run_id=?",
            (json.dumps(output, ensure_ascii=False), row["run_id"]),
        )
    return [item["candidate_id"] for item in output["candidates"]]


def _persist_candidate_decision(db_path, candidate_id, decision):
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT run_id, session_id, completed_at FROM agent_runs "
            "ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO conversation_action_resolutions (
                action_id, source_run_id, session_id, decision, resolved_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                row["run_id"],
                row["session_id"],
                decision,
                row["completed_at"],
            ),
        )


def _clinical_resource_counts(db_path):
    with connect(db_path) as connection:
        return {
            "QuestionnaireResponse": connection.execute(
                "SELECT COUNT(*) FROM fhir_questionnaire_responses"
            ).fetchone()[0],
            "Observation": connection.execute(
                "SELECT COUNT(*) FROM fhir_observations"
            ).fetchone()[0],
            "Task": connection.execute(
                "SELECT COUNT(*) FROM layer4_fhir_resources "
                "WHERE resource_type='Task'"
            ).fetchone()[0],
            "Communication": connection.execute(
                "SELECT COUNT(*) FROM layer4_fhir_resources "
                "WHERE resource_type='Communication'"
            ).fetchone()[0],
            "Summary": connection.execute(
                "SELECT COUNT(*) FROM layer4_contract_records "
                "WHERE record_type='summary_draft'"
            ).fetchone()[0],
            "Alert": connection.execute(
                "SELECT COUNT(*) FROM alerts"
            ).fetchone()[0],
        }


def _database_snapshot(db_path):
    stat = db_path.stat()
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        row_counts = tuple(
            (table, connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        )
    return (
        _file_hash(db_path),
        stat.st_size,
        stat.st_mtime_ns,
        row_counts,
    )


def _assert_projection_is_read_only(db_path):
    before = _database_snapshot(db_path)
    expected = read_competition_demo(db_path)
    assert all(read_competition_demo(db_path) == expected for _ in range(3))
    assert _database_snapshot(db_path) == before
    assert not any(
        (db_path.parent / f"{db_path.name}{suffix}").exists()
        for suffix in ("-journal", "-wal", "-shm")
    )


def _assert_terminal_navigation(progress):
    assert progress.is_terminal
    assert progress.terminal_reason
    assert progress.next_page == "pages/4_audit_log.py"
    assert progress.next_label == "查看终态审计"
    assert progress.terminal_reason in progress.next_help
    assert "明确重新开始" in progress.next_help
    assert "患者端" not in progress.next_label
    assert "护士端" not in progress.next_label
    assert "医生" not in progress.next_label


def test_missing_story_read_is_read_only_and_does_not_create_database(tmp_path):
    db_path = tmp_path / "missing.db"

    progress = read_competition_demo(db_path)

    assert progress.stage == CompetitionDemoStage.NOT_STARTED
    assert not db_path.exists()


def test_start_atomically_prepares_only_one_unreleased_local_candidate(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "competition.db"
    monkeypatch.setenv("CONTINUCARE_LLM_PROVIDER", "xiaomi_mimo")
    monkeypatch.setenv("CONTINUCARE_LLM_API_KEY", "must-not-be-used")

    progress = start_competition_demo(db_path)
    before = _file_hash(db_path)
    reread = read_competition_demo(db_path)

    assert reread == progress
    assert _file_hash(db_path) == before
    assert progress.stage == CompetitionDemoStage.CANDIDATE_READY
    assert progress.candidate_count == 1
    assert progress.questionnaire_response_count == 0
    assert progress.observation_count == 0
    assert progress.manual_task_count == 0
    assert progress.communication_count == 0
    assert progress.manual_brief_count == 0
    assert progress.alert_count == 0
    assert progress.approved_clinical_rule_count == 0
    assert SEND_ENABLED is False
    assert load_builtin_pathways().get("GLP1-14D").clinical_rules == []
    assert not any((tmp_path / f"competition.db{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))
    with connect(db_path) as connection:
        run = connection.execute("SELECT mode, model_provider FROM agent_runs").fetchone()
    assert tuple(run) == ("local_semantic_mock", None)


def test_failed_start_keeps_the_previous_story_byte_for_byte(monkeypatch, tmp_path):
    db_path = tmp_path / "preserved.db"
    previous = start_competition_demo(db_path)
    before = _file_hash(db_path)

    def fail_after_partial_staging(staging):
        reset_demo(staging)
        raise RuntimeError("sensitive staging failure")

    monkeypatch.setattr(
        competition_demo, "load_manual_review_scenario", fail_after_partial_staging
    )
    with pytest.raises(
        competition_demo.CompetitionDemoStartError,
        match="旧故事未被替换",
    ):
        start_competition_demo(db_path)

    assert _file_hash(db_path) == before
    assert read_competition_demo(db_path).generation == previous.generation
    assert not any(item.name.startswith(".preserved.db.m5d-") for item in tmp_path.iterdir())


def test_concurrent_starts_leave_one_complete_candidate_generation(tmp_path):
    db_path = tmp_path / "concurrent.db"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: start_competition_demo(db_path), range(2)))

    progress = read_competition_demo(db_path)
    assert progress.stage == CompetitionDemoStage.CANDIDATE_READY
    with connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM care_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM fhir_observations").fetchone()[0] == 0


def test_all_candidates_rejected_is_a_read_only_terminal_without_clinical_resources(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "candidate-rejected.db"
    start_competition_demo(db_path)
    progress, _, confirmed, _, _ = _services(db_path)
    confirmed.care_agent.reject_candidates(progress.run_id, _candidate_ids(db_path))
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("projection attempted an external connection")
        ),
    )

    rejected = read_competition_demo(db_path)

    assert rejected.stage == CompetitionDemoStage.CANDIDATE_REJECTED
    _assert_terminal_navigation(rejected)
    assert "明确拒绝" in rejected.terminal_reason
    assert _clinical_resource_counts(db_path) == {
        "QuestionnaireResponse": 0,
        "Observation": 0,
        "Task": 0,
        "Communication": 0,
        "Summary": 0,
        "Alert": 0,
    }
    _assert_projection_is_read_only(db_path)


def test_partial_rejected_candidates_keep_the_remaining_decision_available(tmp_path):
    db_path = tmp_path / "candidate-partial.db"
    start_competition_demo(db_path)
    first, second = _add_second_candidate(db_path)
    _persist_candidate_decision(db_path, first, "rejected")

    progress = read_competition_demo(db_path)

    assert progress.stage == CompetitionDemoStage.CANDIDATE_READY
    assert not progress.is_terminal
    assert progress.terminal_reason is None
    assert progress.candidate_decisions == {first: "rejected"}
    assert second not in progress.candidate_decisions
    assert progress.next_page == "pages/1_patient_followup.py"
    assert _clinical_resource_counts(db_path) == {
        "QuestionnaireResponse": 0,
        "Observation": 0,
        "Task": 0,
        "Communication": 0,
        "Summary": 0,
        "Alert": 0,
    }


def test_rejected_and_unsure_candidates_are_non_terminal_and_can_continue(tmp_path):
    db_path = tmp_path / "candidate-unsure-mixed.db"
    start_competition_demo(db_path)
    rejected_id, unsure_id = _add_second_candidate(db_path)
    _persist_candidate_decision(db_path, rejected_id, "rejected")
    _persist_candidate_decision(db_path, unsure_id, "unsure")

    progress = read_competition_demo(db_path)

    assert progress.stage == CompetitionDemoStage.CANDIDATE_UNSURE
    assert not progress.is_terminal
    assert progress.terminal_reason is None
    assert progress.next_page == "pages/1_patient_followup.py"
    assert "接受或拒绝" in progress.next_label
    assert _clinical_resource_counts(db_path) == {
        "QuestionnaireResponse": 0,
        "Observation": 0,
        "Task": 0,
        "Communication": 0,
        "Summary": 0,
        "Alert": 0,
    }


def test_unsure_candidate_can_later_be_accepted_without_duplicate_resources(tmp_path):
    db_path = tmp_path / "candidate-unsure-accepted.db"
    start_competition_demo(db_path)
    progress, _, confirmed, _, _ = _services(db_path)
    candidate_ids = _candidate_ids(db_path)
    confirmed.care_agent.mark_candidates_unsure(progress.run_id, candidate_ids)
    unsure = read_competition_demo(db_path)
    assert unsure.stage == CompetitionDemoStage.CANDIDATE_UNSURE
    assert not unsure.is_terminal

    confirmed.accept_all(progress.run_id, candidate_ids)
    accepted = read_competition_demo(db_path)

    assert accepted.stage == CompetitionDemoStage.TASK_REQUESTED
    assert not accepted.is_terminal
    assert accepted.terminal_reason is None
    assert accepted.candidate_decisions == {candidate_ids[0]: "accepted"}
    assert accepted.questionnaire_response_count == 1
    assert accepted.observation_count == 1
    assert accepted.manual_task_count == 1
    counts = _clinical_resource_counts(db_path)
    assert counts["QuestionnaireResponse"] == 1
    assert counts["Observation"] == 1
    assert counts["Task"] == 1
    assert counts["Communication"] == 0
    assert counts["Summary"] == 0
    _assert_projection_is_read_only(db_path)


def test_unsure_candidate_can_later_be_rejected_without_clinical_resources(tmp_path):
    db_path = tmp_path / "candidate-unsure-rejected.db"
    start_competition_demo(db_path)
    progress, _, confirmed, _, _ = _services(db_path)
    candidate_ids = _candidate_ids(db_path)
    confirmed.care_agent.mark_candidates_unsure(progress.run_id, candidate_ids)
    confirmed.care_agent.reject_candidates(progress.run_id, candidate_ids)

    rejected = read_competition_demo(db_path)

    assert rejected.stage == CompetitionDemoStage.CANDIDATE_REJECTED
    assert rejected.candidate_decisions == {candidate_ids[0]: "rejected"}
    _assert_terminal_navigation(rejected)
    assert _clinical_resource_counts(db_path) == {
        "QuestionnaireResponse": 0,
        "Observation": 0,
        "Task": 0,
        "Communication": 0,
        "Summary": 0,
        "Alert": 0,
    }
    _assert_projection_is_read_only(db_path)


@pytest.mark.parametrize(
    ("action", "expected_stage", "reason_fragment"),
    [
        ("reject", CompetitionDemoStage.TASK_REJECTED, "明确拒绝"),
        ("cancel", CompetitionDemoStage.TASK_CANCELLED, "明确取消"),
    ],
)
def test_manual_task_rejected_or_cancelled_has_terminal_priority(
    action, expected_stage, reason_fragment, tmp_path
):
    db_path = tmp_path / f"task-{action}.db"
    start_competition_demo(db_path)
    _, _, workflow, confirmed = _confirm(db_path)
    if action == "reject":
        received = workflow.acknowledge(
            patient_id=DEMO_PATIENT_ID,
            task_id=confirmed.task["id"],
            note="已收到合成任务。",
            occurred_at=_after(confirmed.task),
        )
        workflow.reject(
            patient_id=DEMO_PATIENT_ID,
            task_id=confirmed.task["id"],
            note="明确拒绝合成人工复核任务。",
            occurred_at=_after(received.task),
        )
    else:
        workflow.cancel(
            patient_id=DEMO_PATIENT_ID,
            task_id=confirmed.task["id"],
            note="明确取消合成人工复核任务。",
            occurred_at=_after(confirmed.task),
        )

    terminal = read_competition_demo(db_path)

    assert terminal.stage == expected_stage
    assert reason_fragment in terminal.terminal_reason
    _assert_terminal_navigation(terminal)
    assert terminal.milestones["patient_confirmed"]
    assert terminal.milestones["task_requested"]
    assert terminal.communication_count == 0
    assert terminal.manual_brief_count == 0
    assert _clinical_resource_counts(db_path)["Communication"] == 0
    assert _clinical_resource_counts(db_path)["Summary"] == 0
    _assert_projection_is_read_only(db_path)


@pytest.mark.parametrize(
    ("task_status", "expected_stage", "reason_fragment"),
    [
        ("failed", CompetitionDemoStage.TASK_FAILED, "fail-closed"),
        (
            "entered-in-error",
            CompetitionDemoStage.TASK_ENTERED_IN_ERROR,
            "fail-closed",
        ),
    ],
)
def test_manual_task_error_statuses_fail_closed_without_success_actions(
    task_status, expected_stage, reason_fragment, tmp_path
):
    db_path = tmp_path / f"task-{task_status}.db"
    start_competition_demo(db_path)
    _, repository, workflow, confirmed = _confirm(db_path)
    task = confirmed.task
    if task_status == "failed":
        received = workflow.acknowledge(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            note="已收到合成任务。",
            occurred_at=_after(task),
        )
        started = workflow.start(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            note="接受并开始核对合成证据。",
            occurred_at=_after(received.task),
        )
        task = started.task
    TaskWorkflowService(repository).transition(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        to_status=task_status,
        actor_reference="PractitionerRole/synthetic-data-steward",
        note=f"将合成任务标记为 {task_status}。",
        transitioned_at=_after(task),
    )

    terminal = read_competition_demo(db_path)

    assert terminal.stage == expected_stage
    assert reason_fragment in terminal.terminal_reason
    _assert_terminal_navigation(terminal)
    assert "成功" not in terminal.next_label
    assert "继续" not in terminal.next_label
    assert terminal.communication_count == 0
    assert terminal.manual_brief_count == 0
    _assert_projection_is_read_only(db_path)


def test_recommended_click_flow_is_derived_from_persisted_facts(tmp_path):
    db_path = tmp_path / "full-flow.db"
    start = start_competition_demo(db_path)
    assert start.stage == CompetitionDemoStage.CANDIDATE_READY

    store, repository, workflow, confirmed = _confirm(db_path)
    requested = read_competition_demo(db_path)
    assert requested.stage == CompetitionDemoStage.TASK_REQUESTED
    assert requested.milestones["patient_confirmed"]
    assert requested.milestones["task_requested"]
    assert requested.questionnaire_response_count == 1
    assert requested.observation_count == 1

    received = workflow.acknowledge(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        note="已收到合成任务。",
        occurred_at=_after(confirmed.task),
    )
    assert read_competition_demo(db_path).stage == CompetitionDemoStage.NURSE_RECEIVED
    started = workflow.start(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        note="接受并开始核对合成证据。",
        occurred_at=_after(received.task),
    )
    assert read_competition_demo(db_path).stage == CompetitionDemoStage.NURSE_IN_PROGRESS
    outcome = workflow.record_outcome(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        outcome="evidence_consistent",
        note="已核对原话、确认结果与最终证据链。",
        occurred_at=_after(started.task),
    )
    pending = read_competition_demo(db_path)
    assert pending.stage == CompetitionDemoStage.COMMUNICATION_PENDING
    assert pending.communication_readiness == "pending-approval"

    briefs = _briefs(store, repository)
    pending_summary = briefs.generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=_after(outcome.task),
    )
    pending_brief = read_competition_demo(db_path)
    assert pending_brief.stage == CompetitionDemoStage.DOCTOR_BRIEF_PENDING
    assert pending_brief.summary_version == pending_summary.version

    approved = workflow.approve_draft(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        communication_id=outcome.communication["id"],
        note="明确批准 ready-to-send；仍未发送。",
        occurred_at=(
            datetime.fromisoformat(pending_summary.created_at) + timedelta(seconds=1)
        ).isoformat(),
    )
    ready = read_competition_demo(db_path)
    assert ready.stage == CompetitionDemoStage.COMMUNICATION_READY
    assert ready.milestones["doctor_brief_pending"]
    assert not ready.milestones["doctor_brief_ready"]

    final_summary = briefs.generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=_after(approved.communication),
    )
    complete = read_competition_demo(db_path)
    assert complete.stage == CompetitionDemoStage.STORY_COMPLETE
    _assert_terminal_navigation(complete)
    assert "9 项持久化事实已完成" in complete.terminal_reason
    assert complete.summary_version == final_summary.version
    assert complete.milestones["doctor_brief_ready"]
    assert complete.milestones["story_complete"]
    assert complete.alert_count == 0
    assert complete.approved_clinical_rule_count == 0
    assert complete.communication_readiness == "ready-to-send"
    assert sum(
        bool(complete.milestones[step]) for step, _ in COMPETITION_STEP_LABELS
    ) == len(COMPETITION_STEP_LABELS) == 9


def test_legal_approval_before_first_brief_does_not_invent_pending_brief(tmp_path):
    db_path = tmp_path / "alternate-order.db"
    start_competition_demo(db_path)
    store, repository, workflow, outcome = _complete_nurse(db_path)
    approved = workflow.approve_draft(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        communication_id=outcome.communication["id"],
        note="先明确批准，尚未生成医生简报。",
        occurred_at=_after(outcome.task),
    )

    progress = read_competition_demo(db_path)
    assert progress.stage == CompetitionDemoStage.COMMUNICATION_READY
    assert not progress.milestones["doctor_brief_pending"]

    _briefs(store, repository).generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=_after(approved.communication),
    )
    assert read_competition_demo(db_path).stage == CompetitionDemoStage.STORY_COMPLETE


def test_knowledge_availability_is_not_a_clinical_completion_gate(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "knowledge-independent.db"
    start_competition_demo(db_path)
    store, repository, workflow, outcome = _complete_nurse(db_path)
    approved = workflow.approve_draft(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        communication_id=outcome.communication["id"],
        note="批准合成草稿。",
        occurred_at=_after(outcome.task),
    )
    _briefs(store, repository).generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=_after(approved.communication),
    )
    monkeypatch.setattr(
        competition_demo,
        "_knowledge_status",
        lambda: (False, "independent registry unavailable"),
    )

    progress = read_competition_demo(db_path)
    assert progress.stage == CompetitionDemoStage.STORY_COMPLETE
    assert progress.is_terminal
    assert not progress.knowledge_available


def test_explicit_restart_removes_the_old_chain_and_stale_writes_are_rejected(
    tmp_path,
):
    db_path = tmp_path / "restart.db"
    original = start_competition_demo(db_path)
    _confirm(db_path)
    restarted = start_competition_demo(db_path)

    assert restarted.generation != original.generation
    assert restarted.stage == CompetitionDemoStage.CANDIDATE_READY
    assert restarted.questionnaire_response_count == 0
    assert restarted.observation_count == 0
    assert restarted.manual_task_count == 0
    with pytest.raises(CompetitionDemoConflict, match="另一标签页重新开始"):
        with demo_write_guard(db_path, expected_generation=original.generation):
            pass


def test_explicit_restart_clears_only_demo_and_current_role_browser_state():
    session_state = {
        "care::session-123::answer": "yes",
        "semantic::confirmed::run-123": ["candidate-123"],
        "manual_review_hint": "legacy",
        "competition::reset_consent": True,
        "care_submission_notice": "saved",
        "cc_patient_pending_decision": {"action": "accept"},
        "cc_patient_other_methods_toggle": True,
        "cc_nurse_selected_task": "task-dynamic-123",
        "cc_nurse_confirm_action": "task-dynamic-123:cancel",
        "cc_nurse_stop_note_task-dynamic-123_cancel": "stop note",
        "cc_nurse_outcome_task-dynamic-123": "evidence_consistent",
        "cc_nurse_notice": "saved",
        "cc_nurse_primary_button_task-dynamic-123_acknowledge": True,
        "cc_doctor_feedback": "saved",
        "cc_doctor_decisions_summary-dynamic-456_7": "modify",
        "cc_doctor_modify_note_summary-dynamic-456_7": "wording note",
        "cc_doctor_reject_note_summary-dynamic-456_7": "reject note",
        "cc_doctor_technical_summary-dynamic-456_7": "Task/task-1",
        "cc_knowledge_topic": "nausea",
        "unrelated_application_key": "keep",
    }
    streamlit = SimpleNamespace(session_state=session_state)

    clear_demo_session_state(streamlit)
    clear_demo_session_state(streamlit)

    assert session_state == {
        "cc_knowledge_topic": "nausea",
        "unrelated_application_key": "keep",
    }


def test_explicit_restart_is_safe_for_an_empty_session_state():
    streamlit = SimpleNamespace(session_state={})

    clear_demo_session_state(streamlit)
    clear_demo_session_state(streamlit)

    assert streamlit.session_state == {}


class _DisclosureDOM(HTMLParser):
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


class _DisclosureRenderer:
    def __init__(self, query_params=None):
        self.query_params = query_params or {}
        self.fragments: list[str] = []

    def markdown(self, value, **_kwargs):
        self.fragments.append(str(value))

    def dom(self) -> _DisclosureDOM:
        parser = _DisclosureDOM()
        parser.feed("\n".join(self.fragments))
        return parser


def test_disclosure_html_keeps_a_unique_real_target_in_every_state():
    options = (("patient", "查看患者原话"), ("technical", "技术详情"))

    collapsed = _DisclosureRenderer()
    assert render_disclosure_controls(
        collapsed,
        query_parameter="view",
        page_path="/example",
        options=options,
        aria_label="进一步查看",
        panel_id="cc-example-panel",
    ) is None
    collapsed_dom = collapsed.dom()
    assert [item[1]["id"] for item in collapsed_dom.ids] == ["cc-example-panel"]
    assert collapsed_dom.ids[0][0] == "span"
    assert collapsed_dom.ids[0][1]["hidden"] is None
    assert collapsed_dom.ids[0][1]["aria-hidden"] == "true"
    assert "tabindex" not in collapsed_dom.ids[0][1]
    assert [item["aria-expanded"] for item in collapsed_dom.controls] == [
        "false",
        "false",
    ]
    assert [item["href"] for item in collapsed_dom.controls] == [
        "/example?view=patient",
        "/example?view=technical",
    ]

    expanded = _DisclosureRenderer({"view": "patient"})
    assert render_disclosure_controls(
        expanded,
        query_parameter="view",
        page_path="/example",
        options=options,
        aria_label="进一步查看",
        panel_id="cc-example-panel",
    ) == "patient"
    expanded.markdown(
        '<section id="cc-example-panel"><p>患者原话</p></section>',
        unsafe_allow_html=True,
    )
    expanded_dom = expanded.dom()
    assert [item[1]["id"] for item in expanded_dom.ids] == ["cc-example-panel"]
    assert expanded_dom.ids[0][0] == "section"
    assert "hidden" not in expanded_dom.ids[0][1]
    assert [item["aria-expanded"] for item in expanded_dom.controls] == [
        "true",
        "false",
    ]
    assert expanded_dom.controls[0]["href"] == "/example?view="

    unknown = _DisclosureRenderer({"view": "future-value"})
    assert render_disclosure_controls(
        unknown,
        query_parameter="view",
        page_path="/example",
        options=options,
        aria_label="进一步查看",
        panel_id="cc-example-panel",
    ) is None
    unknown_dom = unknown.dom()
    assert len(unknown_dom.ids) == 1
    assert all(item["aria-expanded"] == "false" for item in unknown_dom.controls)


def test_patient_page_no_longer_creates_a_session_on_load_and_knowledge_stays_isolated():
    root = __import__("pathlib").Path(__file__).parents[1]
    patient_source = (root / "pages" / "1_patient_followup.py").read_text("utf-8")
    knowledge_source = (root / "pages" / "5_knowledge_evidence.py").read_text("utf-8")

    assert "start_or_resume(" not in patient_source
    assert "read_competition_demo" in patient_source
    assert "with demo_write_guard(settings.db_path):" not in patient_source
    assert "expected_generation=progress.generation" in patient_source
    assert "continucare.services.competition_demo" not in knowledge_source
    assert "continucare.db" not in knowledge_source
    assert "patient_id" not in knowledge_source


class _RenderContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _ProgressRenderer(_RenderContext):
    def __init__(self, *, url=None):
        self.messages = []
        self.links = []
        self.context = type("Context", (), {"url": url})()
        self.stopped = False

    def markdown(self, value, **kwargs):
        self.messages.append(str(value))

    def progress(self, value, **kwargs):
        self.messages.append(str(kwargs.get("text", value)))

    def columns(self, count, **kwargs):
        column_count = count if isinstance(count, int) else len(count)
        return [_RenderContext() for _ in range(column_count)]

    def container(self, **kwargs):
        return _RenderContext()

    def page_link(self, page, *, label, **kwargs):
        self.links.append((page, label))

    def success(self, value):
        self.messages.append(str(value))

    def info(self, value):
        self.messages.append(str(value))

    def warning(self, value):
        self.messages.append(str(value))

    def error(self, value):
        self.messages.append(str(value))

    def caption(self, value):
        self.messages.append(str(value))

    def stop(self):
        self.stopped = True


def test_shared_progress_renderer_and_home_use_terminal_contract(monkeypatch):
    reason = "所有候选均已由患者明确拒绝；未创建临床资源或护士任务。"
    progress = CompetitionDemoProgress(
        stage=CompetitionDemoStage.CANDIDATE_REJECTED,
        generation="session:run",
        is_terminal=True,
        terminal_reason=reason,
        next_page="pages/4_audit_log.py",
        next_label="查看终态审计",
        next_help=f"{reason} 如需新故事，请明确重新开始；系统不会自动重启。",
    )
    renderer = _ProgressRenderer()
    monkeypatch.setattr("continucare.ui.render_integration_status", lambda st: None)

    render_competition_progress(renderer, progress)

    rendered = "\n".join(renderer.messages)
    assert reason in rendered
    assert "推荐下一步" not in rendered
    assert renderer.links == [
        ("pages/4_audit_log.py", "查看终态审计 →"),
        ("app.py", "返回首页（不会自动重新开始） →"),
    ]
    assert renderer.stopped
    home_renderer = _ProgressRenderer(url="http://localhost:8501/")
    render_competition_progress(home_renderer, progress)
    assert not home_renderer.stopped
    doctor_renderer = _ProgressRenderer(url="http://localhost:8501/doctor_summary")
    render_competition_progress(doctor_renderer, progress)
    assert doctor_renderer.stopped
    audit_renderer = _ProgressRenderer(url="http://localhost:8501/audit_log")
    render_competition_progress(audit_renderer, progress)
    assert not audit_renderer.stopped
    records_renderer = _ProgressRenderer(url="http://localhost:8501/records")
    render_competition_progress(records_renderer, progress)
    assert not records_renderer.stopped
    prefixed_audit_renderer = _ProgressRenderer(
        url="https://example.test/continucare/audit_log"
    )
    render_competition_progress(prefixed_audit_renderer, progress)
    assert not prefixed_audit_renderer.stopped
    app_source = (__import__("pathlib").Path(__file__).parents[1] / "app.py").read_text(
        "utf-8"
    )
    assert "render_competition_progress" not in app_source
    assert "render_demo_guide(" in app_source
    assert "当前故事的安全计数" not in app_source
    assert ".metric(" not in app_source


@pytest.mark.parametrize(
    ("stage", "current_step", "current_role", "tone"),
    [
        (CompetitionDemoStage.NOT_STARTED, 1, "演示者", "neutral"),
        (CompetitionDemoStage.CANDIDATE_READY, 2, "患者", "active"),
        (CompetitionDemoStage.CANDIDATE_UNSURE, 2, "患者", "caution"),
        (CompetitionDemoStage.PATIENT_CONFIRMED, 3, "护士", "active"),
        (CompetitionDemoStage.TASK_REQUESTED, 3, "护士", "active"),
        (CompetitionDemoStage.NURSE_RECEIVED, 3, "护士", "active"),
        (CompetitionDemoStage.NURSE_IN_PROGRESS, 3, "护士", "active"),
        (CompetitionDemoStage.COMMUNICATION_PENDING, 4, "医生", "active"),
        (CompetitionDemoStage.DOCTOR_BRIEF_PENDING, 4, "护士", "caution"),
        (CompetitionDemoStage.COMMUNICATION_READY, 4, "医生", "active"),
        (CompetitionDemoStage.DOCTOR_BRIEF_READY, 5, "审核者", "active"),
        (CompetitionDemoStage.CANDIDATE_REJECTED, 5, "审核者", "stopped"),
        (CompetitionDemoStage.TASK_REJECTED, 5, "审核者", "stopped"),
        (CompetitionDemoStage.TASK_CANCELLED, 5, "审核者", "stopped"),
        (CompetitionDemoStage.TASK_FAILED, 5, "审核者", "error"),
        (CompetitionDemoStage.TASK_ENTERED_IN_ERROR, 5, "审核者", "error"),
        (CompetitionDemoStage.STORY_COMPLETE, 5, "审核者", "complete"),
    ],
)
def test_home_guide_projects_every_supported_story_state(
    stage, current_step, current_role, tone
):
    projection = project_demo_guide(
        CompetitionDemoProgress(stage=stage, generation="session:run")
    )

    assert projection.current_step == current_step
    assert projection.current_role == current_role
    assert projection.tone == tone
    assert len(projection.step_states) == len(DEMO_GUIDE_STEPS) == 5
    assert projection.step_states.count("current") == 1
    assert projection.status_title
    assert projection.previous_event
    assert projection.next_destination


def test_integrity_issue_projects_fail_closed_without_a_business_action():
    projection = project_demo_guide(
        CompetitionDemoProgress(integrity_issue="raw internal detail")
    )

    assert projection.current_step == 5
    assert projection.tone == "error"
    assert projection.next_page is None
    assert projection.next_label is None
    assert "没有继续" in projection.status_detail
    assert "raw internal detail" not in projection.status_detail


def test_home_guide_is_human_language_accessible_and_knowledge_independent():
    assert DEMO_GUIDE_STEPS == (
        "一句原话",
        "本人确认",
        "人工核对",
        "复诊速览",
        "完整留痕",
    )
    assert all("Knowledge" not in label for label in DEMO_GUIDE_STEPS)

    for stage in CompetitionDemoStage:
        renderer = _ProgressRenderer(url="http://localhost:8501/")
        progress = CompetitionDemoProgress(stage=stage, generation="session:run")

        projection = render_demo_guide(renderer, progress)
        rendered = "\n".join(renderer.messages)

        assert 'aria-current="step"' in rendered
        assert stage.value not in rendered
        assert "ready-to-send" not in rendered
        assert "candidate" not in rendered
        assert "Layer 3" not in rendered
        assert "M5-D" not in rendered
        if projection.next_page:
            assert renderer.links == [(projection.next_page, projection.next_label)]


def test_negative_terminals_only_offer_record_trace_and_complete_is_not_clinical_success():
    for stage in (
        CompetitionDemoStage.CANDIDATE_REJECTED,
        CompetitionDemoStage.TASK_REJECTED,
        CompetitionDemoStage.TASK_CANCELLED,
        CompetitionDemoStage.TASK_FAILED,
        CompetitionDemoStage.TASK_ENTERED_IN_ERROR,
    ):
        projection = project_demo_guide(
            CompetitionDemoProgress(stage=stage, generation="session:run")
        )
        assert projection.next_page == "pages/4_audit_log.py"
        assert projection.next_label == "查看记录追溯"
        assert "患者" not in projection.next_label
        assert "护士" not in projection.next_label
        assert "医生" not in projection.next_label

    renderer = _ProgressRenderer(url="http://localhost:8501/")
    render_demo_guide(
        renderer,
        CompetitionDemoProgress(
            stage=CompetitionDemoStage.STORY_COMPLETE,
            generation="session:run",
            is_terminal=True,
        ),
    )
    rendered = "\n".join(renderer.messages)
    assert "演示记录链已走完" in rendered
    assert "临床成功" not in rendered
    assert "诊疗完成" not in rendered


def test_home_guide_render_is_pure_and_does_not_write_database(tmp_path):
    db_path = tmp_path / "guide-read-only.db"
    start_competition_demo(db_path)
    before = _database_snapshot(db_path)
    progress = read_competition_demo(db_path)
    renderer = _ProgressRenderer(url="http://localhost:8501/")

    first = render_demo_guide(renderer, progress)
    second = project_demo_guide(progress)

    assert first == second
    assert _database_snapshot(db_path) == before
    assert not any(
        (tmp_path / f"guide-read-only.db{suffix}").exists()
        for suffix in ("-journal", "-wal", "-shm")
    )


def test_home_source_keeps_reset_and_technical_details_secondary():
    app_source = (__import__("pathlib").Path(__file__).parents[1] / "app.py").read_text(
        "utf-8"
    )

    assert '"开始一轮合成演示"' in app_source
    assert '"管理本地演示数据"' in app_source
    assert '"我知道当前这轮合成演示记录会被替换。"' in app_source
    assert '"替换并开始新一轮"' in app_source
    assert "正在准备新的合成演示，请勿重复操作……" in app_source
    assert "新一轮暂时无法开始，原来的演示记录没有被替换。请重试。" in app_source
    assert '"技术详情：外部适配器与当前配置"' in app_source
    assert '"再用 20 秒看负向路径"' in app_source
    assert "THREE ROLE EXPERIENCES" in app_source
    assert "三种角色，只看此刻需要的内容" in app_source
    assert "ROLE_ROUTES" in app_source


def test_hidden_router_exposes_unique_role_specific_urls():
    assert tuple(route.route_id for route in ROLE_ROUTES) == (
        "patient",
        "nurse",
        "doctor",
    )
    assert tuple(route.url_path for route in ROLE_ROUTES) == (
        "patient",
        "nurse",
        "doctor",
    )
    assert len({route.source for route in APP_ROUTES}) == len(APP_ROUTES)
    assert len({route.url_path for route in APP_ROUTES if route.url_path}) == 5
    assert sum(route.default for route in APP_ROUTES) == 1

    router_source = (
        __import__("pathlib").Path(__file__).parents[1] / "streamlit_app.py"
    ).read_text("utf-8")
    assert "st.navigation(pages, position=\"hidden\")" in router_source
    assert 'visibility="hidden"' in router_source

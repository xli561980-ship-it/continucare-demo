from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.agents.contracts import (
    CodingContract,
    SemanticCandidate,
    SemanticResult,
    SemanticStatus,
    Temporality,
)
from continucare.agents.errors import AgentToolDeniedError
from continucare.care_agent import CareAgentService
from continucare.care_agent.model_api import (
    SemanticModelConfig,
    UnconfiguredModelAdapter,
)
from continucare.care_agent.safety import SafetyAgent
from continucare.care_engine import CareEngine
from continucare.db import connect
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.errors import ConcurrentWriteConflict


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "semantic_cases_v1.json"


def _service(tmp_path):
    store = SQLiteStore(tmp_path / "layer3.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    adapter = UnconfiguredModelAdapter(SemanticModelConfig())
    return store, engine, session, CareAgentService(
        store, care_engine=engine, model_adapter=adapter
    )


@pytest.mark.parametrize("case", json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
def test_semantic_fixture_cases_are_deterministic(tmp_path, case):
    _, _, session, service = _service(tmp_path)

    interaction = service.analyze(session.session_id, case["text"])

    assert interaction.result.status.value == case["expected_status"]
    assert sorted(item.link_id for item in interaction.result.candidates) == sorted(
        case["expected_links"]
    )
    assert len(interaction.result.clarifications) == case["expected_clarifications"]
    assert interaction.result.safety_violations == []
    if case["case_id"] in {"current_negation", "current_short_negation"}:
        assert interaction.result.candidates[0].answer is False
    for candidate in interaction.result.candidates:
        assert (
            case["text"][candidate.evidence_start : candidate.evidence_end]
            == candidate.evidence_text
        )


def test_patient_confirmation_is_required_before_layer2_draft_write(tmp_path):
    store, engine, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id, "过去24小时我吐了2次，现在有点恶心。"
    )

    assert store.get_care_session(session.session_id).answers == {}
    assert store.list_messages(DEMO_PATIENT_ID) == []
    candidate_ids = [item.candidate_id for item in interaction.result.candidates]

    updated = service.confirm_candidates(interaction.result.run_id, candidate_ids)

    assert updated.answers["vomiting-count-24h"] == 2
    assert updated.answers["nausea-present"] is True
    assert updated.answers["nausea-severity"] == "LA6752-5"
    assert "过去24小时" in updated.answers["free-text-report"]
    assert store.list_messages(DEMO_PATIENT_ID) == []
    assert store.list_observations(DEMO_PATIENT_ID) == []

    completed = engine.complete(session.session_id, updated.answers)
    assert {item.code for item in completed.observations} == {
        "422587007",
        "81660-3",
        "94070-0",
    }


def test_missing_time_window_is_not_written_until_patient_resolves_it(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "我吐了2次。")
    clarification = interaction.result.clarifications[0]

    assert interaction.result.candidates == []
    assert store.get_care_session(session.session_id).answers == {}

    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "unsure",
    )
    assert store.get_care_session(session.session_id).answers == {}

    updated = service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "yes_24h",
    )
    assert updated.answers["vomiting-count-24h"] == 2


def test_agent_run_is_persisted_and_same_task_replays_idempotently(tmp_path):
    store, _, session, service = _service(tmp_path)
    first = service.analyze(session.session_id, "我现在没有腹痛。")
    second = CareAgentService(
        store,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
    ).analyze(session.session_id, "我现在没有腹痛。")

    assert second.idempotent_replay is True
    assert second.result.run_id == first.result.run_id
    assert len(store.list_agent_runs(session.session_id)) == 1
    assert first.record.model_provider is None
    assert first.result.mode == "local_semantic_mock"


def test_safety_agent_rejects_unknown_link_code_and_invalid_evidence(tmp_path):
    _, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "过去24小时我吐了2次。")
    task = interaction.task
    valid = interaction.result.candidates[0]
    unsafe = valid.model_copy(
        update={
            "candidate_id": "candidate-unsafe",
            "link_id": "invented-link",
            "questionnaire_code": CodingContract(
                system="urn:invented", code="made-up"
            ),
            "evidence_text": "不存在",
            "temporality": Temporality.EXPLICIT_24H,
        }
    )
    draft = SemanticResult(
        run_id="run-unsafe",
        task_id=task.task_id,
        status=SemanticStatus.NEEDS_CONFIRMATION,
        mode="test",
        care_agent_version="test",
        safety_agent_version="pending",
        language_policy_version="1.0.0",
        candidates=[unsafe],
        completed_at="2026-08-01T10:00:00+00:00",
    )

    reviewed = SafetyAgent().review(task, draft)

    assert reviewed.candidates == []
    assert reviewed.status == SemanticStatus.NO_MATCH
    assert any("unknown_link_id" in item for item in reviewed.safety_violations)

    bad_evidence = valid.model_copy(
        update={
            "candidate_id": "candidate-bad-evidence",
            "evidence_text": "不存在的原文",
        }
    )
    evidence_errors = SafetyAgent().review_candidate(task, bad_evidence)
    assert any("invalid_evidence_span" in item for item in evidence_errors)

    bad_code = valid.model_copy(
        update={
            "candidate_id": "candidate-bad-code",
            "questionnaire_code": CodingContract(
                system="urn:invented", code="made-up"
            ),
        }
    )
    code_errors = SafetyAgent().review_candidate(task, bad_code)
    assert any("code_not_governed" in item for item in code_errors)

    wrong_value = valid.model_copy(
        update={
            "candidate_id": "candidate-wrong-value",
            "answer": 9,
        }
    )
    value_errors = SafetyAgent().review_candidate(task, wrong_value)
    assert any("answer_evidence_mismatch" in item for item in value_errors)


def test_safety_agent_rejects_severity_that_disagrees_with_evidence(tmp_path):
    _, _, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id, "我现在有点恶心。"
    )
    severity = next(
        item
        for item in interaction.result.candidates
        if item.link_id == "nausea-severity"
    )
    wrong_severity = severity.model_copy(
        update={
            "candidate_id": "candidate-wrong-severity",
            "answer": "LA6750-9",
        }
    )

    errors = SafetyAgent().review_candidate(interaction.task, wrong_severity)

    assert any("answer_evidence_mismatch" in item for item in errors)


def test_agent_runtime_denies_unregistered_tools_even_after_cached_run(tmp_path):
    _, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "过去24小时我吐了2次。")

    with pytest.raises(AgentToolDeniedError, match="database_write"):
        service.runtime.run(
            "care_agent",
            interaction.task,
            requested_tools=("database_write",),
        )


def test_explicit_count_confirmation_copy_is_friendly_and_semantically_stable(tmp_path):
    _, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "过去24小时呕吐2次。")
    candidate = interaction.result.candidates[0]

    assert candidate.answer == 2
    assert candidate.link_id == "vomiting-count-24h"
    assert candidate.patient_message == (
        "为了确保记录准确：过去24小时您一共呕吐了2次，对吗？"
    )
    assert candidate.requires_patient_confirmation is True


def _resolution_row(store, action_id):
    with connect(store.db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM conversation_action_resolutions
            WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _resolution_rows(store, action_ids):
    return {action_id: _resolution_row(store, action_id) for action_id in action_ids}


def _business_state(store, session_id):
    with connect(store.db_path) as connection:
        session = connection.execute(
            "SELECT * FROM care_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        contexts = connection.execute(
            """
            SELECT * FROM confirmed_answer_contexts
            WHERE session_id = ? ORDER BY answer_context_id
            """,
            (session_id,),
        ).fetchall()
        reports = connection.execute(
            """
            SELECT * FROM confirmed_symptom_reports
            WHERE session_id = ? ORDER BY report_id
            """,
            (session_id,),
        ).fetchall()
    return {
        "session": dict(session) if session is not None else None,
        "answer_contexts": [dict(row) for row in contexts],
        "symptom_reports": [dict(row) for row in reports],
    }


def _audit_state(store):
    return [
        event.model_dump(mode="json")
        for event in store.list_audit_events(DEMO_PATIENT_ID)
    ]


def test_rejected_candidate_cannot_later_be_confirmed(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "我今天拉肚子。")
    candidate = interaction.result.candidates[0]

    service.reject_candidates(interaction.result.run_id, [candidate.candidate_id])
    resolution_before = _resolution_row(store, candidate.candidate_id)
    business_before = _business_state(store, session.session_id)
    audit_before = _audit_state(store)

    service.reject_candidates(
        interaction.result.run_id,
        [candidate.candidate_id],
    )
    assert _resolution_row(store, candidate.candidate_id) == resolution_before
    assert _business_state(store, session.session_id) == business_before
    assert _audit_state(store) == audit_before

    with pytest.raises(ValueError):
        service.confirm_candidates(
            interaction.result.run_id,
            [candidate.candidate_id],
        )

    assert resolution_before["decision"] == "rejected"
    assert _resolution_row(store, candidate.candidate_id) == resolution_before
    assert _business_state(store, session.session_id) == business_before
    assert _audit_state(store) == audit_before
    assert store.get_care_session(session.session_id).answers == {}
    assert store.list_active_answer_contexts(session.session_id) == []
    assert store.list_active_symptom_reports(session.session_id) == []


def test_reject_candidates_strictly_rejects_invalid_and_cross_run_ids(tmp_path):
    store, _, session, service = _service(tmp_path)
    first = service.analyze(session.session_id, "我今天拉肚子。")
    second = service.analyze(session.session_id, "过去24小时我吐了2次。")
    first_id = first.result.candidates[0].candidate_id
    second_id = second.result.candidates[0].candidate_id
    before_business = _business_state(store, session.session_id)
    before_audit = _audit_state(store)

    invalid_sets = [
        [],
        [""],
        [first_id, first_id],
        ["unknown-candidate"],
        [first_id, second_id],
        [first_id, "unknown-candidate"],
    ]
    for candidate_ids in invalid_sets:
        with pytest.raises(ValueError):
            service.reject_candidates(first.result.run_id, candidate_ids)

    assert _resolution_row(store, first_id) is None
    assert _resolution_row(store, second_id) is None
    assert _business_state(store, session.session_id) == before_business
    assert _audit_state(store) == before_audit


@pytest.mark.parametrize(
    "fault_stage",
    [
        "before_material",
        "after_answer_context",
        "after_symptom_report",
        "after_session",
        "after_resolution:0",
        "after_resolution:1",
        "after_audit:care_session_draft_saved",
        "after_audit:semantic_candidate_patient_decision",
        "before_commit",
    ],
)
def test_conversation_decision_faults_roll_back_all_effects(tmp_path, fault_stage):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id,
        "过去24小时我吐了2次，今天拉肚子。",
    )
    candidate_ids = [item.candidate_id for item in interaction.result.candidates]
    assert len(candidate_ids) >= 2
    before_business = _business_state(store, session.session_id)
    before_audit = _audit_state(store)

    def inject(stage):
        if stage == fault_stage:
            raise RuntimeError(f"fault:{stage}")

    store._conversation_decision_fault = inject
    with pytest.raises(RuntimeError, match=fault_stage):
        service.confirm_candidates(interaction.result.run_id, candidate_ids)

    assert _resolution_rows(store, candidate_ids) == {
        action_id: None for action_id in candidate_ids
    }
    assert _business_state(store, session.session_id) == before_business
    assert _audit_state(store) == before_audit


def test_concurrent_different_candidate_decisions_have_one_atomic_winner(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id,
        "过去24小时我吐了2次，今天拉肚子。",
    )
    candidate_ids = [item.candidate_id for item in interaction.result.candidates]
    barrier = Barrier(2)
    original = store.persist_conversation_decision_bundle

    def synchronized(**kwargs):
        barrier.wait(timeout=5)
        return original(**kwargs)

    store.persist_conversation_decision_bundle = synchronized

    def accept():
        return service.confirm_candidates(interaction.result.run_id, candidate_ids)

    def reject():
        service.reject_candidates(interaction.result.run_id, candidate_ids)
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(accept), pool.submit(reject)]
    winners = []
    conflicts = []
    for future in futures:
        try:
            winners.append(future.result())
        except ConcurrentWriteConflict as exc:
            conflicts.append(exc)

    assert len(winners) == 1
    assert len(conflicts) == 1
    decisions = {
        row["decision"] for row in _resolution_rows(store, candidate_ids).values()
    }
    assert decisions in ({"accepted"}, {"rejected"})
    decision_audits = [
        event
        for event in store.list_audit_events(DEMO_PATIENT_ID)
        if event.event_type == "semantic_candidate_patient_decision"
    ]
    assert len(decision_audits) == 1
    if decisions == {"accepted"}:
        assert store.get_care_session(session.session_id).answers
    else:
        assert store.get_care_session(session.session_id).answers == {}


def test_conversation_decision_sqlite_busy_is_a_stable_conflict(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "我今天拉肚子。")
    candidate_id = interaction.result.candidates[0].candidate_id
    before_business = _business_state(store, session.session_id)
    before_audit = _audit_state(store)

    with connect(store.db_path) as locked:
        locked.execute("BEGIN IMMEDIATE")
        with pytest.raises(ConcurrentWriteConflict, match="database is busy"):
            service.confirm_candidates(interaction.result.run_id, [candidate_id])

    assert _resolution_row(store, candidate_id) is None
    assert _business_state(store, session.session_id) == before_business
    assert _audit_state(store) == before_audit


def test_duplicate_confirmation_has_no_business_or_audit_side_effects(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id,
        "过去24小时我吐了2次，今天拉肚子。",
    )
    candidate_ids = [item.candidate_id for item in interaction.result.candidates]

    service.confirm_candidates(interaction.result.run_id, candidate_ids)
    resolutions_before = _resolution_rows(store, candidate_ids)
    business_before = _business_state(store, session.session_id)
    audit_before = _audit_state(store)

    service.confirm_candidates(interaction.result.run_id, candidate_ids)

    business_after = _business_state(store, session.session_id)
    assert _resolution_rows(store, candidate_ids) == resolutions_before
    assert business_after == business_before
    assert business_after["session"]["updated_at"] == business_before["session"][
        "updated_at"
    ]
    assert _audit_state(store) == audit_before
    assert len(business_before["answer_contexts"]) == 1
    assert len(business_before["symptom_reports"]) == 1


def test_confirm_original_closes_all_actions_before_old_candidate_retry(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id,
        "我吐了2次；现在有点恶心。",
    )
    candidate = interaction.result.candidates[0]
    action_ids = [
        *[item.candidate_id for item in interaction.result.candidates],
        *[item.clarification_id for item in interaction.result.clarifications],
    ]
    assert interaction.result.candidates
    assert interaction.result.clarifications

    service.confirm_original_text(interaction.result.run_id)
    resolutions_before = _resolution_rows(store, action_ids)
    business_before = _business_state(store, session.session_id)
    audit_before = _audit_state(store)

    assert {row["decision"] for row in resolutions_before.values()} == {"rejected"}
    service.confirm_original_text(interaction.result.run_id)
    assert _resolution_rows(store, action_ids) == resolutions_before
    assert _business_state(store, session.session_id) == business_before
    assert _audit_state(store) == audit_before

    with pytest.raises(ValueError):
        service.confirm_candidates(
            interaction.result.run_id,
            [candidate.candidate_id],
        )
    assert _resolution_rows(store, action_ids) == resolutions_before
    assert _business_state(store, session.session_id) == business_before
    assert _audit_state(store) == audit_before


def test_candidate_batch_preflight_is_all_or_nothing(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id,
        "过去24小时我吐了2次，现在有点恶心。",
    )
    accepted = next(
        item
        for item in interaction.result.candidates
        if item.link_id == "vomiting-count-24h"
    )
    pending = next(
        item
        for item in interaction.result.candidates
        if item.link_id == "nausea-present"
    )
    action_ids = [accepted.candidate_id, pending.candidate_id]

    service.confirm_candidates(interaction.result.run_id, [accepted.candidate_id])
    resolutions_before = _resolution_rows(store, action_ids)
    business_before = _business_state(store, session.session_id)
    audit_before = _audit_state(store)

    assert resolutions_before[accepted.candidate_id]["decision"] == "accepted"
    assert resolutions_before[pending.candidate_id] is None
    assert pending.link_id not in store.get_care_session(session.session_id).answers
    assert all(
        row["link_id"] != pending.link_id
        for row in business_before["answer_contexts"]
    )
    with pytest.raises(ValueError):
        service.confirm_candidates(interaction.result.run_id, action_ids)

    assert _resolution_rows(store, action_ids) == resolutions_before
    assert _business_state(store, session.session_id) == business_before
    assert _audit_state(store) == audit_before
    assert store.list_active_symptom_reports(session.session_id) == []


def test_unsure_action_can_be_finalized_once_as_accepted(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "我吐了2次。")
    clarification = interaction.result.clarifications[0]

    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "unsure",
    )
    unsure_row = _resolution_row(store, clarification.clarification_id)
    assert unsure_row["decision"] == "unsure"
    business_after_unsure = _business_state(store, session.session_id)
    audit_after_unsure = _audit_state(store)
    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "unsure",
    )
    assert _resolution_row(store, clarification.clarification_id) == unsure_row
    assert _business_state(store, session.session_id) == business_after_unsure
    assert _audit_state(store) == audit_after_unsure

    with connect(store.db_path) as connection:
        connection.execute(
            """
            UPDATE conversation_action_resolutions
            SET option_id = 'stale-option',
                response_run_id = 'stale-response-run',
                response_text = 'stale response',
                resolved_at = '2000-01-01T00:00:00+00:00'
            WHERE action_id = ?
            """,
            (clarification.clarification_id,),
        )

    updated = service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "yes_24h",
    )

    final_row = _resolution_row(store, clarification.clarification_id)
    assert final_row["action_id"] == unsure_row["action_id"]
    assert final_row["source_run_id"] == unsure_row["source_run_id"]
    assert final_row["session_id"] == unsure_row["session_id"]
    assert final_row["decision"] == "accepted"
    assert final_row["option_id"] == "yes_24h"
    assert final_row["response_run_id"] is None
    assert final_row["response_text"] is None
    assert final_row["resolved_at"] != "2000-01-01T00:00:00+00:00"
    assert updated.answers["vomiting-count-24h"] == 2
    contexts = store.list_active_answer_contexts(session.session_id)
    assert [item.link_id for item in contexts] == ["vomiting-count-24h"]
    audits = store.list_audit_events(DEMO_PATIENT_ID)
    assert sum(item.event_type == "care_session_draft_saved" for item in audits) == 1
    patient_decisions = [
        item.details_json["decision"]
        for item in audits
        if item.event_type == "semantic_candidate_patient_decision"
    ]
    assert sorted(patient_decisions) == [
        "clarification_accepted",
        "clarification_unsure",
    ]


def test_finalized_unsure_action_rejects_later_retry_without_side_effects(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "我吐了2次。")
    clarification = interaction.result.clarifications[0]
    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "unsure",
    )
    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "yes_24h",
    )
    resolution_before = _resolution_row(store, clarification.clarification_id)
    business_before = _business_state(store, session.session_id)
    audit_before = _audit_state(store)

    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "yes_24h",
    )

    assert resolution_before["decision"] == "accepted"
    assert _resolution_row(store, clarification.clarification_id) == resolution_before
    assert _business_state(store, session.session_id) == business_before
    assert _audit_state(store) == audit_before


def test_unsure_action_can_be_finalized_once_as_rejected(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "我吐了2次。")
    clarification = interaction.result.clarifications[0]
    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "unsure",
    )

    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "no",
    )
    resolution_before = _resolution_row(store, clarification.clarification_id)
    business_before = _business_state(store, session.session_id)
    audit_before = _audit_state(store)

    assert resolution_before["decision"] == "rejected"
    assert resolution_before["option_id"] == "no"
    assert store.get_care_session(session.session_id).answers == {}
    assert store.list_active_answer_contexts(session.session_id) == []
    assert store.list_active_symptom_reports(session.session_id) == []
    service.resolve_clarification(
        interaction.result.run_id,
        clarification.clarification_id,
        "no",
    )

    assert _resolution_row(store, clarification.clarification_id) == resolution_before
    assert _business_state(store, session.session_id) == business_before
    assert _audit_state(store) == audit_before


def test_context_response_fault_after_run_rolls_back_and_retry_completes(tmp_path):
    store, _, session, service = _service(tmp_path)
    source = service.analyze(
        session.session_id,
        "过去24小时我吐了2次，现在有点恶心。",
        received_at="2026-08-02T01:00:00+00:00",
    )
    action_ids = [item.candidate_id for item in source.result.candidates]
    runs_before = store.list_agent_runs(session.session_id)
    audits_before = _audit_state(store)

    def inject(stage):
        if stage == "after_response_run":
            raise RuntimeError("fault:after_response_run")

    store._conversation_decision_fault = inject
    with pytest.raises(RuntimeError, match="after_response_run"):
        service.analyze(
            session.session_id,
            "都正确",
            received_at="2026-08-02T01:01:00+00:00",
        )

    assert store.list_agent_runs(session.session_id) == runs_before
    assert _audit_state(store) == audits_before
    assert _resolution_rows(store, action_ids) == {
        action_id: None for action_id in action_ids
    }
    assert store.get_care_session(session.session_id).answers == {}

    store._conversation_decision_fault = lambda stage: None
    replay = service.analyze(
        session.session_id,
        "都正确",
        received_at="2026-08-02T01:01:00+00:00",
    )
    assert replay.result.status == SemanticStatus.CONTEXT_RESOLVED
    assert len(store.list_agent_runs(session.session_id)) == len(runs_before) + 1
    assert set(store.get_care_session(session.session_id).answers) >= {
        "vomiting-count-24h",
        "nausea-present",
        "nausea-severity",
    }


def test_context_response_after_commit_replays_without_duplicate_effects(tmp_path):
    store, _, session, service = _service(tmp_path)
    source = service.analyze(
        session.session_id,
        "过去24小时我吐了2次，现在有点恶心。",
        received_at="2026-08-02T01:00:00+00:00",
    )
    action_ids = [item.candidate_id for item in source.result.candidates]

    def inject(stage):
        if stage == "after_commit":
            raise RuntimeError("fault:after_commit")

    store._conversation_decision_fault = inject
    with pytest.raises(RuntimeError, match="after_commit"):
        service.analyze(
            session.session_id,
            "都正确",
            received_at="2026-08-02T01:01:00+00:00",
        )
    committed_state = _business_state(store, session.session_id)
    committed_audits = _audit_state(store)
    committed_resolutions = _resolution_rows(store, action_ids)

    store._conversation_decision_fault = lambda stage: None
    replay = service.analyze(
        session.session_id,
        "都正确",
        received_at="2026-08-02T01:01:00+00:00",
    )

    assert replay.idempotent_replay is True
    assert _business_state(store, session.session_id) == committed_state
    assert _audit_state(store) == committed_audits
    assert _resolution_rows(store, action_ids) == committed_resolutions


def test_context_response_partial_history_is_never_blessed_as_replay(tmp_path):
    store, _, session, service = _service(tmp_path)
    service.analyze(
        session.session_id,
        "过去24小时我吐了2次，现在有点恶心。",
        received_at="2026-08-02T01:00:00+00:00",
    )
    response = service.analyze(
        session.session_id,
        "都正确",
        received_at="2026-08-02T01:01:00+00:00",
    )
    missing_action = response.result.context_resolution.action_ids[0]
    with connect(store.db_path) as connection:
        connection.execute(
            "DELETE FROM conversation_action_resolutions WHERE action_id=?",
            (missing_action,),
        )

    with pytest.raises(ConcurrentWriteConflict, match="replay is blocked"):
        service.analyze(
            session.session_id,
            "都正确",
            received_at="2026-08-02T01:01:00+00:00",
        )


def test_agent_run_bundle_rollback_and_post_commit_replay(tmp_path):
    store, _, session, service = _service(tmp_path)

    def rollback_fault(stage):
        if stage == "after_run":
            raise RuntimeError("fault:after_run")

    store._agent_run_bundle_fault = rollback_fault
    with pytest.raises(RuntimeError, match="after_run"):
        service.analyze(session.session_id, "我现在没有腹痛。")
    assert store.list_agent_runs(session.session_id) == []
    assert not any(
        event.event_type == "semantic_analysis_completed"
        for event in store.list_audit_events(DEMO_PATIENT_ID)
    )

    def commit_fault(stage):
        if stage == "after_commit":
            raise RuntimeError("fault:after_commit")

    store._agent_run_bundle_fault = commit_fault
    with pytest.raises(RuntimeError, match="after_commit"):
        service.analyze(session.session_id, "我现在没有腹痛。")
    committed_runs = store.list_agent_runs(session.session_id)
    committed_audits = _audit_state(store)
    assert len(committed_runs) == 1

    store._agent_run_bundle_fault = lambda stage: None
    replay = service.analyze(session.session_id, "我现在没有腹痛。")
    assert replay.idempotent_replay is True
    assert store.list_agent_runs(session.session_id) == committed_runs
    assert _audit_state(store) == committed_audits


def test_agent_run_bundle_rejects_audit_for_another_run(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(session.session_id, "我现在没有腹痛。")
    audit = next(
        event
        for event in store.list_audit_events(DEMO_PATIENT_ID)
        if event.event_type == "semantic_analysis_completed"
    ).model_copy(
        update={
            "event_id": "audit-agent-run-identity-mismatch",
            "entity_id": "run-another-analysis",
        }
    )

    with pytest.raises(ValueError, match="audit identity mismatch"):
        store.persist_agent_run_bundle(
            record=interaction.record,
            audit_events=[audit],
        )

from __future__ import annotations

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.agents.contracts import SemanticStatus, TemporalKind
from continucare.care_agent import CareAgentService
from continucare.care_agent.model_api import (
    SemanticModelConfig,
    UnconfiguredModelAdapter,
)
from continucare.care_agent.mimo_enhancements import SafetyCriticOutcome
from continucare.care_engine import CareEngine
from continucare.demo_data import DEMO_PATIENT_ID


def _service(tmp_path, *, timezone_name="Asia/Shanghai"):
    store = SQLiteStore(tmp_path / "conversation-time.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    service = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        patient_timezone=timezone_name,
    )
    return store, engine, session, service


def test_natural_yes_resolves_previous_time_clarification_and_keeps_anchor(
    tmp_path,
):
    store, engine, session, service = _service(tmp_path)
    first = service.analyze(
        session.session_id,
        "我吐了2次。",
        received_at="2026-08-02T01:00:00+00:00",
    )

    assert first.result.status == SemanticStatus.NEEDS_CLARIFICATION
    assert first.task.temporal_context.received_at_local == (
        "2026-08-02T09:00:00+08:00"
    )

    second = service.analyze(
        session.session_id,
        "是的",
        received_at="2026-08-02T01:02:00+00:00",
    )

    assert second.result.status == SemanticStatus.CONTEXT_RESOLVED
    assert second.result.context_resolution.applied_link_ids == [
        "vomiting-count-24h"
    ]
    assert store.get_care_session(session.session_id).answers[
        "vomiting-count-24h"
    ] == 2
    contexts = store.list_active_answer_contexts(session.session_id)
    assert len(contexts) == 1
    assert contexts[0].reported_at == "2026-08-02T09:00:00+08:00"
    assert contexts[0].effective_start == "2026-08-01T09:00:00+08:00"
    assert contexts[0].effective_end == "2026-08-02T09:00:00+08:00"
    assert contexts[0].resolution_basis == "patient_confirmation"

    completed = engine.complete(
        session.session_id,
        store.get_care_session(session.session_id).answers,
    )
    observation = next(
        item for item in completed.observations if item.code == "94070-0"
    )
    assert observation.resource["effectivePeriod"] == {
        "start": "2026-08-01T09:00:00+08:00",
        "end": "2026-08-02T09:00:00+08:00",
    }
    assert observation.resource["issued"] == completed.questionnaire_response[
        "authored"
    ]


def test_temporal_context_resolves_local_calendar_words_and_occurrence(tmp_path):
    _, _, session, service = _service(tmp_path, timezone_name="Europe/Berlin")
    interaction = service.analyze(
        session.session_id,
        "昨天吐过2次，今天现在有点恶心。",
        received_at="2026-08-02T22:10:00+00:00",
    )

    temporal = interaction.task.temporal_context
    assert temporal.received_at_local == "2026-08-03T00:10:00+02:00"
    assert temporal.local_date == "2026-08-03"
    mentions = {item.expression: item for item in temporal.detected_mentions}
    assert mentions["昨天"].kind == TemporalKind.LOCAL_CALENDAR_DAY
    assert mentions["昨天"].effective_start == "2026-08-02T00:00:00+02:00"
    assert mentions["昨天"].effective_end == "2026-08-03T00:00:00+02:00"
    assert mentions["今天"].kind == TemporalKind.PARTIAL_LOCAL_DAY
    assert mentions["今天"].effective_start == "2026-08-03T00:00:00+02:00"
    assert mentions["今天"].effective_end == "2026-08-03T00:10:00+02:00"
    assert temporal.followup_occurrence_id.startswith("occurrence-")


def test_short_yes_without_pending_action_never_writes_draft(tmp_path):
    store, _, session, service = _service(tmp_path)

    result = service.analyze(
        session.session_id,
        "是的",
        received_at="2026-08-02T01:00:00+00:00",
    )

    assert result.result.status == SemanticStatus.NO_MATCH
    assert result.result.context_resolution is None
    assert store.get_care_session(session.session_id).answers == {}


def test_button_resolution_closes_context_for_later_short_reply(tmp_path):
    store, _, session, service = _service(tmp_path)
    first = service.analyze(
        session.session_id,
        "我吐了2次。",
        received_at="2026-08-02T01:00:00+00:00",
    )
    clarification = first.result.clarifications[0]
    service.resolve_clarification(
        first.result.run_id,
        clarification.clarification_id,
        "unsure",
    )

    second = service.analyze(
        session.session_id,
        "是的",
        received_at="2026-08-02T01:03:00+00:00",
    )

    assert second.result.status == SemanticStatus.NO_MATCH
    assert second.result.context_resolution is None
    assert "vomiting-count-24h" not in store.get_care_session(
        session.session_id
    ).answers


def test_all_correct_accepts_multiple_candidates_from_latest_turn(tmp_path):
    store, engine, session, service = _service(tmp_path)
    first = service.analyze(
        session.session_id,
        "过去24小时我吐了2次，现在有点恶心。",
        received_at="2026-08-02T01:00:00+00:00",
    )
    assert len(first.result.candidates) == 3

    second = service.analyze(
        session.session_id,
        "都正确",
        received_at="2026-08-02T01:01:00+00:00",
    )

    assert second.result.status == SemanticStatus.CONTEXT_RESOLVED
    assert set(second.result.context_resolution.applied_link_ids) == {
        "vomiting-count-24h",
        "nausea-present",
        "nausea-severity",
    }
    answers = store.get_care_session(session.session_id).answers
    assert answers["vomiting-count-24h"] == 2
    assert answers["nausea-present"] is True
    assert answers["nausea-severity"] == "LA6752-5"
    completed = engine.complete(session.session_id, answers)
    nausea = next(item for item in completed.observations if item.code == "422587007")
    assert nausea.resource["effectiveDateTime"] == "2026-08-02T09:00:00+08:00"
    assert "effectivePeriod" not in nausea.resource


class _MissingSeverityCritic:
    config = SemanticModelConfig(model_name="synthetic-context-critic")
    configured = True

    def review(self, task, hard_result):
        reviews = [
            {
                "candidate_id": item.candidate_id,
                "verdict": "pass",
                "evidence_status": "supported",
                "reason_codes": [],
                "explanation": "候选与当前回答或已绑定问题一致。",
            }
            for item in hard_result.candidates
        ]
        has_severity = any(
            item.link_id == "nausea-severity" for item in hard_result.candidates
        )
        return SafetyCriticOutcome.model_validate(
            {
                "decision": {
                    "overall_verdict": "pass" if has_severity else "revise",
                    "candidate_reviews": reviews,
                    "missing_items": (
                        []
                        if has_severity
                        else [
                            {
                                "link_id": "nausea-severity",
                                "status": "ambiguous",
                                "evidence_text": "恶心",
                                "reason_codes": ["severity_missing"],
                                "explanation": "已明确恶心，但程度尚未回答。",
                            }
                        ]
                    ),
                },
                "mode": "synthetic_context_critic",
                "prompt_version": "test-context-v1",
                "latency_ms": 0,
            }
        )


def test_short_value_answers_one_pending_question_with_traceable_binding(tmp_path):
    store = SQLiteStore(tmp_path / "context-answer.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    service = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        safety_critic=_MissingSeverityCritic(),
        patient_timezone="Asia/Shanghai",
    )
    first = service.analyze(
        session.session_id,
        "我现在有恶心。",
        received_at="2026-08-02T01:00:00+00:00",
    )
    nausea = next(
        item for item in first.result.candidates if item.link_id == "nausea-present"
    )
    severity_question = next(
        item
        for item in first.result.clarifications
        if item.target_link_id == "nausea-severity"
    )
    service.confirm_candidates(first.result.run_id, [nausea.candidate_id])

    second = service.analyze(
        session.session_id,
        "轻度",
        received_at="2026-08-02T01:02:00+00:00",
    )

    assert second.result.status == SemanticStatus.NEEDS_CONFIRMATION
    severity = second.result.candidates[0]
    assert severity.link_id == "nausea-severity"
    assert severity.answer == "LA6752-5"
    assert severity.evidence_text == "轻度"
    assert severity.context_binding.source_action_id == (
        severity_question.clarification_id
    )
    assert severity.effective_time.basis.value == "pending_question"
    replay = service.analyze(
        session.session_id,
        "轻度",
        received_at="2026-08-02T01:02:00+00:00",
    )
    assert replay.idempotent_replay is True
    assert replay.result == second.result
    service.confirm_candidates(second.result.run_id, [severity.candidate_id])
    assert store.get_care_session(session.session_id).answers[
        "nausea-severity"
    ] == "LA6752-5"

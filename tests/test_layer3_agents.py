from __future__ import annotations

import json
from pathlib import Path

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
from continucare.demo_data import DEMO_PATIENT_ID


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

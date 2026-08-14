from __future__ import annotations

from datetime import datetime, timedelta

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.agents.contracts import SemanticStatus
from continucare.care_agent import CareAgentService
from continucare.care_agent.model_api import (
    SemanticModelConfig,
    UnconfiguredModelAdapter,
)
from continucare.care_engine import CareEngine
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.terminology import RepositoryTerminologyBackend


def _service(tmp_path):
    store = SQLiteStore(tmp_path / "terminology-flow.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    service = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        patient_timezone="Asia/Shanghai",
    )
    return store, engine, session, service


def test_catalog_is_versioned_and_head_dizziness_requires_disambiguation():
    backend = RepositoryTerminologyBackend()

    assert len(backend.catalog.concepts) >= 40
    assert backend.catalog.code_system_release.endswith("/version/20250201")
    matches = backend.search("头晕")

    assert {item.concept_id for item in matches} == {
        "dizziness",
        "vertigo",
        "postural-dizziness",
        "lightheadedness",
    }
    assert all(item.validation_status == "repository_release_validated" for item in matches)
    value_set = backend.catalog.as_fhir_value_set()
    assert value_set["resourceType"] == "ValueSet"
    assert len(value_set["compose"]["include"][0]["concept"]) == len(
        backend.catalog.concepts
    )


def test_governed_questionnaire_candidate_also_has_repository_match(tmp_path):
    _, _, session, service = _service(tmp_path)

    interaction = service.analyze(
        session.session_id,
        "我今天现在有点恶心。",
        received_at="2026-08-02T01:00:00+00:00",
    )

    nausea = next(
        item for item in interaction.result.candidates if item.link_id == "nausea-present"
    )
    assert nausea.terminology_match is not None
    assert nausea.terminology_match.catalog_version == "cn-glp1-l1-v1.0.3"
    assert nausea.terminology_match.coding.code == "422587007"
    assert nausea.terminology_match.target_coding.code == "422587007"


def test_new_symptom_is_retained_as_raw_evidence_without_dynamic_observation(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id,
        "我今天拉肚子。",
        received_at="2026-08-02T01:00:00+00:00",
    )

    assert interaction.result.candidates == []
    assert store.list_active_symptom_reports(session.session_id) == []
    assert store.get_agent_run(interaction.result.run_id).input_text == "我今天拉肚子。"
    assert interaction.result.status == SemanticStatus.NO_MATCH


def test_legacy_ambiguous_symptom_is_not_exposed_by_cn_whitelist(tmp_path):
    store, _, session, service = _service(tmp_path)
    interaction = service.analyze(
        session.session_id,
        "我今天有点头晕。",
        received_at="2026-08-02T01:00:00+00:00",
    )

    assert interaction.result.candidates == []
    assert interaction.result.clarifications == []
    assert store.list_active_symptom_reports(session.session_id) == []
    assert store.get_agent_run(interaction.result.run_id).input_text == "我今天有点头晕。"


def test_short_term_memory_is_daily_session_not_five_turns_and_completed_data_is_long_term(
    tmp_path,
):
    store, engine, session, service = _service(tmp_path)
    latest = None
    for index in range(7):
        latest = service.analyze(
            session.session_id,
            f"今天只是第{index}条普通说明。",
            received_at=f"2026-08-02T01:0{index}:00+00:00",
        )
    assert latest is not None
    assert latest.task.conversation_context.memory_scope == "daily_followup_session"
    assert len(latest.task.conversation_context.recent_turns) == 6

    symptom = service.analyze(
        session.session_id,
        "我今天现在有点恶心。",
        received_at="2026-08-02T01:10:00+00:00",
    )
    candidate = next(
        item for item in symptom.result.candidates if item.link_id == "nausea-present"
    )
    service.confirm_candidates(symptom.result.run_id, [candidate.candidate_id])
    engine.complete(
        session.session_id,
        store.get_care_session(session.session_id).answers,
    )

    new_session = engine.start_or_resume(DEMO_PATIENT_ID)
    next_day = service.analyze(
        new_session.session_id,
        "今天感觉还可以。",
        received_at="2026-08-03T01:00:00+00:00",
    )
    assert next_day.result.status == SemanticStatus.NO_MATCH
    remembered = {
        item.code: item for item in next_day.task.long_term_memory
    }
    assert remembered["422587007"].source_kind == "pathway_monitored"
    assert next_day.task.conversation_context.recent_turns == []


def test_patient_page_daily_guard_reuses_completed_session_until_next_local_date(
    tmp_path,
):
    store, engine, session, service = _service(tmp_path)
    symptom = service.analyze(
        session.session_id,
        "我今天现在有点恶心。",
        received_at="2026-08-02T01:00:00+00:00",
    )
    candidate = next(
        item for item in symptom.result.candidates if item.link_id == "nausea-present"
    )
    service.confirm_candidates(
        symptom.result.run_id,
        [candidate.candidate_id],
    )
    completed = engine.complete(
        session.session_id,
        store.get_care_session(session.session_id).answers,
    ).session
    same_day = engine.start_or_resume(
        DEMO_PATIENT_ID,
        allow_repeat_same_day=False,
        patient_timezone="Asia/Shanghai",
        now=completed.completed_at,
    )
    assert same_day.session_id == completed.session_id
    assert same_day.status.value == "completed"

    following_day = (
        datetime.fromisoformat(completed.completed_at) + timedelta(days=1)
    ).isoformat()
    next_session = engine.start_or_resume(
        DEMO_PATIENT_ID,
        allow_repeat_same_day=False,
        patient_timezone="Asia/Shanghai",
        now=following_day,
    )
    assert next_session.session_id != completed.session_id
    assert next_session.status.value == "in_progress"

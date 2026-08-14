from __future__ import annotations

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_engine import CareEngine
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.fhir.questionnaires import build_questionnaire_response
from continucare.fhir.r4 import FHIRValidationError
from continucare.fhir.terminology import UCUM
from continucare.models import CareSessionStatus, ConfidenceTier
from continucare.pathways import load_glp1_questionnaire


def complete_answers() -> dict:
    return {
        "nausea-present": True,
        "nausea-severity": "LA6752-5",
        "vomiting-count-24h": 1,
        "fluid-intake-24h-estimated": {
            "value": 800,
            "unit": "mL",
            "system": UCUM,
            "code": "mL",
        },
        "abdominal-pain-present": False,
        "free-text-report": "今天是合成随访。",
    }


def test_generic_builder_emits_questionnaire_defined_answer_types():
    response = build_questionnaire_response(
        questionnaire=load_glp1_questionnaire(),
        response_id="response-builder-test",
        patient_id=DEMO_PATIENT_ID,
        authored="2026-08-01T10:00:00+00:00",
        answers=complete_answers(),
    )
    items = {item["linkId"]: item for item in response["item"]}

    assert items["nausea-present"]["answer"] == [{"valueBoolean": True}]
    assert (
        items["nausea-severity"]["answer"][0]["valueCoding"]["code"]
        == "LA6752-5"
    )
    assert items["vomiting-count-24h"]["answer"] == [{"valueInteger": 1}]
    assert (
        items["fluid-intake-24h-estimated"]["answer"][0]["valueQuantity"]["code"]
        == "mL"
    )


def test_builder_rejects_hidden_answer_and_unknown_choice():
    questionnaire = load_glp1_questionnaire()
    with pytest.raises(FHIRValidationError, match="disabled Questionnaire item"):
        build_questionnaire_response(
            questionnaire=questionnaire,
            response_id="response-hidden",
            patient_id=DEMO_PATIENT_ID,
            authored="2026-08-01T10:00:00+00:00",
            answers={"nausea-present": False, "nausea-severity": "LA6752-5"},
        )

    with pytest.raises(FHIRValidationError, match="not an allowed option"):
        build_questionnaire_response(
            questionnaire=questionnaire,
            response_id="response-invalid-choice",
            patient_id=DEMO_PATIENT_ID,
            authored="2026-08-01T10:00:00+00:00",
            answers={"nausea-present": True, "nausea-severity": "invented"},
        )


def test_completed_builder_enforces_required_but_draft_can_be_partial():
    questionnaire = load_glp1_questionnaire()
    questionnaire["item"][0]["required"] = True

    draft = build_questionnaire_response(
        questionnaire=questionnaire,
        response_id="draft-required",
        patient_id=DEMO_PATIENT_ID,
        authored="2026-08-01T10:00:00+00:00",
        answers={},
        status="in-progress",
    )
    assert draft["status"] == "in-progress"

    with pytest.raises(FHIRValidationError, match="required Questionnaire item"):
        build_questionnaire_response(
            questionnaire=questionnaire,
            response_id="response-required",
            patient_id=DEMO_PATIENT_ID,
            authored="2026-08-01T10:00:00+00:00",
            answers={"free-text-report": "合成内容"},
        )


def test_session_draft_is_resumable_and_version_locked(tmp_path):
    store = SQLiteStore(tmp_path / "care-session.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)

    engine.save_draft(session.session_id, {"nausea-present": False})
    resumed = CareEngine(SQLiteStore(store.db_path)).start_or_resume(DEMO_PATIENT_ID)

    assert resumed.session_id == session.session_id
    assert resumed.answers == {"nausea-present": False}
    assert resumed.pathway_version == "1.0.0"
    assert resumed.questionnaire_version == "1.0.0"
    assert resumed.status == CareSessionStatus.IN_PROGRESS


def test_complete_session_persists_response_and_deterministic_observations(tmp_path):
    store = SQLiteStore(tmp_path / "care-complete.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    answers = complete_answers()

    result = engine.complete(session.session_id, answers)
    reopened = SQLiteStore(store.db_path)

    assert result.session.status == CareSessionStatus.COMPLETED
    assert result.questionnaire_response["status"] == "completed"
    assert {item.code for item in result.observations} == {
        "422587007",
        "81660-3",
        "94070-0",
        "75301-2",
    }
    assert all(
        item.confidence_tier == ConfidenceTier.PATIENT_CONFIRMED
        for item in result.observations
    )
    assert all(
        item.resource["derivedFrom"][0]["reference"]
        == f"QuestionnaireResponse/{result.questionnaire_response['id']}"
        for item in result.observations
    )
    assert len(
        reopened.list_observations_for_message(result.questionnaire_response["id"])
    ) == 4
    assert reopened.list_alerts() == []

    repeated = engine.complete(session.session_id, answers)
    assert repeated.questionnaire_response["id"] == result.questionnaire_response["id"]
    assert len(reopened.list_messages(DEMO_PATIENT_ID)) == 1

    with pytest.raises(ValueError, match="不同答案"):
        engine.complete(session.session_id, {"nausea-present": False})


def test_free_text_and_negative_answers_do_not_invent_observations(tmp_path):
    store = SQLiteStore(tmp_path / "care-no-inference.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)

    result = engine.complete(
        session.session_id,
        {
            "nausea-present": False,
            "abdominal-pain-present": False,
            "free-text-report": "喝水比较少，但没有明确数值（合成）。",
        },
    )

    assert result.observations == []
    assert "喝水比较少" in store.get_message(result.questionnaire_response["id"]).message_text
    assert store.list_alerts() == []


def test_stopped_session_cannot_be_submitted(tmp_path):
    store = SQLiteStore(tmp_path / "care-stop.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)

    stopped = engine.stop(session.session_id)

    assert stopped.status == CareSessionStatus.STOPPED
    with pytest.raises(ValueError, match="只有进行中的"):
        engine.complete(session.session_id, {"nausea-present": True})

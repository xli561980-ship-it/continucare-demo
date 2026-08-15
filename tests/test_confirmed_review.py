from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_agent import CareAgentService
from continucare.care_agent.model_api import SemanticModelConfig, UnconfiguredModelAdapter
from continucare.care_engine import CareEngine
from continucare.db import connect
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.layer4.fhir import (
    build_patient_confirmed_review_task,
    validate_layer4_fhir_resource,
)
from continucare.layer4.manual_reviews import (
    MANUAL_REVIEW_IDENTIFIER_SYSTEM,
    ManualReviewQueue,
    admit_final_patient_report,
    is_clinical_rule_task,
)
from continucare.layer4.storage import Layer4SQLiteStore
from continucare.pathways import load_builtin_pathways
from continucare.services.confirmed_review import ConfirmedReviewService
from continucare.services.demo_scenarios import load_manual_review_scenario


CLINICAL_TABLES = (
    "confirmed_answer_contexts",
    "confirmed_symptom_reports",
    "followup_messages",
    "fhir_questionnaire_responses",
    "fhir_observations",
    "observation_evidence",
    "layer4_fhir_resources",
)


def _counts(db_path):
    with connect(db_path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in CLINICAL_TABLES
        }


def _services(db_path, store_type=SQLiteStore):
    interaction = load_manual_review_scenario(db_path)
    store = store_type(db_path)
    engine = CareEngine(store)
    agent = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        patient_timezone="Asia/Shanghai",
    )
    review = ConfirmedReviewService(store, care_agent=agent, care_engine=engine)
    candidate_ids = [item.candidate_id for item in interaction.result.candidates]
    return interaction, store, engine, agent, review, candidate_ids


def test_one_click_analysis_has_no_released_clinical_resource(tmp_path):
    interaction = load_manual_review_scenario(tmp_path / "analysis.db")

    assert len(interaction.result.candidates) == 1
    assert interaction.result.candidates[0].link_id.endswith("::diarrhea")
    assert _counts(tmp_path / "analysis.db") == {
        table: 0 for table in CLINICAL_TABLES
    }


def test_patient_confirmation_atomically_creates_complete_evidence_and_manual_task(
    tmp_path,
):
    db_path = tmp_path / "accepted.db"
    interaction, store, _, _, review, candidate_ids = _services(db_path)

    outcome = review.accept_all(interaction.result.run_id, candidate_ids)
    replay = review.accept_all(interaction.result.run_id, candidate_ids)

    assert outcome.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.task["id"] == outcome.task["id"]
    assert outcome.questionnaire_response["status"] == "completed"
    assert len(outcome.observations) == 1
    assert outcome.observations[0].resource["status"] == "final"
    assert outcome.observations[0].code == "62315008"
    assert outcome.task["priority"] == "routine"
    assert outcome.task["description"] == "人工复核患者已确认报告"
    assert "severity" not in outcome.task
    assert "ClinicalRule" not in json.dumps(outcome.task, ensure_ascii=False)
    assert interaction.result.run_id not in json.dumps(outcome.task)
    assert interaction.result.run_id not in json.dumps(outcome.provenance)
    assert all(
        candidate_id not in json.dumps(outcome.task)
        for candidate_id in candidate_ids
    )
    assert all(
        candidate_id not in json.dumps(outcome.provenance)
        for candidate_id in candidate_ids
    )
    identifier = outcome.task["identifier"][0]
    assert identifier["system"] == MANUAL_REVIEW_IDENTIFIER_SYSTEM
    assert len(identifier["value"]) == 64
    assert is_clinical_rule_task(outcome.task) is False
    assert len(outcome.provenance["agent"]) == 2
    assert {item["who"]["reference"] for item in outcome.provenance["agent"]} == {
        f"Patient/{DEMO_PATIENT_ID}",
        "Device/continucare-deterministic-assembler",
    }
    assert f"Task/{outcome.task['id']}" in {
        item["reference"] for item in outcome.provenance["target"]
    }
    assert len(ManualReviewQueue(Layer4SQLiteStore(db_path)).list_for_patient(
        DEMO_PATIENT_ID
    )) == 1
    assert store.get_care_session(interaction.record.session_id).status.value == "completed"
    assert load_builtin_pathways().get("GLP1-14D").clinical_rules == []


@pytest.mark.parametrize("decision", ["rejected", "unsure", "cancelled"])
def test_non_acceptance_never_creates_clinical_resources_or_task(tmp_path, decision):
    db_path = tmp_path / f"{decision}.db"
    interaction, _, engine, agent, _, candidate_ids = _services(db_path)

    if decision == "rejected":
        agent.reject_candidates(interaction.result.run_id, candidate_ids)
    elif decision == "unsure":
        agent.mark_candidates_unsure(interaction.result.run_id, candidate_ids)
    else:
        engine.stop(interaction.record.session_id)

    assert _counts(db_path) == {table: 0 for table in CLINICAL_TABLES}


class _FailAfterLayer4Store(SQLiteStore):
    def _confirmed_review_fault(self, stage: str) -> None:
        if stage == "after_layer4_insert":
            raise RuntimeError("injected failure after task insert")


def test_failure_after_task_insert_rolls_back_entire_bundle(tmp_path):
    db_path = tmp_path / "rollback.db"
    interaction, _, _, _, review, candidate_ids = _services(
        db_path, _FailAfterLayer4Store
    )
    before = _counts(db_path)

    with pytest.raises(RuntimeError, match="injected failure"):
        review.accept_all(interaction.result.run_id, candidate_ids)

    assert _counts(db_path) == before
    with connect(db_path) as connection:
        status = connection.execute(
            "SELECT status FROM care_sessions WHERE session_id = ?",
            (interaction.record.session_id,),
        ).fetchone()[0]
        decisions = connection.execute(
            "SELECT COUNT(*) FROM conversation_action_resolutions"
        ).fetchone()[0]
    assert status == "in_progress"
    assert decisions == 0


def test_unsure_can_only_create_task_after_a_later_explicit_acceptance(tmp_path):
    db_path = tmp_path / "unsure-then-accepted.db"
    interaction, _, _, agent, review, candidate_ids = _services(db_path)
    agent.mark_candidates_unsure(interaction.result.run_id, candidate_ids)

    assert _counts(db_path) == {table: 0 for table in CLINICAL_TABLES}

    outcome = review.accept_all(interaction.result.run_id, candidate_ids)

    assert outcome.task["status"] == "requested"
    with connect(db_path) as connection:
        decision = connection.execute(
            "SELECT decision FROM conversation_action_resolutions"
        ).fetchone()[0]
    assert decision == "accepted"


def test_two_concurrent_accepts_create_one_resource_set(tmp_path):
    db_path = tmp_path / "concurrent.db"
    interaction, _, _, _, _, candidate_ids = _services(db_path)

    def accept():
        store = SQLiteStore(db_path)
        engine = CareEngine(store)
        agent = CareAgentService(
            store,
            care_engine=engine,
            model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
            patient_timezone="Asia/Shanghai",
        )
        return ConfirmedReviewService(
            store, care_agent=agent, care_engine=engine
        ).accept_all(interaction.result.run_id, candidate_ids)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: accept(), range(2)))

    assert {item.task["id"] for item in results} == {results[0].task["id"]}
    counts = _counts(db_path)
    assert counts["fhir_questionnaire_responses"] == 1
    assert counts["fhir_observations"] == 1
    assert counts["layer4_fhir_resources"] == 2


def test_admission_rejects_nonfinal_or_mismatched_evidence(tmp_path):
    interaction, _, _, _, review, candidate_ids = _services(tmp_path / "admit.db")
    outcome = review.accept_all(interaction.result.run_id, candidate_ids)
    invalid = outcome.observations[0].as_fhir()
    invalid["status"] = "preliminary"

    with pytest.raises(ValueError, match="final Observation"):
        admit_final_patient_report(
            patient_id=DEMO_PATIENT_ID,
            questionnaire_response=outcome.questionnaire_response,
            observations=[invalid],
        )


def test_manual_task_builder_is_strict_fhir_and_rule_free():
    task = build_patient_confirmed_review_task(
        patient_id=DEMO_PATIENT_ID,
        receipt_digest="a" * 64,
        questionnaire_response_reference="QuestionnaireResponse/r1",
        observation_references=["Observation/o1"],
        pathway_reference="urn:continucare:pathway:GLP1-14D|1.0.0",
        authored_on="2026-08-13T10:00:00+00:00",
        task_id="task-manual-review-a",
    )

    assert validate_layer4_fhir_resource(task) == task
    assert is_clinical_rule_task(task) is False


def test_one_click_loader_ignores_ambient_model_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTINUCARE_LLM_PROVIDER", "xiaomi_mimo")
    monkeypatch.setenv("CONTINUCARE_LLM_MODEL", "configured-model")
    monkeypatch.setenv("CONTINUCARE_LLM_BASE_URL", "https://invalid.example")
    monkeypatch.setenv("CONTINUCARE_LLM_API_KEY", "synthetic-test-key")

    interaction = load_manual_review_scenario(tmp_path / "offline.db")

    assert interaction.result.mode == "local_semantic_mock"
    assert interaction.record.model_provider is None

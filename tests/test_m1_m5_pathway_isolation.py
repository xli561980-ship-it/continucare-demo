from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_agent import CareAgentService
from continucare.care_agent.model_api import SemanticModelConfig, UnconfiguredModelAdapter
from continucare.db import connect
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.fhir.observations import (
    build_patient_reported_observation,
    per_day_quantity,
)
from continucare.fhir.questionnaires import build_free_text_questionnaire_response
from continucare.fhir.terminology import VOMITING_COUNT_24H
from continucare.pathways.fhir_artifacts import load_glp1_questionnaire
from continucare.layer4 import (
    ClinicalMemoryService,
    ClinicalStateService,
    ControlledSummaryService,
    DoctorWorkbenchService,
    EvidenceSummaryService,
    Layer4InputReader,
    Layer4SQLiteStore,
    StateMetricDefinition,
    UnconfiguredSummaryModelAdapter,
    WorkbenchAccessContext,
    WorkbenchPurpose,
    WorkbenchRole,
)
from continucare.models import (
    AuditEvent,
    CareSession,
    CareSessionStatus,
    ConfidenceTier,
    FollowUpMessage,
    Observation,
    ObservationEvidence,
)


PATHWAY_A = ("GLP1-14D", "1.0.0")
PATHWAY_B = ("SYNTHETIC-B", "2.0.0")
AUTHORED_A = "2026-08-02T10:00:00+00:00"
AUTHORED_B = "2026-08-02T10:05:00+00:00"
AS_OF = "2026-08-02T12:00:00+00:00"
GENERATED = "2026-08-02T12:01:00+00:00"
UCUM = "http://unitsofmeasure.org"


@dataclass
class ScopedProjection:
    snapshot: object
    memory: ClinicalMemoryService
    state: object
    evidence_summary: object
    controlled_summary: object
    workbench: object


def _message(response_id: str, authored: str) -> FollowUpMessage:
    return FollowUpMessage(
        message_id=response_id,
        patient_id=DEMO_PATIENT_ID,
        message_text=f"Synthetic report for {response_id}.",
        submitted_at=authored,
        processing_status="processed",
    )


def _observation(
    observation_id: str, response_id: str, authored: str, value: int
) -> Observation:
    resource = build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=DEMO_PATIENT_ID,
        questionnaire_response_id=response_id,
        effective_time=authored,
        code=VOMITING_COUNT_24H,
        value_element="valueQuantity",
        value=per_day_quantity(value, unit="vomiting episodes/24 hours"),
    )
    return Observation(
        resource=resource,
        evidence=ObservationEvidence(
            questionnaire_response_id=response_id,
            confidence_tier=ConfidenceTier.PATIENT_CONFIRMED,
            evidence_text=str(value),
            evidence_start=0,
            evidence_end=1,
            recorded_at=authored,
        ),
    )


def _persist_pathway_bundle(
    store: SQLiteStore,
    *,
    pathway: tuple[str, str],
    session_id: str,
    response_id: str,
    observation_id: str,
    authored: str,
    value: int,
) -> Observation:
    code, version = pathway
    questionnaire = load_glp1_questionnaire()
    response = build_free_text_questionnaire_response(
        response_id=response_id,
        patient_id=DEMO_PATIENT_ID,
        authored=authored,
        text=f"Synthetic response for {code}|{version}.",
    )
    observation = _observation(observation_id, response_id, authored, value)
    session = CareSession(
        session_id=session_id,
        patient_id=DEMO_PATIENT_ID,
        pathway_code=code,
        pathway_version=version,
        questionnaire_canonical=questionnaire["url"],
        questionnaire_version=questionnaire["version"],
        status=CareSessionStatus.IN_PROGRESS,
        created_at=authored,
        updated_at=authored,
    )
    store.save_care_session(session)
    store.complete_care_session_submission(
        session=session,
        message=_message(response_id, authored),
        questionnaire_response=response,
        questionnaire=questionnaire,
        observations=[observation],
        completed_at=authored,
    )
    return observation


def _reader(store: SQLiteStore, pathway: tuple[str, str]):
    return Layer4InputReader(store).read(
        DEMO_PATIENT_ID,
        pathway_code=pathway[0],
        pathway_version=pathway[1],
        assembled_at=AS_OF,
    )


def _metric(pathway: tuple[str, str]) -> StateMetricDefinition:
    return StateMetricDefinition(
        metric_id="vomiting-count",
        version="1.0.0",
        pathway_code=pathway[0],
        pathway_version=pathway[1],
        code_system=VOMITING_COUNT_24H.system,
        code=VOMITING_COUNT_24H.code,
        display="Vomiting count",
        unit="/d",
        unit_system=UCUM,
        lookback_hours=48,
        stale_after_hours=48,
        trend_window_hours=48,
    )


def _project(
    store: SQLiteStore, repository: Layer4SQLiteStore, pathway: tuple[str, str]
) -> ScopedProjection:
    reader = Layer4InputReader(store)
    snapshot = reader.read(
        DEMO_PATIENT_ID,
        pathway_code=pathway[0],
        pathway_version=pathway[1],
        assembled_at=AS_OF,
    )
    memory = ClinicalMemoryService(
        reader,
        repository,
        pathway_code=pathway[0],
        pathway_version=pathway[1],
    )
    memory.rebuild(DEMO_PATIENT_ID)
    state = ClinicalStateService(
        reader,
        repository,
        pathway_code=pathway[0],
        pathway_version=pathway[1],
    ).build(
        patient_id=DEMO_PATIENT_ID,
        definitions=[_metric(pathway)],
        as_of=AS_OF,
    )
    evidence_summary = EvidenceSummaryService(memory, repository).generate(
        patient_id=DEMO_PATIENT_ID,
        period_start="2026-08-02T00:00:00+00:00",
        period_end=AS_OF,
        generated_at=GENERATED,
    )
    controlled_summary = ControlledSummaryService(
        memory,
        repository,
        model_adapter=UnconfiguredSummaryModelAdapter(SemanticModelConfig()),
    ).generate(
        patient_id=DEMO_PATIENT_ID,
        period_start="2026-08-02T00:00:00+00:00",
        period_end=AS_OF,
        generated_at="2026-08-02T12:02:00+00:00",
    ).summary
    workbench = DoctorWorkbenchService(
        reader,
        repository,
        pathway_code=pathway[0],
        pathway_version=pathway[1],
    ).query(
        patient_id=DEMO_PATIENT_ID,
        access=WorkbenchAccessContext(
            actor_reference="Practitioner/synthetic-doctor",
            role=WorkbenchRole.DOCTOR,
            purpose=WorkbenchPurpose.TREATMENT,
            permitted_patient_ids=[DEMO_PATIENT_ID],
            identity_verified=True,
        ),
        as_of="2026-08-02T12:03:00+00:00",
        generated_at="2026-08-02T12:03:00+00:00",
    )
    return ScopedProjection(
        snapshot=snapshot,
        memory=memory,
        state=state,
        evidence_summary=evidence_summary,
        controlled_summary=controlled_summary,
        workbench=workbench,
    )


def _db_digest(path) -> tuple[str, int, int]:
    data = path.read_bytes()
    stat = path.stat()
    return hashlib.sha256(data).hexdigest(), stat.st_size, stat.st_mtime_ns


def test_patient_pathway_and_version_projections_are_fully_isolated(tmp_path):
    db_path = tmp_path / "pathway-isolation.db"
    store = SQLiteStore(db_path)
    observation_a = _persist_pathway_bundle(
        store,
        pathway=PATHWAY_A,
        session_id="session-pathway-a",
        response_id="response-pathway-a",
        observation_id="observation-pathway-a",
        authored=AUTHORED_A,
        value=1,
    )
    observation_b = _persist_pathway_bundle(
        store,
        pathway=PATHWAY_B,
        session_id="session-pathway-b",
        response_id="response-pathway-b",
        observation_id="observation-pathway-b",
        authored=AUTHORED_B,
        value=2,
    )
    repository = Layer4SQLiteStore(db_path)
    store.append_audit_event(
        AuditEvent(
            event_id="audit-pathway-a",
            patient_id=DEMO_PATIENT_ID,
            entity_type="CareSession",
            entity_id="session-pathway-a",
            event_type="synthetic_pathway_a",
            actor_type="test",
            details_json={},
            created_at=AUTHORED_A,
        )
    )
    store.append_audit_event(
        AuditEvent(
            event_id="audit-pathway-b-cross-link",
            patient_id=DEMO_PATIENT_ID,
            entity_type="CareSession",
            entity_id="session-pathway-b",
            event_type="synthetic_cross_link",
            actor_type="test",
            details_json={"unrelated_reference": "CareSession/session-pathway-a"},
            created_at="2026-08-02T10:06:00+00:00",
        )
    )
    store.append_audit_event(
        AuditEvent(
            event_id="audit-pathway-b",
            patient_id=DEMO_PATIENT_ID,
            entity_type="CareSession",
            entity_id="session-pathway-b",
            event_type="synthetic_pathway_b",
            actor_type="test",
            details_json={},
            created_at=AUTHORED_B,
        )
    )

    projected_a = _project(store, repository, PATHWAY_A)
    projected_b = _project(store, repository, PATHWAY_B)

    assert {item["id"] for item in projected_a.snapshot.questionnaire_responses} == {
        "response-pathway-a"
    }
    assert {item["id"] for item in projected_b.snapshot.questionnaire_responses} == {
        "response-pathway-b"
    }
    assert {item["id"] for item in projected_a.snapshot.observations} == {
        observation_a.observation_id
    }
    assert {item["id"] for item in projected_b.snapshot.observations} == {
        observation_b.observation_id
    }
    assert {item.event_id for item in projected_a.snapshot.audit_events} == {
        "audit-pathway-a"
    }
    assert {item.event_id for item in projected_b.snapshot.audit_events} == {
        "audit-pathway-b",
        "audit-pathway-b-cross-link",
    }
    assert {
        item.source.reference for item in projected_a.memory.list_memory(DEMO_PATIENT_ID)
    } == {
        "QuestionnaireResponse/response-pathway-a",
        "Observation/observation-pathway-a",
        "AuditEvent/audit-pathway-a",
    }
    assert {
        item.source.reference for item in projected_b.memory.list_memory(DEMO_PATIENT_ID)
    } == {
        "QuestionnaireResponse/response-pathway-b",
        "Observation/observation-pathway-b",
        "AuditEvent/audit-pathway-b",
        "AuditEvent/audit-pathway-b-cross-link",
    }
    assert {
        item.reference for item in projected_a.state.source_observation_refs
    } == {"Observation/observation-pathway-a"}
    assert {
        item.reference for item in projected_b.state.source_observation_refs
    } == {"Observation/observation-pathway-b"}
    assert projected_a.evidence_summary.summary_id != projected_b.evidence_summary.summary_id
    assert projected_a.evidence_summary.summary_id.startswith("summary-v2-")
    assert projected_b.evidence_summary.summary_id.startswith("summary-v2-")
    assert projected_a.evidence_summary.version == "1"
    assert projected_b.evidence_summary.version == "1"
    assert projected_a.controlled_summary.summary_id == projected_a.evidence_summary.summary_id
    assert projected_b.controlled_summary.summary_id == projected_b.evidence_summary.summary_id
    assert all(
        item.pathway_code == PATHWAY_A[0]
        and item.pathway_version == PATHWAY_A[1]
        for item in projected_a.workbench.timeline
    )
    assert all(
        item.pathway_code == PATHWAY_B[0]
        and item.pathway_version == PATHWAY_B[1]
        for item in projected_b.workbench.timeline
    )
    assert projected_a.workbench.tasks == projected_b.workbench.tasks == []


def test_same_pathway_code_different_versions_are_isolated(tmp_path):
    db_path = tmp_path / "pathway-version-isolation.db"
    store = SQLiteStore(db_path)
    _persist_pathway_bundle(
        store,
        pathway=PATHWAY_A,
        session_id="session-version-1",
        response_id="response-version-1",
        observation_id="observation-version-1",
        authored=AUTHORED_A,
        value=1,
    )
    pathway_v2 = (PATHWAY_A[0], "2.0.0")
    _persist_pathway_bundle(
        store,
        pathway=pathway_v2,
        session_id="session-version-2",
        response_id="response-version-2",
        observation_id="observation-version-2",
        authored=AUTHORED_B,
        value=2,
    )

    version_1 = _reader(store, PATHWAY_A)
    version_2 = _reader(store, pathway_v2)
    repository = Layer4SQLiteStore(db_path)
    projected_1 = _project(store, repository, PATHWAY_A)
    projected_2 = _project(store, repository, pathway_v2)

    assert {item["id"] for item in version_1.observations} == {
        "observation-version-1"
    }
    assert {item["id"] for item in version_2.observations} == {
        "observation-version-2"
    }
    assert projected_1.evidence_summary.summary_id != (
        projected_2.evidence_summary.summary_id
    )
    assert {
        item.source.reference
        for item in projected_1.memory.list_memory(DEMO_PATIENT_ID)
    } == {
        "QuestionnaireResponse/response-version-1",
        "Observation/observation-version-1",
    }
    assert {
        item.source.reference
        for item in projected_2.memory.list_memory(DEMO_PATIENT_ID)
    } == {
        "QuestionnaireResponse/response-version-2",
        "Observation/observation-version-2",
    }


def test_layer3_long_term_history_is_pathway_scoped(tmp_path):
    db_path = tmp_path / "layer3-history.db"
    store = SQLiteStore(db_path)
    _persist_pathway_bundle(
        store,
        pathway=PATHWAY_A,
        session_id="session-history-a-completed",
        response_id="response-history-a",
        observation_id="observation-history-a",
        authored=AUTHORED_A,
        value=1,
    )
    _persist_pathway_bundle(
        store,
        pathway=PATHWAY_B,
        session_id="session-history-b-completed",
        response_id="response-history-b",
        observation_id="observation-history-b",
        authored=AUTHORED_B,
        value=2,
    )
    active = CareSession(
        session_id="session-history-a-active",
        patient_id=DEMO_PATIENT_ID,
        pathway_code=PATHWAY_A[0],
        pathway_version=PATHWAY_A[1],
        questionnaire_canonical=load_glp1_questionnaire()["url"],
        questionnaire_version=load_glp1_questionnaire()["version"],
        created_at="2026-08-03T10:00:00+00:00",
        updated_at="2026-08-03T10:00:00+00:00",
    )
    store.save_care_session(active)
    service = CareAgentService(
        store,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
    )

    interaction = service.analyze(active.session_id, "我今天情况稳定。")

    assert {item.observation_id for item in interaction.task.long_term_memory} == {
        "observation-history-a"
    }


def test_layer3_history_contamination_fails_before_agent_run_or_audit(tmp_path):
    db_path = tmp_path / "layer3-history-fail-closed.db"
    store = SQLiteStore(db_path)
    observation = _persist_pathway_bundle(
        store,
        pathway=PATHWAY_A,
        session_id="session-history-corrupt-completed",
        response_id="response-history-corrupt",
        observation_id="observation-history-corrupt",
        authored=AUTHORED_A,
        value=1,
    )
    _persist_pathway_bundle(
        store,
        pathway=PATHWAY_B,
        session_id="session-history-other-completed",
        response_id="response-history-other",
        observation_id="observation-history-other",
        authored=AUTHORED_B,
        value=2,
    )
    corrupted = observation.as_fhir()
    corrupted["derivedFrom"] = [
        {"reference": "QuestionnaireResponse/response-history-other"}
    ]
    with connect(db_path) as connection:
        connection.execute(
            "UPDATE fhir_observations SET resource_json=? WHERE observation_id=?",
            (
                json.dumps(corrupted, ensure_ascii=False, separators=(",", ":")),
                observation.observation_id,
            ),
        )
    questionnaire = load_glp1_questionnaire()
    active = CareSession(
        session_id="session-history-corrupt-active",
        patient_id=DEMO_PATIENT_ID,
        pathway_code=PATHWAY_A[0],
        pathway_version=PATHWAY_A[1],
        questionnaire_canonical=questionnaire["url"],
        questionnaire_version=questionnaire["version"],
        created_at="2026-08-03T10:00:00+00:00",
        updated_at="2026-08-03T10:00:00+00:00",
    )
    store.save_care_session(active)
    before_audits = len(store.list_audit_events(DEMO_PATIENT_ID))
    service = CareAgentService(
        store,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
    )

    with pytest.raises(ValueError, match="Observation"):
        service.analyze(active.session_id, "我今天情况稳定。")

    assert store.list_agent_runs(active.session_id) == []
    assert len(store.list_audit_events(DEMO_PATIENT_ID)) == before_audits


@pytest.mark.parametrize("failure", ["orphan", "cross_pathway", "ambiguous"])
def test_layer4_snapshot_rejects_invalid_derived_from_without_writes(tmp_path, failure):
    db_path = tmp_path / f"invalid-{failure}.db"
    store = SQLiteStore(db_path)
    observation_a = _persist_pathway_bundle(
        store,
        pathway=PATHWAY_A,
        session_id="session-invalid-a",
        response_id="response-invalid-a",
        observation_id="observation-invalid-a",
        authored=AUTHORED_A,
        value=1,
    )
    _persist_pathway_bundle(
        store,
        pathway=PATHWAY_B,
        session_id="session-invalid-b",
        response_id="response-invalid-b",
        observation_id="observation-invalid-b",
        authored=AUTHORED_B,
        value=2,
    )
    malformed = observation_a.as_fhir()
    if failure == "orphan":
        malformed["derivedFrom"] = [
            {"reference": "QuestionnaireResponse/response-orphan"}
        ]
    elif failure == "cross_pathway":
        malformed["derivedFrom"] = [
            {"reference": "QuestionnaireResponse/response-invalid-b"}
        ]
    else:
        malformed["derivedFrom"].append(
            {"reference": "QuestionnaireResponse/response-invalid-b"}
        )
    with connect(db_path) as connection:
        connection.execute(
            "UPDATE fhir_observations SET resource_json=? WHERE observation_id=?",
            (
                json.dumps(malformed, ensure_ascii=False, separators=(",", ":")),
                observation_a.observation_id,
            ),
        )
    before = _db_digest(db_path)

    with pytest.raises(ValueError, match="Observation"):
        _reader(store, PATHWAY_A)

    assert _db_digest(db_path) == before


def test_readers_are_database_side_effect_free(tmp_path):
    db_path = tmp_path / "read-only-invariants.db"
    store = SQLiteStore(db_path)
    _persist_pathway_bundle(
        store,
        pathway=PATHWAY_A,
        session_id="session-read-only",
        response_id="response-read-only",
        observation_id="observation-read-only",
        authored=AUTHORED_A,
        value=1,
    )
    repository = Layer4SQLiteStore(db_path, initialize=False)
    before = _db_digest(db_path)
    with connect(db_path) as connection:
        counts_before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "fhir_questionnaire_responses",
                "fhir_observations",
                "audit_events",
                "layer4_fhir_resources",
                "layer4_contract_records",
            )
        }

    snapshot = _reader(store, PATHWAY_A)
    DoctorWorkbenchService(
        Layer4InputReader(store),
        repository,
        pathway_code=PATHWAY_A[0],
        pathway_version=PATHWAY_A[1],
    ).query(
        patient_id=DEMO_PATIENT_ID,
        access=WorkbenchAccessContext(
            actor_reference="Practitioner/synthetic-doctor",
            role=WorkbenchRole.DOCTOR,
            purpose=WorkbenchPurpose.TREATMENT,
            permitted_patient_ids=[DEMO_PATIENT_ID],
            identity_verified=True,
        ),
        as_of=AS_OF,
        generated_at=AS_OF,
    )

    with connect(db_path) as connection:
        counts_after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        }
    assert snapshot.observations
    assert counts_after == counts_before
    assert _db_digest(db_path) == before

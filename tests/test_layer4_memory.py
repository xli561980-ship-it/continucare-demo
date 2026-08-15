from __future__ import annotations

from copy import deepcopy

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_engine import CareEngine
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.fhir.observations import (
    build_patient_reported_observation,
    per_day_quantity,
)
from continucare.fhir.questionnaires import build_free_text_questionnaire_response
from continucare.fhir.terminology import (
    BODY_WEIGHT,
    NAUSEA_FINDING,
    VOMITING_COUNT_24H,
)
from continucare.layer4 import Layer4InputSnapshot, Layer4SQLiteStore
from continucare.layer4.contracts import (
    MemoryEventKind,
    MissingDataExpectation,
    ResourceReference,
    RevisionRelationship,
    TimelineEventState,
)
from continucare.layer4.memory import ClinicalMemoryService
from continucare.layer4.fhir import build_communication, build_workflow_task
from continucare.layer4.inputs import Layer4InputReader
from continucare.models import AuditEvent


PATIENT_ID = "P-DEMO-001"


class MutableInputReader:
    def __init__(self, snapshot: Layer4InputSnapshot):
        self.snapshot = snapshot

    def read(
        self, patient_id: str, *, pathway_code: str, pathway_version: str
    ) -> Layer4InputSnapshot:
        assert patient_id == self.snapshot.patient_id
        assert pathway_code == self.snapshot.pathway_code
        assert pathway_version == self.snapshot.pathway_version
        return self.snapshot


def _response(response_id: str, authored: str) -> dict:
    return build_free_text_questionnaire_response(
        response_id=response_id,
        patient_id=PATIENT_ID,
        authored=authored,
        text="合成随访内容。",
    )


def _vomiting(
    observation_id: str, response_id: str, effective_time: str, value: int
) -> dict:
    return build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=PATIENT_ID,
        questionnaire_response_id=response_id,
        effective_time=effective_time,
        code=VOMITING_COUNT_24H,
        value_element="valueQuantity",
        value=per_day_quantity(value, unit="vomiting episodes/24 hours"),
        effective_period_hours=24,
    )


def _nausea(observation_id: str, response_id: str, effective_time: str) -> dict:
    return build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=PATIENT_ID,
        questionnaire_response_id=response_id,
        effective_time=effective_time,
        code=NAUSEA_FINDING,
        value_element="valueBoolean",
        value=True,
    )


def _snapshot(*, responses: list[dict], observations: list[dict]) -> Layer4InputSnapshot:
    return Layer4InputSnapshot(
        patient_id=PATIENT_ID,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
        questionnaire_responses=responses,
        observations=observations,
        audit_events=[
            AuditEvent(
                event_id="audit-memory-1",
                patient_id=PATIENT_ID,
                entity_type="CareSession",
                entity_id="session-1",
                event_type="questionnaire_completed",
                actor_type="care_engine",
                details_json={"synthetic_only": True},
                created_at="2026-08-02T10:01:00+00:00",
            )
        ],
        assembled_at="2026-08-02T12:00:00+00:00",
    )


def _service(tmp_path, snapshot: Layer4InputSnapshot):
    repository = Layer4SQLiteStore(tmp_path / "clinical-memory.db")
    reader = MutableInputReader(snapshot)
    service = ClinicalMemoryService(
        reader,
        repository,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
    )
    return service, repository, reader


def test_rebuild_is_idempotent_evidence_bound_and_hides_audit_by_default(tmp_path):
    response = _response("response-memory-1", "2026-08-02T10:00:00+00:00")
    observation = _vomiting(
        "observation-memory-1", response["id"], response["authored"], 2
    )
    service, repository, _ = _service(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )

    first = service.rebuild(PATIENT_ID)
    second = service.rebuild(PATIENT_ID)

    assert second == first
    assert len(repository.list_contracts("memory_event", patient_id=PATIENT_ID)) == 3
    assert len(repository.list_contracts("timeline_event", patient_id=PATIENT_ID)) == 3
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID, resource_type="Provenance"
        )
    ) == 3
    visible = service.list_timeline(PATIENT_ID)
    assert {item.kind for item in visible} == {
        MemoryEventKind.QUESTIONNAIRE_RESPONSE,
        MemoryEventKind.OBSERVATION,
    }
    observation_event = next(
        item for item in visible if item.kind == MemoryEventKind.OBSERVATION
    )
    assert {
        item.resource.reference for item in observation_event.evidence_refs
    } == {
        "Observation/observation-memory-1",
        "QuestionnaireResponse/response-memory-1",
    }
    assert service.list_timeline(PATIENT_ID, include_audit=True)[0].kind in {
        MemoryEventKind.AUDIT,
        MemoryEventKind.OBSERVATION,
    }


def test_late_arriving_data_is_reordered_by_clinical_effective_time(tmp_path):
    recent_response = _response("response-recent", "2026-08-02T10:00:00+00:00")
    recent = _vomiting("observation-recent", recent_response["id"], recent_response["authored"], 1)
    snapshot = _snapshot(responses=[recent_response], observations=[recent])
    service, _, reader = _service(tmp_path, snapshot)
    service.rebuild(PATIENT_ID)

    late_response = _response("response-late", "2026-07-20T09:00:00+00:00")
    late = _nausea("observation-late", late_response["id"], late_response["authored"])
    reader.snapshot = _snapshot(
        responses=[recent_response, late_response], observations=[recent, late]
    )
    service.rebuild(PATIENT_ID)

    observation_events = [
        item
        for item in service.list_timeline(PATIENT_ID)
        if item.kind == MemoryEventKind.OBSERVATION
    ]
    assert [item.source.reference for item in observation_events] == [
        "Observation/observation-recent",
        "Observation/observation-late",
    ]


def test_conflicting_values_create_separate_conflict_event_without_choosing_value(
    tmp_path,
):
    at = "2026-08-02T10:00:00+00:00"
    first_response = _response("response-conflict-1", at)
    second_response = _response("response-conflict-2", at)
    first = _vomiting("observation-conflict-1", first_response["id"], at, 1)
    second = _vomiting("observation-conflict-2", second_response["id"], at, 2)
    service, _, _ = _service(
        tmp_path,
        _snapshot(
            responses=[first_response, second_response], observations=[first, second]
        ),
    )

    result = service.rebuild(PATIENT_ID)

    assert len(result.conflict_event_ids) == 1
    conflict = next(
        item
        for item in service.list_timeline(PATIENT_ID)
        if item.kind == MemoryEventKind.CONFLICT
    )
    assert conflict.state == TimelineEventState.CONFLICT
    assert "未选择其中任何一个值" in conflict.summary
    assert {
        item.resource.reference for item in conflict.evidence_refs
    } == {
        "Observation/observation-conflict-1",
        "Observation/observation-conflict-2",
    }


def test_missing_expectation_creates_explicit_non_clinical_missing_event(tmp_path):
    response = _response("response-missing-1", "2026-08-02T10:00:00+00:00")
    observation = _vomiting(
        "observation-missing-1", response["id"], response["authored"], 1
    )
    service, _, _ = _service(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    expectation = MissingDataExpectation(
        expectation_id="expect-weight-1",
        patient_id=PATIENT_ID,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
        code_system=BODY_WEIGHT.system,
        code=BODY_WEIGHT.code,
        display="体重",
        period_start="2026-08-01T00:00:00+00:00",
        period_end="2026-08-02T11:00:00+00:00",
        pathway_reference=ResourceReference(
            reference="PlanDefinition/glp1-followup-plan-v1", version_id="1.0.0"
        ),
    )

    result = service.rebuild(PATIENT_ID, missing_expectations=[expectation])

    assert len(result.missing_event_ids) == 1
    missing = next(
        item
        for item in service.list_timeline(PATIENT_ID)
        if item.kind == MemoryEventKind.MISSING_DATA
    )
    assert "只表示数据缺失，不代表患者状态" in missing.summary
    assert missing.source.reference.startswith("PlanDefinition/")


def test_present_observation_satisfies_missing_expectation(tmp_path):
    response = _response("response-present-1", "2026-08-02T10:00:00+00:00")
    weight = build_patient_reported_observation(
        observation_id="observation-weight-1",
        patient_id=PATIENT_ID,
        questionnaire_response_id=response["id"],
        effective_time=response["authored"],
        code=BODY_WEIGHT,
        value_element="valueQuantity",
        value={
            "value": 70,
            "unit": "kg",
            "system": "http://unitsofmeasure.org",
            "code": "kg",
        },
    )
    service, _, _ = _service(
        tmp_path, _snapshot(responses=[response], observations=[weight])
    )
    expectation = MissingDataExpectation(
        expectation_id="expect-weight-present",
        patient_id=PATIENT_ID,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
        code_system=BODY_WEIGHT.system,
        code=BODY_WEIGHT.code,
        display="体重",
        period_start="2026-08-01T00:00:00+00:00",
        period_end="2026-08-02T11:00:00+00:00",
        pathway_reference=ResourceReference(
            reference="PlanDefinition/glp1-followup-plan-v1", version_id="1.0.0"
        ),
    )

    result = service.rebuild(PATIENT_ID, missing_expectations=[expectation])

    assert result.missing_event_ids == []


def test_arriving_observation_supersedes_missing_marker_but_preserves_history(
    tmp_path,
):
    at = "2026-08-02T10:00:00+00:00"
    response = _response("response-resolves-missing", at)
    service, repository, reader = _service(
        tmp_path, _snapshot(responses=[response], observations=[])
    )
    expectation = MissingDataExpectation(
        expectation_id="expect-weight-resolved",
        patient_id=PATIENT_ID,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
        code_system=BODY_WEIGHT.system,
        code=BODY_WEIGHT.code,
        display="体重",
        period_start="2026-08-01T00:00:00+00:00",
        period_end="2026-08-02T11:00:00+00:00",
        pathway_reference=ResourceReference(
            reference="PlanDefinition/glp1-followup-plan-v1", version_id="1.0.0"
        ),
    )
    first = service.rebuild(PATIENT_ID, missing_expectations=[expectation])
    missing_id = first.missing_event_ids[0]
    weight = build_patient_reported_observation(
        observation_id="observation-resolves-missing",
        patient_id=PATIENT_ID,
        questionnaire_response_id=response["id"],
        effective_time=at,
        code=BODY_WEIGHT,
        value_element="valueQuantity",
        value={
            "value": 70,
            "unit": "kg",
            "system": "http://unitsofmeasure.org",
            "code": "kg",
        },
    )
    reader.snapshot = _snapshot(responses=[response], observations=[weight])

    second = service.rebuild(PATIENT_ID, missing_expectations=[expectation])

    assert second.missing_event_ids == []
    assert all(
        item.kind != MemoryEventKind.MISSING_DATA
        for item in service.list_timeline(PATIENT_ID)
    )
    historical = next(
        item
        for item in service.list_timeline(PATIENT_ID, include_history=True)
        if item.memory_event_id == missing_id
    )
    assert historical.state == TimelineEventState.SUPERSEDED
    revision = next(
        item
        for item in repository.list_contracts(
            "revision_link", patient_id=PATIENT_ID
        )
        if item.predecessor.reference
        == f"urn:continucare:memory-event:{missing_id}"
    )
    provenance = repository.get_fhir_resource(
        "Provenance", revision.provenance_reference.removeprefix("Provenance/")
    )
    assert provenance is not None
    assert provenance["entity"][0]["what"]["reference"] == (
        f"urn:continucare:memory-event:{missing_id}"
    )


def test_missing_expectations_sharing_pathway_source_remain_distinct(tmp_path):
    service, _, _ = _service(
        tmp_path, _snapshot(responses=[], observations=[])
    )
    shared = ResourceReference(
        reference="PlanDefinition/glp1-followup-plan-v1", version_id="1.0.0"
    )
    expectations = [
        MissingDataExpectation(
            expectation_id=f"expect-shared-{suffix}",
            patient_id=PATIENT_ID,
            pathway_code="GLP1-14D",
            pathway_version="1.0.0",
            code_system=BODY_WEIGHT.system,
            code=BODY_WEIGHT.code,
            display=display,
            period_start="2026-08-01T00:00:00+00:00",
            period_end="2026-08-02T11:00:00+00:00",
            pathway_reference=shared,
        )
        for suffix, display in (("weight", "体重"), ("weight-check", "复测体重"))
    ]

    result = service.rebuild(PATIENT_ID, missing_expectations=expectations)

    assert len(result.missing_event_ids) == 2
    missing = [
        item
        for item in service.list_memory(PATIENT_ID)
        if item.kind == MemoryEventKind.MISSING_DATA
    ]
    assert {item.expectation_id for item in missing} == {
        "expect-shared-weight",
        "expect-shared-weight-check",
    }
    assert len({item.deduplication_key for item in missing}) == 2


def test_open_expectation_window_is_not_marked_missing_early(tmp_path):
    service, _, _ = _service(
        tmp_path, _snapshot(responses=[], observations=[])
    )
    expectation = MissingDataExpectation(
        expectation_id="expect-window-still-open",
        patient_id=PATIENT_ID,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
        code_system=BODY_WEIGHT.system,
        code=BODY_WEIGHT.code,
        display="体重",
        period_start="2026-08-02T00:00:00+00:00",
        period_end="2026-08-02T13:00:00+00:00",
        pathway_reference=ResourceReference(
            reference="PlanDefinition/glp1-followup-plan-v1", version_id="1.0.0"
        ),
    )

    result = service.rebuild(PATIENT_ID, missing_expectations=[expectation])

    assert result.missing_event_ids == []
    assert all(
        item.kind != MemoryEventKind.MISSING_DATA
        for item in service.list_timeline(PATIENT_ID)
    )


def test_revision_is_immutable_idempotent_and_has_provenance(tmp_path):
    service, repository, _ = _service(
        tmp_path, _snapshot(responses=[], observations=[])
    )
    predecessor = ResourceReference(
        reference="Observation/observation-revision", version_id="1"
    )
    successor = ResourceReference(
        reference="Observation/observation-revision", version_id="2"
    )

    first = service.record_revision(
        patient_id=PATIENT_ID,
        predecessor=predecessor,
        successor=successor,
        relationship=RevisionRelationship.CORRECTS,
        reason="患者更正合成记录。",
        actor_reference=f"Patient/{PATIENT_ID}",
        recorded_at="2026-08-02T11:00:00+00:00",
    )
    second = service.record_revision(
        patient_id=PATIENT_ID,
        predecessor=predecessor,
        successor=successor,
        relationship=RevisionRelationship.CORRECTS,
        reason="患者更正合成记录。",
        actor_reference=f"Patient/{PATIENT_ID}",
        recorded_at="2026-08-02T11:00:00+00:00",
    )

    assert second == first
    assert repository.get_contract("revision_link", first.link_id) == first
    provenance_id = first.provenance_reference.removeprefix("Provenance/")
    provenance = repository.get_fhir_resource("Provenance", provenance_id)
    assert provenance is not None
    assert provenance["target"][0]["reference"].endswith("/_history/2")
    assert provenance["entity"][0]["what"]["reference"].endswith("/_history/1")


def test_mutating_same_revision_identity_is_rejected(tmp_path):
    service, repository, _ = _service(
        tmp_path, _snapshot(responses=[], observations=[])
    )
    link = service.record_revision(
        patient_id=PATIENT_ID,
        predecessor=ResourceReference(
            reference="Observation/observation-revision-2", version_id="1"
        ),
        successor=ResourceReference(
            reference="Observation/observation-revision-2", version_id="2"
        ),
        relationship=RevisionRelationship.AMENDS,
        reason="原始原因。",
        actor_reference=f"Patient/{PATIENT_ID}",
        recorded_at="2026-08-02T11:00:00+00:00",
    )
    changed = deepcopy(link)
    changed.reason = "同一版本被篡改。"

    try:
        repository.save_contract(changed)
    except ValueError as exc:
        assert "version is immutable" in str(exc)
    else:
        raise AssertionError("mutated revision contract should be rejected")


def test_real_layer3_boundary_builds_memory_and_keeps_rule_tasks_disabled(tmp_path):
    db_path = tmp_path / "layer3-to-layer4.db"
    source_store = SQLiteStore(db_path)
    engine = CareEngine(source_store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    completed = engine.complete(
        session.session_id,
        {
            "nausea-present": True,
            "nausea-severity": "LA6752-5",
            "vomiting-count-24h": 2,
            "abdominal-pain-present": False,
            "free-text-report": "合成数据。",
        },
    )
    repository = Layer4SQLiteStore(db_path)
    communication = build_communication(
        patient_id=DEMO_PATIENT_ID,
        content_text="患者已确认合成随访记录。",
        sender_reference=f"Patient/{DEMO_PATIENT_ID}",
        recipient_references=["PractitionerRole/nurse"],
        sent_at=completed.questionnaire_response["authored"],
        communication_id="communication-real-boundary",
    )
    repository.save_fhir_resource(communication, patient_id=DEMO_PATIENT_ID)
    service = ClinicalMemoryService(
        Layer4InputReader(source_store),
        repository,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
    )

    result = service.rebuild(DEMO_PATIENT_ID)

    assert result.memory_event_ids
    kinds = {item.kind for item in service.list_timeline(DEMO_PATIENT_ID)}
    assert MemoryEventKind.QUESTIONNAIRE_RESPONSE in kinds
    assert MemoryEventKind.OBSERVATION in kinds
    assert MemoryEventKind.COMMUNICATION not in kinds
    assert repository.list_fhir_resources(
        patient_id=DEMO_PATIENT_ID, resource_type="Task"
    ) == []


def test_revision_overlay_hides_superseded_fact_but_preserves_history(tmp_path):
    at = "2026-08-02T10:00:00+00:00"
    response = _response("response-versioned", at)
    version_1 = _vomiting("observation-versioned", response["id"], at, 1)
    version_1["meta"] = {"versionId": "1", "lastUpdated": at}
    version_2 = _vomiting("observation-versioned", response["id"], at, 2)
    version_2["meta"] = {
        "versionId": "2",
        "lastUpdated": "2026-08-02T11:00:00+00:00",
    }
    service, _, _ = _service(
        tmp_path,
        _snapshot(responses=[response], observations=[version_1, version_2]),
    )
    service.rebuild(PATIENT_ID)
    service.record_revision(
        patient_id=PATIENT_ID,
        predecessor=ResourceReference(
            reference="Observation/observation-versioned", version_id="1"
        ),
        successor=ResourceReference(
            reference="Observation/observation-versioned", version_id="2"
        ),
        relationship=RevisionRelationship.CORRECTS,
        reason="患者更正合成记录。",
        actor_reference=f"Patient/{PATIENT_ID}",
        recorded_at="2026-08-02T11:00:00+00:00",
    )

    current = [
        item
        for item in service.list_timeline(PATIENT_ID)
        if item.source.reference == "Observation/observation-versioned"
        and item.kind == MemoryEventKind.OBSERVATION
    ]
    assert all(
        item.kind != MemoryEventKind.CONFLICT
        for item in service.list_timeline(PATIENT_ID)
    )
    history = [
        item
        for item in service.list_timeline(PATIENT_ID, include_history=True)
        if item.source.reference == "Observation/observation-versioned"
        and item.kind == MemoryEventKind.OBSERVATION
    ]

    assert [item.source.version_id for item in current] == ["2"]
    assert {item.source.version_id for item in history} == {"1", "2"}
    superseded = next(item for item in history if item.source.version_id == "1")
    assert superseded.state == TimelineEventState.SUPERSEDED
    historical_conflict = next(
        item
        for item in service.list_timeline(PATIENT_ID, include_history=True)
        if item.kind == MemoryEventKind.CONFLICT
    )
    assert historical_conflict.state == TimelineEventState.SUPERSEDED
    assert all(
        item.source.version_id != "1"
        for item in service.list_memory(PATIENT_ID)
        if item.source.reference == "Observation/observation-versioned"
    )
    assert any(
        item.source.reference == "Observation/observation-versioned"
        and item.source.version_id == "1"
        and item.current is False
        for item in service.list_memory(PATIENT_ID, include_history=True)
    )


def test_memory_projection_bundle_rolls_back_and_replays_after_commit(tmp_path):
    response = _response("response-memory-atomic", "2026-08-02T10:00:00+00:00")
    observation = _vomiting(
        "observation-memory-atomic",
        response["id"],
        response["authored"],
        2,
    )
    service, repository, _ = _service(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )

    def rollback_fault(stage):
        if stage == "memory:after_provenance":
            raise RuntimeError("fault:memory:after_provenance")

    repository._provenance_contract_bundle_fault = rollback_fault
    with pytest.raises(RuntimeError, match="memory:after_provenance"):
        service.rebuild(PATIENT_ID)
    assert repository.list_contracts("memory_event", patient_id=PATIENT_ID) == []
    assert repository.list_contracts("timeline_event", patient_id=PATIENT_ID) == []
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    ) == []

    def commit_fault(stage):
        if stage == "memory:after_commit":
            raise RuntimeError("fault:memory:after_commit")

    repository._provenance_contract_bundle_fault = commit_fault
    with pytest.raises(RuntimeError, match="memory:after_commit"):
        service.rebuild(PATIENT_ID)
    assert len(repository.list_contracts("memory_event", patient_id=PATIENT_ID)) == 1
    assert len(
        repository.list_contracts("timeline_event", patient_id=PATIENT_ID)
    ) == 1
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Provenance",
            current_only=False,
        )
    ) == 1

    repository._provenance_contract_bundle_fault = lambda stage: None
    completed = service.rebuild(PATIENT_ID)
    assert len(completed.memory_event_ids) == 3
    assert len(repository.list_contracts("memory_event", patient_id=PATIENT_ID)) == 3
    assert len(
        repository.list_contracts("timeline_event", patient_id=PATIENT_ID)
    ) == 3
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Provenance",
            current_only=False,
        )
    ) == 3


def test_workflow_projection_and_supersede_revision_are_one_atomic_bundle(tmp_path):
    service, repository, _ = _service(
        tmp_path, _snapshot(responses=[], observations=[])
    )
    pathway = "urn:continucare:pathway:GLP1-14D|1.0.0"

    def task(version: str, status: str, authored_on: str) -> dict:
        return build_workflow_task(
            patient_id=PATIENT_ID,
            rule_id="synthetic-memory-atomic-rule",
            rule_version="1.0.0",
            task_code_system="urn:continucare:task-code",
            task_code="synthetic-memory-atomic",
            task_code_display="合成工作流原子性验证",
            description="只验证 Memory 投影与修订原子性。",
            requester_reference="Device/continucare-rule-engine",
            owner_reference="PractitionerRole/synthetic-nurse",
            authored_on=authored_on,
            trigger_reference="Observation/synthetic-memory-trigger/_history/1",
            due_at="2026-08-03T12:00:00+00:00",
            status=status,
            task_id="task-memory-workflow-atomic",
            version_id=version,
            based_on_references=[pathway],
            evidence_references=[
                "Observation/synthetic-memory-trigger/_history/1"
            ],
        )

    version_1 = task("1", "requested", "2026-08-02T10:00:00+00:00")
    repository.save_fhir_resource(version_1, patient_id=PATIENT_ID)
    service.rebuild(PATIENT_ID)

    version_2 = task("2", "in-progress", "2026-08-02T11:00:00+00:00")
    repository.save_fhir_resource(version_2, patient_id=PATIENT_ID)

    def rollback_fault(stage):
        if stage == "memory:before_revision:0":
            raise RuntimeError("fault:memory:before_revision:0")

    repository._provenance_contract_bundle_fault = rollback_fault
    with pytest.raises(RuntimeError, match="memory:before_revision:0"):
        service.rebuild(PATIENT_ID)

    projected_after_fault = [
        item
        for item in repository.list_contracts(
            "memory_event", patient_id=PATIENT_ID
        )
        if item.source.reference == "Task/task-memory-workflow-atomic"
    ]
    assert [item.source.version_id for item in projected_after_fault] == ["1"]
    assert repository.list_contracts(
        "revision_link", patient_id=PATIENT_ID
    ) == []

    repository._provenance_contract_bundle_fault = lambda stage: None
    service.rebuild(PATIENT_ID)

    current = [
        item
        for item in service.list_memory(PATIENT_ID)
        if item.source.reference == "Task/task-memory-workflow-atomic"
    ]
    history = [
        item
        for item in service.list_memory(PATIENT_ID, include_history=True)
        if item.source.reference == "Task/task-memory-workflow-atomic"
    ]
    revisions = repository.list_contracts(
        "revision_link", patient_id=PATIENT_ID
    )
    assert [item.source.version_id for item in current] == ["2"]
    assert {item.source.version_id for item in history} == {"1", "2"}
    assert len(revisions) == 1
    assert revisions[0].predecessor.version_id == "1"
    assert revisions[0].successor.version_id == "2"

    version_3 = task("3", "completed", "2026-08-02T12:00:00+00:00")
    repository.save_fhir_resource(version_3, patient_id=PATIENT_ID)

    def commit_fault(stage):
        if stage == "memory:after_commit":
            raise RuntimeError("fault:memory:after_commit")

    repository._provenance_contract_bundle_fault = commit_fault
    with pytest.raises(RuntimeError, match="memory:after_commit"):
        service.rebuild(PATIENT_ID)
    committed_memory = repository.list_contracts(
        "memory_event", patient_id=PATIENT_ID
    )
    committed_revisions = repository.list_contracts(
        "revision_link", patient_id=PATIENT_ID
    )

    repository._provenance_contract_bundle_fault = lambda stage: None
    service.rebuild(PATIENT_ID)

    assert repository.list_contracts(
        "memory_event", patient_id=PATIENT_ID
    ) == committed_memory
    assert repository.list_contracts(
        "revision_link", patient_id=PATIENT_ID
    ) == committed_revisions
    assert [
        item.source.version_id
        for item in service.list_memory(PATIENT_ID)
        if item.source.reference == "Task/task-memory-workflow-atomic"
    ] == ["3"]


def test_revision_bundle_rolls_back_and_replays_after_commit(tmp_path):
    service, repository, _ = _service(
        tmp_path, _snapshot(responses=[], observations=[])
    )
    arguments = {
        "patient_id": PATIENT_ID,
        "predecessor": ResourceReference(
            reference="Observation/observation-revision-atomic", version_id="1"
        ),
        "successor": ResourceReference(
            reference="Observation/observation-revision-atomic", version_id="2"
        ),
        "relationship": RevisionRelationship.CORRECTS,
        "reason": "患者更正合成记录。",
        "actor_reference": f"Patient/{PATIENT_ID}",
        "recorded_at": "2026-08-02T11:00:00+00:00",
    }

    def rollback_fault(stage):
        if stage == "revision:after_provenance":
            raise RuntimeError("fault:revision:after_provenance")

    repository._provenance_contract_bundle_fault = rollback_fault
    with pytest.raises(RuntimeError, match="revision:after_provenance"):
        service.record_revision(**arguments)
    assert repository.list_contracts("revision_link", patient_id=PATIENT_ID) == []
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    ) == []

    def commit_fault(stage):
        if stage == "revision:after_commit":
            raise RuntimeError("fault:revision:after_commit")

    repository._provenance_contract_bundle_fault = commit_fault
    with pytest.raises(RuntimeError, match="revision:after_commit"):
        service.record_revision(**arguments)
    committed = repository.list_contracts("revision_link", patient_id=PATIENT_ID)
    assert len(committed) == 1
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Provenance",
            current_only=False,
        )
    ) == 1

    repository._provenance_contract_bundle_fault = lambda stage: None
    replay = service.record_revision(**arguments)
    assert replay == committed[0]
    assert len(repository.list_contracts("revision_link", patient_id=PATIENT_ID)) == 1

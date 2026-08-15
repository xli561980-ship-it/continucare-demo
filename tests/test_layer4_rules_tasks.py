from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from continucare.errors import ConcurrentWriteConflict
from continucare.fhir.observations import (
    build_patient_reported_observation,
    per_day_quantity,
)
from continucare.fhir.r4 import validate_official_json_schema
from continucare.fhir.terminology import VOMITING_COUNT_24H
from continucare.layer4 import (
    ApprovedRuleEngine,
    ClinicalMemoryService,
    Layer4InputSnapshot,
    Layer4SQLiteStore,
    TaskWorkflowService,
)
from continucare.layer4.contracts import (
    ApprovalDecision,
    ClinicalRuleDefinition,
    EvidenceReference,
    EvidenceRole,
    ResourceReference,
    RuleApplicability,
    RuleApproval,
    RuleCondition,
    RuleConditionStatus,
    RuleEvaluationStatus,
    RuleLifecycle,
    RuleObservationInput,
    RuleOperator,
    RuleTaskAction,
    MemoryEventKind,
    TimelineEventState,
)


PATIENT_ID = "P-DEMO-001"
PATHWAY_CODE = "GLP1-14D"
PATHWAY_VERSION = "1.0.0"
NOW = "2026-08-02T12:00:00+00:00"


def _observation(
    *,
    value: int = 2,
    effective_time: str = "2026-08-02T10:00:00+00:00",
    observation_id: str = "observation-approved-rule",
) -> dict:
    return build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=PATIENT_ID,
        questionnaire_response_id="response-approved-rule",
        effective_time=effective_time,
        code=VOMITING_COUNT_24H,
        value_element="valueQuantity",
        value=per_day_quantity(value, unit="vomiting episodes/24 hours"),
    )


def _rule(
    *,
    lifecycle: RuleLifecycle = RuleLifecycle.ACTIVE,
    condition_logic: str = "all",
    owner_role: str = "nurse",
    region: str = "DE-demo",
) -> ClinicalRuleDefinition:
    approval = (
        RuleApproval(
            clinical_status=ApprovalDecision.APPROVED,
            terminology_status=ApprovalDecision.APPROVED,
            clinical_approver="Practitioner/clinical-reviewer",
            terminology_approver="Practitioner/terminology-reviewer",
            clinical_approved_at="2026-08-01T09:00:00+00:00",
            terminology_approved_at="2026-08-01T10:00:00+00:00",
        )
        if lifecycle in {RuleLifecycle.APPROVED, RuleLifecycle.ACTIVE}
        else RuleApproval()
    )
    return ClinicalRuleDefinition(
        rule_id="synthetic-approved-workflow-rule",
        version="1.0.0",
        title="合成数据人工复核规则",
        description="仅用于确定性规则引擎测试，不是临床阈值。",
        lifecycle=lifecycle,
        applicability=RuleApplicability(
            pathway_code=PATHWAY_CODE,
            pathway_version=PATHWAY_VERSION,
            synthetic_only=True,
            population="合成比赛测试患者",
            region=region,
        ),
        evidence_refs=[
            EvidenceReference(
                evidence_id="synthetic-rule-source",
                resource=ResourceReference(
                    reference="PlanDefinition/glp1-followup-plan-v1",
                    version_id="1.0.0",
                ),
                role=EvidenceRole.SOURCE,
                evidence_text="测试专用规则来源占位，不构成临床依据。",
            )
        ],
        inputs=[
            RuleObservationInput(
                input_id="vomiting-count",
                code_system=VOMITING_COUNT_24H.system,
                code=VOMITING_COUNT_24H.code,
                unit="/d",
                lookback_hours=24,
            )
        ],
        conditions=[
            RuleCondition(
                input_id="vomiting-count",
                operator=RuleOperator.GTE,
                expected_value=2,
                unit="/d",
            )
        ],
        condition_logic=condition_logic,
        action=RuleTaskAction(
            task_code_system="urn:continucare:task-code",
            task_code="synthetic-human-review",
            task_code_display="合成数据人工复核",
            title="合成数据人工复核",
            description="请复核触发证据；不构成诊断或处置建议。",
            owner_role=owner_role,
            sla_hours=4,
            deduplication_window_hours=24,
        ),
        approval=approval,
        test_case_ids=["synthetic-approved-rule-match"],
        rollback_plan="停用测试规则并恢复 not_assessed。",
        created_at="2026-08-01T08:00:00+00:00",
    )


class _RuleInputReader:
    def __init__(self):
        self.observations: list[dict] = []

    def read(
        self,
        patient_id: str,
        *,
        pathway_code: str,
        pathway_version: str,
        assembled_at: str | None = None,
    ) -> Layer4InputSnapshot:
        return Layer4InputSnapshot(
            patient_id=patient_id,
            pathway_code=pathway_code,
            pathway_version=pathway_version,
            observations=self.observations,
            assembled_at=assembled_at or NOW,
        )


def _engine(repository: Layer4SQLiteStore) -> ApprovedRuleEngine:
    input_reader = _RuleInputReader()
    return ApprovedRuleEngine(
        repository,
        input_reader=input_reader,
        requester_reference="Organization/continucare",
        owner_references={"nurse": "PractitionerRole/nurse"},
    )


def _evaluate(
    engine: ApprovedRuleEngine,
    observations: list[dict],
    *,
    evaluated_at: str = NOW,
    region: str = "DE-demo",
):
    engine.input_reader.observations = observations
    return engine.evaluate(
        patient_id=PATIENT_ID,
        observations=observations,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
        evaluated_at=evaluated_at,
        region=region,
        synthetic_data=True,
    )


class _EmptyInputReader:
    def read(
        self, patient_id: str, *, pathway_code: str, pathway_version: str
    ) -> Layer4InputSnapshot:
        return Layer4InputSnapshot(
            patient_id=patient_id,
            pathway_code=pathway_code,
            pathway_version=pathway_version,
            assembled_at=NOW,
        )


def test_no_active_rule_remains_not_assessed_and_creates_no_task(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "no-approved-rules.db")

    result = _evaluate(_engine(repository), [_observation()])

    assert result.status == RuleEvaluationStatus.NOT_ASSESSED
    assert result.reason_codes == ["no_active_dual_approved_rule"]
    assert result.evaluations == []
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Task"
    ) == []
    assert len(result.provenance_references) == 1


def test_draft_rule_is_not_executable(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "draft-rule.db")
    repository.save_contract(_rule(lifecycle=RuleLifecycle.DRAFT))

    result = _evaluate(_engine(repository), [_observation()])

    assert result.status == RuleEvaluationStatus.NOT_ASSESSED
    assert result.reason_codes == ["no_active_dual_approved_rule"]
    assert result.task_references == []


def test_active_dual_approved_rule_creates_evidence_bound_task_idempotently(
    tmp_path,
):
    repository = Layer4SQLiteStore(tmp_path / "approved-rule.db")
    repository.save_contract(_rule())
    observation = _observation()
    engine = _engine(repository)

    first = _evaluate(engine, [observation])
    second = _evaluate(engine, [observation])

    assert first.status == RuleEvaluationStatus.MATCHED
    evaluation = first.evaluations[0]
    assert evaluation.status == RuleEvaluationStatus.MATCHED
    assert evaluation.task_created is True
    assert second.evaluations[0].task_created is True
    assert second == first
    assert second.task_references == first.task_references
    condition = evaluation.conditions[0]
    assert condition.status == RuleConditionStatus.MATCHED
    assert condition.actual_value == 2
    assert condition.expected_value == 2
    assert condition.unit == "/d"
    assert condition.evidence_refs[0].resource.reference == (
        "Observation/observation-approved-rule"
    )
    tasks = repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Task"
    )
    assert len(tasks) == 1
    task = tasks[0]
    assert task["identifier"][0]["value"] == (
        "synthetic-approved-workflow-rule|1.0.0"
    )
    assert task["owner"]["reference"] == "PractitionerRole/nurse"
    assert task["input"][0]["valueReference"]["reference"].endswith(
        "/_history/1"
    )
    assert repository.get_fhir_resource(
        "Provenance", evaluation.provenance_reference.removeprefix("Provenance/")
    ) is not None


@pytest.mark.parametrize(
    ("observations", "expected_status", "reason"),
    [
        ([_observation(value=1)], RuleEvaluationStatus.NO_MATCH, "condition_not_matched"),
        ([], RuleEvaluationStatus.NOT_ASSESSED, "required_input_missing"),
        (
            [
                _observation(
                    effective_time="2026-07-30T10:00:00+00:00",
                    observation_id="observation-outside-lookback",
                )
            ],
            RuleEvaluationStatus.NOT_ASSESSED,
            "required_input_missing",
        ),
    ],
)
def test_no_match_and_missing_inputs_never_create_task(
    tmp_path, observations, expected_status, reason
):
    repository = Layer4SQLiteStore(tmp_path / f"{reason}.db")
    repository.save_contract(_rule())

    result = _evaluate(_engine(repository), observations)

    assert result.status == expected_status
    assert reason in result.reason_codes
    assert result.task_references == []
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Task"
    ) == []


def test_unit_applicability_and_owner_mapping_fail_closed(tmp_path):
    mismatched_unit = _observation()
    mismatched_unit["valueQuantity"]["code"] = "mg"
    mismatched_unit["valueQuantity"]["unit"] = "mg"

    unit_repository = Layer4SQLiteStore(tmp_path / "unit-mismatch.db")
    unit_repository.save_contract(_rule())
    unit_result = _evaluate(_engine(unit_repository), [mismatched_unit])
    assert unit_result.status == RuleEvaluationStatus.NOT_ASSESSED
    assert "unit_mismatch" in unit_result.reason_codes

    region_repository = Layer4SQLiteStore(tmp_path / "region-mismatch.db")
    region_repository.save_contract(_rule())
    region_result = _evaluate(
        _engine(region_repository), [_observation()], region="US-demo"
    )
    assert region_result.status == RuleEvaluationStatus.NOT_ASSESSED
    assert "region_mismatch" in region_result.reason_codes

    owner_repository = Layer4SQLiteStore(tmp_path / "owner-mismatch.db")
    owner_repository.save_contract(_rule(owner_role="unmapped-role"))
    owner_result = _evaluate(_engine(owner_repository), [_observation()])
    assert owner_result.status == RuleEvaluationStatus.NOT_ASSESSED
    assert "owner_role_unmapped" in owner_result.reason_codes
    assert owner_result.task_references == []


def test_any_condition_logic_can_act_on_one_complete_matching_branch(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "any-condition.db")
    data = _rule().model_dump(mode="json")
    data["condition_logic"] = "any"
    data["conditions"].append(
        {
            "input_id": "vomiting-count",
            "operator": "lte",
            "expected_value": 1,
            "unit": "/d",
        }
    )
    repository.save_contract(ClinicalRuleDefinition.model_validate(data))

    result = _evaluate(_engine(repository), [_observation(value=2)])

    assert result.status == RuleEvaluationStatus.MATCHED
    assert [item.status for item in result.evaluations[0].conditions] == [
        RuleConditionStatus.MATCHED,
        RuleConditionStatus.NOT_MATCHED,
    ]
    assert len(result.task_references) == 1


def test_rule_engine_rejects_non_final_or_wrong_patient_observation(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "invalid-observations.db")
    engine = _engine(repository)
    preliminary = deepcopy(_observation())
    preliminary["status"] = "preliminary"
    with pytest.raises(ValueError, match="only accepts final Observation"):
        _evaluate(engine, [preliminary])

    wrong_patient = deepcopy(_observation())
    wrong_patient["subject"]["reference"] = "Patient/P-WRONG"
    with pytest.raises(ValueError, match="patient does not match"):
        _evaluate(engine, [wrong_patient])


def test_rule_engine_rejects_caller_supplied_unadmitted_final_observation(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "unadmitted-rule-input.db")
    repository.save_contract(_rule())
    engine = _engine(repository)

    with pytest.raises(ValueError, match="Pathway-admitted Observation"):
        engine.evaluate(
            patient_id=PATIENT_ID,
            observations=[_observation()],
            pathway_code=PATHWAY_CODE,
            pathway_version=PATHWAY_VERSION,
            evaluated_at=NOW,
            region="DE-demo",
            synthetic_data=True,
        )


def test_task_deduplication_window_reuses_then_allows_new_task(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "dedup-window.db")
    repository.save_contract(_rule())
    engine = _engine(repository)
    first = _evaluate(engine, [_observation()])
    within = _evaluate(
        engine,
        [
            _observation(
                observation_id="observation-within-dedup",
                effective_time="2026-08-02T13:00:00+00:00",
            )
        ],
        evaluated_at="2026-08-02T14:00:00+00:00",
    )
    outside = _evaluate(
        engine,
        [
            _observation(
                observation_id="observation-outside-dedup",
                effective_time="2026-08-04T13:00:00+00:00",
            )
        ],
        evaluated_at="2026-08-04T14:00:00+00:00",
    )

    assert within.task_references == first.task_references
    assert within.evaluations[0].task_created is False
    assert outside.evaluations[0].task_created is True
    assert outside.task_references != first.task_references
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Task",
            current_only=False,
        )
    ) == 2


def test_task_workflow_is_versioned_audited_and_idempotent(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "task-workflow.db")
    repository.save_contract(_rule())
    evaluation = _evaluate(_engine(repository), [_observation()])
    task_id = evaluation.task_references[0].removeprefix("Task/")
    workflow = TaskWorkflowService(repository)
    steps = [
        ("received", "2026-08-02T12:05:00+00:00"),
        ("accepted", "2026-08-02T12:10:00+00:00"),
        ("in-progress", "2026-08-02T12:15:00+00:00"),
        ("completed", "2026-08-02T12:30:00+00:00"),
    ]
    results = []
    for status, at in steps:
        results.append(
            workflow.transition(
                patient_id=PATIENT_ID,
                task_id=task_id,
                to_status=status,
                actor_reference="PractitionerRole/nurse",
                note=f"合成任务状态更新：{status}",
                transitioned_at=at,
            )
        )

    retry = workflow.transition(
        patient_id=PATIENT_ID,
        task_id=task_id,
        to_status="completed",
        actor_reference="PractitionerRole/nurse",
        note="合成任务状态更新：completed",
        transitioned_at="2026-08-02T12:30:00+00:00",
    )

    assert retry == results[-1]
    current = repository.get_fhir_resource("Task", task_id)
    assert current is not None
    assert current["status"] == "completed"
    assert current["meta"]["versionId"] == "5"
    assert current["executionPeriod"] == {
        "start": "2026-08-02T12:15:00+00:00",
        "end": "2026-08-02T12:30:00+00:00",
    }
    assert len(current["note"]) == 4
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Task",
            current_only=False,
        )
    ) == 5
    assert all(
        repository.get_fhir_resource(
            "Provenance", item.provenance_reference.removeprefix("Provenance/")
        )
        is not None
        for item in results
    )
    with pytest.raises(ValueError, match="terminal Task status"):
        workflow.transition(
            patient_id=PATIENT_ID,
            task_id=task_id,
            to_status="cancelled",
            actor_reference="PractitionerRole/nurse",
            note="不能修改已完成任务。",
            transitioned_at="2026-08-02T12:31:00+00:00",
        )


def test_task_workflow_rejects_skips_and_supports_entered_in_error(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "task-invalid-transition.db")
    repository.save_contract(_rule())
    evaluation = _evaluate(_engine(repository), [_observation()])
    task_id = evaluation.task_references[0].removeprefix("Task/")
    workflow = TaskWorkflowService(repository)

    with pytest.raises(ValueError, match="is not allowed"):
        workflow.transition(
            patient_id=PATIENT_ID,
            task_id=task_id,
            to_status="completed",
            actor_reference="PractitionerRole/nurse",
            note="非法跳转。",
            transitioned_at="2026-08-02T12:05:00+00:00",
        )
    with pytest.raises(ValueError, match="non-blank note"):
        workflow.transition(
            patient_id=PATIENT_ID,
            task_id=task_id,
            to_status="received",
            actor_reference="PractitionerRole/nurse",
            note="   ",
            transitioned_at="2026-08-02T12:05:00+00:00",
        )
    with pytest.raises(ValueError, match="must follow the current version"):
        workflow.transition(
            patient_id=PATIENT_ID,
            task_id=task_id,
            to_status="received",
            actor_reference="PractitionerRole/nurse",
            note="时间戳早于当前版本。",
            transitioned_at="2026-08-02T11:59:00+00:00",
        )
    result = workflow.transition(
        patient_id=PATIENT_ID,
        task_id=task_id,
        to_status="entered-in-error",
        actor_reference="Practitioner/data-steward",
        note="合成任务录入错误。",
        transitioned_at="2026-08-02T12:06:00+00:00",
    )
    assert result.to_status == "entered-in-error"
    assert repository.get_fhir_resource("Task", task_id)["status"] == (
        "entered-in-error"
    )
    replacement = _evaluate(
        _engine(repository),
        [
            _observation(
                observation_id="observation-after-task-error",
                effective_time="2026-08-02T12:07:00+00:00",
            )
        ],
        evaluated_at="2026-08-02T12:08:00+00:00",
    )
    assert replacement.status == RuleEvaluationStatus.MATCHED
    assert replacement.task_references != evaluation.task_references
    assert replacement.evaluations[0].task_created is True


def test_task_versions_project_to_one_current_memory_event_and_keep_history(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "task-memory-history.db")
    repository.save_contract(_rule())
    evaluation = _evaluate(_engine(repository), [_observation()])
    task_id = evaluation.task_references[0].removeprefix("Task/")
    memory = ClinicalMemoryService(
        _EmptyInputReader(),
        repository,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
    )
    memory.rebuild(PATIENT_ID)
    workflow = TaskWorkflowService(repository)
    workflow.transition(
        patient_id=PATIENT_ID,
        task_id=task_id,
        to_status="received",
        actor_reference="PractitionerRole/nurse",
        note="已收到合成任务。",
        transitioned_at="2026-08-02T12:05:00+00:00",
    )
    memory.rebuild(PATIENT_ID)

    current = [
        item
        for item in memory.list_timeline(PATIENT_ID)
        if item.kind == MemoryEventKind.TASK
    ]
    history = [
        item
        for item in memory.list_timeline(PATIENT_ID, include_history=True)
        if item.kind == MemoryEventKind.TASK
    ]
    assert [item.source.version_id for item in current] == ["2"]
    assert {item.source.version_id for item in history} == {"1", "2"}
    assert next(item for item in history if item.source.version_id == "1").state == (
        TimelineEventState.SUPERSEDED
    )

    workflow.transition(
        patient_id=PATIENT_ID,
        task_id=task_id,
        to_status="entered-in-error",
        actor_reference="Practitioner/data-steward",
        note="合成任务录入错误。",
        transitioned_at="2026-08-02T12:06:00+00:00",
    )
    memory.rebuild(PATIENT_ID)
    assert all(
        item.kind != MemoryEventKind.TASK for item in memory.list_timeline(PATIENT_ID)
    )
    entered_in_error = next(
        item
        for item in memory.list_timeline(PATIENT_ID, include_history=True)
        if item.kind == MemoryEventKind.TASK and item.source.version_id == "3"
    )
    assert entered_in_error.state == TimelineEventState.ENTERED_IN_ERROR


def test_generated_and_transitioned_tasks_pass_official_schema_when_available(
    tmp_path,
):
    schema_path = os.environ.get("FHIR_R4_SCHEMA_ZIP")
    if not schema_path:
        pytest.skip("set FHIR_R4_SCHEMA_ZIP to the official HL7 R4 schema archive")
    repository = Layer4SQLiteStore(tmp_path / "task-official-schema.db")
    repository.save_contract(_rule())
    evaluation = _evaluate(_engine(repository), [_observation()])
    task_id = evaluation.task_references[0].removeprefix("Task/")
    TaskWorkflowService(repository).transition(
        patient_id=PATIENT_ID,
        task_id=task_id,
        to_status="received",
        actor_reference="PractitionerRole/nurse",
        note="FHIR Schema 合成校验。",
        transitioned_at="2026-08-02T12:05:00+00:00",
    )
    for resource in repository.list_fhir_resources(
        patient_id=PATIENT_ID, current_only=False
    ):
        validate_official_json_schema(resource, schema_path)


def test_rule_task_creation_bundle_rolls_back_and_replays_after_commit(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "rule-creation-atomic.db")
    repository.save_contract(_rule())
    engine = _engine(repository)

    def rollback_fault(stage):
        if stage == "after_resource:0":
            raise RuntimeError("fault:after_resource:0")

    repository._fhir_creation_bundle_fault = rollback_fault
    with pytest.raises(RuntimeError, match="after_resource:0"):
        _evaluate(engine, [_observation()])
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Task", current_only=False
    ) == []
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    ) == []

    def commit_fault(stage):
        if stage == "after_commit":
            raise RuntimeError("fault:after_commit")

    repository._fhir_creation_bundle_fault = commit_fault
    with pytest.raises(RuntimeError, match="after_commit"):
        _evaluate(engine, [_observation()])
    tasks = repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Task", current_only=False
    )
    provenance = repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    )
    assert len(tasks) == 1
    assert len(provenance) == 1

    repository._fhir_creation_bundle_fault = lambda stage: None
    replay = _evaluate(engine, [_observation()])
    assert replay.task_references == [f"Task/{tasks[0]['id']}"]
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID, resource_type="Task", current_only=False
        )
    ) == 1
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Provenance",
            current_only=False,
        )
    ) == 1


def test_task_transition_bundle_rolls_back_and_replays_after_commit(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "task-transition-atomic.db")
    repository.save_contract(_rule())
    evaluation = _evaluate(_engine(repository), [_observation()])
    task_id = evaluation.task_references[0].removeprefix("Task/")
    workflow = TaskWorkflowService(repository)
    transition = {
        "patient_id": PATIENT_ID,
        "task_id": task_id,
        "to_status": "received",
        "actor_reference": "PractitionerRole/nurse",
        "note": "原子事务故障注入。",
        "transitioned_at": "2026-08-02T12:05:00+00:00",
    }
    provenance_before = repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    )

    def rollback_fault(stage):
        if stage == "after_task":
            raise RuntimeError("fault:after_task")

    repository._task_transition_fault = rollback_fault
    with pytest.raises(RuntimeError, match="after_task"):
        workflow.transition(**transition)
    current = repository.get_fhir_resource("Task", task_id)
    assert current["status"] == "requested"
    assert current["meta"]["versionId"] == "1"
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    ) == provenance_before

    def commit_fault(stage):
        if stage == "after_commit":
            raise RuntimeError("fault:after_commit")

    repository._task_transition_fault = commit_fault
    with pytest.raises(RuntimeError, match="after_commit"):
        workflow.transition(**transition)
    committed = repository.get_fhir_resource("Task", task_id)
    assert committed["status"] == "received"
    assert committed["meta"]["versionId"] == "2"

    repository._task_transition_fault = lambda stage: None
    replay = workflow.transition(**transition)
    assert replay.to_version == "2"
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID, resource_type="Task", current_only=False
        )
    ) == 2
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Provenance",
            current_only=False,
        )
    ) == len(provenance_before) + 1


def test_concurrent_task_creation_has_one_complete_bundle(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "rule-creation-concurrent.db")
    repository.save_contract(_rule())
    engines = [_engine(repository), _engine(repository)]
    barrier = Barrier(2)
    original = repository.persist_fhir_creation_bundle

    def synchronized(**kwargs):
        barrier.wait(timeout=5)
        return original(**kwargs)

    repository.persist_fhir_creation_bundle = synchronized
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_evaluate, engine, [_observation()])
            for engine in engines
        ]
    outcomes = []
    conflicts = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except ConcurrentWriteConflict as exc:
            conflicts.append(exc)

    assert len(outcomes) + len(conflicts) == 2
    assert outcomes
    tasks = repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Task", current_only=False
    )
    provenance = repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    )
    assert len(tasks) == 1
    assert len(provenance) == 1


def test_concurrent_same_task_transition_has_one_complete_version(tmp_path):
    repository = Layer4SQLiteStore(tmp_path / "task-transition-concurrent.db")
    repository.save_contract(_rule())
    evaluation = _evaluate(_engine(repository), [_observation()])
    task_id = evaluation.task_references[0].removeprefix("Task/")
    barrier = Barrier(2)
    original = repository.persist_task_transition

    def synchronized(**kwargs):
        barrier.wait(timeout=5)
        return original(**kwargs)

    repository.persist_task_transition = synchronized
    arguments = {
        "patient_id": PATIENT_ID,
        "task_id": task_id,
        "to_status": "received",
        "actor_reference": "PractitionerRole/nurse",
        "note": "并发原子事务测试。",
        "transitioned_at": "2026-08-02T12:05:00+00:00",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(TaskWorkflowService(repository).transition, **arguments)
            for _ in range(2)
        ]
    outcomes = []
    conflicts = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except ConcurrentWriteConflict as exc:
            conflicts.append(exc)

    assert len(outcomes) + len(conflicts) == 2
    assert outcomes
    versions = repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Task", current_only=False
    )
    assert [item["meta"]["versionId"] for item in versions] == ["2", "1"]
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Provenance",
            current_only=False,
        )
    ) == 2

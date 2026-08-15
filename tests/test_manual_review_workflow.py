from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_agent import CareAgentService
from continucare.care_agent.model_api import SemanticModelConfig, UnconfiguredModelAdapter
from continucare.care_engine import CareEngine
from continucare.db import connect
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.layer4 import manual_reviews
from continucare.layer4.manual_reviews import (
    PENDING_APPROVAL,
    READY_TO_SEND,
    communication_readiness,
    is_send_eligible,
)
from continucare.layer4.storage import Layer4SQLiteStore
from continucare.pathways import load_builtin_pathways
from continucare.services.confirmed_review import ConfirmedReviewService
from continucare.services.demo_scenarios import load_manual_review_scenario
from continucare.services.manual_review_workflow import (
    MANUAL_REVIEW_DRAFT_TEMPLATE,
    ManualReviewWorkflowService,
)


def _after(resource, seconds=60):
    current = datetime.fromisoformat(resource["meta"]["lastUpdated"])
    return (current + timedelta(seconds=seconds)).isoformat()


def _scenario(db_path, *, layer4_store=None):
    interaction = load_manual_review_scenario(db_path)
    store = SQLiteStore(db_path)
    engine = CareEngine(store)
    agent = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        patient_timezone="Asia/Shanghai",
    )
    confirmed = ConfirmedReviewService(
        store, care_agent=agent, care_engine=engine
    ).accept_all(
        interaction.result.run_id,
        [item.candidate_id for item in interaction.result.candidates],
    )
    repository = layer4_store or Layer4SQLiteStore(db_path)
    return (
        store,
        repository,
        ManualReviewWorkflowService(store, layer4_store=repository),
        confirmed.task,
    )


def _advance_to_in_progress(service, task):
    received = service.acknowledge(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        note="已收到合成人工复核任务。",
        occurred_at=_after(task),
    )
    started = service.start(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        note="接受并开始核对合成证据。",
        occurred_at=_after(received.task),
    )
    return started.task


def _counts(db_path):
    with connect(db_path) as connection:
        return {
            "tasks": connection.execute(
                "SELECT COUNT(*) FROM layer4_fhir_resources WHERE resource_type='Task'"
            ).fetchone()[0],
            "communications": connection.execute(
                "SELECT COUNT(*) FROM layer4_fhir_resources WHERE resource_type='Communication'"
            ).fetchone()[0],
            "provenances": connection.execute(
                "SELECT COUNT(*) FROM layer4_fhir_resources WHERE resource_type='Provenance'"
            ).fetchone()[0],
            "audits": connection.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0],
        }


def test_manual_review_happy_path_requires_explicit_approval_and_never_sends(tmp_path):
    db_path = tmp_path / "manual-happy.db"
    store, repository, service, task = _scenario(db_path)
    in_progress = _advance_to_in_progress(service, task)

    outcome = service.record_outcome(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        outcome="evidence_consistent",
        note="已核对患者原话、确认结果和证据链。",
        occurred_at=_after(in_progress),
    )
    retry = service.record_outcome(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        outcome="evidence_consistent",
        note="已核对患者原话、确认结果和证据链。",
        occurred_at=_after(outcome.task),
    )

    assert retry.idempotent_replay is True
    assert outcome.task["status"] == "completed"
    assert communication_readiness(outcome.communication) == PENDING_APPROVAL
    assert outcome.communication["status"] == "preparation"
    assert outcome.communication["payload"] == [
        {"contentString": MANUAL_REVIEW_DRAFT_TEMPLATE}
    ]
    raw_draft = json.dumps(outcome.communication, ensure_ascii=False)
    assert "sent" not in outcome.communication
    assert "received" not in outcome.communication
    assert "已核对患者原话、确认结果和证据链。" not in raw_draft
    assert "evidence_consistent" not in raw_draft
    assert manual_reviews.SEND_ENABLED is False
    assert is_send_eligible(
        outcome.communication, current_task=outcome.task
    ) is False

    approved = service.approve_draft(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        communication_id=outcome.communication["id"],
        note="人工明确批准该合成草稿进入可发送状态。",
        occurred_at=_after(outcome.task),
    )

    assert communication_readiness(approved.communication) == READY_TO_SEND
    assert approved.communication["status"] == "preparation"
    assert "sent" not in approved.communication
    assert "received" not in approved.communication
    assert is_send_eligible(
        approved.communication, current_task=outcome.task
    ) is False
    monkeypatch_state = manual_reviews.SEND_ENABLED
    manual_reviews.SEND_ENABLED = True
    try:
        assert is_send_eligible(
            outcome.communication, current_task=outcome.task
        ) is False
        assert is_send_eligible(
            approved.communication, current_task=outcome.task
        ) is True
    finally:
        manual_reviews.SEND_ENABLED = monkeypatch_state

    versions = repository.list_fhir_resources(
        patient_id=DEMO_PATIENT_ID, resource_type="Task", current_only=False
    )
    manual_versions = sorted(
        (item for item in versions if item["id"] == task["id"]),
        key=lambda item: int(item["meta"]["versionId"]),
    )
    assert [item["status"] for item in manual_versions] == [
        "requested",
        "received",
        "accepted",
        "in-progress",
        "completed",
    ]
    communications = service.list_communications_for_task(
        DEMO_PATIENT_ID, task["id"], current_only=False
    )
    assert [
        communication_readiness(item)
        for item in sorted(
            communications, key=lambda item: int(item["meta"]["versionId"])
        )
    ] == [PENDING_APPROVAL, READY_TO_SEND]
    assert all("sent" not in item and "received" not in item for item in communications)
    assert all(
        "已核对患者原话、确认结果和证据链。"
        not in json.dumps(item, ensure_ascii=False)
        for item in communications
    )
    before_conflict = _counts(db_path)
    with pytest.raises(ValueError, match="must be 'in-progress'"):
        service.record_outcome(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            outcome="clarification_needed",
            note="不能用不同结果覆盖已完成处理。",
            occurred_at=_after(approved.communication),
        )
    assert _counts(db_path) == before_conflict

    audit_types = [
        item.event_type for item in store.list_audit_events(DEMO_PATIENT_ID)
    ]
    assert audit_types.count("manual_review_task_acknowledged") == 1
    assert audit_types.count("manual_review_task_started") == 1
    assert audit_types.count("manual_review_outcome_recorded") == 1
    assert audit_types.count("manual_review_communication_approved") == 1
    assert load_builtin_pathways().get("GLP1-14D").clinical_rules == []


def test_patient_visible_template_is_outcome_symmetric(tmp_path):
    payloads = []
    for outcome in ("evidence_consistent", "clarification_needed"):
        db_path = tmp_path / f"{outcome}.db"
        _, _, service, task = _scenario(db_path)
        in_progress = _advance_to_in_progress(service, task)
        result = service.record_outcome(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            outcome=outcome,
            note=f"合成处理记录：{outcome}",
            occurred_at=_after(in_progress),
        )
        payloads.append(result.communication["payload"])

    assert payloads == [[{"contentString": MANUAL_REVIEW_DRAFT_TEMPLATE}]] * 2


@pytest.mark.parametrize("decision", ["rejected", "cancelled"])
def test_rejected_and_cancelled_write_only_task_provenance_and_audit(
    tmp_path, decision
):
    db_path = tmp_path / f"{decision}.db"
    _, _, service, task = _scenario(db_path)
    if decision == "rejected":
        received = service.acknowledge(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            note="已收到，准备判断是否接受。",
            occurred_at=_after(task),
        )
        task = received.task
    before = _counts(db_path)

    result = getattr(service, "reject" if decision == "rejected" else "cancel")(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        note=f"合成任务{decision}理由。",
        occurred_at=_after(task),
    )

    after = _counts(db_path)
    assert result.task["status"] == decision
    assert after == {
        **before,
        "tasks": before["tasks"] + 1,
        "provenances": before["provenances"] + 1,
        "audits": before["audits"] + 1,
    }
    assert service.list_communications_for_task(DEMO_PATIENT_ID, task["id"]) == []
    assert "output" not in result.task


class _FailingLayer4Store(Layer4SQLiteStore):
    def _manual_review_action_fault(self, event_type: str) -> None:
        if event_type == "manual_review_outcome_recorded":
            raise RuntimeError("injected M5-B failure")


def test_record_outcome_failure_rolls_back_task_draft_provenance_and_audit(tmp_path):
    db_path = tmp_path / "manual-failure.db"
    layer4_store = _FailingLayer4Store(db_path)
    _, _, service, task = _scenario(db_path, layer4_store=layer4_store)
    in_progress = _advance_to_in_progress(service, task)
    before = _counts(db_path)

    with pytest.raises(RuntimeError, match="injected M5-B failure"):
        service.record_outcome(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            outcome="evidence_consistent",
            note="这次动作必须整体回滚。",
            occurred_at=_after(in_progress),
        )

    assert _counts(db_path) == before
    assert layer4_store.get_fhir_resource("Task", task["id"])["status"] == (
        "in-progress"
    )


def test_evidence_integrity_failure_is_fail_closed(tmp_path):
    db_path = tmp_path / "evidence-failure.db"
    _, repository, service, task = _scenario(db_path)
    in_progress = _advance_to_in_progress(service, task)
    before = _counts(db_path)
    response_id = task["reasonReference"]["reference"].split("/", 1)[1]
    with connect(db_path) as connection:
        connection.execute(
            "DELETE FROM fhir_questionnaire_responses WHERE resource_id = ?",
            (response_id,),
        )

    with pytest.raises(ValueError, match="response missing"):
        service.record_outcome(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            outcome="evidence_consistent",
            note="证据缺失时不能处理。",
            occurred_at=_after(in_progress),
        )

    assert _counts(db_path) == before
    assert repository.get_fhir_resource("Task", task["id"])["status"] == (
        "in-progress"
    )
    cancelled = service.cancel(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        note="证据完整性失败后取消任务。",
        occurred_at=_after(in_progress),
    )
    assert cancelled.task["status"] == "cancelled"
    assert cancelled.task["statusReason"]["coding"][0]["code"] == (
        "evidence-integrity-failure"
    )
    assert service.list_communications_for_task(DEMO_PATIENT_ID, task["id"]) == []


def test_concurrent_record_and_approval_each_create_one_atomic_action(tmp_path):
    db_path = tmp_path / "manual-concurrent.db"
    _, _, service, task = _scenario(db_path)
    in_progress = _advance_to_in_progress(service, task)
    record_at = _after(in_progress)

    def record():
        local = ManualReviewWorkflowService(SQLiteStore(db_path))
        return local.record_outcome(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            outcome="evidence_consistent",
            note="并发幂等处理记录。",
            occurred_at=record_at,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: record(), range(2)))

    assert {item.communication["id"] for item in outcomes} == {
        outcomes[0].communication["id"]
    }
    assert sum(item.idempotent_replay for item in outcomes) == 1
    approve_at = _after(outcomes[0].task)

    def approve():
        local = ManualReviewWorkflowService(SQLiteStore(db_path))
        return local.approve_draft(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            communication_id=outcomes[0].communication["id"],
            note="并发明确批准。",
            occurred_at=approve_at,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        approvals = list(pool.map(lambda _: approve(), range(2)))

    assert sum(item.idempotent_replay for item in approvals) == 1
    with connect(db_path) as connection:
        ready_rows = [
            json.loads(row["resource_json"])
            for row in connection.execute(
                """
                SELECT resource_json FROM layer4_fhir_resources
                WHERE resource_type='Communication'
                """
            ).fetchall()
            if communication_readiness(json.loads(row["resource_json"]))
            == READY_TO_SEND
        ]
    assert len(ready_rows) == 1

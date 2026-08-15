"""Application orchestration for patient confirmation to manual nurse review."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from continucare.agents.contracts import SemanticResult
from continucare.care_agent import CareAgentService
from continucare.care_engine import CareEngine
from continucare.db import utc_now_iso
from continucare.layer4.fhir import (
    build_patient_confirmation_provenance,
    build_patient_confirmed_review_task,
)
from continucare.layer4.manual_reviews import admit_final_patient_report
from continucare.layer4.storage import Layer4SQLiteStore
from continucare.models import AuditEvent, CareSessionStatus, Observation


class ConfirmedReviewResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    receipt_digest: str
    questionnaire_response: dict[str, Any]
    observations: list[Observation]
    task: dict[str, Any]
    provenance: dict[str, Any]
    idempotent_replay: bool = False


class ConfirmedReviewService:
    """Release one complete, human-confirmed bundle or release nothing."""

    def __init__(self, store, *, care_agent: CareAgentService, care_engine: CareEngine):
        self.store = store
        self.care_agent = care_agent
        self.care_engine = care_engine
        self.layer4_store = Layer4SQLiteStore(store.db_path)

    def accept_all(self, run_id: str, candidate_ids: list[str]) -> ConfirmedReviewResult:
        receipt, record, candidates = self._receipt(run_id, candidate_ids)
        task_id = f"task-manual-review-{receipt[:24]}"
        existing = self.layer4_store.get_fhir_resource("Task", task_id)
        if existing is not None:
            return self._stored_result(receipt, existing)

        confirmed_at = utc_now_iso()
        try:
            candidate_plan = self.care_agent.prepare_confirmed_candidates(
                run_id,
                candidate_ids,
                resolved_at=confirmed_at,
                require_complete_set=True,
            )
            response_id = f"response-confirmed-{receipt[:24]}"
            submission = self.care_engine.prepare_completion(
                record.session_id,
                candidate_plan.answers,
                response_id=response_id,
                completed_at=confirmed_at,
                answer_contexts=candidate_plan.answer_contexts,
                symptom_reports=candidate_plan.symptom_reports,
            )
        except ValueError:
            # A concurrent caller may have committed the exact receipt between
            # the initial replay lookup and the pure preparation reads.
            existing = self.layer4_store.get_fhir_resource("Task", task_id)
            if existing is not None:
                return self._stored_result(receipt, existing)
            raise
        response, observations = admit_final_patient_report(
            patient_id=record.patient_id,
            questionnaire_response=submission.questionnaire_response,
            observations=[item.as_fhir() for item in submission.observations],
        )
        observation_refs = [f"Observation/{item['id']}" for item in observations]
        pathway_reference = (
            f"urn:continucare:pathway:{submission.session.pathway_code}"
            f"|{submission.session.pathway_version}"
        )
        task = build_patient_confirmed_review_task(
            patient_id=record.patient_id,
            receipt_digest=receipt,
            questionnaire_response_reference=f"QuestionnaireResponse/{response['id']}",
            observation_references=observation_refs,
            pathway_reference=pathway_reference,
            authored_on=confirmed_at,
            task_id=task_id,
        )
        confirmation_audit = self._audit(
            receipt,
            "confirmation",
            patient_id=record.patient_id,
            entity_type="AgentRun",
            entity_id=record.run_id,
            event_type="semantic_candidate_patient_decision",
            actor_type="synthetic_patient",
            created_at=confirmed_at,
            details={
                "session_id": record.session_id,
                "decision": "accepted_for_manual_review",
                "candidate_ids": [item.candidate_id for item in candidates],
                "confirmed_link_ids": [item.link_id for item in candidates],
                "clinical_assessment": "not_assessed",
            },
        )
        response_audit = self._audit(
            receipt,
            "response",
            patient_id=record.patient_id,
            entity_type="QuestionnaireResponse",
            entity_id=response["id"],
            event_type="questionnaire_response_completed",
            actor_type="synthetic_patient",
            created_at=confirmed_at,
            details={
                "session_id": record.session_id,
                "answered_link_ids": sorted(candidate_plan.answers),
                "observation_refs": [item["id"] for item in observations],
                "clinical_assessment": "not_assessed",
            },
        )
        task_audit = self._audit(
            receipt,
            "task",
            patient_id=record.patient_id,
            entity_type="Task",
            entity_id=task["id"],
            event_type="manual_review_task_created",
            actor_type="deterministic_workflow",
            created_at=confirmed_at,
            details={
                "questionnaire_response_ref": response["id"],
                "observation_refs": [item["id"] for item in observations],
                "priority": "routine",
                "clinical_assessment": "not_assessed",
                "authorization_basis": "explicit_patient_confirmation",
            },
        )
        provenance = build_patient_confirmation_provenance(
            target_references=[
                f"QuestionnaireResponse/{response['id']}",
                *observation_refs,
                f"Task/{task['id']}",
            ],
            entity_source_references=[
                f"QuestionnaireResponse/{response['id']}",
                *observation_refs,
                f"urn:continucare:audit-event:{confirmation_audit.event_id}",
            ],
            confirmed_at=confirmed_at,
            patient_id=record.patient_id,
            provenance_id=f"provenance-confirmed-{receipt[:24]}",
        )
        created = self.store.persist_confirmed_review_bundle(
            session=submission.session,
            message=submission.message,
            questionnaire_response=response,
            questionnaire=submission.questionnaire,
            observations=submission.observations,
            answer_contexts=candidate_plan.answer_contexts,
            symptom_reports=candidate_plan.symptom_reports,
            action_ids=[item.candidate_id for item in candidates],
            source_run_id=record.run_id,
            resolved_at=confirmed_at,
            audit_events=[confirmation_audit, response_audit, task_audit],
            layer4_resources=[task, provenance],
        )
        if not created:
            stored = self.layer4_store.get_fhir_resource("Task", task_id)
            if stored is None:
                raise RuntimeError("幂等提交未找到已存在的护士复核任务")
            return self._stored_result(receipt, stored)
        return ConfirmedReviewResult(
            receipt_digest=receipt,
            questionnaire_response=response,
            observations=submission.observations,
            task=task,
            provenance=provenance,
        )

    def _receipt(self, run_id: str, candidate_ids: list[str]):
        record = self.store.get_agent_run(run_id)
        if record is None:
            raise ValueError("Agent 运行记录不存在")
        result = SemanticResult.model_validate(record.output_json)
        available = {item.candidate_id: item for item in result.candidates}
        if not candidate_ids or set(candidate_ids) != set(available):
            raise ValueError("必须一次处理本轮全部候选")
        candidates = [available[item_id] for item_id in sorted(candidate_ids)]
        session = self.store.get_care_session(record.session_id)
        if session is None or session.patient_id != record.patient_id:
            raise ValueError("Agent 运行记录与随访会话不一致")
        identity = {
            "patient_id": record.patient_id,
            "session_id": record.session_id,
            "pathway": f"{session.pathway_code}|{session.pathway_version}",
            "run_id": record.run_id,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "link_id": item.link_id,
                    "answer": item.answer,
                    "evidence_text": item.evidence_text,
                }
                for item in candidates
            ],
        }
        encoded = json.dumps(
            identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest(), record, candidates

    def _stored_result(self, receipt: str, task: dict[str, Any]) -> ConfirmedReviewResult:
        response_id = f"response-confirmed-{receipt[:24]}"
        session = next(
            (
                item
                for item in self.store.list_care_sessions(task["for"]["reference"].split("/", 1)[1])
                if item.questionnaire_response_id == response_id
            ),
            None,
        )
        if session is None or session.status != CareSessionStatus.COMPLETED:
            raise RuntimeError("已存在任务缺少完成的随访证据")
        response = self.store.get_questionnaire_response(response_id)
        provenance = self.layer4_store.get_fhir_resource(
            "Provenance", f"provenance-confirmed-{receipt[:24]}"
        )
        if response is None or provenance is None:
            raise RuntimeError("已存在任务的证据链不完整")
        return ConfirmedReviewResult(
            receipt_digest=receipt,
            questionnaire_response=response,
            observations=self.store.list_observations_for_message(response_id),
            task=task,
            provenance=provenance,
            idempotent_replay=True,
        )

    @staticmethod
    def _audit(
        receipt: str,
        suffix: str,
        *,
        patient_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_type: str,
        created_at: str,
        details: dict[str, Any],
    ) -> AuditEvent:
        event_hash = hashlib.sha256(f"{receipt}|{suffix}".encode("utf-8")).hexdigest()
        return AuditEvent(
            event_id=f"audit_{event_hash[:32]}",
            patient_id=patient_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor_type=actor_type,
            details_json=details,
            created_at=created_at,
        )

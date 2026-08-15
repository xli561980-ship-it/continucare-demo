"""Atomic, synthetic-only processing for patient-confirmed manual review Tasks."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from continucare.layer4.fhir import (
    build_manual_review_action_provenance,
    build_manual_review_communication,
    validate_layer4_fhir_resource,
)
from continucare.layer4.manual_reviews import (
    EVIDENCE_DIGEST_EXTENSION_URL,
    MANUAL_REVIEW_OUTCOME_LABELS,
    MANUAL_REVIEW_COMMUNICATION_IDENTIFIER_SYSTEM,
    PENDING_APPROVAL,
    READY_TO_SEND,
    admit_final_patient_report,
    communication_readiness,
    is_manual_review_communication,
    is_manual_review_task,
)
from continucare.layer4.storage import Layer4SQLiteStore
from continucare.layer4.tasks import is_task_transition_allowed
from continucare.models import AuditEvent


SYNTHETIC_NURSE_REFERENCE = "PractitionerRole/synthetic-nurse-review"
MANUAL_REVIEW_DRAFT_TEMPLATE = (
    "您好，我们已人工查看并记录您提交且确认的合成随访内容。"
    "此沟通草稿仅用于确认记录，不构成诊断、风险评估、治疗或用药建议。"
)

ManualReviewOutcome = Literal["evidence_consistent", "clarification_needed"]

_OUTCOME_LABELS = MANUAL_REVIEW_OUTCOME_LABELS


class ManualReviewActionResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    action: str
    task: dict[str, Any]
    provenance: dict[str, Any]
    audit_event: AuditEvent
    communication: dict[str, Any] | None = None
    idempotent_replay: bool = False


class _EvidenceBundle(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    response: dict[str, Any]
    observations: list[dict[str, Any]]
    source_references: list[str]
    digest: str


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("manual review times must include a timezone offset")
    return parsed


def _reference_set(task: dict[str, Any]) -> set[str]:
    return {
        item.get("valueReference", {}).get("reference")
        for item in task.get("input", [])
        if item.get("valueReference", {}).get("reference")
    }


class ManualReviewWorkflowService:
    """Advance only M5-A manual Tasks and never expose a send operation."""

    def __init__(self, store, *, layer4_store: Layer4SQLiteStore | None = None):
        self.store = store
        self.repository = layer4_store or Layer4SQLiteStore(store.db_path)

    def acknowledge(
        self,
        *,
        patient_id: str,
        task_id: str,
        note: str,
        occurred_at: str,
        actor_reference: str = SYNTHETIC_NURSE_REFERENCE,
    ) -> ManualReviewActionResult:
        task = self._task(patient_id, task_id)
        replay = self._replay(
            patient_id, task_id, "manual_review_task_acknowledged", actor_reference, note
        )
        if replay:
            return replay
        self._require_status(task, "requested")
        evidence = self._evidence(patient_id, task)
        updated = self._advance(
            task,
            "received",
            actor_reference=actor_reference,
            note=self._note(note),
            occurred_at=occurred_at,
        )
        return self._commit_action(
            action="acknowledge",
            event_type="manual_review_task_acknowledged",
            event_label="护士确认收到人工复核任务",
            patient_id=patient_id,
            actor_reference=actor_reference,
            note=self._note(note),
            occurred_at=occurred_at,
            expected_task=task,
            task_versions=[updated],
            evidence=evidence,
        )

    def start(
        self,
        *,
        patient_id: str,
        task_id: str,
        note: str,
        occurred_at: str,
        actor_reference: str = SYNTHETIC_NURSE_REFERENCE,
    ) -> ManualReviewActionResult:
        task = self._task(patient_id, task_id)
        replay = self._replay(
            patient_id, task_id, "manual_review_task_started", actor_reference, note
        )
        if replay:
            return replay
        self._require_status(task, "received")
        evidence = self._evidence(patient_id, task)
        accepted = self._advance(
            task,
            "accepted",
            actor_reference=actor_reference,
            note=self._note(note),
            occurred_at=occurred_at,
        )
        in_progress = self._advance(
            accepted,
            "in-progress",
            actor_reference=actor_reference,
            note=None,
            occurred_at=occurred_at,
            allow_same_time=True,
        )
        return self._commit_action(
            action="accept-and-start",
            event_type="manual_review_task_started",
            event_label="护士接受并开始人工复核任务",
            patient_id=patient_id,
            actor_reference=actor_reference,
            note=self._note(note),
            occurred_at=occurred_at,
            expected_task=task,
            task_versions=[accepted, in_progress],
            evidence=evidence,
        )

    def reject(
        self,
        *,
        patient_id: str,
        task_id: str,
        note: str,
        occurred_at: str,
        actor_reference: str = SYNTHETIC_NURSE_REFERENCE,
    ) -> ManualReviewActionResult:
        return self._close_without_draft(
            patient_id=patient_id,
            task_id=task_id,
            to_status="rejected",
            event_type="manual_review_task_rejected",
            note=note,
            occurred_at=occurred_at,
            actor_reference=actor_reference,
        )

    def cancel(
        self,
        *,
        patient_id: str,
        task_id: str,
        note: str,
        occurred_at: str,
        actor_reference: str = SYNTHETIC_NURSE_REFERENCE,
    ) -> ManualReviewActionResult:
        return self._close_without_draft(
            patient_id=patient_id,
            task_id=task_id,
            to_status="cancelled",
            event_type="manual_review_task_cancelled",
            note=note,
            occurred_at=occurred_at,
            actor_reference=actor_reference,
        )

    def record_outcome(
        self,
        *,
        patient_id: str,
        task_id: str,
        outcome: ManualReviewOutcome,
        note: str,
        occurred_at: str,
        actor_reference: str = SYNTHETIC_NURSE_REFERENCE,
    ) -> ManualReviewActionResult:
        if outcome not in _OUTCOME_LABELS:
            raise ValueError("unsupported manual review outcome")
        note_text = self._note(note)
        payload_key = f"{outcome}\x1f{note_text}"
        task = self._task(patient_id, task_id)
        replay = self._replay(
            patient_id,
            task_id,
            "manual_review_outcome_recorded",
            actor_reference,
            payload_key,
        )
        if replay:
            return replay
        self._require_status(task, "in-progress")
        evidence = self._evidence(patient_id, task)
        action_digest = self._action_digest(
            task=task,
            action="record-outcome",
            actor_reference=actor_reference,
            payload=payload_key,
        )
        communication_id = f"communication-manual-review-{action_digest[:24]}"
        completed = self._advance(
            task,
            "completed",
            actor_reference=actor_reference,
            note=note_text,
            occurred_at=occurred_at,
        )
        completed["output"] = [
            {
                "type": {
                    "coding": [
                        {
                            "system": "urn:continucare:manual-review-output",
                            "code": "review-outcome",
                            "display": "Manual review outcome",
                        }
                    ]
                },
                "valueCode": outcome,
            },
            {
                "type": {
                    "coding": [
                        {
                            "system": "urn:continucare:manual-review-output",
                            "code": "review-note",
                            "display": "Manual review note",
                        }
                    ]
                },
                "valueString": note_text,
            },
            {
                "type": {
                    "coding": [
                        {
                            "system": "urn:continucare:manual-review-output",
                            "code": "evidence-digest",
                            "display": "Evidence digest",
                        }
                    ]
                },
                "valueString": evidence.digest,
            },
            {
                "type": {
                    "coding": [
                        {
                            "system": "urn:continucare:manual-review-output",
                            "code": "communication-draft",
                            "display": "Communication draft",
                        }
                    ]
                },
                "valueReference": {"reference": f"Communication/{communication_id}"},
            },
        ]
        completed = validate_layer4_fhir_resource(
            completed, expected_resource_type="Task"
        )
        task_reference = (
            f"Task/{task_id}/_history/{completed['meta']['versionId']}"
        )
        communication = build_manual_review_communication(
            patient_id=patient_id,
            task_reference=task_reference,
            evidence_references=evidence.source_references,
            evidence_digest=evidence.digest,
            content_text=MANUAL_REVIEW_DRAFT_TEMPLATE,
            updated_at=occurred_at,
            communication_id=communication_id,
            identifier_digest=action_digest,
            readiness=PENDING_APPROVAL,
        )
        return self._commit_action(
            action="record-outcome",
            event_type="manual_review_outcome_recorded",
            event_label="护士记录人工复核结果并生成待批准草稿",
            patient_id=patient_id,
            actor_reference=actor_reference,
            note=payload_key,
            occurred_at=occurred_at,
            expected_task=task,
            task_versions=[completed],
            evidence=evidence,
            communication=communication,
            extra_details={
                "outcome": outcome,
                "outcome_label": _OUTCOME_LABELS[outcome],
                "nurse_note": note_text,
                "communication_readiness": PENDING_APPROVAL,
            },
        )

    def approve_draft(
        self,
        *,
        patient_id: str,
        task_id: str,
        communication_id: str,
        note: str,
        occurred_at: str,
        actor_reference: str = SYNTHETIC_NURSE_REFERENCE,
    ) -> ManualReviewActionResult:
        note_text = self._note(note)
        task = self._task(patient_id, task_id)
        replay = self._replay(
            patient_id,
            task_id,
            "manual_review_communication_approved",
            actor_reference,
            note_text,
        )
        if replay:
            return replay
        self._require_status(task, "completed")
        current = self.repository.get_fhir_resource(
            "Communication", communication_id
        )
        if current is None or not is_manual_review_communication(current):
            raise ValueError("manual review Communication draft was not found")
        if current.get("subject", {}).get("reference") != f"Patient/{patient_id}":
            raise ValueError("manual review Communication patient mismatch")
        if communication_readiness(current) != PENDING_APPROVAL:
            replay = self._replay(
                patient_id,
                task_id,
                "manual_review_communication_approved",
                actor_reference,
                note_text,
            )
            if replay:
                return replay
            raise ValueError("only a pending draft can be approved")
        task_reference = f"Task/{task_id}/_history/{task['meta']['versionId']}"
        if task_reference not in {
            item.get("reference") for item in current.get("basedOn", [])
        }:
            raise ValueError("manual review draft is not based on the completed Task")
        evidence = self._evidence(patient_id, task)
        stored_digest = self._extension_value(current, EVIDENCE_DIGEST_EXTENSION_URL)
        if stored_digest != evidence.digest:
            raise ValueError("manual review draft evidence digest mismatch")
        identifier = next(
            item["value"]
            for item in current["identifier"]
            if item.get("system") == MANUAL_REVIEW_COMMUNICATION_IDENTIFIER_SYSTEM
        )
        approved = build_manual_review_communication(
            patient_id=patient_id,
            task_reference=task_reference,
            evidence_references=[item["reference"] for item in current["about"]],
            evidence_digest=evidence.digest,
            content_text=current["payload"][0]["contentString"],
            updated_at=occurred_at,
            communication_id=communication_id,
            identifier_digest=identifier,
            readiness=READY_TO_SEND,
            version_id=str(int(current["meta"]["versionId"]) + 1),
            approver_reference=actor_reference,
            approval_note=note_text,
        )
        return self._commit_action(
            action="approve-draft",
            event_type="manual_review_communication_approved",
            event_label="护士明确批准沟通草稿进入可发送状态",
            patient_id=patient_id,
            actor_reference=actor_reference,
            note=note_text,
            occurred_at=occurred_at,
            expected_task=task,
            task_versions=[],
            evidence=evidence,
            communication=approved,
            expected_communication=current,
            extra_details={"communication_readiness": READY_TO_SEND},
        )

    def list_communications_for_task(
        self, patient_id: str, task_id: str, *, current_only: bool = True
    ) -> list[dict[str, Any]]:
        prefix = f"Task/{task_id}/_history/"
        return [
            item
            for item in self.repository.list_fhir_resources(
                patient_id=patient_id,
                resource_type="Communication",
                current_only=current_only,
            )
            if is_manual_review_communication(item)
            and any(
                ref.get("reference", "").startswith(prefix)
                for ref in item.get("basedOn", [])
            )
        ]

    def _close_without_draft(
        self,
        *,
        patient_id: str,
        task_id: str,
        to_status: Literal["rejected", "cancelled"],
        event_type: str,
        note: str,
        occurred_at: str,
        actor_reference: str,
    ) -> ManualReviewActionResult:
        note_text = self._note(note)
        task = self._task(patient_id, task_id)
        replay = self._replay(
            patient_id, task_id, event_type, actor_reference, note_text
        )
        if replay:
            return replay
        if to_status == "rejected" and task.get("status") != "received":
            raise ValueError("manual review Task can only be rejected after receipt")
        if not is_task_transition_allowed(task["status"], to_status):
            raise ValueError(
                f"Task transition {task['status']!r} -> {to_status!r} is not allowed"
            )
        evidence_integrity_failure: str | None = None
        try:
            evidence = self._evidence(patient_id, task)
        except ValueError as exc:
            if to_status != "cancelled":
                raise
            evidence_integrity_failure = str(exc)
            source_references = sorted(
                {
                    task.get("reasonReference", {}).get("reference"),
                    *_reference_set(task),
                }
                - {None, ""}
            )
            evidence = _EvidenceBundle(
                response={},
                observations=[],
                source_references=source_references,
                digest=_sha256(
                    {
                        "integrity": "failed",
                        "task_id": task["id"],
                        "source_references": source_references,
                    }
                ),
            )
        updated = self._advance(
            task,
            to_status,
            actor_reference=actor_reference,
            note=note_text,
            occurred_at=occurred_at,
            status_reason=(
                "nurse-rejected"
                if to_status == "rejected"
                else (
                    "evidence-integrity-failure"
                    if evidence_integrity_failure
                    else "nurse-cancelled"
                )
            ),
        )
        return self._commit_action(
            action=to_status,
            event_type=event_type,
            event_label=f"护士{to_status}人工复核任务",
            patient_id=patient_id,
            actor_reference=actor_reference,
            note=note_text,
            occurred_at=occurred_at,
            expected_task=task,
            task_versions=[updated],
            evidence=evidence,
            extra_details={
                "evidence_integrity_failure": evidence_integrity_failure
            },
        )

    def _task(self, patient_id: str, task_id: str) -> dict[str, Any]:
        task = self.repository.get_fhir_resource("Task", task_id)
        if task is None:
            raise ValueError("manual review Task was not found")
        if not is_manual_review_task(task):
            raise ValueError("Task is not a patient-confirmed manual review")
        if task.get("for", {}).get("reference") != f"Patient/{patient_id}":
            raise ValueError("manual review Task patient mismatch")
        return task

    def _evidence(self, patient_id: str, task: dict[str, Any]) -> _EvidenceBundle:
        response_ref = task.get("reasonReference", {}).get("reference", "")
        if not response_ref.startswith("QuestionnaireResponse/"):
            raise ValueError("manual review Task lacks its completed response")
        response_id = response_ref.split("/", 1)[1]
        response = self.store.get_questionnaire_response(response_id)
        if response is None:
            raise ValueError("manual review evidence integrity check failed: response missing")
        observation_refs = sorted(
            ref for ref in _reference_set(task) if ref.startswith("Observation/")
        )
        observations: list[dict[str, Any]] = []
        for reference in observation_refs:
            observation = self.store.get_observation(reference.split("/", 1)[1])
            if observation is None:
                raise ValueError(
                    "manual review evidence integrity check failed: Observation missing"
                )
            observations.append(observation.as_fhir())
        response, observations = admit_final_patient_report(
            patient_id=patient_id,
            questionnaire_response=response,
            observations=observations,
        )
        expected_refs = {response_ref, *observation_refs}
        if _reference_set(task) != expected_refs:
            raise ValueError("manual review Task evidence references changed")
        message = self.store.get_message(response_id)
        if message is None or message.patient_id != patient_id:
            raise ValueError("manual review evidence integrity check failed: message missing")
        evidence_payload = {
            "message": message.model_dump(mode="json"),
            "questionnaire_response": response,
            "observations": sorted(observations, key=lambda item: item["id"]),
        }
        return _EvidenceBundle(
            response=response,
            observations=observations,
            source_references=[
                f"urn:continucare:followup-message:{message.message_id}",
                response_ref,
                *observation_refs,
            ],
            digest=_sha256(evidence_payload),
        )

    @staticmethod
    def _note(value: str) -> str:
        note = value.strip()
        if not note:
            raise ValueError("manual review action requires a non-blank note")
        if len(note) > 1000:
            raise ValueError("manual review note cannot exceed 1000 characters")
        return note

    @staticmethod
    def _require_status(task: dict[str, Any], status: str) -> None:
        if task.get("status") != status:
            raise ValueError(
                f"manual review Task must be {status!r}, got {task.get('status')!r}"
            )

    @staticmethod
    def _advance(
        task: dict[str, Any],
        to_status: str,
        *,
        actor_reference: str,
        note: str | None,
        occurred_at: str,
        status_reason: str | None = None,
        allow_same_time: bool = False,
    ) -> dict[str, Any]:
        if not actor_reference.startswith("PractitionerRole/synthetic-"):
            raise ValueError("M5-B only accepts a synthetic nurse identity")
        if not is_task_transition_allowed(task["status"], to_status):
            raise ValueError(
                f"Task transition {task['status']!r} -> {to_status!r} is not allowed"
            )
        if (
            _instant(occurred_at) < _instant(task["meta"]["lastUpdated"])
            or (
                not allow_same_time
                and _instant(occurred_at) == _instant(task["meta"]["lastUpdated"])
            )
        ):
            raise ValueError("manual review action time must follow the current version")
        updated = deepcopy(task)
        updated["status"] = to_status
        updated["meta"] = {
            **updated["meta"],
            "versionId": str(int(updated["meta"]["versionId"]) + 1),
            "lastUpdated": occurred_at,
        }
        if note is not None:
            updated.setdefault("note", []).append(
                {
                    "authorReference": {"reference": actor_reference},
                    "time": occurred_at,
                    "text": note,
                }
            )
        if status_reason:
            updated["statusReason"] = {
                "coding": [
                    {
                        "system": "urn:continucare:manual-review-status-reason",
                        "code": status_reason,
                        "display": status_reason,
                    }
                ]
            }
        if to_status == "in-progress":
            updated.setdefault("executionPeriod", {})["start"] = occurred_at
        if to_status in {"rejected", "cancelled", "completed"}:
            updated.setdefault("executionPeriod", {}).setdefault(
                "start", task.get("authoredOn", occurred_at)
            )
            updated["executionPeriod"]["end"] = occurred_at
        return validate_layer4_fhir_resource(updated, expected_resource_type="Task")

    def _commit_action(
        self,
        *,
        action: str,
        event_type: str,
        event_label: str,
        patient_id: str,
        actor_reference: str,
        note: str,
        occurred_at: str,
        expected_task: dict[str, Any],
        task_versions: list[dict[str, Any]],
        evidence: _EvidenceBundle,
        communication: dict[str, Any] | None = None,
        expected_communication: dict[str, Any] | None = None,
        extra_details: dict[str, Any] | None = None,
    ) -> ManualReviewActionResult:
        if not actor_reference.startswith("PractitionerRole/synthetic-"):
            raise ValueError("M5-B only accepts a synthetic nurse identity")
        action_digest = self._action_digest(
            task=expected_task,
            action=action,
            actor_reference=actor_reference,
            payload=note,
        )
        targets = [
            f"Task/{item['id']}/_history/{item['meta']['versionId']}"
            for item in task_versions
        ]
        if communication is not None:
            targets.append(
                f"Communication/{communication['id']}/_history/{communication['meta']['versionId']}"
            )
        sources = [
            f"Task/{expected_task['id']}/_history/{expected_task['meta']['versionId']}",
            *evidence.source_references,
        ]
        if expected_communication is not None:
            sources.append(
                f"Communication/{expected_communication['id']}/_history/{expected_communication['meta']['versionId']}"
            )
        provenance = build_manual_review_action_provenance(
            target_references=targets,
            source_references=sources,
            evidence_digest=evidence.digest,
            action_code=action,
            action_display=event_label,
            actor_reference=actor_reference,
            occurred_at=occurred_at,
            provenance_id=f"provenance-manual-action-{action_digest[:24]}",
        )
        payload_hash = _sha256(note)
        final_task = task_versions[-1] if task_versions else expected_task
        details = {
            "action": action,
            "actor_reference": actor_reference,
            "payload_hash": payload_hash,
            "task_ref": f"Task/{final_task['id']}/_history/{final_task['meta']['versionId']}",
            "task_version": final_task["meta"]["versionId"],
            "task_status": final_task["status"],
            "evidence_refs": evidence.source_references,
            "evidence_digest": evidence.digest,
            "provenance_id": provenance["id"],
            "clinical_assessment": "not_assessed",
            "communication_id": communication["id"] if communication else None,
            **(extra_details or {}),
        }
        audit = AuditEvent(
            event_id=f"audit_{action_digest[:32]}",
            patient_id=patient_id,
            entity_type="Task",
            entity_id=expected_task["id"],
            event_type=event_type,
            actor_type="synthetic_nurse_demo_user",
            details_json=details,
            created_at=occurred_at,
        )
        resources = [*task_versions]
        if communication is not None:
            resources.append(communication)
        resources.append(provenance)
        try:
            created = self.repository.persist_manual_review_action(
                patient_id=patient_id,
                expected_task=expected_task,
                resources=resources,
                audit_event=audit,
                expected_communication=expected_communication,
            )
        except ValueError:
            replay = self._replay(
                patient_id, expected_task["id"], event_type, actor_reference, note
            )
            if replay:
                return replay
            raise
        if not created:
            replay = self._replay(
                patient_id, expected_task["id"], event_type, actor_reference, note
            )
            if replay:
                return replay
            raise RuntimeError("manual review idempotent action is incomplete")
        return ManualReviewActionResult(
            action=action,
            task=final_task,
            communication=communication,
            provenance=provenance,
            audit_event=audit,
        )

    @staticmethod
    def _action_digest(
        *, task: dict[str, Any], action: str, actor_reference: str, payload: str
    ) -> str:
        return _sha256(
            {
                "task_id": task["id"],
                "action": action,
                "from_version": task["meta"]["versionId"],
                "actor_reference": actor_reference,
                "payload_hash": _sha256(payload),
            }
        )

    def _replay(
        self,
        patient_id: str,
        task_id: str,
        event_type: str,
        actor_reference: str,
        payload: str,
    ) -> ManualReviewActionResult | None:
        payload_hash = _sha256(payload)
        event = next(
            (
                item
                for item in self.store.list_audit_events(patient_id)
                if item.entity_type == "Task"
                and item.entity_id == task_id
                and item.event_type == event_type
                and item.details_json.get("actor_reference") == actor_reference
                and item.details_json.get("payload_hash") == payload_hash
            ),
            None,
        )
        if event is None:
            return None
        task = self.repository.get_fhir_resource(
            "Task", task_id, version_id=event.details_json["task_version"]
        )
        provenance = self.repository.get_fhir_resource(
            "Provenance", event.details_json["provenance_id"]
        )
        communication_id = event.details_json.get("communication_id")
        communication = (
            self.repository.get_fhir_resource("Communication", communication_id)
            if communication_id
            else None
        )
        if task is None or provenance is None or (
            communication_id and communication is None
        ):
            raise RuntimeError("manual review replay evidence is incomplete")
        return ManualReviewActionResult(
            action=event.details_json["action"],
            task=task,
            communication=communication,
            provenance=provenance,
            audit_event=event,
            idempotent_replay=True,
        )

    @staticmethod
    def _extension_value(resource: dict[str, Any], url: str) -> str | None:
        matches = [
            item.get("valueString")
            for item in resource.get("extension", [])
            if item.get("url") == url
        ]
        return matches[0] if len(matches) == 1 else None

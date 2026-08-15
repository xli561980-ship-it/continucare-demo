"""Controlled, rule-free admission and read model for manual nurse review."""

from __future__ import annotations

from typing import Any

from continucare.fhir.r4 import validate_r4_resource


MANUAL_REVIEW_IDENTIFIER_SYSTEM = "urn:continucare:patient-confirmed-review"
CLINICAL_RULE_IDENTIFIER_SYSTEM = "urn:continucare:clinical-rule"
MANUAL_REVIEW_COMMUNICATION_IDENTIFIER_SYSTEM = (
    "urn:continucare:manual-review-communication"
)
COMMUNICATION_READINESS_EXTENSION_URL = (
    "urn:continucare:communication-readiness"
)
EVIDENCE_DIGEST_EXTENSION_URL = "urn:continucare:evidence-digest"
PENDING_APPROVAL = "pending-approval"
READY_TO_SEND = "ready-to-send"
MANUAL_REVIEW_OUTCOME_LABELS = {
    "evidence_consistent": "已核对原话、确认结果与最终证据链，记录一致",
    "clarification_needed": "已核对原话、确认结果与最终证据链，需要后续补充说明",
}

# M5-B deliberately has no sender.  A future slice must change this flag and
# still pass is_send_eligible before it can introduce any delivery adapter.
SEND_ENABLED = False


def admit_final_patient_report(
    *,
    patient_id: str,
    questionnaire_response: dict[str, Any],
    observations: list[dict[str, Any]],
    require_observations: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Positive admission predicate shared by every Layer-4 report consumer."""

    response = validate_r4_resource(
        questionnaire_response, expected_resource_type="QuestionnaireResponse"
    )
    if response.get("status") != "completed":
        raise ValueError("Layer 4 only accepts completed QuestionnaireResponse")
    patient_reference = f"Patient/{patient_id}"
    if response.get("subject", {}).get("reference") != patient_reference:
        raise ValueError("QuestionnaireResponse patient does not match admission patient")
    if require_observations and not observations:
        raise ValueError("manual review requires at least one final Observation")
    expected_source = f"QuestionnaireResponse/{response['id']}"
    normalized: list[dict[str, Any]] = []
    for observation in observations:
        resource = validate_r4_resource(
            observation, expected_resource_type="Observation"
        )
        if resource.get("status") != "final":
            raise ValueError("Layer 4 only accepts final Observation resources")
        if resource.get("subject", {}).get("reference") != patient_reference:
            raise ValueError("Observation patient does not match admission patient")
        sources = {
            item.get("reference") for item in resource.get("derivedFrom", [])
        }
        if expected_source not in sources:
            raise ValueError("Observation must derive from the admitted response")
        normalized.append(resource)
    return response, normalized


def has_identifier_system(resource: dict[str, Any], system: str) -> bool:
    return any(item.get("system") == system for item in resource.get("identifier", []))


def is_manual_review_task(resource: dict[str, Any]) -> bool:
    return resource.get("resourceType") == "Task" and has_identifier_system(
        resource, MANUAL_REVIEW_IDENTIFIER_SYSTEM
    )


def is_clinical_rule_task(resource: dict[str, Any]) -> bool:
    return resource.get("resourceType") == "Task" and has_identifier_system(
        resource, CLINICAL_RULE_IDENTIFIER_SYSTEM
    )


def is_manual_review_communication(resource: dict[str, Any]) -> bool:
    return resource.get("resourceType") == "Communication" and has_identifier_system(
        resource, MANUAL_REVIEW_COMMUNICATION_IDENTIFIER_SYSTEM
    )


def communication_readiness(resource: dict[str, Any]) -> str | None:
    values = [
        item.get("valueCode")
        for item in resource.get("extension", [])
        if item.get("url") == COMMUNICATION_READINESS_EXTENSION_URL
    ]
    if len(values) != 1:
        return None
    return values[0]


def is_send_eligible(
    communication: dict[str, Any], *, current_task: dict[str, Any]
) -> bool:
    """Single future-facing approval gate; M5-B never invokes a sender."""

    task_reference = f"Task/{current_task.get('id')}/_history/{current_task.get('meta', {}).get('versionId')}"
    return (
        SEND_ENABLED
        and is_manual_review_communication(communication)
        and is_manual_review_task(current_task)
        and current_task.get("status") == "completed"
        and communication.get("status") == "preparation"
        and communication_readiness(communication) == READY_TO_SEND
        and "sent" not in communication
        and "received" not in communication
        and task_reference
        in {item.get("reference") for item in communication.get("basedOn", [])}
    )


class ManualReviewQueue:
    """Read-only queue; it intentionally exposes no Task transition service."""

    def __init__(self, repository):
        self.repository = repository

    def list_for_patient(self, patient_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.repository.list_fhir_resources(
                patient_id=patient_id,
                resource_type="Task",
                current_only=True,
            )
            if is_manual_review_task(item)
        ]

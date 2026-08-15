"""FHIR R4 builders and validation for Layer-4 workflow resources."""

from __future__ import annotations

from typing import Any, Iterable, Literal
from uuid import uuid4

from continucare.fhir.r4 import FHIRValidationError, validate_r4_resource
from continucare.layer4.manual_reviews import (
    COMMUNICATION_READINESS_EXTENSION_URL,
    EVIDENCE_DIGEST_EXTENSION_URL,
    MANUAL_REVIEW_COMMUNICATION_IDENTIFIER_SYSTEM,
)


LAYER4_FHIR_RESOURCE_TYPES = frozenset({"Communication", "Provenance", "Task"})


def _reference(reference: str) -> dict[str, str]:
    value = reference.strip()
    if not value or " " in value:
        raise ValueError("FHIR reference must be non-empty and contain no spaces")
    return {"reference": value}


def _meta(*, version_id: str, last_updated: str) -> dict[str, str]:
    if not version_id.strip():
        raise ValueError("FHIR meta.versionId cannot be blank")
    return {"versionId": version_id, "lastUpdated": last_updated}


def validate_layer4_fhir_resource(
    resource: dict[str, Any], *, expected_resource_type: str | None = None
) -> dict[str, Any]:
    resource_type = resource.get("resourceType")
    if resource_type not in LAYER4_FHIR_RESOURCE_TYPES:
        raise FHIRValidationError(
            f"FHIR resource type {resource_type!r} is not enabled for Layer 4"
        )
    return validate_r4_resource(
        resource, expected_resource_type=expected_resource_type or str(resource_type)
    )


def build_communication(
    *,
    patient_id: str,
    content_text: str,
    sender_reference: str,
    recipient_references: Iterable[str],
    sent_at: str,
    status: Literal[
        "preparation",
        "in-progress",
        "not-done",
        "on-hold",
        "stopped",
        "completed",
        "entered-in-error",
        "unknown",
    ] = "completed",
    communication_id: str | None = None,
    version_id: str = "1",
    based_on_references: Iterable[str] = (),
    about_references: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one auditable patient or clinician communication."""

    text = content_text.strip()
    if not text:
        raise ValueError("Communication payload cannot be blank")
    recipients = [_reference(item) for item in recipient_references]
    if not recipients:
        raise ValueError("Communication requires at least one recipient")
    resource: dict[str, Any] = {
        "resourceType": "Communication",
        "id": communication_id or f"communication-{uuid4().hex}",
        "meta": _meta(version_id=version_id, last_updated=sent_at),
        "status": status,
        "subject": _reference(f"Patient/{patient_id}"),
        "sender": _reference(sender_reference),
        "recipient": recipients,
        "sent": sent_at,
        "payload": [{"contentString": text}],
    }
    based_on = [_reference(item) for item in based_on_references]
    if based_on:
        resource["basedOn"] = based_on
    about = [_reference(item) for item in about_references]
    if about:
        resource["about"] = about
    return validate_layer4_fhir_resource(
        resource, expected_resource_type="Communication"
    )


def build_manual_review_communication(
    *,
    patient_id: str,
    task_reference: str,
    evidence_references: Iterable[str],
    evidence_digest: str,
    content_text: str,
    updated_at: str,
    communication_id: str,
    identifier_digest: str,
    readiness: Literal["pending-approval", "ready-to-send"],
    version_id: str = "1",
    approver_reference: str | None = None,
    approval_note: str | None = None,
) -> dict[str, Any]:
    """Build an unsent manual-review draft; readiness never implies delivery."""

    text = content_text.strip()
    if not text:
        raise ValueError("manual review Communication payload cannot be blank")
    evidence = list(evidence_references)
    if not evidence:
        raise ValueError("manual review Communication requires evidence")
    for label, digest in (
        ("communication identifier", identifier_digest),
        ("evidence", evidence_digest),
    ):
        normalized = digest.strip().lower()
        if len(normalized) != 64 or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            raise ValueError(f"{label} digest must be an opaque SHA-256 digest")
    resource: dict[str, Any] = {
        "resourceType": "Communication",
        "id": communication_id,
        "meta": _meta(version_id=version_id, last_updated=updated_at),
        "identifier": [
            {
                "system": MANUAL_REVIEW_COMMUNICATION_IDENTIFIER_SYSTEM,
                "value": identifier_digest,
            }
        ],
        "extension": [
            {
                "url": COMMUNICATION_READINESS_EXTENSION_URL,
                "valueCode": readiness,
            },
            {
                "url": EVIDENCE_DIGEST_EXTENSION_URL,
                "valueString": evidence_digest,
            },
        ],
        "status": "preparation",
        "subject": _reference(f"Patient/{patient_id}"),
        "sender": _reference("PractitionerRole/synthetic-nurse-review"),
        "recipient": [_reference(f"Patient/{patient_id}")],
        "basedOn": [_reference(task_reference)],
        "about": [_reference(item) for item in evidence],
        "payload": [{"contentString": text}],
    }
    if readiness == "ready-to-send":
        actor = (approver_reference or "").strip()
        note = (approval_note or "").strip()
        if not actor or not note:
            raise ValueError("ready-to-send requires an explicit approver and note")
        resource["note"] = [
            {
                "authorReference": _reference(actor),
                "time": updated_at,
                "text": note,
            }
        ]
    elif approver_reference is not None or approval_note is not None:
        raise ValueError("pending draft cannot contain approval metadata")
    return validate_layer4_fhir_resource(
        resource, expected_resource_type="Communication"
    )


def build_manual_review_action_provenance(
    *,
    target_references: Iterable[str],
    source_references: Iterable[str],
    evidence_digest: str,
    action_code: str,
    action_display: str,
    actor_reference: str,
    occurred_at: str,
    provenance_id: str,
) -> dict[str, Any]:
    """Record one human action and the deterministic assembly it authorized."""

    targets = [_reference(item) for item in target_references]
    sources = [_reference(item) for item in source_references]
    if not targets or not sources:
        raise ValueError("manual review Provenance requires targets and sources")
    if not action_code.strip() or not action_display.strip():
        raise ValueError("manual review Provenance requires an action")
    resource: dict[str, Any] = {
        "resourceType": "Provenance",
        "id": provenance_id,
        "meta": _meta(version_id="1", last_updated=occurred_at),
        "extension": [
            {
                "url": EVIDENCE_DIGEST_EXTENSION_URL,
                "valueString": evidence_digest,
            }
        ],
        "target": targets,
        "occurredDateTime": occurred_at,
        "recorded": occurred_at,
        "activity": {
            "coding": [
                {
                    "system": "urn:continucare:manual-review-activity",
                    "code": action_code,
                    "display": action_display,
                }
            ]
        },
        "agent": [
            {
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                            "code": "performer",
                            "display": "Performer",
                        }
                    ]
                },
                "who": _reference(actor_reference),
            },
            {
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                            "code": "assembler",
                            "display": "Assembler",
                        }
                    ]
                },
                "who": _reference("Device/continucare-deterministic-assembler"),
            },
        ],
        "entity": [
            {"role": "source", "what": source} for source in sources
        ],
    }
    return validate_layer4_fhir_resource(resource, expected_resource_type="Provenance")


def build_workflow_task(
    *,
    patient_id: str,
    rule_id: str,
    rule_version: str,
    task_code_system: str,
    task_code: str,
    task_code_display: str,
    description: str,
    requester_reference: str,
    owner_reference: str,
    authored_on: str,
    trigger_reference: str,
    due_at: str,
    status: Literal[
        "draft",
        "requested",
        "received",
        "accepted",
        "rejected",
        "ready",
        "cancelled",
        "in-progress",
        "on-hold",
        "failed",
        "completed",
        "entered-in-error",
    ] = "requested",
    intent: Literal[
        "unknown",
        "proposal",
        "plan",
        "order",
        "original-order",
        "reflex-order",
        "filler-order",
        "instance-order",
        "option",
    ] = "order",
    priority: Literal["routine", "urgent", "asap", "stat"] = "routine",
    task_id: str | None = None,
    version_id: str = "1",
    based_on_references: Iterable[str] = (),
    evidence_references: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a governed FHIR Task without evaluating any clinical rule."""

    if not all(
        value.strip()
        for value in (
            rule_id,
            rule_version,
            task_code_system,
            task_code,
            task_code_display,
            description,
        )
    ):
        raise ValueError("Task rule, code and description fields cannot be blank")
    resource: dict[str, Any] = {
        "resourceType": "Task",
        "id": task_id or f"task-{uuid4().hex}",
        "meta": _meta(version_id=version_id, last_updated=authored_on),
        "identifier": [
            {
                "system": "urn:continucare:clinical-rule",
                "value": f"{rule_id}|{rule_version}",
            }
        ],
        "status": status,
        "intent": intent,
        "priority": priority,
        "code": {
            "coding": [
                {
                    "system": task_code_system,
                    "code": task_code,
                    "display": task_code_display,
                }
            ],
            "text": task_code_display,
        },
        "description": description,
        "for": _reference(f"Patient/{patient_id}"),
        "authoredOn": authored_on,
        "requester": _reference(requester_reference),
        "owner": _reference(owner_reference),
        "reasonReference": _reference(trigger_reference),
        "restriction": {"period": {"end": due_at}},
    }
    based_on = [_reference(item) for item in based_on_references]
    if based_on:
        resource["basedOn"] = based_on
    evidence = [_reference(item) for item in evidence_references]
    if evidence:
        resource["input"] = [
            {
                "type": {
                    "coding": [
                        {
                            "system": "urn:continucare:task-input",
                            "code": "rule-evidence",
                            "display": "Rule evidence",
                        }
                    ]
                },
                "valueReference": item,
            }
            for item in evidence
        ]
    return validate_layer4_fhir_resource(resource, expected_resource_type="Task")


def build_patient_confirmed_review_task(
    *,
    patient_id: str,
    receipt_digest: str,
    questionnaire_response_reference: str,
    observation_references: Iterable[str],
    pathway_reference: str,
    authored_on: str,
    task_id: str,
) -> dict[str, Any]:
    """Build a routine human-review Task without a clinical rule or conclusion."""

    digest = receipt_digest.strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("manual review receipt must be an opaque SHA-256 digest")
    observations = list(observation_references)
    if not observations:
        raise ValueError("manual review Task requires final Observation evidence")
    evidence = [questionnaire_response_reference, *observations]
    resource: dict[str, Any] = {
        "resourceType": "Task",
        "id": task_id,
        "meta": _meta(version_id="1", last_updated=authored_on),
        "identifier": [
            {
                "system": "urn:continucare:patient-confirmed-review",
                "value": digest,
            }
        ],
        "status": "requested",
        "intent": "order",
        "priority": "routine",
        "code": {
            "coding": [
                {
                    "system": "urn:continucare:task-code",
                    "code": "patient-confirmed-review",
                    "display": "Patient-confirmed report review",
                }
            ],
            "text": "人工复核患者已确认报告",
        },
        "description": "人工复核患者已确认报告",
        "for": _reference(f"Patient/{patient_id}"),
        "authoredOn": authored_on,
        "requester": _reference(f"Patient/{patient_id}"),
        "owner": _reference("PractitionerRole/nurse-review"),
        "reasonReference": _reference(questionnaire_response_reference),
        "basedOn": [_reference(pathway_reference)],
        "input": [
            {
                "type": {
                    "coding": [
                        {
                            "system": "urn:continucare:task-input",
                            "code": "patient-confirmed-evidence",
                            "display": "Patient-confirmed evidence",
                        }
                    ]
                },
                "valueReference": _reference(reference),
            }
            for reference in evidence
        ],
    }
    return validate_layer4_fhir_resource(resource, expected_resource_type="Task")


def build_patient_confirmation_provenance(
    *,
    target_references: Iterable[str],
    entity_source_references: Iterable[str],
    confirmed_at: str,
    patient_id: str,
    provenance_id: str,
) -> dict[str, Any]:
    """Record the human confirmation and deterministic software assembly roles."""

    targets = [_reference(item) for item in target_references]
    if not targets:
        raise ValueError("patient confirmation Provenance requires targets")
    resource: dict[str, Any] = {
        "resourceType": "Provenance",
        "id": provenance_id,
        "meta": _meta(version_id="1", last_updated=confirmed_at),
        "target": targets,
        "occurredDateTime": confirmed_at,
        "recorded": confirmed_at,
        "activity": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                    "code": "CREATE",
                    "display": "create",
                }
            ]
        },
        "agent": [
            {
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                            "code": "author",
                            "display": "Author",
                        }
                    ]
                },
                "who": _reference(f"Patient/{patient_id}"),
            },
            {
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                            "code": "assembler",
                            "display": "Assembler",
                        }
                    ]
                },
                "who": _reference("Device/continucare-deterministic-assembler"),
            },
        ],
        "entity": [
            {"role": "source", "what": _reference(item)}
            for item in entity_source_references
        ],
    }
    return validate_layer4_fhir_resource(resource, expected_resource_type="Provenance")


def build_provenance(
    *,
    target_references: Iterable[str],
    recorded_at: str,
    agent_reference: str,
    agent_role_code: str,
    agent_role_display: str,
    provenance_id: str | None = None,
    version_id: str = "1",
    activity_code: str | None = None,
    activity_display: str | None = None,
    entity_source_references: Iterable[str] = (),
) -> dict[str, Any]:
    """Build provenance for resource creation, review or revision."""

    targets = [_reference(item) for item in target_references]
    if not targets:
        raise ValueError("Provenance requires at least one target")
    if not agent_role_code.strip() or not agent_role_display.strip():
        raise ValueError("Provenance agent role cannot be blank")
    resource: dict[str, Any] = {
        "resourceType": "Provenance",
        "id": provenance_id or f"provenance-{uuid4().hex}",
        "meta": _meta(version_id=version_id, last_updated=recorded_at),
        "target": targets,
        "recorded": recorded_at,
        "agent": [
            {
                "type": {
                    "coding": [
                        {
                            "system": (
                                "http://terminology.hl7.org/CodeSystem/"
                                "provenance-participant-type"
                            ),
                            "code": agent_role_code,
                            "display": agent_role_display,
                        }
                    ]
                },
                "who": _reference(agent_reference),
            }
        ],
    }
    if activity_code:
        resource["activity"] = {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                    "code": activity_code,
                    "display": activity_display or activity_code,
                }
            ]
        }
    entities = [
        {"role": "source", "what": _reference(item)}
        for item in entity_source_references
    ]
    if entities:
        resource["entity"] = entities
    return validate_layer4_fhir_resource(
        resource, expected_resource_type="Provenance"
    )

"""Versioned, auditable FHIR Task responsibility workflow."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime
from typing import Any

from continucare.layer4.contracts import TaskTransitionResult
from continucare.layer4.fhir import build_provenance, validate_layer4_fhir_resource
from continucare.layer4.repository import Layer4Repository


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"requested", "cancelled", "entered-in-error"}),
    "requested": frozenset({"received", "cancelled", "entered-in-error"}),
    "received": frozenset(
        {"accepted", "rejected", "cancelled", "entered-in-error"}
    ),
    "accepted": frozenset(
        {"ready", "in-progress", "cancelled", "entered-in-error"}
    ),
    "ready": frozenset({"in-progress", "cancelled", "entered-in-error"}),
    "in-progress": frozenset(
        {"on-hold", "completed", "failed", "cancelled", "entered-in-error"}
    ),
    "on-hold": frozenset(
        {"in-progress", "failed", "cancelled", "entered-in-error"}
    ),
}

_TERMINAL_STATUSES = frozenset(
    {"rejected", "cancelled", "failed", "completed", "entered-in-error"}
)


def is_task_transition_allowed(from_status: str, to_status: str) -> bool:
    """Pure shared predicate; callers remain responsible for atomic persistence."""

    return to_status in _ALLOWED_TRANSITIONS.get(from_status, frozenset())


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Task transition times must include a timezone offset")
    return parsed


class TaskWorkflowService:
    """Advance Task status only through the published state machine."""

    def __init__(self, repository: Layer4Repository):
        self.repository = repository

    def transition(
        self,
        *,
        patient_id: str,
        task_id: str,
        to_status: str,
        actor_reference: str,
        note: str,
        transitioned_at: str,
    ) -> TaskTransitionResult:
        actor = actor_reference.strip()
        note_text = note.strip()
        if not actor:
            raise ValueError("Task transition actor_reference cannot be blank")
        if not note_text:
            raise ValueError("Task transition requires a non-blank note")
        current = self.repository.get_fhir_resource("Task", task_id)
        if current is None:
            raise ValueError(f"Task {task_id!r} does not exist")
        if current.get("for", {}).get("reference") != f"Patient/{patient_id}":
            raise ValueError("Task patient does not match transition patient_id")
        current_status = current["status"]
        current_version = current["meta"]["versionId"]
        if current_status == to_status:
            return self._idempotent_retry(
                current=current,
                patient_id=patient_id,
                actor_reference=actor,
                note=note_text,
                transitioned_at=transitioned_at,
            )
        if current_status in _TERMINAL_STATUSES:
            raise ValueError(f"terminal Task status {current_status!r} cannot transition")
        if not is_task_transition_allowed(current_status, to_status):
            raise ValueError(
                f"Task transition {current_status!r} -> {to_status!r} is not allowed"
            )
        if _instant(transitioned_at) <= _instant(current["meta"]["lastUpdated"]):
            raise ValueError("Task transition time must follow the current version")
        try:
            next_version = str(int(current_version) + 1)
        except ValueError as exc:
            raise ValueError("Task workflow requires numeric meta.versionId") from exc

        updated = deepcopy(current)
        updated["status"] = to_status
        updated["meta"] = {
            **updated.get("meta", {}),
            "versionId": next_version,
            "lastUpdated": transitioned_at,
        }
        updated.setdefault("note", []).append(
            {
                "authorReference": {"reference": actor},
                "time": transitioned_at,
                "text": note_text,
            }
        )
        if to_status == "in-progress" or to_status in _TERMINAL_STATUSES:
            execution = updated.setdefault("executionPeriod", {})
            if to_status == "in-progress" and "start" not in execution:
                execution["start"] = transitioned_at
            if to_status in _TERMINAL_STATUSES:
                execution.setdefault(
                    "start", current.get("authoredOn", transitioned_at)
                )
                execution["end"] = transitioned_at
        updated = validate_layer4_fhir_resource(
            updated, expected_resource_type="Task"
        )

        transition_id = _stable_id(
            "task-transition", task_id, current_version, next_version
        )
        provenance_id = _stable_id("provenance", transition_id)
        provenance = build_provenance(
            target_references=[f"Task/{task_id}/_history/{next_version}"],
            recorded_at=transitioned_at,
            agent_reference=actor,
            agent_role_code="author",
            agent_role_display="Author",
            provenance_id=provenance_id,
            activity_code="UPDATE",
            activity_display=f"Task {current_status} -> {to_status}",
            entity_source_references=[
                f"Task/{task_id}/_history/{current_version}"
            ],
        )
        self.repository.persist_task_transition(
            patient_id=patient_id,
            expected_task=current,
            task=updated,
            provenance=provenance,
        )
        return TaskTransitionResult(
            transition_id=transition_id,
            patient_id=patient_id,
            task_id=task_id,
            from_status=current_status,
            to_status=to_status,
            from_version=current_version,
            to_version=next_version,
            actor_reference=actor,
            note=note_text,
            task_reference=f"Task/{task_id}/_history/{next_version}",
            provenance_reference=f"Provenance/{provenance_id}",
            transitioned_at=transitioned_at,
        )

    def _idempotent_retry(
        self,
        *,
        current: dict[str, Any],
        patient_id: str,
        actor_reference: str,
        note: str,
        transitioned_at: str,
    ) -> TaskTransitionResult:
        annotations = current.get("note", [])
        latest = annotations[-1] if annotations else {}
        latest_actor = latest.get("authorReference", {}).get("reference")
        if (
            current.get("meta", {}).get("lastUpdated") != transitioned_at
            or latest.get("time") != transitioned_at
            or latest.get("text") != note
            or latest_actor != actor_reference
        ):
            raise ValueError(
                "Task already has the requested status with different transition data"
            )
        to_version = current["meta"]["versionId"]
        try:
            from_version = str(int(to_version) - 1)
        except ValueError as exc:
            raise ValueError("Task workflow requires numeric meta.versionId") from exc
        previous = self.repository.get_fhir_resource(
            "Task", current["id"], version_id=from_version
        )
        if previous is None:
            raise ValueError("Task transition predecessor version is missing")
        transition_id = _stable_id(
            "task-transition", current["id"], from_version, to_version
        )
        provenance_id = _stable_id("provenance", transition_id)
        provenance = self.repository.get_fhir_resource("Provenance", provenance_id)
        if provenance is None:
            raise ValueError("Task transition Provenance is missing")
        return TaskTransitionResult(
            transition_id=transition_id,
            patient_id=patient_id,
            task_id=current["id"],
            from_status=previous["status"],
            to_status=current["status"],
            from_version=from_version,
            to_version=to_version,
            actor_reference=actor_reference,
            note=note,
            task_reference=f"Task/{current['id']}/_history/{to_version}",
            provenance_reference=f"Provenance/{provenance_id}",
            transitioned_at=transitioned_at,
        )

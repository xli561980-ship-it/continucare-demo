"""Append-only audit helpers."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from continucare.db import utc_now_iso
from continucare.models import AuditEvent


def record_audit_event(
    store,
    *,
    patient_id: str | None,
    entity_type: str,
    entity_id: str,
    event_type: str,
    actor_type: str,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = build_audit_event(
        patient_id=patient_id,
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor_type=actor_type,
        details=details,
    )
    store.append_audit_event(event)
    return event


def build_audit_event(
    *,
    patient_id: str | None,
    entity_type: str,
    entity_id: str,
    event_type: str,
    actor_type: str,
    details: dict[str, Any] | None = None,
    event_id: str | None = None,
    created_at: str | None = None,
) -> AuditEvent:
    """Build an audit fact without persisting it.

    Atomic commands supply deterministic IDs and commit the returned event in
    the same transaction as their domain writes.
    """

    return AuditEvent(
        event_id=event_id or f"audit_{uuid4().hex}",
        patient_id=patient_id,
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor_type=actor_type,
        details_json=details or {},
        created_at=created_at or utc_now_iso(),
    )

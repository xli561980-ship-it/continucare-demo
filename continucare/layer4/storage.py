"""SQLite persistence for versioned Layer-4 contracts and FHIR resources."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from continucare.db import connect, initialize_database
from continucare.errors import ConcurrentWriteConflict, is_sqlite_busy
from continucare.layer4.contracts import (
    ClinicalStateSnapshot,
    ClinicalRuleDefinition,
    DoctorReview,
    Layer4ContractRecord,
    Layer4SummaryDraft,
    MemoryEvent,
    RevisionLink,
    TimelineEvent,
)
from continucare.layer4.fhir import validate_layer4_fhir_resource
from continucare.fhir.r4 import validate_r4_resource
from continucare.layer4.manual_reviews import (
    PENDING_APPROVAL,
    READY_TO_SEND,
    communication_readiness,
    is_manual_review_communication,
    is_manual_review_task,
)
from continucare.models import AuditEvent, FollowUpMessage

_CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "clinical_rule": ClinicalRuleDefinition,
    "memory_event": MemoryEvent,
    "timeline_event": TimelineEvent,
    "revision_link": RevisionLink,
    "summary_draft": Layer4SummaryDraft,
    "doctor_review": DoctorReview,
    "state_snapshot": ClinicalStateSnapshot,
}


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fhir_patient_reference(resource: dict[str, Any]) -> str | None:
    resource_type = resource["resourceType"]
    if resource_type == "Task":
        return resource.get("for", {}).get("reference")
    if resource_type == "Communication":
        return resource.get("subject", {}).get("reference")
    return None


def _fhir_clinical_time(resource: dict[str, Any]) -> str:
    resource_type = resource["resourceType"]
    if resource_type == "Communication":
        return resource.get("sent") or resource.get("received") or resource["meta"][
            "lastUpdated"
        ]
    if resource_type == "Task":
        return resource.get("authoredOn") or resource["meta"]["lastUpdated"]
    return resource.get("recorded") or resource["meta"]["lastUpdated"]


def _contract_identity(
    record: Layer4ContractRecord,
) -> tuple[str, str, str, str | None, str | None, str, str, str]:
    if isinstance(record, ClinicalRuleDefinition):
        return (
            "clinical_rule",
            record.rule_id,
            record.version,
            None,
            record.applicability.pathway_code,
            record.lifecycle.value,
            record.created_at,
            record.created_at,
        )
    if isinstance(record, MemoryEvent):
        return (
            "memory_event",
            record.event_id,
            "1",
            record.patient_id,
            record.pathway_code,
            "current" if record.current else "superseded",
            record.effective_start,
            record.recorded_at,
        )
    if isinstance(record, TimelineEvent):
        return (
            "timeline_event",
            record.timeline_event_id,
            "1",
            record.patient_id,
            record.pathway_code,
            record.state.value,
            record.effective_start,
            record.recorded_at,
        )
    if isinstance(record, RevisionLink):
        return (
            "revision_link",
            record.link_id,
            "1",
            record.patient_id,
            None,
            record.relationship.value,
            record.created_at,
            record.created_at,
        )
    if isinstance(record, Layer4SummaryDraft):
        return (
            "summary_draft",
            record.summary_id,
            record.version,
            record.patient_id,
            record.pathway_code,
            record.status.value,
            record.period_end,
            record.created_at,
        )
    if isinstance(record, DoctorReview):
        return (
            "doctor_review",
            record.review_id,
            record.version,
            record.patient_id,
            None,
            record.decision.value,
            record.reviewed_at,
            record.reviewed_at,
        )
    if isinstance(record, ClinicalStateSnapshot):
        return (
            "state_snapshot",
            record.snapshot_id,
            record.version,
            record.patient_id,
            record.pathway_code,
            "complete",
            record.as_of,
            record.created_at,
        )
    raise TypeError(f"unsupported Layer-4 contract type: {type(record).__name__}")


def _fhir_storage_values(
    resource: dict[str, Any], *, patient_id: str | None
) -> tuple[
    dict[str, Any],
    tuple[str, str, str],
    str,
    str,
    str,
]:
    normalized = validate_layer4_fhir_resource(resource)
    resource_type = normalized["resourceType"]
    resource_id = normalized.get("id")
    meta = normalized.get("meta", {})
    version_id = meta.get("versionId")
    updated_at = meta.get("lastUpdated")
    if not resource_id or not version_id or not updated_at:
        raise ValueError(
            "Layer-4 FHIR persistence requires id, meta.versionId and "
            "meta.lastUpdated"
        )
    expected_patient = _fhir_patient_reference(normalized)
    if expected_patient is not None:
        if not patient_id:
            raise ValueError(f"{resource_type} persistence requires patient_id")
        if expected_patient != f"Patient/{patient_id}":
            raise ValueError(
                f"{resource_type} patient reference does not match patient_id"
            )
    return (
        normalized,
        (resource_type, resource_id, version_id),
        _json(normalized),
        _fhir_clinical_time(normalized),
        updated_at,
    )


def _insert_fhir_row(
    connection: sqlite3.Connection,
    *,
    normalized: dict[str, Any],
    identity: tuple[str, str, str],
    patient_id: str | None,
    payload: str,
    clinical_time: str,
    updated_at: str,
) -> None:
    resource_type, resource_id, version_id = identity
    connection.execute(
        """
        UPDATE layer4_fhir_resources SET is_current = 0
        WHERE resource_type = ? AND resource_id = ? AND is_current = 1
        """,
        (resource_type, resource_id),
    )
    connection.execute(
        """
        INSERT INTO layer4_fhir_resources (
            resource_type, resource_id, version_id, patient_id, status,
            clinical_time, resource_json, is_current, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            resource_type,
            resource_id,
            version_id,
            patient_id,
            normalized.get("status"),
            clinical_time,
            payload,
            updated_at,
            updated_at,
        ),
    )


def _contract_storage_values(
    record: Layer4ContractRecord,
) -> tuple[
    tuple[str, str, str],
    str | None,
    str | None,
    str,
    str,
    str,
    str,
]:
    (
        record_type,
        record_id,
        record_version,
        patient_id,
        pathway_code,
        status,
        effective_time,
        updated_at,
    ) = _contract_identity(record)
    return (
        (record_type, record_id, record_version),
        patient_id,
        pathway_code,
        status,
        effective_time,
        updated_at,
        _json(record.model_dump(mode="json")),
    )


def _versioned_reference(reference: Any) -> str:
    if reference.reference.startswith("urn:") or not reference.version_id:
        return reference.reference
    return f"{reference.reference}/_history/{reference.version_id}"


def _validated_revision_provenance(
    link: RevisionLink, provenance: dict[str, Any]
) -> dict[str, Any]:
    normalized = validate_layer4_fhir_resource(
        provenance, expected_resource_type="Provenance"
    )
    provenance_reference = f"Provenance/{normalized['id']}"
    if link.provenance_reference != provenance_reference:
        raise ValueError("Revision link must reference the exact bundle Provenance")
    if {
        item.get("reference") for item in normalized.get("target", [])
    } != {_versioned_reference(link.successor)}:
        raise ValueError("Revision Provenance must target the exact successor")
    if {
        item.get("what", {}).get("reference")
        for item in normalized.get("entity", [])
    } != {_versioned_reference(link.predecessor)}:
        raise ValueError("Revision Provenance must source the exact predecessor")
    return normalized


def _insert_contract_row(
    connection: sqlite3.Connection,
    *,
    identity: tuple[str, str, str],
    patient_id: str | None,
    pathway_code: str | None,
    status: str,
    effective_time: str,
    updated_at: str,
    payload: str,
) -> None:
    record_type, record_id, record_version = identity
    connection.execute(
        """
        UPDATE layer4_contract_records SET is_current = 0
        WHERE record_type = ? AND record_id = ? AND is_current = 1
        """,
        (record_type, record_id),
    )
    connection.execute(
        """
        INSERT INTO layer4_contract_records (
            record_type, record_id, record_version, patient_id,
            pathway_code, status, effective_time, record_json,
            is_current, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            record_type,
            record_id,
            record_version,
            patient_id,
            pathway_code,
            status,
            effective_time,
            payload,
            updated_at,
            updated_at,
        ),
    )


class Layer4SQLiteStore:
    """Persist exact JSON while keeping only query projections in columns."""

    def __init__(self, db_path: Path | str, *, initialize: bool = True):
        self.db_path = Path(db_path)
        if initialize:
            initialize_database(self.db_path)

    def save_fhir_resource(
        self, resource: dict[str, Any], *, patient_id: str | None
    ) -> dict[str, Any]:
        normalized, identity, payload, clinical_time, updated_at = (
            _fhir_storage_values(resource, patient_id=patient_id)
        )
        with connect(self.db_path) as connection:
            existing = connection.execute(
                """
                SELECT resource_json, patient_id FROM layer4_fhir_resources
                WHERE resource_type = ? AND resource_id = ? AND version_id = ?
                """,
                identity,
            ).fetchone()
            if existing:
                if existing["resource_json"] != payload or existing["patient_id"] != patient_id:
                    raise ValueError(
                        "FHIR resource version is immutable and conflicts with stored JSON"
                    )
                return normalized
            _insert_fhir_row(
                connection,
                normalized=normalized,
                identity=identity,
                patient_id=patient_id,
                payload=payload,
                clinical_time=clinical_time,
                updated_at=updated_at,
            )
        return normalized

    def persist_fhir_creation_bundle(
        self,
        *,
        resources: list[dict[str, Any]],
        patient_id: str | None,
    ) -> bool:
        """Create one immutable FHIR write set without a partial current state."""

        if not resources:
            raise ValueError("FHIR creation bundle requires resources")
        prepared = [
            _fhir_storage_values(item, patient_id=patient_id) for item in resources
        ]
        identities = [item[1] for item in prepared]
        if len(identities) != len(set(identities)):
            raise ValueError("FHIR creation bundle identities must be unique")
        try:
            with connect(self.db_path) as connection:
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("BEGIN IMMEDIATE")
                existing = {
                    identity: connection.execute(
                        """
                        SELECT resource_json, patient_id
                        FROM layer4_fhir_resources
                        WHERE resource_type=? AND resource_id=? AND version_id=?
                        """,
                        identity,
                    ).fetchone()
                    for identity in identities
                }
                present = {key: row for key, row in existing.items() if row is not None}
                if present:
                    exact = len(present) == len(prepared) and all(
                        row["resource_json"] == item[2]
                        and row["patient_id"] == patient_id
                        for item in prepared
                        for row in [present.get(item[1])]
                        if row is not None
                    )
                    if exact:
                        return False
                    raise ConcurrentWriteConflict(
                        "FHIR creation bundle has a conflicting or partial replay"
                    )
                for index, item in enumerate(prepared):
                    normalized, identity, payload, clinical_time, updated_at = item
                    prior = connection.execute(
                        """
                        SELECT 1 FROM layer4_fhir_resources
                        WHERE resource_type=? AND resource_id=?
                        LIMIT 1
                        """,
                        identity[:2],
                    ).fetchone()
                    if prior is not None:
                        raise ConcurrentWriteConflict(
                            "FHIR creation bundle cannot append an existing resource"
                        )
                    _insert_fhir_row(
                        connection,
                        normalized=normalized,
                        identity=identity,
                        patient_id=patient_id,
                        payload=payload,
                        clinical_time=clinical_time,
                        updated_at=updated_at,
                    )
                    self._fhir_creation_bundle_fault(f"after_resource:{index}")
                self._fhir_creation_bundle_fault("before_commit")
            self._fhir_creation_bundle_fault("after_commit")
        except sqlite3.OperationalError as exc:
            if is_sqlite_busy(exc):
                raise ConcurrentWriteConflict(
                    "FHIR creation database is busy; retry the same request"
                ) from exc
            raise
        return True

    def _fhir_creation_bundle_fault(self, stage: str) -> None:
        """Test seam for creation rollback and post-commit replay."""

        return None

    def persist_task_transition(
        self,
        *,
        patient_id: str,
        expected_task: dict[str, Any],
        task: dict[str, Any],
        provenance: dict[str, Any],
    ) -> bool:
        """CAS one Task version and its exact transition Provenance."""

        expected = validate_layer4_fhir_resource(
            expected_task, expected_resource_type="Task"
        )
        updated, task_identity, task_payload, task_time, task_updated = (
            _fhir_storage_values(task, patient_id=patient_id)
        )
        if updated["resourceType"] != "Task" or expected["id"] != updated["id"]:
            raise ValueError("Task transition resources must identify the same Task")
        expected_version = expected["meta"]["versionId"]
        updated_version = updated["meta"]["versionId"]
        try:
            expected_number = int(expected_version)
            updated_number = int(updated_version)
        except ValueError as exc:
            raise ValueError("Task transition versions must be numeric and consecutive") from exc
        if updated_number != expected_number + 1:
            raise ValueError("Task transition must persist the exact next version")
        normalized_provenance, provenance_identity, provenance_payload, provenance_time, provenance_updated = (
            _fhir_storage_values(provenance, patient_id=patient_id)
        )
        if normalized_provenance["resourceType"] != "Provenance":
            raise ValueError("Task transition requires Provenance")
        expected_target = f"Task/{updated['id']}/_history/{updated_version}"
        expected_source = f"Task/{expected['id']}/_history/{expected_version}"
        targets = {
            item.get("reference")
            for item in normalized_provenance.get("target", [])
        }
        sources = {
            item.get("what", {}).get("reference")
            for item in normalized_provenance.get("entity", [])
        }
        if targets != {expected_target} or sources != {expected_source}:
            raise ValueError(
                "Task transition Provenance must bind the exact predecessor and successor"
            )
        expected_payload = _json(expected)
        try:
            with connect(self.db_path) as connection:
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("BEGIN IMMEDIATE")
                task_row = connection.execute(
                    """
                    SELECT resource_json, patient_id FROM layer4_fhir_resources
                    WHERE resource_type='Task' AND resource_id=? AND version_id=?
                    """,
                    (updated["id"], updated_version),
                ).fetchone()
                provenance_row = connection.execute(
                    """
                    SELECT resource_json, patient_id FROM layer4_fhir_resources
                    WHERE resource_type='Provenance' AND resource_id=? AND version_id=?
                    """,
                    provenance_identity[1:],
                ).fetchone()
                if task_row is not None or provenance_row is not None:
                    if (
                        task_row is not None
                        and task_row["resource_json"] == task_payload
                        and task_row["patient_id"] == patient_id
                        and provenance_row is not None
                        and provenance_row["resource_json"] == provenance_payload
                        and provenance_row["patient_id"] == patient_id
                    ):
                        return False
                    raise ConcurrentWriteConflict(
                        "Task transition has a conflicting or partial replay"
                    )
                current = connection.execute(
                    """
                    SELECT resource_json, patient_id FROM layer4_fhir_resources
                    WHERE resource_type='Task' AND resource_id=? AND is_current=1
                    """,
                    (expected["id"],),
                ).fetchone()
                if (
                    current is None
                    or current["resource_json"] != expected_payload
                    or current["patient_id"] != patient_id
                ):
                    raise ConcurrentWriteConflict(
                        "Task changed; refresh and retry the transition"
                    )
                _insert_fhir_row(
                    connection,
                    normalized=updated,
                    identity=task_identity,
                    patient_id=patient_id,
                    payload=task_payload,
                    clinical_time=task_time,
                    updated_at=task_updated,
                )
                self._task_transition_fault("after_task")
                _insert_fhir_row(
                    connection,
                    normalized=normalized_provenance,
                    identity=provenance_identity,
                    patient_id=patient_id,
                    payload=provenance_payload,
                    clinical_time=provenance_time,
                    updated_at=provenance_updated,
                )
                self._task_transition_fault("after_provenance")
                self._task_transition_fault("before_commit")
            self._task_transition_fault("after_commit")
        except sqlite3.OperationalError as exc:
            if is_sqlite_busy(exc):
                raise ConcurrentWriteConflict(
                    "Task transition database is busy; retry the same request"
                ) from exc
            raise
        return True

    def _task_transition_fault(self, stage: str) -> None:
        """Test seam for transition rollback and post-commit replay."""

        return None

    def persist_doctor_review_bundle(
        self,
        *,
        expected_source: Layer4SummaryDraft,
        provenance: dict[str, Any],
        result_summary: Layer4SummaryDraft,
        review: DoctorReview,
        audit_event: AuditEvent,
    ) -> bool:
        """CAS one exact Summary review and commit every result atomically."""

        normalized_provenance = validate_layer4_fhir_resource(
            provenance, expected_resource_type="Provenance"
        )
        patient_id = expected_source.patient_id
        if (
            result_summary.patient_id != patient_id
            or review.patient_id != patient_id
            or audit_event.patient_id != patient_id
        ):
            raise ValueError("doctor review bundle patient mismatch")
        if (
            result_summary.summary_id != expected_source.summary_id
            or review.summary_id != expected_source.summary_id
            or review.summary_version != expected_source.version
            or review.result_summary_id != result_summary.summary_id
            or review.result_summary_version != result_summary.version
        ):
            raise ValueError("doctor review bundle Summary references do not match")
        try:
            expected_result_version = str(int(expected_source.version) + 1)
        except ValueError as exc:
            raise ValueError("summary versions must be numeric") from exc
        if result_summary.version != expected_result_version:
            raise ValueError("doctor review result must be the next Summary version")
        provenance_ref = f"Provenance/{normalized_provenance['id']}"
        if review.provenance_reference != provenance_ref:
            raise ValueError("doctor review Provenance reference mismatch")
        targets = {
            item.get("reference") for item in normalized_provenance.get("target", [])
        }
        if {
            f"urn:continucare:summary:{result_summary.summary_id}:version:{result_summary.version}",
            f"urn:continucare:doctor-review:{review.review_id}:version:{review.version}",
        } - targets:
            raise ValueError("doctor review Provenance must target result and review")
        if (
            audit_event.entity_type != "Layer4SummaryDraft"
            or audit_event.entity_id != result_summary.summary_id
            or audit_event.event_type != "doctor_reviewed_summary"
        ):
            raise ValueError("doctor review audit identity is invalid")

        source_payload = _json(expected_source.model_dump(mode="json"))
        provenance_payload = _json(normalized_provenance)
        result_identity = _contract_identity(result_summary)
        result_payload = _json(result_summary.model_dump(mode="json"))
        review_identity = _contract_identity(review)
        review_payload = _json(review.model_dump(mode="json"))
        audit_payload = _json(audit_event.details_json)

        try:
            with connect(self.db_path) as connection:
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("BEGIN IMMEDIATE")
                source_row = connection.execute(
                    """
                    SELECT record_json, patient_id, is_current
                    FROM layer4_contract_records
                    WHERE record_type='summary_draft' AND record_id=?
                      AND record_version=?
                    """,
                    (expected_source.summary_id, expected_source.version),
                ).fetchone()
                provenance_row = connection.execute(
                    """
                    SELECT resource_json, patient_id FROM layer4_fhir_resources
                    WHERE resource_type='Provenance' AND resource_id=? AND version_id=?
                    """,
                    (
                        normalized_provenance["id"],
                        normalized_provenance["meta"]["versionId"],
                    ),
                ).fetchone()
                result_row = connection.execute(
                    """
                    SELECT record_json, patient_id FROM layer4_contract_records
                    WHERE record_type='summary_draft' AND record_id=?
                      AND record_version=?
                    """,
                    (result_summary.summary_id, result_summary.version),
                ).fetchone()
                review_row = connection.execute(
                    """
                    SELECT record_json, patient_id FROM layer4_contract_records
                    WHERE record_type='doctor_review' AND record_id=?
                      AND record_version=?
                    """,
                    (review.review_id, review.version),
                ).fetchone()
                audit_row = connection.execute(
                    "SELECT * FROM audit_events WHERE event_id=?",
                    (audit_event.event_id,),
                ).fetchone()
                output_rows = (provenance_row, result_row, review_row, audit_row)
                if any(row is not None for row in output_rows):
                    exact = (
                        provenance_row is not None
                        and provenance_row["resource_json"] == provenance_payload
                        and provenance_row["patient_id"] == patient_id
                        and result_row is not None
                        and result_row["record_json"] == result_payload
                        and result_row["patient_id"] == patient_id
                        and review_row is not None
                        and review_row["record_json"] == review_payload
                        and review_row["patient_id"] == patient_id
                        and audit_row is not None
                        and audit_row["patient_id"] == patient_id
                        and audit_row["entity_type"] == audit_event.entity_type
                        and audit_row["entity_id"] == audit_event.entity_id
                        and audit_row["event_type"] == audit_event.event_type
                        and audit_row["actor_type"] == audit_event.actor_type
                        and _json(json.loads(audit_row["details_json"]))
                        == audit_payload
                        and audit_row["created_at"] == audit_event.created_at
                    )
                    if exact:
                        return False
                    raise ConcurrentWriteConflict(
                        "doctor review has a conflicting or partial replay"
                    )
                if (
                    source_row is None
                    or source_row["patient_id"] != patient_id
                    or source_row["record_json"] != source_payload
                    or source_row["is_current"] != 1
                ):
                    raise ConcurrentWriteConflict(
                        "doctor review source changed; refresh and retry"
                    )

                self._doctor_review_bundle_fault("before_provenance")
                meta = normalized_provenance["meta"]
                connection.execute(
                    """
                    INSERT INTO layer4_fhir_resources (
                        resource_type, resource_id, version_id, patient_id, status,
                        clinical_time, resource_json, is_current, created_at, updated_at
                    ) VALUES ('Provenance', ?, ?, ?, NULL, ?, ?, 1, ?, ?)
                    """,
                    (
                        normalized_provenance["id"],
                        meta["versionId"],
                        patient_id,
                        _fhir_clinical_time(normalized_provenance),
                        provenance_payload,
                        meta["lastUpdated"],
                        meta["lastUpdated"],
                    ),
                )
                self._doctor_review_bundle_fault("after_provenance")

                connection.execute(
                    """
                    UPDATE layer4_contract_records SET is_current=0
                    WHERE record_type='summary_draft' AND record_id=? AND is_current=1
                    """,
                    (result_summary.summary_id,),
                )
                connection.execute(
                    """
                    INSERT INTO layer4_contract_records (
                        record_type, record_id, record_version, patient_id,
                        pathway_code, status, effective_time, record_json,
                        is_current, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        result_identity[0],
                        result_identity[1],
                        result_identity[2],
                        result_identity[3],
                        result_identity[4],
                        result_identity[5],
                        result_identity[6],
                        result_payload,
                        result_identity[7],
                        result_identity[7],
                    ),
                )
                self._doctor_review_bundle_fault("after_summary")

                connection.execute(
                    """
                    INSERT INTO layer4_contract_records (
                        record_type, record_id, record_version, patient_id,
                        pathway_code, status, effective_time, record_json,
                        is_current, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        review_identity[0],
                        review_identity[1],
                        review_identity[2],
                        review_identity[3],
                        review_identity[4],
                        review_identity[5],
                        review_identity[6],
                        review_payload,
                        review_identity[7],
                        review_identity[7],
                    ),
                )
                self._doctor_review_bundle_fault("after_review")

                connection.execute(
                    """
                    INSERT INTO audit_events (
                        event_id, patient_id, entity_type, entity_id, event_type,
                        actor_type, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_event.event_id,
                        audit_event.patient_id,
                        audit_event.entity_type,
                        audit_event.entity_id,
                        audit_event.event_type,
                        audit_event.actor_type,
                        audit_payload,
                        audit_event.created_at,
                    ),
                )
                self._doctor_review_bundle_fault("after_audit")
                self._doctor_review_bundle_fault("before_commit")
        except sqlite3.OperationalError as exc:
            if is_sqlite_busy(exc):
                raise ConcurrentWriteConflict(
                    "doctor review database is busy; retry the same request"
                ) from exc
            raise
        return True

    def _doctor_review_bundle_fault(self, stage: str) -> None:
        """Test seam for proving rollback at every DoctorReview write point."""

        return None

    def persist_manual_review_action(
        self,
        *,
        patient_id: str,
        expected_task: dict[str, Any],
        resources: list[dict[str, Any]],
        audit_event: AuditEvent,
        expected_communication: dict[str, Any] | None = None,
    ) -> bool:
        """CAS one manual-review action as an exact, all-or-nothing write set."""

        expected_task = validate_layer4_fhir_resource(
            expected_task, expected_resource_type="Task"
        )
        if not is_manual_review_task(expected_task):
            raise ValueError("manual review action requires a manual-review Task")
        if expected_task.get("for", {}).get("reference") != f"Patient/{patient_id}":
            raise ValueError("manual review action Task patient mismatch")
        expected_communication_json: str | None = None
        if expected_communication is not None:
            expected_communication = validate_layer4_fhir_resource(
                expected_communication, expected_resource_type="Communication"
            )
            if not is_manual_review_communication(expected_communication):
                raise ValueError("manual review action Communication mismatch")
            if expected_communication.get("subject", {}).get("reference") != (
                f"Patient/{patient_id}"
            ):
                raise ValueError("manual review Communication patient mismatch")
            expected_communication_json = _json(expected_communication)
        if not resources:
            raise ValueError("manual review action requires resources")
        normalized = [validate_layer4_fhir_resource(item) for item in resources]
        identities = [
            (item["resourceType"], item["id"], item["meta"]["versionId"])
            for item in normalized
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("manual review action resource identities must be unique")
        if audit_event.patient_id != patient_id:
            raise ValueError("manual review action audit patient mismatch")

        non_provenance_refs: set[str] = set()
        for item in normalized:
            resource_type = item["resourceType"]
            if resource_type == "Task":
                if not is_manual_review_task(item):
                    raise ValueError("manual review action cannot write another Task class")
                if item.get("for", {}).get("reference") != f"Patient/{patient_id}":
                    raise ValueError("manual review Task patient mismatch")
                non_provenance_refs.add(
                    f"Task/{item['id']}/_history/{item['meta']['versionId']}"
                )
            elif resource_type == "Communication":
                if not is_manual_review_communication(item):
                    raise ValueError(
                        "manual review action cannot write another Communication class"
                    )
                if item.get("subject", {}).get("reference") != f"Patient/{patient_id}":
                    raise ValueError("manual review Communication patient mismatch")
                if "sent" in item or "received" in item:
                    raise ValueError("manual review Communication must remain unsent")
                if communication_readiness(item) not in {
                    PENDING_APPROVAL,
                    READY_TO_SEND,
                }:
                    raise ValueError("manual review Communication readiness is invalid")
                non_provenance_refs.add(
                    f"Communication/{item['id']}/_history/{item['meta']['versionId']}"
                )
        provenances = [
            item for item in normalized if item["resourceType"] == "Provenance"
        ]
        if len(provenances) != 1:
            raise ValueError("manual review action requires exactly one Provenance")
        provenance_targets = {
            target.get("reference")
            for target in provenances[0].get("target", [])
        }
        if not non_provenance_refs or not non_provenance_refs.issubset(
            provenance_targets
        ):
            raise ValueError("manual review Provenance must target every action resource")

        resource_payloads = {
            identity: _json(item) for identity, item in zip(identities, normalized)
        }
        expected_task_json = _json(expected_task)
        audit_details = _json(audit_event.details_json)
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_resources: dict[tuple[str, str, str], Any] = {}
            for identity in identities:
                row = connection.execute(
                    """
                    SELECT resource_json, patient_id FROM layer4_fhir_resources
                    WHERE resource_type = ? AND resource_id = ? AND version_id = ?
                    """,
                    identity,
                ).fetchone()
                if row is not None:
                    existing_resources[identity] = row
            existing_audit = connection.execute(
                "SELECT * FROM audit_events WHERE event_id = ?",
                (audit_event.event_id,),
            ).fetchone()
            if existing_resources or existing_audit is not None:
                exact_resources = len(existing_resources) == len(identities) and all(
                    row["resource_json"] == resource_payloads[identity]
                    and row["patient_id"] == patient_id
                    for identity, row in existing_resources.items()
                )
                exact_audit = (
                    existing_audit is not None
                    and existing_audit["patient_id"] == audit_event.patient_id
                    and existing_audit["entity_type"] == audit_event.entity_type
                    and existing_audit["entity_id"] == audit_event.entity_id
                    and existing_audit["event_type"] == audit_event.event_type
                    and existing_audit["actor_type"] == audit_event.actor_type
                    and _json(json.loads(existing_audit["details_json"]))
                    == audit_details
                    and existing_audit["created_at"] == audit_event.created_at
                )
                if exact_resources and exact_audit:
                    return False
                raise ValueError("manual review action has a conflicting partial replay")

            current_task = connection.execute(
                """
                SELECT resource_json FROM layer4_fhir_resources
                WHERE resource_type = 'Task' AND resource_id = ? AND is_current = 1
                """,
                (expected_task["id"],),
            ).fetchone()
            if current_task is None or current_task["resource_json"] != expected_task_json:
                raise ValueError("manual review Task changed; refresh and retry")
            if expected_communication is not None:
                current_communication = connection.execute(
                    """
                    SELECT resource_json FROM layer4_fhir_resources
                    WHERE resource_type = 'Communication' AND resource_id = ?
                      AND is_current = 1
                    """,
                    (expected_communication["id"],),
                ).fetchone()
                if (
                    current_communication is None
                    or current_communication["resource_json"]
                    != expected_communication_json
                ):
                    raise ValueError(
                        "manual review Communication changed; refresh and retry"
                    )

            ready = [
                item
                for item in normalized
                if item["resourceType"] == "Communication"
                and communication_readiness(item) == READY_TO_SEND
            ]
            if ready:
                if expected_task.get("status") != "completed":
                    raise ValueError("only a completed Task can authorize approval")
                if (
                    expected_communication is None
                    or communication_readiness(expected_communication)
                    != PENDING_APPROVAL
                ):
                    raise ValueError("only a pending draft can be approved")
                previous_rows = connection.execute(
                    """
                    SELECT resource_json FROM layer4_fhir_resources
                    WHERE resource_type = 'Communication' AND resource_id = ?
                    """,
                    (ready[0]["id"],),
                ).fetchall()
                if any(
                    communication_readiness(json.loads(row["resource_json"]))
                    == READY_TO_SEND
                    for row in previous_rows
                ):
                    raise ValueError("manual review Communication is already approved")

            for item in normalized:
                resource_type = item["resourceType"]
                meta = item["meta"]
                connection.execute(
                    """
                    UPDATE layer4_fhir_resources SET is_current = 0
                    WHERE resource_type = ? AND resource_id = ? AND is_current = 1
                    """,
                    (resource_type, item["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO layer4_fhir_resources (
                        resource_type, resource_id, version_id, patient_id, status,
                        clinical_time, resource_json, is_current, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        resource_type,
                        item["id"],
                        meta["versionId"],
                        patient_id,
                        item.get("status"),
                        _fhir_clinical_time(item),
                        _json(item),
                        meta["lastUpdated"],
                        meta["lastUpdated"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, patient_id, entity_type, entity_id, event_type,
                    actor_type, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_event.event_id,
                    audit_event.patient_id,
                    audit_event.entity_type,
                    audit_event.entity_id,
                    audit_event.event_type,
                    audit_event.actor_type,
                    audit_details,
                    audit_event.created_at,
                ),
            )
            self._manual_review_action_fault(audit_event.event_type)
        return True

    def _manual_review_action_fault(self, event_type: str) -> None:
        """Test seam for proving rollback after all M5-B domain writes."""

        return None

    def persist_manual_review_brief(
        self,
        *,
        patient_id: str,
        expected_task: dict[str, Any],
        expected_communication: dict[str, Any],
        expected_questionnaire_response: dict[str, Any],
        expected_observations: list[dict[str, Any]],
        expected_message: FollowUpMessage,
        expected_provenances: list[dict[str, Any]],
        expected_audits: list[AuditEvent],
        expected_current_summary: Layer4SummaryDraft | None,
        summary: Layer4SummaryDraft,
        summary_provenance: dict[str, Any],
        audit_event: AuditEvent,
    ) -> bool:
        """CAS one M5-C brief after rechecking every source in one transaction."""

        if summary.summary_kind != "manual_review_brief":
            raise ValueError("M5-C persistence requires a manual-review brief")
        if summary.patient_id != patient_id or audit_event.patient_id != patient_id:
            raise ValueError("M5-C brief patient mismatch")
        task = validate_layer4_fhir_resource(
            expected_task, expected_resource_type="Task"
        )
        communication = validate_layer4_fhir_resource(
            expected_communication, expected_resource_type="Communication"
        )
        response = validate_r4_resource(
            expected_questionnaire_response,
            expected_resource_type="QuestionnaireResponse",
        )
        observations = [
            validate_r4_resource(item, expected_resource_type="Observation")
            for item in expected_observations
        ]
        provenances = [
            validate_layer4_fhir_resource(item, expected_resource_type="Provenance")
            for item in expected_provenances
        ]
        generated_provenance = validate_layer4_fhir_resource(
            summary_provenance, expected_resource_type="Provenance"
        )
        summary_reference = (
            f"urn:continucare:summary:{summary.summary_id}:version:{summary.version}"
        )
        if summary_reference not in {
            item.get("reference")
            for item in generated_provenance.get("target", [])
        }:
            raise ValueError("M5-C Provenance must target the exact Summary version")
        if not provenances or not expected_audits:
            raise ValueError("M5-C brief requires Provenance and process audit evidence")

        summary_identity = _contract_identity(summary)
        summary_payload = _json(summary.model_dump(mode="json"))
        generated_provenance_payload = _json(generated_provenance)
        audit_payload = _json(audit_event.details_json)
        with connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")

            existing_summary = connection.execute(
                """
                SELECT record_json, patient_id FROM layer4_contract_records
                WHERE record_type='summary_draft' AND record_id=? AND record_version=?
                """,
                (summary.summary_id, summary.version),
            ).fetchone()
            existing_provenance = connection.execute(
                """
                SELECT resource_json, patient_id FROM layer4_fhir_resources
                WHERE resource_type='Provenance' AND resource_id=? AND version_id=?
                """,
                (
                    generated_provenance["id"],
                    generated_provenance["meta"]["versionId"],
                ),
            ).fetchone()
            existing_audit = connection.execute(
                "SELECT * FROM audit_events WHERE event_id=?",
                (audit_event.event_id,),
            ).fetchone()
            if any(
                item is not None
                for item in (existing_summary, existing_provenance, existing_audit)
            ):
                exact = (
                    existing_summary is not None
                    and existing_summary["record_json"] == summary_payload
                    and existing_summary["patient_id"] == patient_id
                    and existing_provenance is not None
                    and existing_provenance["resource_json"]
                    == generated_provenance_payload
                    and existing_provenance["patient_id"] == patient_id
                    and existing_audit is not None
                    and existing_audit["patient_id"] == patient_id
                    and existing_audit["entity_type"] == audit_event.entity_type
                    and existing_audit["entity_id"] == audit_event.entity_id
                    and existing_audit["event_type"] == audit_event.event_type
                    and existing_audit["actor_type"] == audit_event.actor_type
                    and _json(json.loads(existing_audit["details_json"]))
                    == audit_payload
                    and existing_audit["created_at"] == audit_event.created_at
                )
                if exact:
                    return False
                raise ValueError("M5-C brief has a conflicting partial replay")

            current_summary = connection.execute(
                """
                SELECT record_json FROM layer4_contract_records
                WHERE record_type='summary_draft' AND record_id=? AND is_current=1
                """,
                (summary.summary_id,),
            ).fetchone()
            expected_summary_json = (
                _json(expected_current_summary.model_dump(mode="json"))
                if expected_current_summary is not None
                else None
            )
            if (current_summary is None) != (expected_summary_json is None) or (
                current_summary is not None
                and current_summary["record_json"] != expected_summary_json
            ):
                raise ValueError("M5-C Summary changed; refresh and retry")

            self._require_current_fhir_source(
                connection, task, patient_id=patient_id
            )
            self._require_current_fhir_source(
                connection, communication, patient_id=patient_id
            )
            qr_row = connection.execute(
                """
                SELECT resource_json, patient_id FROM fhir_questionnaire_responses
                WHERE resource_id=?
                """,
                (response["id"],),
            ).fetchone()
            if (
                qr_row is None
                or qr_row["patient_id"] != patient_id
                or _json(json.loads(qr_row["resource_json"])) != _json(response)
            ):
                raise ValueError("M5-C QuestionnaireResponse changed")
            for observation in observations:
                row = connection.execute(
                    """
                    SELECT resource_json, patient_id FROM fhir_observations
                    WHERE observation_id=?
                    """,
                    (observation["id"],),
                ).fetchone()
                if (
                    row is None
                    or row["patient_id"] != patient_id
                    or _json(json.loads(row["resource_json"])) != _json(observation)
                ):
                    raise ValueError("M5-C Observation changed")
            message_row = connection.execute(
                "SELECT * FROM followup_messages WHERE message_id=?",
                (expected_message.message_id,),
            ).fetchone()
            if message_row is None or any(
                message_row[field] != value
                for field, value in expected_message.model_dump(mode="json").items()
            ):
                raise ValueError("M5-C patient message changed")
            for provenance in provenances:
                row = connection.execute(
                    """
                    SELECT resource_json, patient_id FROM layer4_fhir_resources
                    WHERE resource_type='Provenance' AND resource_id=? AND version_id=?
                    """,
                    (provenance["id"], provenance["meta"]["versionId"]),
                ).fetchone()
                if (
                    row is None
                    or row["patient_id"] != patient_id
                    or row["resource_json"] != _json(provenance)
                ):
                    raise ValueError("M5-C Provenance changed")
            for expected_audit in expected_audits:
                row = connection.execute(
                    "SELECT * FROM audit_events WHERE event_id=?",
                    (expected_audit.event_id,),
                ).fetchone()
                if (
                    row is None
                    or row["patient_id"] != patient_id
                    or row["entity_type"] != expected_audit.entity_type
                    or row["entity_id"] != expected_audit.entity_id
                    or row["event_type"] != expected_audit.event_type
                    or row["actor_type"] != expected_audit.actor_type
                    or _json(json.loads(row["details_json"]))
                    != _json(expected_audit.details_json)
                    or row["created_at"] != expected_audit.created_at
                ):
                    raise ValueError("M5-C audit evidence changed")

            meta = generated_provenance["meta"]
            connection.execute(
                """
                UPDATE layer4_fhir_resources SET is_current=0
                WHERE resource_type='Provenance' AND resource_id=? AND is_current=1
                """,
                (generated_provenance["id"],),
            )
            connection.execute(
                """
                INSERT INTO layer4_fhir_resources (
                    resource_type, resource_id, version_id, patient_id, status,
                    clinical_time, resource_json, is_current, created_at, updated_at
                ) VALUES ('Provenance', ?, ?, ?, NULL, ?, ?, 1, ?, ?)
                """,
                (
                    generated_provenance["id"],
                    meta["versionId"],
                    patient_id,
                    _fhir_clinical_time(generated_provenance),
                    generated_provenance_payload,
                    meta["lastUpdated"],
                    meta["lastUpdated"],
                ),
            )
            connection.execute(
                """
                UPDATE layer4_contract_records SET is_current=0
                WHERE record_type='summary_draft' AND record_id=? AND is_current=1
                """,
                (summary.summary_id,),
            )
            connection.execute(
                """
                INSERT INTO layer4_contract_records (
                    record_type, record_id, record_version, patient_id,
                    pathway_code, status, effective_time, record_json,
                    is_current, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    summary_identity[0],
                    summary_identity[1],
                    summary_identity[2],
                    summary_identity[3],
                    summary_identity[4],
                    summary_identity[5],
                    summary_identity[6],
                    summary_payload,
                    summary_identity[7],
                    summary_identity[7],
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, patient_id, entity_type, entity_id, event_type,
                    actor_type, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_event.event_id,
                    audit_event.patient_id,
                    audit_event.entity_type,
                    audit_event.entity_id,
                    audit_event.event_type,
                    audit_event.actor_type,
                    audit_payload,
                    audit_event.created_at,
                ),
            )
            self._manual_review_brief_fault(summary.version)
        return True

    @staticmethod
    def _require_current_fhir_source(connection, resource, *, patient_id: str) -> None:
        row = connection.execute(
            """
            SELECT resource_json, patient_id FROM layer4_fhir_resources
            WHERE resource_type=? AND resource_id=? AND is_current=1
            """,
            (resource["resourceType"], resource["id"]),
        ).fetchone()
        if (
            row is None
            or row["patient_id"] != patient_id
            or row["resource_json"] != _json(resource)
        ):
            raise ValueError("M5-C source changed; refresh and retry")

    def _manual_review_brief_fault(self, version: str) -> None:
        """Test seam proving rollback after every M5-C write."""

        return None

    def get_fhir_resource(
        self,
        resource_type: str,
        resource_id: str,
        *,
        version_id: str | None = None,
    ) -> dict[str, Any] | None:
        query = (
            "SELECT resource_json FROM layer4_fhir_resources "
            "WHERE resource_type = ? AND resource_id = ?"
        )
        params: tuple[Any, ...] = (resource_type, resource_id)
        if version_id is None:
            query += " AND is_current = 1"
        else:
            query += " AND version_id = ?"
            params += (version_id,)
        with connect(self.db_path) as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            return None
        return validate_layer4_fhir_resource(json.loads(row["resource_json"]))

    def list_fhir_resources(
        self,
        *,
        patient_id: str | None = None,
        resource_type: str | None = None,
        status: str | None = None,
        current_only: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if patient_id is not None:
            clauses.append("patient_id = ?")
            params.append(patient_id)
        if resource_type is not None:
            clauses.append("resource_type = ?")
            params.append(resource_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if current_only:
            clauses.append("is_current = 1")
        query = "SELECT resource_json FROM layer4_fhir_resources"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY clinical_time DESC, resource_id"
        with connect(self.db_path) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [
            validate_layer4_fhir_resource(json.loads(row["resource_json"]))
            for row in rows
        ]

    def save_contract(self, record: Layer4ContractRecord) -> Layer4ContractRecord:
        (
            identity,
            patient_id,
            pathway_code,
            status,
            effective_time,
            updated_at,
            payload,
        ) = _contract_storage_values(record)
        with connect(self.db_path) as connection:
            existing = connection.execute(
                """
                SELECT record_json, patient_id FROM layer4_contract_records
                WHERE record_type = ? AND record_id = ? AND record_version = ?
                """,
                identity,
            ).fetchone()
            if existing:
                if existing["record_json"] != payload or existing["patient_id"] != patient_id:
                    raise ValueError(
                        "Layer-4 contract version is immutable and conflicts with stored JSON"
                    )
                return record
            _insert_contract_row(
                connection,
                identity=identity,
                patient_id=patient_id,
                pathway_code=pathway_code,
                status=status,
                effective_time=effective_time,
                updated_at=updated_at,
                payload=payload,
            )
        return record

    def persist_summary_bundle(
        self,
        *,
        expected_current: Layer4SummaryDraft | None,
        summary: Layer4SummaryDraft,
        provenance: dict[str, Any],
    ) -> bool:
        normalized = validate_layer4_fhir_resource(
            provenance, expected_resource_type="Provenance"
        )
        provenance_reference = f"Provenance/{normalized['id']}"
        if {
            (item.reference, item.version_id) for item in summary.provenance_refs
        } != {(provenance_reference, normalized["meta"]["versionId"])}:
            raise ValueError("Summary must reference the exact bundle Provenance")
        expected_target = (
            f"urn:continucare:summary:{summary.summary_id}:version:{summary.version}"
        )
        if {
            item.get("reference") for item in normalized.get("target", [])
        } != {expected_target}:
            raise ValueError("Summary Provenance must target the exact Summary version")
        return self._persist_provenance_contract_records(
            provenance=normalized,
            records=[summary],
            patient_id=summary.patient_id,
            expected_current=expected_current,
            cas_identity=("summary_draft", summary.summary_id),
            fault_prefix="summary",
        )

    def persist_state_snapshot_bundle(
        self,
        *,
        expected_current: ClinicalStateSnapshot | None,
        snapshot: ClinicalStateSnapshot,
        provenance: dict[str, Any],
    ) -> bool:
        normalized = validate_layer4_fhir_resource(
            provenance, expected_resource_type="Provenance"
        )
        provenance_reference = f"Provenance/{normalized['id']}"
        if {
            (item.reference, item.version_id) for item in snapshot.provenance_refs
        } != {(provenance_reference, normalized["meta"]["versionId"])}:
            raise ValueError("State snapshot must reference the exact bundle Provenance")
        expected_target = (
            "urn:continucare:state-snapshot:"
            f"{snapshot.snapshot_id}:version:{snapshot.version}"
        )
        if {
            item.get("reference") for item in normalized.get("target", [])
        } != {expected_target}:
            raise ValueError(
                "State snapshot Provenance must target the exact snapshot version"
            )
        return self._persist_provenance_contract_records(
            provenance=normalized,
            records=[snapshot],
            patient_id=snapshot.patient_id,
            expected_current=expected_current,
            cas_identity=("state_snapshot", snapshot.snapshot_id),
            fault_prefix="state",
        )

    def persist_memory_projection_bundle(
        self,
        *,
        memory: MemoryEvent,
        timeline: TimelineEvent,
        provenance: dict[str, Any],
        revision_bundles: list[tuple[RevisionLink, dict[str, Any]]] | None = None,
    ) -> bool:
        normalized = validate_layer4_fhir_resource(
            provenance, expected_resource_type="Provenance"
        )
        if memory.patient_id != timeline.patient_id:
            raise ValueError("Memory projection patient mismatch")
        if timeline.memory_event_id != memory.event_id:
            raise ValueError("Timeline must reference the bundled Memory event")
        provenance_reference = f"Provenance/{normalized['id']}"
        if {
            (item.reference, item.version_id) for item in memory.provenance_refs
        } != {(provenance_reference, normalized["meta"]["versionId"])}:
            raise ValueError("Memory event must reference the exact bundle Provenance")
        expected_targets = {
            f"urn:continucare:memory-event:{memory.event_id}",
            f"urn:continucare:timeline-event:{timeline.timeline_event_id}",
        }
        if {
            item.get("reference") for item in normalized.get("target", [])
        } != expected_targets:
            raise ValueError(
                "Memory Provenance must target the exact Memory and Timeline events"
            )
        revisions = list(revision_bundles or [])
        source_reference = memory.source.reference
        workflow_projection = source_reference.startswith(("Task/", "Communication/"))
        if revisions and not workflow_projection:
            raise ValueError("Only workflow Memory projections can bundle revisions")

        revision_values: list[
            tuple[
                RevisionLink,
                tuple[dict[str, Any], tuple[str, str, str], str, str, str],
            ]
        ] = []
        for link, revision_provenance in revisions:
            normalized_revision = _validated_revision_provenance(
                link, revision_provenance
            )
            if (
                link.patient_id != memory.patient_id
                or link.relationship.value != "supersedes"
                or link.predecessor.reference != source_reference
                or link.predecessor.version_id == memory.source.version_id
                or link.successor != memory.source
            ):
                raise ValueError(
                    "Workflow revision must supersede a prior version of the bundled source"
                )
            revision_values.append(
                (
                    link,
                    _fhir_storage_values(
                        normalized_revision, patient_id=memory.patient_id
                    ),
                )
            )

        primary_provenance = _fhir_storage_values(
            normalized, patient_id=memory.patient_id
        )
        fhir_values = [
            primary_provenance,
            *(item[1] for item in revision_values),
        ]
        fhir_identities = [item[1] for item in fhir_values]
        if len(fhir_identities) != len(set(fhir_identities)):
            raise ValueError("Memory projection Provenance identities must be unique")

        records: list[Layer4ContractRecord] = [
            memory,
            timeline,
            *(item[0] for item in revision_values),
        ]
        prepared = [_contract_storage_values(record) for record in records]
        contract_identities = [item[0] for item in prepared]
        if len(contract_identities) != len(set(contract_identities)):
            raise ValueError("Memory projection contract identities must be unique")
        if any(item[1] != memory.patient_id for item in prepared):
            raise ValueError("Memory projection bundle patient mismatch")

        expected_predecessors = {
            _versioned_reference(link.predecessor) for link, _ in revision_values
        }
        try:
            with connect(self.db_path) as connection:
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("BEGIN IMMEDIATE")
                fhir_rows = {
                    item[1]: connection.execute(
                        """
                        SELECT resource_json, patient_id FROM layer4_fhir_resources
                        WHERE resource_type=? AND resource_id=? AND version_id=?
                        """,
                        item[1],
                    ).fetchone()
                    for item in fhir_values
                }
                contract_rows = {
                    item[0]: connection.execute(
                        """
                        SELECT record_json, patient_id FROM layer4_contract_records
                        WHERE record_type=? AND record_id=? AND record_version=?
                        """,
                        item[0],
                    ).fetchone()
                    for item in prepared
                }
                any_output = any(row is not None for row in fhir_rows.values()) or any(
                    row is not None for row in contract_rows.values()
                )
                if any_output:
                    exact = all(
                        (row := fhir_rows[item[1]]) is not None
                        and row["resource_json"] == item[2]
                        and row["patient_id"] == memory.patient_id
                        for item in fhir_values
                    ) and all(
                        (row := contract_rows[item[0]]) is not None
                        and row["record_json"] == item[-1]
                        and row["patient_id"] == memory.patient_id
                        for item in prepared
                    )
                    if exact:
                        return False
                    raise ConcurrentWriteConflict(
                        "Memory projection bundle has a conflicting or partial replay"
                    )

                if workflow_projection:
                    prior_rows = connection.execute(
                        """
                        SELECT record_json FROM layer4_contract_records
                        WHERE record_type='memory_event' AND patient_id=?
                          AND pathway_code=? AND is_current=1
                        """,
                        (memory.patient_id, memory.pathway_code),
                    ).fetchall()
                    actual_predecessors = {
                        _versioned_reference(event.source)
                        for row in prior_rows
                        if (
                            event := MemoryEvent.model_validate_json(
                                row["record_json"]
                            )
                        ).pathway_version
                        == memory.pathway_version
                        and event.source.reference == source_reference
                        and event.source.version_id != memory.source.version_id
                    }
                    if actual_predecessors != expected_predecessors:
                        raise ConcurrentWriteConflict(
                            "Workflow Memory history changed; refresh and retry"
                        )

                _insert_fhir_row(
                    connection,
                    normalized=primary_provenance[0],
                    identity=primary_provenance[1],
                    patient_id=memory.patient_id,
                    payload=primary_provenance[2],
                    clinical_time=primary_provenance[3],
                    updated_at=primary_provenance[4],
                )
                self._provenance_contract_bundle_fault("memory:after_provenance")
                for index, item in enumerate(prepared[:2]):
                    _insert_contract_row(
                        connection,
                        identity=item[0],
                        patient_id=item[1],
                        pathway_code=item[2],
                        status=item[3],
                        effective_time=item[4],
                        updated_at=item[5],
                        payload=item[6],
                    )
                    self._provenance_contract_bundle_fault(
                        f"memory:after_contract:{index}"
                    )
                for index, ((_, revision_fhir), revision_record) in enumerate(
                    zip(revision_values, prepared[2:])
                ):
                    self._provenance_contract_bundle_fault(
                        f"memory:before_revision:{index}"
                    )
                    _insert_fhir_row(
                        connection,
                        normalized=revision_fhir[0],
                        identity=revision_fhir[1],
                        patient_id=memory.patient_id,
                        payload=revision_fhir[2],
                        clinical_time=revision_fhir[3],
                        updated_at=revision_fhir[4],
                    )
                    self._provenance_contract_bundle_fault(
                        f"memory:after_revision_provenance:{index}"
                    )
                    _insert_contract_row(
                        connection,
                        identity=revision_record[0],
                        patient_id=revision_record[1],
                        pathway_code=revision_record[2],
                        status=revision_record[3],
                        effective_time=revision_record[4],
                        updated_at=revision_record[5],
                        payload=revision_record[6],
                    )
                    self._provenance_contract_bundle_fault(
                        f"memory:after_revision_contract:{index}"
                    )
                self._provenance_contract_bundle_fault("memory:before_commit")
            self._provenance_contract_bundle_fault("memory:after_commit")
        except sqlite3.OperationalError as exc:
            if is_sqlite_busy(exc):
                raise ConcurrentWriteConflict(
                    "Memory projection database is busy; retry the same request"
                ) from exc
            raise
        return True

    def persist_revision_link_bundle(
        self,
        *,
        link: RevisionLink,
        provenance: dict[str, Any],
    ) -> bool:
        normalized = _validated_revision_provenance(link, provenance)
        return self._persist_provenance_contract_records(
            provenance=normalized,
            records=[link],
            patient_id=link.patient_id,
            expected_current=None,
            cas_identity=None,
            fault_prefix="revision",
        )

    def _persist_provenance_contract_records(
        self,
        *,
        provenance: dict[str, Any],
        records: list[Layer4ContractRecord],
        patient_id: str,
        expected_current: Layer4ContractRecord | None,
        cas_identity: tuple[str, str] | None,
        fault_prefix: str,
    ) -> bool:
        if not records:
            raise ValueError("Provenance contract bundle requires records")
        provenance_values = _fhir_storage_values(
            provenance, patient_id=patient_id
        )
        prepared = [_contract_storage_values(record) for record in records]
        identities = [item[0] for item in prepared]
        if len(identities) != len(set(identities)):
            raise ValueError("Provenance contract identities must be unique")
        if any(item[1] != patient_id for item in prepared):
            raise ValueError("Provenance contract bundle patient mismatch")
        expected_payload = (
            _contract_storage_values(expected_current)[-1]
            if expected_current is not None
            else None
        )
        try:
            with connect(self.db_path) as connection:
                connection.execute("PRAGMA busy_timeout=0")
                connection.execute("BEGIN IMMEDIATE")
                provenance_row = connection.execute(
                    """
                    SELECT resource_json, patient_id FROM layer4_fhir_resources
                    WHERE resource_type='Provenance' AND resource_id=? AND version_id=?
                    """,
                    provenance_values[1][1:],
                ).fetchone()
                contract_rows = {
                    identity: connection.execute(
                        """
                        SELECT record_json, patient_id FROM layer4_contract_records
                        WHERE record_type=? AND record_id=? AND record_version=?
                        """,
                        identity,
                    ).fetchone()
                    for identity in identities
                }
                any_output = provenance_row is not None or any(
                    row is not None for row in contract_rows.values()
                )
                if any_output:
                    exact = (
                        provenance_row is not None
                        and provenance_row["resource_json"] == provenance_values[2]
                        and provenance_row["patient_id"] == patient_id
                        and all(
                            (row := contract_rows[item[0]]) is not None
                            and row["record_json"] == item[-1]
                            and row["patient_id"] == patient_id
                            for item in prepared
                        )
                    )
                    if exact:
                        return False
                    raise ConcurrentWriteConflict(
                        "Provenance contract bundle has a conflicting or partial replay"
                    )
                if cas_identity is not None:
                    current = connection.execute(
                        """
                        SELECT record_json FROM layer4_contract_records
                        WHERE record_type=? AND record_id=? AND is_current=1
                        """,
                        cas_identity,
                    ).fetchone()
                    if expected_payload is None:
                        if current is not None:
                            raise ConcurrentWriteConflict(
                                "derived contract changed; refresh and retry"
                            )
                    elif current is None or current["record_json"] != expected_payload:
                        raise ConcurrentWriteConflict(
                            "derived contract changed; refresh and retry"
                        )
                _insert_fhir_row(
                    connection,
                    normalized=provenance_values[0],
                    identity=provenance_values[1],
                    patient_id=patient_id,
                    payload=provenance_values[2],
                    clinical_time=provenance_values[3],
                    updated_at=provenance_values[4],
                )
                self._provenance_contract_bundle_fault(
                    f"{fault_prefix}:after_provenance"
                )
                for index, item in enumerate(prepared):
                    (
                        identity,
                        record_patient_id,
                        pathway_code,
                        status,
                        effective_time,
                        updated_at,
                        payload,
                    ) = item
                    _insert_contract_row(
                        connection,
                        identity=identity,
                        patient_id=record_patient_id,
                        pathway_code=pathway_code,
                        status=status,
                        effective_time=effective_time,
                        updated_at=updated_at,
                        payload=payload,
                    )
                    self._provenance_contract_bundle_fault(
                        f"{fault_prefix}:after_contract:{index}"
                    )
                self._provenance_contract_bundle_fault(
                    f"{fault_prefix}:before_commit"
                )
            self._provenance_contract_bundle_fault(f"{fault_prefix}:after_commit")
        except sqlite3.OperationalError as exc:
            if is_sqlite_busy(exc):
                raise ConcurrentWriteConflict(
                    "Provenance contract database is busy; retry the same request"
                ) from exc
            raise
        return True

    def _provenance_contract_bundle_fault(self, stage: str) -> None:
        """Test seam for derived-record rollback and post-commit replay."""

        return None

    def get_contract(
        self,
        record_type: str,
        record_id: str,
        *,
        version: str | None = None,
    ) -> Layer4ContractRecord | None:
        model = _CONTRACT_MODELS.get(record_type)
        if model is None:
            raise ValueError(f"unknown Layer-4 record type {record_type!r}")
        query = (
            "SELECT record_json FROM layer4_contract_records "
            "WHERE record_type = ? AND record_id = ?"
        )
        params: tuple[Any, ...] = (record_type, record_id)
        if version is None:
            query += " AND is_current = 1"
        else:
            query += " AND record_version = ?"
            params += (version,)
        with connect(self.db_path) as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            return None
        return cast(Layer4ContractRecord, model.model_validate_json(row["record_json"]))

    def list_contracts(
        self,
        record_type: str,
        *,
        patient_id: str | None = None,
        pathway_code: str | None = None,
        status: str | None = None,
        current_only: bool = True,
    ) -> list[Layer4ContractRecord]:
        model = _CONTRACT_MODELS.get(record_type)
        if model is None:
            raise ValueError(f"unknown Layer-4 record type {record_type!r}")
        clauses = ["record_type = ?"]
        params: list[Any] = [record_type]
        if patient_id is not None:
            clauses.append("patient_id = ?")
            params.append(patient_id)
        if pathway_code is not None:
            clauses.append("pathway_code = ?")
            params.append(pathway_code)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if current_only:
            clauses.append("is_current = 1")
        query = (
            "SELECT record_json FROM layer4_contract_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY effective_time DESC, record_id"
        )
        with connect(self.db_path) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [
            cast(Layer4ContractRecord, model.model_validate_json(row["record_json"]))
            for row in rows
        ]

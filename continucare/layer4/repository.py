"""Technology-neutral persistence port for Layer-4 services."""

from __future__ import annotations

from typing import Any, Protocol

from continucare.layer4.contracts import (
    ClinicalStateSnapshot,
    DoctorReview,
    Layer4ContractRecord,
    Layer4SummaryDraft,
    MemoryEvent,
    RevisionLink,
    TimelineEvent,
)
from continucare.models import AuditEvent, FollowUpMessage


class Layer4Repository(Protocol):
    def save_fhir_resource(
        self, resource: dict[str, Any], *, patient_id: str | None
    ) -> dict[str, Any]: ...

    def persist_fhir_creation_bundle(
        self,
        *,
        resources: list[dict[str, Any]],
        patient_id: str | None,
    ) -> bool: ...

    def persist_task_transition(
        self,
        *,
        patient_id: str,
        expected_task: dict[str, Any],
        task: dict[str, Any],
        provenance: dict[str, Any],
    ) -> bool: ...

    def get_fhir_resource(
        self,
        resource_type: str,
        resource_id: str,
        *,
        version_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    def list_fhir_resources(
        self,
        *,
        patient_id: str | None = None,
        resource_type: str | None = None,
        status: str | None = None,
        current_only: bool = True,
    ) -> list[dict[str, Any]]: ...

    def save_contract(self, record: Layer4ContractRecord) -> Layer4ContractRecord: ...

    def persist_summary_bundle(
        self,
        *,
        expected_current: Layer4SummaryDraft | None,
        summary: Layer4SummaryDraft,
        provenance: dict[str, Any],
    ) -> bool: ...

    def persist_state_snapshot_bundle(
        self,
        *,
        expected_current: ClinicalStateSnapshot | None,
        snapshot: ClinicalStateSnapshot,
        provenance: dict[str, Any],
    ) -> bool: ...

    def persist_memory_projection_bundle(
        self,
        *,
        memory: MemoryEvent,
        timeline: TimelineEvent,
        provenance: dict[str, Any],
        revision_bundles: list[tuple[RevisionLink, dict[str, Any]]] | None = None,
    ) -> bool: ...

    def persist_revision_link_bundle(
        self,
        *,
        link: RevisionLink,
        provenance: dict[str, Any],
    ) -> bool: ...

    def persist_doctor_review_bundle(
        self,
        *,
        expected_source: Layer4SummaryDraft,
        provenance: dict[str, Any],
        result_summary: Layer4SummaryDraft,
        review: DoctorReview,
        audit_event: AuditEvent,
    ) -> bool: ...

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
    ) -> bool: ...

    def get_contract(
        self,
        record_type: str,
        record_id: str,
        *,
        version: str | None = None,
    ) -> Layer4ContractRecord | None: ...

    def list_contracts(
        self,
        record_type: str,
        *,
        patient_id: str | None = None,
        pathway_code: str | None = None,
        status: str | None = None,
        current_only: bool = True,
    ) -> list[Layer4ContractRecord]: ...

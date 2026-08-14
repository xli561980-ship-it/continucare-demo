"""Final-resource-only input port for future Layer-4 consumers.

The port deliberately exposes no chat turn, semantic candidate, AgentRun, or
fallback-parser output. Layer 4 must reconstruct its state from finalized FHIR
resources and durable audit events.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from continucare.care_agent.release import LAYER3_RELEASE
from continucare.db import utc_now_iso
from continucare.fhir.r4 import validate_r4_resource
from continucare.models import AuditEvent, Observation


class Layer4ReadStore(Protocol):
    def list_completed_questionnaire_responses(
        self, patient_id: str
    ) -> list[dict[str, Any]]: ...

    def list_final_observations(self, patient_id: str) -> list[Observation]: ...

    def list_audit_events(self, patient_id: str | None = None) -> list[AuditEvent]: ...


class Layer4InputSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = LAYER3_RELEASE.version
    patient_id: str
    questionnaire_responses: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    audit_events: list[AuditEvent] = Field(default_factory=list)
    assembled_at: str


class Layer4InputReader:
    """Assemble only accepted, durable outputs from Layers 1-3."""

    def __init__(self, store: Layer4ReadStore):
        self.store = store

    def read(
        self, patient_id: str, *, assembled_at: str | None = None
    ) -> Layer4InputSnapshot:
        responses = []
        for resource in self.store.list_completed_questionnaire_responses(patient_id):
            resource = validate_r4_resource(
                resource, expected_resource_type="QuestionnaireResponse"
            )
            if resource.get("status") != "completed":
                raise ValueError("Layer 4 only accepts completed QuestionnaireResponse")
            responses.append(resource)

        observations = []
        for item in self.store.list_final_observations(patient_id):
            resource = validate_r4_resource(
                item.as_fhir(), expected_resource_type="Observation"
            )
            if resource.get("status") != "final":
                raise ValueError("Layer 4 only accepts final Observation resources")
            observations.append(resource)
        return Layer4InputSnapshot(
            patient_id=patient_id,
            questionnaire_responses=responses,
            observations=observations,
            audit_events=self.store.list_audit_events(patient_id),
            assembled_at=assembled_at or utc_now_iso(),
        )

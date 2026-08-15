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
from continucare.models import AuditEvent, FollowUpMessage, Observation
from continucare.layer4.manual_reviews import admit_final_patient_report


class Layer4ReadStore(Protocol):
    def get_message(self, message_id: str) -> FollowUpMessage | None: ...

    def list_completed_questionnaire_responses(
        self,
        patient_id: str,
        *,
        pathway_code: str,
        pathway_version: str,
    ) -> list[dict[str, Any]]: ...

    def list_final_observations(
        self,
        patient_id: str,
        *,
        pathway_code: str,
        pathway_version: str,
    ) -> list[Observation]: ...

    def list_pathway_audit_events(
        self,
        patient_id: str,
        *,
        pathway_code: str,
        pathway_version: str,
    ) -> list[AuditEvent]: ...


class Layer4InputSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = LAYER3_RELEASE.version
    patient_id: str
    pathway_code: str = Field(min_length=1)
    pathway_version: str = Field(min_length=1)
    questionnaire_responses: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    audit_events: list[AuditEvent] = Field(default_factory=list)
    assembled_at: str


class Layer4InputReader:
    """Assemble only accepted, durable outputs from Layers 1-3."""

    def __init__(self, store: Layer4ReadStore):
        self.store = store

    def read(
        self,
        patient_id: str,
        *,
        pathway_code: str,
        pathway_version: str,
        assembled_at: str | None = None,
    ) -> Layer4InputSnapshot:
        responses = []
        for resource in self.store.list_completed_questionnaire_responses(
            patient_id,
            pathway_code=pathway_code,
            pathway_version=pathway_version,
        ):
            resource = validate_r4_resource(
                resource, expected_resource_type="QuestionnaireResponse"
            )
            if resource.get("status") != "completed":
                raise ValueError("Layer 4 only accepts completed QuestionnaireResponse")
            responses.append(resource)

        response_references = {
            f"QuestionnaireResponse/{item['id']}" for item in responses
        }
        if len(response_references) != len(responses):
            raise ValueError("Layer 4 requires unique QuestionnaireResponse ids")

        observations = []
        observations_by_response = {reference: [] for reference in response_references}
        for item in self.store.list_final_observations(
            patient_id,
            pathway_code=pathway_code,
            pathway_version=pathway_version,
        ):
            resource = validate_r4_resource(
                item.as_fhir(), expected_resource_type="Observation"
            )
            if resource.get("status") != "final":
                raise ValueError("Layer 4 only accepts final Observation resources")
            derived_responses = [
                reference
                for reference in (
                    entry.get("reference")
                    for entry in resource.get("derivedFrom", [])
                )
                if isinstance(reference, str)
                and reference.startswith("QuestionnaireResponse/")
            ]
            if len(derived_responses) != 1:
                raise ValueError(
                    "Layer 4 Observation must derive from exactly one "
                    "QuestionnaireResponse"
                )
            source_reference = derived_responses[0]
            if source_reference not in response_references:
                raise ValueError(
                    "Layer 4 Observation derives from a response outside the "
                    "requested Pathway"
                )
            observations.append(resource)
            observations_by_response[source_reference].append(resource)
        for response in responses:
            admit_final_patient_report(
                patient_id=patient_id,
                questionnaire_response=response,
                observations=observations_by_response[
                    f"QuestionnaireResponse/{response['id']}"
                ],
                require_observations=False,
            )
        return Layer4InputSnapshot(
            patient_id=patient_id,
            pathway_code=pathway_code,
            pathway_version=pathway_version,
            questionnaire_responses=responses,
            observations=observations,
            audit_events=self.store.list_pathway_audit_events(
                patient_id,
                pathway_code=pathway_code,
                pathway_version=pathway_version,
            ),
            assembled_at=assembled_at or utc_now_iso(),
        )

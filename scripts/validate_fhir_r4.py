"""Validate governed examples with HL7's official FHIR R4 JSON Schema."""

from __future__ import annotations

import argparse

from continucare.fhir.r4 import validate_official_json_schema
from continucare.fhir.questionnaires import build_free_text_questionnaire_response
from continucare.fhir.terminology import VOMITING_COUNT_24H
from continucare.fhir.observations import (
    build_patient_reported_observation,
    per_day_quantity,
)
from continucare.layer4.fhir import (
    build_communication,
    build_provenance,
    build_workflow_task,
)
from continucare.pathways import load_glp1_plan_definition, load_glp1_questionnaire


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--schema",
        required=True,
        help="path to the official HL7 fhir.schema.json.zip for R4 4.0.1",
    )
    args = parser.parse_args()
    response = build_free_text_questionnaire_response(
        response_id="message-validation-example",
        patient_id="P-DEMO-001",
        authored="2026-07-31T10:00:00+00:00",
        text="今天吐了一次。",
    )
    observation = build_patient_reported_observation(
        observation_id="observation-validation-example",
        patient_id="P-DEMO-001",
        questionnaire_response_id=response["id"],
        effective_time=response["authored"],
        code=VOMITING_COUNT_24H,
        value_element="valueQuantity",
        value=per_day_quantity(1, unit="vomiting episodes/24 hours"),
        effective_period_hours=24,
    )
    communication = build_communication(
        patient_id="P-DEMO-001",
        content_text="合成随访确认。",
        sender_reference="Patient/P-DEMO-001",
        recipient_references=["PractitionerRole/nurse"],
        sent_at=response["authored"],
        communication_id="communication-validation-example",
    )
    task = build_workflow_task(
        patient_id="P-DEMO-001",
        rule_id="synthetic-validation-rule",
        rule_version="1.0.0",
        task_code_system="urn:continucare:task-code",
        task_code="human-review",
        task_code_display="人工复核",
        description="仅用于 FHIR 合成资源校验。",
        requester_reference="Organization/continucare",
        owner_reference="PractitionerRole/nurse",
        authored_on=response["authored"],
        trigger_reference=f"Observation/{observation['id']}",
        due_at="2026-07-31T14:00:00+00:00",
        task_id="task-validation-example",
        evidence_references=[f"Observation/{observation['id']}/_history/1"],
    )
    provenance = build_provenance(
        target_references=["Task/task-validation-example"],
        recorded_at=response["authored"],
        agent_reference="Device/continucare-rule-engine",
        agent_role_code="author",
        agent_role_display="Author",
        provenance_id="provenance-validation-example",
        activity_code="CREATE",
        activity_display="create",
    )
    resources = [
        load_glp1_questionnaire(),
        load_glp1_plan_definition(),
        response,
        observation,
        communication,
        task,
        provenance,
    ]
    for resource in resources:
        validate_official_json_schema(resource, args.schema)
        print(f"{resource['resourceType']}/{resource['id']}: valid")


if __name__ == "__main__":
    main()

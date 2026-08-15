from __future__ import annotations

import hashlib
import json
import os
import zipfile

import pytest

import continucare.fhir.r4 as r4_module
from continucare.adapters.mock_extractor import MockExtractor
from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_agent.release import LAYER3_RELEASE
from continucare.fhir.r4 import (
    FHIRValidationError,
    OFFICIAL_R4_SCHEMA_SHA256,
    validate_official_json_schema,
    validate_r4_resource,
)
from continucare.fhir.questionnaires import build_free_text_questionnaire_response
from continucare.fhir.references import (
    validate_questionnaire_response_against_questionnaire,
)
from continucare.models import FollowUpMessage
from continucare.layer4.fhir import (
    build_communication,
    build_provenance,
    build_workflow_task,
)
from continucare.pathways import load_glp1_plan_definition, load_glp1_questionnaire


def _write_synthetic_schema_zip(path) -> str:
    schema = {
        "$schema": "http://json-schema.org/draft-06/schema#",
        "type": "object",
        "required": ["resourceType"],
        "properties": {"resourceType": {"type": "string"}},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fhir.schema.json", json.dumps(schema))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def example_resources():
    response = build_free_text_questionnaire_response(
        response_id="message-fhir-test",
        patient_id="P-DEMO-001",
        authored="2026-07-17T10:00:00+00:00",
        text="今天吐了一次，估计过去24小时喝水800毫升。",
    )
    message = FollowUpMessage(
        message_id=response["id"],
        patient_id="P-DEMO-001",
        message_text=response["item"][0]["answer"][0]["valueString"],
        submitted_at=response["authored"],
        processing_status="received",
    )
    observations = [item.resource for item in MockExtractor().extract(message).observations]
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
        trigger_reference=f"Observation/{observations[0]['id']}",
        due_at="2026-07-17T14:00:00+00:00",
        task_id="task-validation-example",
        evidence_references=[f"Observation/{observations[0]['id']}/_history/1"],
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
    return [
        load_glp1_questionnaire(),
        load_glp1_plan_definition(),
        response,
        *observations,
        communication,
        task,
        provenance,
    ]


def test_all_clinical_resources_pass_strict_r4_models():
    for resource in example_resources():
        normalized = validate_r4_resource(resource)
        assert normalized["resourceType"] == resource["resourceType"]


def test_all_clinical_resources_pass_official_hl7_json_schema():
    schema_path = os.environ.get("FHIR_R4_SCHEMA_ZIP")
    if not schema_path:
        pytest.skip("set FHIR_R4_SCHEMA_ZIP to the official HL7 R4 schema archive")
    for resource in example_resources():
        validate_official_json_schema(resource, schema_path)


def test_synthetic_schema_zip_passes_with_explicit_actual_sha256(tmp_path):
    schema_path = tmp_path / "synthetic-fhir-schema.zip"
    actual_sha256 = _write_synthetic_schema_zip(schema_path)

    validate_official_json_schema(
        {"resourceType": "Patient"},
        schema_path,
        expected_sha256=actual_sha256,
    )


def test_wrong_sha256_fails_before_opening_schema_zip(tmp_path, monkeypatch):
    schema_path = tmp_path / "synthetic-fhir-schema.zip"
    actual_sha256 = _write_synthetic_schema_zip(schema_path)
    expected_sha256 = "f" * 64

    def unexpected_zip_open(*_args, **_kwargs):
        raise AssertionError("schema ZIP must not be opened after a sha256 mismatch")

    def unexpected_json_load(*_args, **_kwargs):
        raise AssertionError("schema JSON must not be parsed after a sha256 mismatch")

    monkeypatch.setattr(r4_module.zipfile, "ZipFile", unexpected_zip_open)
    monkeypatch.setattr(r4_module.json, "loads", unexpected_json_load)

    with pytest.raises(FHIRValidationError, match="sha256") as exc_info:
        validate_official_json_schema(
            {"resourceType": "Patient"},
            schema_path,
            expected_sha256=expected_sha256,
        )

    assert str(exc_info.value) == (
        "official FHIR schema archive sha256 mismatch: "
        f"expected {expected_sha256[:16]}, actual {actual_sha256[:16]}"
    )


def test_default_official_sha256_rejects_parseable_substitute_zip(tmp_path):
    schema_path = tmp_path / "substitute-fhir-schema.zip"
    _write_synthetic_schema_zip(schema_path)

    with pytest.raises(FHIRValidationError, match="sha256"):
        validate_official_json_schema({"resourceType": "Patient"}, schema_path)


def test_invalid_schema_json_encoding_uses_project_error(tmp_path):
    schema_path = tmp_path / "invalid-json-encoding.zip"
    with zipfile.ZipFile(schema_path, "w") as archive:
        archive.writestr("fhir.schema.json", b"\xff")
    actual_sha256 = hashlib.sha256(schema_path.read_bytes()).hexdigest()

    with pytest.raises(FHIRValidationError, match="invalid official FHIR schema"):
        validate_official_json_schema(
            {"resourceType": "Patient"},
            schema_path,
            expected_sha256=actual_sha256,
        )


def test_official_schema_sha256_matches_layer3_release_manifest():
    assert OFFICIAL_R4_SCHEMA_SHA256 == LAYER3_RELEASE.fhir_schema_sha256


def test_unknown_fhir_element_is_rejected():
    resource = example_resources()[2]
    resource["inventedClinicalField"] = "not allowed"

    with pytest.raises(FHIRValidationError):
        validate_r4_resource(resource)


def test_missing_observation_status_is_rejected():
    resource = example_resources()[3]
    resource.pop("status")

    with pytest.raises(FHIRValidationError):
        validate_r4_resource(resource)


def test_questionnaire_response_must_match_questionnaire():
    questionnaire = load_glp1_questionnaire()
    response = example_resources()[2]
    response["item"][0]["linkId"] = "invented-question"

    with pytest.raises(FHIRValidationError, match="not present in Questionnaire"):
        validate_questionnaire_response_against_questionnaire(
            response, questionnaire
        )


def test_persistence_keeps_exact_fhir_resource(tmp_path):
    store = SQLiteStore(tmp_path / "fhir-round-trip.db")
    resource = example_resources()[2]

    store.save_message(
        FollowUpMessage(
            message_id=resource["id"],
            patient_id="P-DEMO-001",
            message_text=resource["item"][0]["answer"][0]["valueString"],
            submitted_at=resource["authored"],
            processing_status="received",
        )
    )
    store.save_questionnaire_response(resource)

    assert store.get_questionnaire_response(resource["id"]) == resource


def test_persistence_revalidates_mutated_observation(tmp_path):
    store = SQLiteStore(tmp_path / "fhir-write-boundary.db")
    resource = example_resources()[2]
    message = FollowUpMessage(
        message_id=resource["id"],
        patient_id="P-DEMO-001",
        message_text=resource["item"][0]["answer"][0]["valueString"],
        submitted_at=resource["authored"],
        processing_status="received",
    )
    store.save_message(message)
    store.save_questionnaire_response(resource)
    observation = MockExtractor().extract(message).observations[0]
    observation.resource["inventedClinicalField"] = True

    with pytest.raises(FHIRValidationError):
        store.save_observations([observation])

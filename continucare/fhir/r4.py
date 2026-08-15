"""Strict FHIR R4 (4.0.1) parsing and independent schema validation.

The SMART on FHIR generated models reject unknown elements and enforce the
base-resource cardinalities used by this project. CI can additionally pass the
official HL7 ``fhir.schema.json.zip`` to ``validate_official_json_schema``.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from fhirclient.models.observation import Observation as R4Observation
from fhirclient.models.plandefinition import PlanDefinition as R4PlanDefinition
from fhirclient.models.communication import Communication as R4Communication
from fhirclient.models.provenance import Provenance as R4Provenance
from fhirclient.models.questionnaire import Questionnaire as R4Questionnaire
from fhirclient.models.questionnaireresponse import (
    QuestionnaireResponse as R4QuestionnaireResponse,
)
from fhirclient.models.resource import Resource
from fhirclient.models.task import Task as R4Task
from fhirclient.models.fhirabstractbase import FHIRValidationError as ClientValidationError
from jsonschema import Draft6Validator

FHIR_R4_VERSION = "4.0.1"
OFFICIAL_R4_SCHEMA_SHA256 = (
    "75e5560da3cf503895a44c8ca7af17a83b4cca6c2cb5ba1883d2aec0d1cb5ac6"
)


class FHIRValidationError(ValueError):
    """Raised when a resource is not valid against the selected FHIR boundary."""


_RESOURCE_MODELS: dict[str, type[Resource]] = {
    "Communication": R4Communication,
    "Observation": R4Observation,
    "Questionnaire": R4Questionnaire,
    "QuestionnaireResponse": R4QuestionnaireResponse,
    "PlanDefinition": R4PlanDefinition,
    "Provenance": R4Provenance,
    "Task": R4Task,
}


def validate_r4_resource(
    resource: dict[str, Any], *, expected_resource_type: str | None = None
) -> dict[str, Any]:
    """Validate and normalize one JSON resource against generated R4 models.

    Only resource types explicitly placed on the clinical boundary are accepted.
    This is deliberate: a new clinical resource cannot enter persistence until a
    corresponding conformance test has been added.
    """

    resource_type = resource.get("resourceType")
    if expected_resource_type and resource_type != expected_resource_type:
        raise FHIRValidationError(
            f"expected {expected_resource_type}, received {resource_type or 'missing resourceType'}"
        )
    model_class = _RESOURCE_MODELS.get(str(resource_type))
    if model_class is None:
        raise FHIRValidationError(
            f"FHIR resource type {resource_type!r} is not enabled at this boundary"
        )
    try:
        parsed = model_class(resource)
        normalized = parsed.as_json()
    except (ClientValidationError, TypeError, ValueError) as exc:
        raise FHIRValidationError(str(exc)) from exc
    if normalized.get("resourceType") != resource_type:
        raise FHIRValidationError("FHIR serializer changed resourceType")
    return normalized


def validate_official_json_schema(
    resource: dict[str, Any],
    schema_zip_path: str | Path,
    *,
    expected_sha256: str = OFFICIAL_R4_SCHEMA_SHA256,
) -> None:
    """Validate with HL7's official R4 JSON Schema distribution.

    The official artifact is downloaded from
    https://hl7.org/fhir/R4/fhir.schema.json.zip and is not silently substituted
    with an application-authored schema.
    """

    path = Path(schema_zip_path)
    if not path.is_file():
        raise FHIRValidationError(f"official FHIR schema archive not found: {path}")
    try:
        archive_bytes = path.read_bytes()
    except OSError as exc:
        raise FHIRValidationError(f"invalid official FHIR schema archive: {path}") from exc

    actual_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        raise FHIRValidationError(
            "official FHIR schema archive sha256 mismatch: "
            f"expected {expected_sha256[:16]}, actual {actual_sha256[:16]}"
        )

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            schema = json.loads(archive.read("fhir.schema.json"))
    except (
        OSError,
        KeyError,
        UnicodeDecodeError,
        zipfile.BadZipFile,
        json.JSONDecodeError,
    ) as exc:
        raise FHIRValidationError(f"invalid official FHIR schema archive: {path}") from exc

    errors = sorted(
        Draft6Validator(schema).iter_errors(resource),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors[:10]
        )
        raise FHIRValidationError(f"HL7 FHIR R4 JSON Schema validation failed: {details}")

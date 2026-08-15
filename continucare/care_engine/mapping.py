"""Deterministic mapping of patient-confirmed answers to FHIR Observations."""

from __future__ import annotations

import math
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from continucare.fhir.observations import (
    build_patient_reported_observation,
    millilitres_per_24_hours,
    per_day_quantity,
)
from continucare.fhir.questionnaires import (
    flatten_questionnaire_items,
    questionnaire_canonical,
    questionnaire_response_answers,
    questionnaire_response_summary,
)
from continucare.fhir.r4 import FHIRValidationError
from continucare.fhir.terminology import UCUM, CodingDefinition
from continucare.agents.contracts import CodingContract
from continucare.models import ConfidenceTier, ConfirmedAnswerContext, Observation
from continucare.pathways.mappings import ObservationMappingPolicy
from continucare.terminology import load_glp1_symptom_catalog


def map_response_to_observations(
    *,
    response: dict[str, Any],
    questionnaire: dict[str, Any],
    policy: ObservationMappingPolicy,
    answer_contexts: dict[str, ConfirmedAnswerContext] | None = None,
) -> list[Observation]:
    """Apply only the explicit, governed mapping policy; never infer values."""

    canonical = questionnaire_canonical(questionnaire)
    if response.get("questionnaire") != canonical or policy.questionnaire != canonical:
        raise FHIRValidationError(
            "QuestionnaireResponse and mapping policy must target the same Questionnaire"
        )
    patient_id = response["subject"]["reference"].removeprefix("Patient/")
    answers = questionnaire_response_answers(response)
    questions = {
        item["linkId"]: item
        for item in flatten_questionnaire_items(questionnaire.get("item", []))
    }
    source_text, fragments = questionnaire_response_summary(response, questionnaire)
    observations: list[Observation] = []
    answer_contexts = answer_contexts or {}

    for mapping in policy.mappings:
        if mapping.link_id not in questions:
            raise FHIRValidationError(
                f"mapping linkId {mapping.link_id!r} is absent from Questionnaire"
            )
        if mapping.link_id not in answers:
            continue
        answer = answers[mapping.link_id]
        question = questions[mapping.link_id]
        code = _question_code(question)
        value_element, value = _mapped_value(mapping, answer)
        if value_element is None:
            continue
        fragment = fragments[mapping.link_id]
        evidence_start = source_text.find(fragment)
        if evidence_start < 0:
            raise FHIRValidationError("structured answer evidence fragment was not retained")
        observation_id = f"observation-{uuid5(NAMESPACE_URL, response['id'] + '|' + mapping.link_id).hex}"
        answer_context = answer_contexts.get(mapping.link_id)
        terminology_match = (
            answer_context.terminology_match
            if answer_context and answer_context.terminology_match is not None
            else load_glp1_symptom_catalog()
            .match_for_questionnaire(
                link_id=mapping.link_id,
                coding=CodingContract(
                    system=code.system,
                    code=code.code,
                    display=code.display,
                    version=code.version,
                ),
                matched_text=question.get("text", mapping.link_id),
            )
            .model_dump(mode="json")
        )
        effective_time = (
            answer_context.reported_at if answer_context else response["authored"]
        )
        resource = build_patient_reported_observation(
            observation_id=observation_id,
            patient_id=patient_id,
            questionnaire_response_id=response["id"],
            effective_time=effective_time,
            issued_time=response["authored"],
            code=code,
            value_element=value_element,
            value=value,
            effective_period_hours=mapping.effective_period_hours,
            effective_period_start=(
                answer_context.effective_start
                if answer_context
                and answer_context.temporal_kind != "point_in_time"
                else None
            ),
            effective_period_end=(
                answer_context.effective_end
                if answer_context
                and answer_context.temporal_kind != "point_in_time"
                else None
            ),
        )
        observations.append(
            Observation(
                resource=resource,
                evidence={
                    "questionnaire_response_id": response["id"],
                    "confidence_tier": ConfidenceTier.PATIENT_CONFIRMED,
                    "evidence_text": fragment,
                    "evidence_start": evidence_start,
                    "evidence_end": evidence_start + len(fragment),
                    "recorded_at": response["authored"],
                    "source_kind": "pathway_monitored",
                    "terminology_match": terminology_match,
                },
            )
        )
    return observations


def _question_code(question: dict[str, Any]) -> CodingDefinition:
    codings = question.get("code", [])
    if not codings:
        raise FHIRValidationError(
            f"mapped Questionnaire item {question['linkId']!r} has no code"
        )
    coding = codings[0]
    return CodingDefinition(
        system=coding["system"],
        code=coding["code"],
        display=coding.get("display", coding["code"]),
        version=coding.get("version"),
    )


def _mapped_value(mapping, answer: Any) -> tuple[str | None, Any]:
    if mapping.kind == "boolean":
        if type(answer) is not bool:
            raise FHIRValidationError(f"{mapping.link_id!r} mapping expects boolean")
        if mapping.positive_only and not answer:
            return None, None
        return "valueBoolean", answer
    if mapping.kind == "coded_choice":
        if not isinstance(answer, dict) or "code" not in answer:
            raise FHIRValidationError(f"{mapping.link_id!r} mapping expects Coding")
        return "valueCodeableConcept", {"coding": [answer]}
    if mapping.kind == "count_per_day":
        if type(answer) is not int or answer < 0:
            raise FHIRValidationError(
                f"{mapping.link_id!r} requires a non-negative integer"
            )
        return (
            "valueQuantity",
            per_day_quantity(answer, unit="vomiting episodes/24 hours"),
        )
    if mapping.kind == "millilitres_per_24_hours":
        if (
            not isinstance(answer, dict)
            or set(answer) != {"value", "unit", "system", "code"}
            or type(answer.get("value")) not in {int, float}
        ):
            raise FHIRValidationError(f"{mapping.link_id!r} requires a Quantity")
        if type(answer["value"]) is float and not math.isfinite(answer["value"]):
            raise FHIRValidationError(
                f"{mapping.link_id!r} requires a finite quantity value"
            )
        if answer["value"] < 0:
            raise FHIRValidationError(f"{mapping.link_id!r} cannot be negative")
        if (
            answer["system"] != UCUM
            or answer["unit"] != "mL"
            or answer["code"] != "mL"
            or answer["code"] not in mapping.accepted_quantity_unit_codes
        ):
            raise FHIRValidationError(
                f"{mapping.link_id!r} uses an unapproved quantity unit"
            )
        return "valueQuantity", millilitres_per_24_hours(answer["value"])
    raise FHIRValidationError(f"unsupported observation mapping kind {mapping.kind!r}")

"""Compile a validated release into deterministic runtime artifacts."""

from __future__ import annotations

from copy import deepcopy

from continucare.fhir.r4 import validate_r4_resource
from continucare.knowledge.models import KnowledgeRelease
from continucare.knowledge.registry import KnowledgeRegistry, load_cn_glp1_release
from continucare.knowledge.validator import validate_release
from continucare.pathways import (
    load_glp1_observation_mapping,
    load_glp1_plan_definition,
    load_glp1_questionnaire,
)
from continucare.fhir.questionnaires import flatten_questionnaire_items


EVIDENCE_EXTENSION = "urn:continucare:StructureDefinition:evidence-claim-id"
METRIC_EXTENSION = "urn:continucare:StructureDefinition:metric-id"
RELEASE_EXTENSION = "urn:continucare:StructureDefinition:knowledge-release-id"


def _append_extension(target: dict, extension: dict) -> None:
    extensions = target.setdefault("extension", [])
    if extension not in extensions:
        extensions.append(extension)


def _release(value: KnowledgeRegistry | KnowledgeRelease | None) -> KnowledgeRelease:
    if value is None:
        value = load_cn_glp1_release()
    return value.release if isinstance(value, KnowledgeRegistry) else value


def compile_knowledge_release(
    value: KnowledgeRegistry | KnowledgeRelease | None = None,
) -> KnowledgeRelease:
    release = _release(value)
    validate_release(release)
    return release


def compile_questionnaire(
    value: KnowledgeRegistry | KnowledgeRelease | None = None,
) -> dict:
    """Bind the operational questionnaire to release metrics and evidence."""

    release = compile_knowledge_release(value)
    resource = deepcopy(load_glp1_questionnaire())
    _append_extension(
        resource,
        {"url": RELEASE_EXTENSION, "valueString": release.manifest.release_id},
    )
    by_link = {item.link_id: item for item in release.patient_content}
    metrics = {item.metric_id: item for item in release.metrics}
    flattened = flatten_questionnaire_items(resource["item"])
    link_ids = [item["linkId"] for item in flattened]
    if len(link_ids) != len(set(link_ids)):
        raise ValueError("questionnaire contains duplicate linkId")
    known_link_ids = set(link_ids)
    for item in flattened:
        for condition in item.get("enableWhen", []):
            if condition.get("question") not in known_link_ids:
                raise ValueError(
                    f"questionnaire enableWhen has unknown source for {item['linkId']}"
                )
        content = by_link.get(item["linkId"])
        if not content:
            continue
        if item["text"] != content.text:
            raise ValueError(f"questionnaire wording drift for {item['linkId']}")
        # The Pathway file is only a structural template. Release governance
        # extensions are rebuilt from L1 on every compilation so stale foreign
        # or superseded claims cannot survive in the runtime Questionnaire.
        item["extension"] = [
            extension
            for extension in item.get("extension", [])
            if extension.get("url") not in {METRIC_EXTENSION, EVIDENCE_EXTENSION}
        ]
        if content.metric_id:
            _append_extension(
                item,
                {"url": METRIC_EXTENSION, "valueString": content.metric_id},
            )
            metric = metrics[content.metric_id]
            codings = item.get("code", [])
            if metric.observation_code is not None:
                expected_code = metric.observation_code.model_dump(mode="json")
                if codings != [expected_code]:
                    raise ValueError(f"questionnaire code drift for {item['linkId']}")
                if not any(
                    entry.metric_id == metric.metric_id
                    and entry.entry_type == "observation_code"
                    and entry.coding == metric.observation_code
                    for entry in release.terminology
                ):
                    raise ValueError(
                        f"terminology manifest lacks observation code for {item['linkId']}"
                    )
            options = [
                option["valueCoding"]
                for option in item.get("answerOption", [])
                if "valueCoding" in option
            ]
            if options:
                value_sets = [
                    entry
                    for entry in release.terminology
                    if entry.metric_id == metric.metric_id
                    and entry.entry_type == "answer_value_set"
                ]
                if len(value_sets) != 1 or options != [
                    code.model_dump(mode="json") for code in value_sets[0].allowed_codes
                ]:
                    raise ValueError(f"questionnaire ValueSet drift for {item['linkId']}")
            for unit in metric.allowed_units:
                if not any(
                    entry.metric_id == metric.metric_id
                    and entry.entry_type == "unit"
                    and entry.coding.system == unit.system
                    and entry.coding.code == unit.code
                    for entry in release.terminology
                ):
                    raise ValueError(
                        f"terminology manifest lacks UCUM unit for {item['linkId']}"
                    )
        for claim_id in content.evidence_claim_ids:
            _append_extension(
                item,
                {"url": EVIDENCE_EXTENSION, "valueString": claim_id},
            )
        if not item["extension"]:
            item.pop("extension")
    return validate_r4_resource(resource, expected_resource_type="Questionnaire")


def compile_pro_ctcae_questionnaire(
    value: KnowledgeRegistry | KnowledgeRelease | None = None,
) -> dict:
    """Compile a licensed local PRO-CTCAE overlay, never the public release."""

    release = compile_knowledge_release(value)
    content = [item for item in release.patient_content if item.link_id.startswith("pro-")]
    if not content:
        raise ValueError(
            "restricted PRO-CTCAE content is not included in the public release"
        )
    resource = {
        "resourceType": "Questionnaire",
        "id": "pro-ctcae-glp1-7d-zh-cn-v1",
        "url": "urn:continucare:Questionnaire:pro-ctcae-glp1-7d-zh-cn",
        "version": "1.0.0",
        "name": "PROCTCAEGLP1SevenDayChineseSimplified",
        "title": "PRO-CTCAE GLP-1 胃肠道条目子集（简体中文）",
        "status": "draft",
        "experimental": True,
        "date": "2026-08-13",
        "publisher": "ContinuCare",
        "copyright": "PRO-CTCAE © U.S. National Cancer Institute. Terms of Use apply.",
        "description": "由已登记的NCI Form Builder定制表原样编译；仅用于合成数据和工程验证，不作自动临床分级。",
        "subjectType": ["Patient"],
        "extension": [
            {"url": RELEASE_EXTENSION, "valueString": release.manifest.release_id},
            {
                "url": "urn:continucare:StructureDefinition:source-id",
                "valueString": "nci-pro-ctcae-zh-cn-glp1-custom-2026-08-13",
            },
        ],
        "item": [],
    }
    for item in content:
        compiled = {
            "linkId": item.link_id,
            "text": item.text,
            "type": item.item_type,
            "extension": [
                {"url": METRIC_EXTENSION, "valueString": item.metric_id},
                *[
                    {"url": EVIDENCE_EXTENSION, "valueString": claim_id}
                    for claim_id in item.evidence_claim_ids
                ],
            ],
        }
        if item.answers:
            compiled["answerOption"] = [
                {"valueString": answer} for answer in item.answers
            ]
        resource["item"].append(compiled)
    return validate_r4_resource(resource, expected_resource_type="Questionnaire")


def compile_plan_definition(
    value: KnowledgeRegistry | KnowledgeRelease | None = None,
) -> dict:
    release = compile_knowledge_release(value)
    resource = deepcopy(load_glp1_plan_definition())
    _append_extension(
        resource,
        {"url": RELEASE_EXTENSION, "valueString": release.manifest.release_id},
    )
    questionnaire = compile_questionnaire(release)
    expected = f"{questionnaire['url']}|{questionnaire['version']}"
    for action in resource.get("action", []):
        definition = action.get("definitionCanonical")
        if definition and definition != expected:
            raise ValueError("PlanDefinition Questionnaire canonical drift")
    return validate_r4_resource(resource, expected_resource_type="PlanDefinition")


def compile_observation_mappings(
    value: KnowledgeRegistry | KnowledgeRelease | None = None,
) -> dict:
    release = compile_knowledge_release(value)
    policy = load_glp1_observation_mapping()
    content = {item.link_id: item for item in release.patient_content}
    metrics = {item.metric_id: item for item in release.metrics}
    semantics = {
        "boolean": ("boolean", "current", None, None, []),
        "coded_choice": ("coded", "current", None, None, []),
        "count_per_day": (
            "quantity",
            "previous_24_hours",
            "/d",
            24,
            [],
        ),
        "millilitres_per_24_hours": (
            "quantity",
            "previous_24_hours",
            "mL/(24.h)",
            24,
            ["mL"],
        ),
    }
    mappings = []
    for mapping in policy.mappings:
        item = content.get(mapping.link_id)
        if item is None or item.metric_id is None:
            raise ValueError(f"mapping {mapping.link_id} lacks a release metric binding")
        if mapping.metric_id != item.metric_id:
            raise ValueError(f"mapping metric drift for {mapping.link_id}")
        if mapping.evidence_claim_ids != item.evidence_claim_ids:
            raise ValueError(f"mapping evidence drift for {mapping.link_id}")
        metric = metrics[item.metric_id]
        (
            expected_type,
            expected_window,
            expected_unit,
            expected_period,
            expected_input_units,
        ) = semantics[mapping.kind]
        metric_units = [unit.code for unit in metric.allowed_units]
        if metric.data_type != expected_type:
            raise ValueError(f"mapping data type drift for {mapping.link_id}")
        if metric.time_window != expected_window:
            raise ValueError(f"mapping time window drift for {mapping.link_id}")
        if metric_units != ([] if expected_unit is None else [expected_unit]):
            raise ValueError(f"mapping UCUM unit drift for {mapping.link_id}")
        if mapping.effective_period_hours != expected_period:
            raise ValueError(f"mapping effective period drift for {mapping.link_id}")
        if mapping.accepted_quantity_unit_codes != expected_input_units:
            raise ValueError(f"mapping input unit drift for {mapping.link_id}")
        mappings.append(
            {
                **mapping.model_dump(mode="json"),
                "metric_id": item.metric_id,
                "evidence_claim_ids": item.evidence_claim_ids,
            }
        )
    return {
        "knowledge_release_id": release.manifest.release_id,
        "pathway_code": policy.pathway_code,
        "pathway_version": policy.pathway_version,
        "questionnaire": policy.questionnaire,
        "mappings": mappings,
    }

"""Human-readable labels for the role-facing demo pages.

Business codes remain unchanged in persistence and audit. This module only
translates them for the presentation layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from continucare.knowledge.models import KnowledgeRelease
from continucare.knowledge.registry import load_cn_glp1_release
from continucare.models import Alert, AlertStatus, Observation
from continucare.pathways.models import PathwayDefinition
from continucare.pathways.registry import load_builtin_pathways


L5_REQUIRED_DISCLAIMERS = (
    "仅用于合成数据和工程验证",
    "未完成临床审核",
    "不提供诊断和治疗建议",
)

INDICATION_LABELS = {
    "chronic_weight_management": "慢性体重管理",
    "cardiovascular_mace_risk_reduction": "降低主要心血管不良事件风险",
    "type_2_diabetes": "2型糖尿病",
    "moderate_to_severe_obstructive_sleep_apnea": "中重度阻塞性睡眠呼吸暂停",
}


@dataclass(frozen=True)
class L5GovernanceView:
    pathway_code: str
    pathway_version: str
    knowledge_release_id: str
    products: tuple[str, ...]
    indications: tuple[str, ...]
    data_sources: tuple[str, ...]
    knowledge_status: str
    pathway_status: str
    review_status: str
    disclaimers: tuple[str, ...] = L5_REQUIRED_DISCLAIMERS


@dataclass(frozen=True)
class L5SubmissionView:
    response_id: str
    questionnaire: str
    authored: str
    response_status: str
    raw_answer_rows: tuple[dict[str, str], ...]
    observation_rows: tuple[dict[str, str], ...]
    response_resource: dict[str, Any]
    observation_resources: tuple[dict[str, Any], ...]


def build_l5_governance_view(
    pathway_code: str,
    pathway_version: str | None = None,
    *,
    knowledge_release_id: str | None = None,
    pathway: PathwayDefinition | None = None,
    release: KnowledgeRelease | None = None,
) -> L5GovernanceView:
    """Resolve one fail-closed L5 view from the packaged Pathway and L1 release."""

    pathway = pathway or load_builtin_pathways().get(pathway_code, pathway_version)
    release = release or load_cn_glp1_release().release
    expected_release = pathway.knowledge_release_id
    if expected_release != release.manifest.release_id:
        raise ValueError("Pathway and L1 knowledge release do not match")
    if knowledge_release_id and knowledge_release_id != expected_release:
        raise ValueError("CareSession and Pathway knowledge release do not match")

    product_by_id = {item.product_id: item for item in release.products}
    missing_products = set(pathway.product_scope) - set(product_by_id)
    if missing_products:
        raise ValueError("Pathway contains an unknown L1 product")
    products = tuple(
        (
            f"{product_by_id[product_id].brand_name_zh} · "
            f"{product_by_id[product_id].approval_number or product_id} · "
            f"{product_by_id[product_id].strength or '规格待核验'} · "
            f"{product_by_id[product_id].verification_status}"
        )
        for product_id in pathway.product_scope
    )

    applicable_metrics = [
        metric
        for metric in release.metrics
        if metric.runtime_eligible
        and _scope_overlaps(metric.product_scope, pathway.product_scope)
        and _scope_overlaps(metric.indication_scope, pathway.indication_scope)
        and _scope_overlaps(metric.population_scope, pathway.population_scope)
    ]
    claim_ids = {
        claim_id
        for metric in applicable_metrics
        for claim_id in metric.evidence_claim_ids
    }
    claim_by_id = {item.claim_id: item for item in release.evidence_claims}
    unknown_claims = claim_ids - set(claim_by_id)
    if unknown_claims:
        raise ValueError("L1 metric contains an unknown Evidence Claim")
    source_ids = {
        claim_by_id[claim_id].source_id
        for claim_id in claim_ids
    }
    for product_id in pathway.product_scope:
        product = product_by_id[product_id]
        source_ids.update(product.approval_source_ids)
        if product.label_source_id:
            source_ids.add(product.label_source_id)
    source_by_id = {item.source_id: item for item in release.sources}
    unknown_sources = source_ids - set(source_by_id)
    if unknown_sources:
        raise ValueError("L5 scope contains an unknown L1 Source")
    data_sources = tuple(
        (
            f"{source.authority} · {source.title} · "
            f"{source.source_id} · {source.verification_status} · "
            f"{_source_boundary_label(source.usage)}"
        )
        for source in release.sources
        if source.source_id in source_ids
    )

    approval_status = pathway.approval.status.value
    return L5GovernanceView(
        pathway_code=pathway.code,
        pathway_version=pathway.version,
        knowledge_release_id=release.manifest.release_id,
        products=products,
        indications=tuple(
            INDICATION_LABELS.get(item, item) for item in pathway.indication_scope
        ),
        data_sources=data_sources,
        knowledge_status=release.manifest.status,
        pathway_status=pathway.status.value,
        review_status=(
            "未完成临床审核"
            if release.manifest.clinical_approval is None
            or approval_status != "approved"
            else "已完成临床审核"
        ),
    )


def build_l5_governance_for_patient(store, patient_id: str) -> L5GovernanceView:
    patient = store.get_patient(patient_id)
    if patient is None:
        raise ValueError("patient was not found for L5 governance view")
    sessions = store.list_care_sessions(patient_id)
    session = sessions[0] if sessions else None
    return build_l5_governance_view(
        patient.pathway_code,
        session.pathway_version if session else None,
        knowledge_release_id=session.knowledge_release_id if session else None,
    )


def build_latest_l5_submission_view(store, patient_id: str) -> L5SubmissionView | None:
    """Read the latest completed raw answers and their standardized facts."""

    patient = store.get_patient(patient_id)
    if patient is None:
        raise ValueError("patient was not found for L5 submission view")
    sessions = store.list_care_sessions(patient_id)
    session = sessions[0] if sessions else None
    pathway = load_builtin_pathways().get(
        patient.pathway_code,
        session.pathway_version if session else None,
    )
    responses = store.list_completed_questionnaire_responses(
        patient_id,
        pathway_code=pathway.code,
        pathway_version=pathway.version,
    )
    if not responses:
        return None
    response = responses[0]
    response_id = response["id"]
    observations = [
        item
        for item in store.list_final_observations(
            patient_id,
            pathway_code=pathway.code,
            pathway_version=pathway.version,
        )
        if item.message_id == response_id
    ]
    return L5SubmissionView(
        response_id=response_id,
        questionnaire=response.get("questionnaire", ""),
        authored=response.get("authored", ""),
        response_status=response.get("status", ""),
        raw_answer_rows=tuple(questionnaire_raw_answer_rows(response)),
        observation_rows=tuple(observation_trace_rows(observations)),
        response_resource=response,
        observation_resources=tuple(item.as_fhir() for item in observations),
    )


def questionnaire_raw_answer_rows(response: dict[str, Any]) -> list[dict[str, str]]:
    """Preserve each QuestionnaireResponse answer's exact FHIR value[x]."""

    rows: list[dict[str, str]] = []
    for item in _flatten_response_items(response.get("item", [])):
        for answer in item.get("answer", []):
            populated = [
                (key, value) for key, value in answer.items() if key.startswith("value")
            ]
            if len(populated) != 1:
                continue
            value_element, value = populated[0]
            rows.append(
                {
                    "linkId": item["linkId"],
                    "问题": item.get("text", item["linkId"]),
                    "FHIR value[x]": value_element,
                    "原始答案": json.dumps(value, ensure_ascii=False, sort_keys=True),
                }
            )
    return rows


def observation_trace_rows(observations: list[Observation]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in observations:
        coding = item.resource["code"]["coding"][0]
        rows.append(
            {
                "Observation": item.observation_id,
                "FHIR状态": item.resource["status"],
                "标准代码": (
                    f"{coding.get('system', '')} | {coding.get('code', '')}"
                    + (f" | {coding['version']}" if coding.get("version") else "")
                ),
                "标准化值": item.value_display,
                "metric_id": item.evidence.metric_id or "—",
                "Evidence Claim": "、".join(item.evidence.evidence_claim_ids) or "—",
                "知识Release": item.evidence.knowledge_release_id or "—",
                "原始回答来源": f"QuestionnaireResponse/{item.message_id}",
            }
        )
    return rows


def _scope_overlaps(metric_scope: list[str], pathway_scope: list[str]) -> bool:
    return not metric_scope or not pathway_scope or bool(set(metric_scope) & set(pathway_scope))


def _source_boundary_label(usage: str) -> str:
    labels = {
        "data_contract_standard": "数据契约标准，不作为中国临床依据",
        "engineering_data_collection_contract": "工程采集契约，不提供临床判断",
        "cn_product_approval_evidence": "中国产品批准范围依据",
        "cn_product_label": "中国产品说明书来源",
    }
    return labels.get(usage, f"登记用途：{usage}")


def _flatten_response_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for item in items:
        flattened.append(item)
        flattened.extend(_flatten_response_items(item.get("item", [])))
        for answer in item.get("answer", []):
            flattened.extend(_flatten_response_items(answer.get("item", [])))
    return flattened


OBSERVATION_LABELS = {
    "422587007": "报告恶心",
    "94070-0": "报告过去24小时呕吐次数",
    "75301-2": "报告过去24小时估计液体摄入量",
    "21522001": "报告腹痛",
}

OWNER_LABELS = {
    "nurse": "随访护士",
    "doctor": "医生",
    "on_call_clinician": "值班医护角色",
}

STATUS_LABELS = {
    AlertStatus.OPEN: "待处理",
    AlertStatus.ACKNOWLEDGED: "已确认收到",
    AlertStatus.ESCALATED: "已升级医生",
    AlertStatus.RESOLVED: "已完成",
}

EVENT_LABELS = {
    "demo_reset": "Demo 已重置",
    "patient_message_submitted": "患者提交院外状态",
    "care_session_started": "开始版本锁定的随访会话",
    "care_session_draft_saved": "保存患者随访草稿",
    "care_session_stopped": "停止患者随访草稿",
    "questionnaire_response_completed": "完成结构化随访问卷",
    "semantic_analysis_completed": "Care Agent 完成受控语义整理",
    "semantic_candidate_patient_decision": "患者确认或拒绝语义候选",
    "manual_review_task_created": "创建护士人工复核任务",
    "manual_review_task_acknowledged": "护士确认收到人工复核任务",
    "manual_review_task_started": "护士接受并开始人工复核",
    "manual_review_task_rejected": "护士拒绝人工复核任务",
    "manual_review_task_cancelled": "护士取消人工复核任务",
    "manual_review_outcome_recorded": "护士记录人工复核结果并生成草稿",
    "manual_review_communication_approved": "护士明确批准沟通草稿",
    "extraction_completed": "形成结构化患者报告",
    "risk_evaluated": "完成工作流规则检查",
    "risk_rule_matched": "确定性规则命中",
    "alert_created": "创建医护处理任务",
    "notification_mock_sent": "记录模拟飞书通知",
    "nurse_alert_action": "护士更新处理进展",
    "summary_generated": "生成复诊前简报",
    "manual_review_brief_generated": "生成确定性人工复核简报",
    "summary_notification_mock_sent": "记录模拟医生通知",
    "doctor_reviewed_summary": "医生完成简报审阅",
}

ACTOR_LABELS = {
    "synthetic_patient": "合成患者",
    "deterministic_care_engine": "确定性 Care Engine",
    "controlled_care_agent": "受控 Care Agent",
    "local_mock_extractor": "本地 Mock 抽取",
    "deterministic_rule_engine": "确定性规则引擎",
    "deterministic_workflow": "确定性工作流",
    "mock_notifier": "Mock 通知适配器",
    "nurse_demo_user": "演示护士",
    "synthetic_nurse_demo_user": "合成演示护士",
    "local_template_generator": "本地摘要模板",
    "doctor_demo_user": "演示医生",
    "demo_operator": "Demo 操作者",
}


def observation_text(observation: Observation) -> str:
    if observation.code == "94070-0":
        return f"报告呕吐 {observation.value} 次"
    if observation.code == "75301-2":
        return f"报告估计液体摄入 {observation.value_display}"
    return OBSERVATION_LABELS.get(
        observation.code,
        f"患者报告 {observation.code_display} = {observation.value_display}",
    )


def observation_evidence_text(observation: Observation) -> str:
    return f"{observation_text(observation)} · 原文“{observation.evidence_text}”"


def alert_status_text(alert: Alert) -> str:
    return STATUS_LABELS.get(alert.status, alert.status.value)


def owner_text(role: str) -> str:
    return OWNER_LABELS.get(role, role)


def alert_next_step(alert: Alert) -> str:
    return "责任医护需要按已批准的工作流要求查看原文证据并记录处理结果。"


def event_text(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, event_type)


def actor_text(actor_type: str) -> str:
    return ACTOR_LABELS.get(actor_type, actor_type)

"""Human-readable labels for the role-facing demo pages.

Business codes remain unchanged in persistence and audit. This module only
translates them for the presentation layer.
"""

from __future__ import annotations

from continucare.models import Alert, AlertStatus, Observation


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
    "extraction_completed": "形成结构化患者报告",
    "risk_evaluated": "完成工作流规则检查",
    "risk_rule_matched": "确定性规则命中",
    "alert_created": "创建医护处理任务",
    "notification_mock_sent": "记录模拟飞书通知",
    "nurse_alert_action": "护士更新处理进展",
    "summary_generated": "生成复诊前简报",
    "summary_notification_mock_sent": "记录模拟医生通知",
    "doctor_reviewed_summary": "医生完成简报审阅",
}

ACTOR_LABELS = {
    "synthetic_patient": "合成患者",
    "deterministic_care_engine": "确定性 Care Engine",
    "controlled_care_agent": "受控 Care Agent",
    "local_mock_extractor": "本地 Mock 抽取",
    "deterministic_rule_engine": "确定性规则引擎",
    "mock_notifier": "Mock 通知适配器",
    "nurse_demo_user": "演示护士",
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

"""Shared visual safety cues and responsive layout rules."""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse


COMPETITION_STEP_LABELS = (
    ("candidate_ready", "候选已准备"),
    ("patient_confirmed", "患者已确认"),
    ("task_requested", "任务已创建"),
    ("nurse_received", "护士已接收"),
    ("nurse_in_progress", "护士处理中"),
    ("communication_pending", "草稿待批准"),
    ("doctor_brief_pending", "pending 简报"),
    ("communication_ready", "草稿已批准"),
    ("doctor_brief_ready", "ready 简报"),
)


DEMO_GUIDE_STEPS = (
    "一句原话",
    "本人确认",
    "人工核对",
    "复诊速览",
    "完整留痕",
)


PATIENT_DECISION_ACTIONS = (
    ("accept", "对，就是这个意思"),
    ("unsure", "我还不确定"),
    ("reject", "不是这个意思"),
)


PATIENT_CONSEQUENCE = (
    "如果这一轮的内容全部选择“不是这个意思”，本轮会结束，当前不能立即重新表述。"
)


PATIENT_DECISION_BOUNDARY = (
    "确认前不会写入随访记录。"
)


PATIENT_EMERGENCY_NOTICE = (
    "这里不是急救通道。如情况紧急，请立即联系当地急救服务或前往急诊，"
    "不要在这里等待回复。"
)


NURSE_ROLE_BOUNDARY = (
    "这里核对的是患者记录，不判断风险，也不提供诊疗建议。"
)


NURSE_RESULT_BOUNDARY = (
    "这里只记录核对结果，不形成诊断、风险等级或治疗建议。"
)


NURSE_STOP_CONSEQUENCE = (
    "这会停止后续业务动作；不会生成新的沟通文字或医生速览。"
    "已有记录仍会保留供追溯。"
)


DOCTOR_ROLE_BOUNDARY = (
    "尚未提供临床评估。以下内容只整理已确认记录与护理动作。"
)


DOCTOR_DECISION_BOUNDARY = (
    "以上只调整速览的文字表达，不等于临床评估。"
)


DOCTOR_REJECT_BOUNDARY = (
    "不采用只影响这段速览文字，不改变患者确认的记录。"
)


DOCTOR_DECISION_ACTIONS = (
    ("accept", "保留这版速览"),
    ("modify", "调整速览措辞"),
    ("reject", "不采用这版速览"),
)


_NURSE_KNOWN_STAGES = {
    "not_started",
    "candidate_ready",
    "candidate_unsure",
    "candidate_rejected",
    "patient_confirmed",
    "task_requested",
    "nurse_received",
    "nurse_in_progress",
    "task_rejected",
    "task_cancelled",
    "task_failed",
    "task_entered_in_error",
    "communication_pending",
    "doctor_brief_pending",
    "communication_ready",
    "doctor_brief_ready",
    "story_complete",
}


_NURSE_STAGE_TASK_STATUS = {
    "task_requested": "requested",
    "nurse_received": "received",
    "nurse_in_progress": "in-progress",
    "task_rejected": "rejected",
    "task_cancelled": "cancelled",
    "task_failed": "failed",
    "task_entered_in_error": "entered-in-error",
    "communication_pending": "completed",
    "doctor_brief_pending": "completed",
    "communication_ready": "completed",
    "doctor_brief_ready": "completed",
    "story_complete": "completed",
}


@dataclass(frozen=True, slots=True)
class DemoGuideProjection:
    """Human-language home projection derived only from persisted progress."""

    current_step: int
    step_states: tuple[str, ...]
    current_role: str
    status_title: str
    status_detail: str
    context_lines: tuple[tuple[str, str], ...]
    previous_event: str
    next_destination: str
    next_page: str | None
    next_label: str | None
    tone: str


@dataclass(frozen=True, slots=True)
class PatientFollowupProjection:
    """Patient-language view derived from the persisted competition story."""

    state: str
    tone: str
    notice_title: str | None
    notice_detail: str | None
    original_quote: str | None
    recorded_meanings: tuple[str, ...]
    question: str | None
    consequence: str | None
    decision_actions: tuple[tuple[str, str], ...]
    boundary: str
    produced: tuple[str, ...]
    not_produced: tuple[str, ...]
    show_record_link: bool
    show_home_link: bool
    show_nurse_demo_link: bool


@dataclass(frozen=True, slots=True)
class NurseTaskProjection:
    """One persisted manual-review Task in nurse-facing language."""

    task_id: str
    submitted_at: str
    patient_label: str
    queue: str
    tone: str
    status_title: str
    status_detail: str
    original_quote: str | None
    confirmed_statement: str
    primary_action: str | None
    primary_label: str | None
    primary_writes: bool
    secondary_actions: tuple[tuple[str, str], ...]
    outcome_label: str | None
    review_note: str | None
    communication_text: str | None
    communication_marker: str | None
    stop_reason: str | None
    produced: tuple[str, ...]
    not_produced: tuple[str, ...]
    history: tuple[tuple[str, str, str], ...]
    technical_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NurseWorkbenchProjection:
    """Pure nurse workbench projection derived from already-read facts."""

    state: str
    tone: str
    notice_title: str | None
    notice_detail: str | None
    pending_tasks: tuple[NurseTaskProjection, ...]
    completed_tasks: tuple[NurseTaskProjection, ...]
    selected_task_id: str | None


@dataclass(frozen=True, slots=True)
class DoctorFactProjection:
    """One first-viewport fact whose wording is derived from persisted facts."""

    label: str
    value: str
    source_key: str | None


@dataclass(frozen=True, slots=True)
class DoctorWordingItemProjection:
    """One existing Summary item exposed as a constrained wording choice."""

    item_id: str
    section: str
    label: str
    text: str


@dataclass(frozen=True, slots=True)
class DoctorVisitBriefProjection:
    """Pure doctor-facing projection; it never owns or mutates workflow state."""

    state: str
    tone: str
    notice_title: str | None
    notice_detail: str | None
    facts: tuple[DoctorFactProjection, ...]
    summary_text: str | None
    wording_items: tuple[DoctorWordingItemProjection, ...]
    summary_id: str | None
    summary_version: str | None
    patient_quote: str | None
    nursing_detail: str | None
    previous_summary_text: str | None
    source_actions: tuple[tuple[str, str], ...]
    source_notice: str | None
    primary_action: str | None
    primary_label: str | None
    primary_task_id: str | None
    show_decisions: bool
    decision_actions: tuple[tuple[str, str], ...]
    decision_boundary: str
    reject_boundary: str
    recorded_decision: str | None
    decision_note: str | None
    show_nurse_link: bool
    show_audit_link: bool
    show_knowledge_link: bool
    produced: tuple[str, ...]
    not_produced: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuditActionProjection:
    """One persisted AuditEvent translated for progressive disclosure."""

    sequence: int
    participant: str
    action: str
    time: str
    effect: str
    before_state: str | None
    after_state: str | None
    event_type: str
    event_id: str
    entity_type: str
    entity_id: str
    resource_type: str
    resource_version: str | None
    provenance_refs: tuple[str, ...]
    details_json: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AuditTrailProjection:
    """Read-only audit explanation derived only from persisted workflow facts."""

    state: str
    tone: str
    title: str
    reason: str
    explanation: str
    produced: tuple[str, ...]
    not_produced: tuple[str, ...]
    actions: tuple[AuditActionProjection, ...]
    resource_relations: tuple[str, ...]
    show_guide_link: bool


@dataclass(frozen=True, slots=True)
class KnowledgeSourceProjection:
    """Offline source metadata; the URL is never fetched by the projection."""

    source_ref: str
    title: str
    issuing_authority: str
    document_version: str
    locators: tuple[str, ...]
    access_mode: str
    integrity: str
    license_terms: str
    url: str
    registry_status: str


@dataclass(frozen=True, slots=True)
class KnowledgeClaimProjection:
    """One exact registered Claim and its review/scope metadata."""

    claim_ref: str
    statement: str
    supports: tuple[str, ...]
    does_not_support: tuple[str, ...]
    lifecycle: str
    review_aggregate: str
    scope_json: str


@dataclass(frozen=True, slots=True)
class KnowledgeBindingProjection:
    """One exact informational-only Binding."""

    binding_ref: str
    pathway_scope: str
    purpose: str
    artifact_json: str


@dataclass(frozen=True, slots=True)
class KnowledgeGapProjection:
    """One exact CoverageGap record."""

    gap_ref: str
    reason: str
    gap_kind: str
    lifecycle: str
    pathway_scope: str


@dataclass(frozen=True, slots=True)
class KnowledgeTopicProjection:
    """One catalog-resolved topic without patient or runtime context."""

    topic_id: str
    record_version: int
    mode: str
    name: str | None
    catalog_resolved: bool
    catalog_detail: str
    catalog_system: str | None
    catalog_code: str | None
    catalog_version: str | None
    claims: tuple[KnowledgeClaimProjection, ...]
    supports: tuple[str, ...]
    does_not_support: tuple[str, ...]
    gaps: tuple[KnowledgeGapProjection, ...]
    coverage_message: str
    sources: tuple[KnowledgeSourceProjection, ...]
    bindings: tuple[KnowledgeBindingProjection, ...]
    manifest_json: str


@dataclass(frozen=True, slots=True)
class KnowledgeLibraryProjection:
    """Independent read-only library projection from one offline registry."""

    mode: str
    topics: tuple[KnowledgeTopicProjection, ...]
    selected_topic_id: str | None
    selected_topic: KnowledgeTopicProjection | None
    unbound_sources: tuple[KnowledgeSourceProjection, ...]
    independence_notice: str
    readonly_notice: str


_AUDIT_EVENT_ORDER = {
    "demo_reset": 0,
    "patient_message_submitted": 10,
    "care_session_started": 20,
    "semantic_analysis_completed": 30,
    "semantic_candidate_patient_decision": 40,
    "questionnaire_response_completed": 50,
    "manual_review_task_created": 60,
    "manual_review_task_acknowledged": 70,
    "manual_review_task_started": 80,
    "manual_review_outcome_recorded": 90,
    "manual_review_brief_generated": 100,
    "doctor_reviewed_summary": 110,
    "manual_review_communication_approved": 120,
    "manual_review_task_rejected": 130,
    "manual_review_task_cancelled": 130,
    "notification_mock_sent": 140,
    "summary_notification_mock_sent": 140,
}


def _progress_stage(progress: Any) -> str:
    value = getattr(getattr(progress, "stage", None), "value", None)
    if value is not None:
        return str(value)
    return str(getattr(progress, "stage", ""))


def _audit_task_reason(task: dict[str, Any] | None) -> str | None:
    if not task:
        return None
    notes = task.get("note", ())
    if isinstance(notes, list):
        for item in reversed(notes):
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                return str(item["text"]).strip()
    status_reason = task.get("statusReason")
    if isinstance(status_reason, dict):
        text = str(status_reason.get("text") or "").strip()
        if text:
            return text
        coding = status_reason.get("coding", ())
        if isinstance(coding, list):
            for item in coding:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("display") or item.get("code") or "").strip()
                if value:
                    return value
    return None


def _audit_action_language(event: Any) -> tuple[str, str, str, str | None, str | None]:
    event_type = str(getattr(event, "event_type", ""))
    details = getattr(event, "details_json", {}) or {}
    actor = str(getattr(event, "actor_type", ""))
    participants = {
        "synthetic_patient": "患者",
        "patient": "患者",
        "synthetic_nurse_demo_user": "护士",
        "nurse_demo_user": "护士",
        "nurse": "护士",
        "doctor_demo_user": "医生",
        "doctor": "医生",
        "mock_notifier": "模拟服务",
        "demo_operator": "演示者",
        "controlled_care_agent": "系统",
        "deterministic_care_engine": "系统",
        "deterministic_workflow": "系统",
        "local_mock_extractor": "系统",
        "local_template_generator": "系统",
    }
    participant = participants.get(actor, "记录者")
    if event_type == "demo_reset":
        return "演示者", "准备新的合成演示记录", "替换本地合成演示数据后开始这一轮。", None, "本轮已准备"
    if event_type == "patient_message_submitted":
        return "患者", "提交原话", "患者原话已保存在本轮记录中。", None, "原话已记录"
    if event_type == "care_session_started":
        return participant, "开始本轮随访记录", "建立本轮记录边界。", None, "本轮已开始"
    if event_type == "semantic_analysis_completed":
        return "系统", "生成待确认记录", "形成等待患者决定的记录；没有形成临床判断。", "原话已记录", "等待患者确认"
    if event_type == "semantic_candidate_patient_decision":
        decision = str(details.get("decision") or "")
        action = {
            "accepted": "选择确认",
            "accepted_for_manual_review": "选择确认",
            "unsure": "选择不确定",
            "rejected": "选择拒绝",
        }.get(decision, "记录患者决定")
        after = {
            "accepted": "患者已确认",
            "accepted_for_manual_review": "患者已确认",
            "unsure": "仍待患者明确决定",
            "rejected": "本轮已停止",
        }.get(decision, "决定已记录")
        return "患者", action, "只保存患者真实作出的决定。", "等待患者确认", after
    if event_type == "questionnaire_response_completed":
        return "系统", "保存患者确认记录", "确认记录及其最终来源已经保存。", "等待患者确认", "患者确认已保存"
    if event_type == "manual_review_task_created":
        return "系统", "创建例行记录核对", "把已确认记录交给护士核对；不是风险警报。", "患者确认已保存", "等待护士接手"
    if event_type == "manual_review_task_acknowledged":
        return "护士", "接手记录核对", "护士成为当前记录核对的处理者。", "等待护士接手", "护士已接手"
    if event_type == "manual_review_task_started":
        return "护士", "开始核对", "记录核对进入处理中。", "护士已接手", "正在核对"
    if event_type == "manual_review_outcome_recorded":
        return "护士", "记录核对结果", "保存受控核对结果，并形成未发送的沟通文字。", "正在核对", "沟通文字待核对"
    if event_type == "manual_review_communication_approved":
        return "护士", "核对沟通文字", "只确认文字进入后续演示流程；没有真实发送。", "沟通文字待核对", "沟通文字已核对"
    if event_type == "manual_review_brief_generated":
        return "系统", "按当前来源生成复诊速览", "从当时的持久化来源生成一个不可变版本。", None, "复诊速览已生成"
    if event_type == "doctor_reviewed_summary":
        return "医生", "记录速览措辞决定", "只记录文字表达决定；不形成临床评估。", "复诊速览已生成", "措辞决定已记录"
    if event_type == "manual_review_task_rejected":
        return "护士", "停止记录核对：未接受", "保留拒绝动作，停止后续业务动作。", "等待核对", "流程已停止"
    if event_type == "manual_review_task_cancelled":
        return "护士", "停止记录核对：已取消", "保留取消动作，停止后续业务动作。", None, "流程已停止"
    if event_type in {"notification_mock_sent", "summary_notification_mock_sent"}:
        return "模拟服务", "模拟（未真实发送 / 未写入）", "只留下 Mock 流程记录，没有外部发送或写入。", None, "模拟动作已记录"
    return participant, "记录了一项流程动作", "这项真实事件未配置第一层业务名称；完整技术名保留在技术详情。", None, None


def _audit_resource_version(details: dict[str, Any]) -> str | None:
    for key in (
        "task_version",
        "summary_version",
        "result_summary_version",
        "resource_version",
    ):
        value = details.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    for key in ("task_ref", "communication_ref", "summary_ref"):
        value = str(details.get(key) or "")
        if "/_history/" in value:
            return value.rsplit("/_history/", 1)[1]
        if ":version:" in value:
            return value.rsplit(":version:", 1)[1]
    return None


def _audit_exact_target_refs(event: Any, details: dict[str, Any]) -> tuple[str, ...]:
    """Return only persisted, exact targets for the event's primary resource."""

    entity_type = str(getattr(event, "entity_type", "") or "").strip()
    entity_id = str(getattr(event, "entity_id", "") or "").strip()

    summary_ref = str(details.get("summary_ref") or "").strip()
    if summary_ref:
        return (summary_ref,)

    if entity_type in {"Layer4SummaryDraft", "Summary"}:
        version = str(
            details.get("result_summary_version")
            or details.get("summary_version")
            or details.get("resource_version")
            or ""
        ).strip()
        if entity_id and version:
            return (f"urn:continucare:summary:{entity_id}:version:{version}",)
        return ()

    explicit_ref = ""
    if entity_type == "Task":
        explicit_ref = str(details.get("task_ref") or "").strip()
    elif entity_type == "Communication":
        explicit_ref = str(details.get("communication_ref") or "").strip()
    if explicit_ref:
        return (explicit_ref,)

    if not entity_type or not entity_id:
        return ()
    version = _audit_resource_version(details)
    if version:
        return (f"{entity_type}/{entity_id}/_history/{version}",)
    return (f"{entity_type}/{entity_id}",)


def _audit_provenance_refs(event: Any, provenances: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    details = getattr(event, "details_json", {}) or {}
    direct = str(details.get("provenance_id") or "").strip()
    if direct:
        return (f"Provenance/{direct}",)

    exact_targets = set(_audit_exact_target_refs(event, details))
    if not exact_targets:
        return ()

    refs = []
    for item in provenances:
        provenance_id = str(item.get("id") or "").strip()
        targets = {
            str(target.get("reference") or "")
            for target in item.get("target", ())
            if isinstance(target, dict)
        }
        if provenance_id and exact_targets.intersection(targets):
            refs.append(f"Provenance/{provenance_id}")
    return tuple(dict.fromkeys(refs))


def _audit_display_time(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "时间未记录"


def _audit_sort_time(value: Any) -> float:
    raw = str(value or "").strip()
    try:
        instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.timestamp()
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def _audit_products(progress: Any, *, has_events: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    produced = []
    if getattr(progress, "generation", None) or getattr(progress, "candidate_count", 0):
        produced.extend(("患者原话", "待确认记录"))
    if getattr(progress, "candidate_decisions", {}):
        produced.append("患者决定记录")
    if getattr(progress, "questionnaire_response_count", 0) or getattr(progress, "observation_count", 0):
        produced.append("患者确认记录")
    if getattr(progress, "manual_task_count", 0):
        produced.append("例行护士核对 Task 与处理历史")
    if getattr(progress, "communication_count", 0):
        produced.append(
            "未发送的沟通文字及人工核对记录"
            if getattr(progress, "communication_readiness", None) == "ready-to-send"
            else "未发送的沟通文字"
        )
    if getattr(progress, "manual_brief_count", 0):
        produced.append("按当前来源生成的复诊速览")
    if has_events or getattr(progress, "audit_count", 0):
        produced.append("本地追溯记录")

    not_produced = []
    if not (getattr(progress, "questionnaire_response_count", 0) or getattr(progress, "observation_count", 0)):
        not_produced.append("患者确认记录")
    if not getattr(progress, "manual_task_count", 0):
        not_produced.append("例行护士核对 Task")
    if not getattr(progress, "communication_count", 0):
        not_produced.append("沟通文字")
    if not getattr(progress, "manual_brief_count", 0):
        not_produced.append("复诊速览")
    not_produced.extend(("临床评估", "诊断或风险分级", "治疗建议"))
    if not getattr(progress, "alert_count", 0):
        not_produced.append("真实 Alert")
    not_produced.extend(("真实消息发送", "EMR 写回或真实外部集成"))
    return tuple(dict.fromkeys(produced)), tuple(dict.fromkeys(not_produced))


def project_audit_trail(
    progress: Any,
    *,
    events: tuple[Any, ...] = (),
    tasks: tuple[dict[str, Any], ...] = (),
    provenances: tuple[dict[str, Any], ...] = (),
) -> AuditTrailProjection:
    """Translate already-read durable facts without mutating business state."""

    stage = _progress_stage(progress)
    ordered_events = sorted(
        enumerate(events),
        key=lambda pair: (
            _audit_sort_time(getattr(pair[1], "created_at", None)),
            _AUDIT_EVENT_ORDER.get(str(getattr(pair[1], "event_type", "")), 999),
            pair[0],
        ),
    )
    actions = []
    for sequence, (_, event) in enumerate(ordered_events, start=1):
        participant, action, effect, before, after = _audit_action_language(event)
        details = dict(getattr(event, "details_json", {}) or {})
        entity_type = str(getattr(event, "entity_type", "") or "未记录")
        actions.append(
            AuditActionProjection(
                sequence=sequence,
                participant=participant,
                action=action,
                time=_audit_display_time(getattr(event, "created_at", None)),
                effect=effect,
                before_state=before,
                after_state=after,
                event_type=str(getattr(event, "event_type", "") or "未记录"),
                event_id=str(getattr(event, "event_id", "") or "未记录"),
                entity_type=entity_type,
                entity_id=str(getattr(event, "entity_id", "") or "未记录"),
                resource_type=entity_type,
                resource_version=_audit_resource_version(details),
                provenance_refs=_audit_provenance_refs(event, provenances),
                details_json=details,
            )
        )

    progress_task_id = str(getattr(progress, "task_id", None) or "")
    current_task = next(
        (
            item
            for item in tasks
            if str(item.get("id") or "") == progress_task_id
        ),
        None,
    )
    if current_task is None and not progress_task_id and len(tasks) == 1:
        current_task = tasks[0]
    expected_status = _NURSE_STAGE_TASK_STATUS.get(stage)
    task_mismatch = bool(
        expected_status
        and (
            current_task is None
            or str(current_task.get("status") or "") != expected_status
        )
    )
    produced, not_produced = _audit_products(progress, has_events=bool(events))

    human_relations = []
    if getattr(progress, "generation", None) or getattr(progress, "candidate_count", 0):
        human_relations.append("患者原话 → 待确认记录")
    if getattr(progress, "questionnaire_response_count", 0) or getattr(progress, "observation_count", 0):
        human_relations.append("患者决定 → 患者确认记录")
    if getattr(progress, "manual_task_count", 0):
        human_relations.append("患者确认记录 → 例行护士核对")
    if getattr(progress, "communication_count", 0):
        human_relations.append("护士核对结果 → 未发送的沟通文字")
    if getattr(progress, "manual_brief_count", 0):
        human_relations.append("当前来源 → 复诊速览版本")

    if getattr(progress, "integrity_issue", None) or stage not in _NURSE_KNOWN_STAGES or task_mismatch:
        reason = (
            "记录完整性检查未通过"
            if getattr(progress, "integrity_issue", None)
            else "当前 Task 状态与记录链不一致"
            if task_mismatch
            else "当前状态无法安全解释"
        )
        return AuditTrailProjection(
            state="integrity_issue",
            tone="error",
            title="记录错误：这一轮记录无法安全解释",
            reason=reason,
            explanation="已有历史记录仍会保留；页面保持只读，后续业务动作已经停止。",
            produced=produced,
            not_produced=not_produced,
            actions=tuple(actions),
            resource_relations=tuple(human_relations),
            show_guide_link=True,
        )

    states = {
        "not_started": ("neutral", "这一轮还没有留下流程记录", "这次合成演示还没有开始", "没有补造默认时间线；查看本页不会创建记录。"),
        "candidate_ready": ("active", "目前等待患者确认", "待确认记录已经形成，仍需患者明确决定", "尚未形成患者确认记录或护士任务。"),
        "candidate_unsure": ("active", "目前等待患者明确决定", "患者选择了“不太确定”", "患者仍可在患者页确认或拒绝；尚未形成患者确认记录或护士任务。"),
        "candidate_rejected": ("stopped", "本轮已结束：患者没有确认这段记录", "患者选择了“不是这个意思”", "患者原话和决定记录会保留；本轮不能立即重新表述。"),
        "patient_confirmed": ("active", "患者已确认，等待记录核对", "患者确认记录已经保存", "临床评估仍未进行。"),
        "task_requested": ("active", "目前等待护士接手", "例行记录核对已经创建", "这不是风险警报；页面查看不会接手任务。"),
        "nurse_received": ("active", "护士已接手，等待开始核对", "护士已经记录接手动作", "当前只核对记录，不判断风险。"),
        "nurse_in_progress": ("active", "护士正在核对", "记录核对已经开始", "尚未形成临床评估、治疗建议或真实发送。"),
        "communication_pending": ("active", "沟通文字仍待人工核对", "护士核对结果已经记录", "沟通文字未发送；复诊速览尚未按当前来源生成。"),
        "doctor_brief_pending": ("active", "沟通文字仍待人工核对", "复诊速览已基于当前待核对文字生成", "待核对不等于发送或临床结论。"),
        "communication_ready": ("active", "复诊速览需要按当前来源刷新", "沟通文字已经人工核对", "文字没有真实发送；旧速览继续保留但不能冒充当前版本。"),
        "doctor_brief_ready": ("active", "复诊速览已按当前来源生成", "当前来源已有对应速览版本", "这仍不代表临床评估或真实发送。"),
        "story_complete": ("complete", "演示记录链已走完", "合成演示 9/9 完成", "9/9 只代表合成本地持久化接力完成，不代表临床成功。"),
        "task_rejected": ("stopped", "流程已停止：护士未接受这项核对", _audit_task_reason(current_task) or "未记录", "已有记录继续保留；没有继续产生后续沟通文字或复诊速览。"),
        "task_cancelled": ("stopped", "流程已停止：这项核对已取消", _audit_task_reason(current_task) or "未记录", "取消前的记录继续保留；后续业务动作已经停止。"),
        "task_failed": ("error", "任务没有完成，后续流程已停止", _audit_task_reason(current_task) or "未记录", "失败前的历史记录继续保留；页面不提供重试业务动作。"),
        "task_entered_in_error": ("error", "记录错误：任务已标记为不应存在", _audit_task_reason(current_task) or "未记录", "已有历史记录会保留并标明状态；这项任务不再被当作有效业务记录，后续业务动作已经停止。"),
    }
    tone, title, reason, explanation = states[stage]
    return AuditTrailProjection(
        state=stage,
        tone=tone,
        title=title,
        reason=reason,
        explanation=explanation,
        produced=produced,
        not_produced=not_produced,
        actions=tuple(actions),
        resource_relations=tuple(human_relations),
        show_guide_link=True,
    )


def _knowledge_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _knowledge_source_url(source: Any) -> str:
    canonical = getattr(source, "canonical_url", None)
    if canonical:
        return str(canonical)
    urls = tuple(getattr(source, "access_urls", ()))
    return str(getattr(urls[0], "url", "")) if urls else ""


def _knowledge_source_projection(
    source: Any,
    *,
    integrity: str,
    locators: tuple[str, ...] = (),
) -> KnowledgeSourceProjection:
    access = getattr(source, "access", None)
    license_uri = getattr(source, "license_terms_uri", None)
    status = getattr(getattr(source, "registry_status", None), "value", None)
    return KnowledgeSourceProjection(
        source_ref=f"{getattr(source, 'source_id', 'unresolved')}@{getattr(source, 'record_version', '—')}",
        title=str(getattr(source, "title", "来源标题未登记")),
        issuing_authority=str(getattr(source, "issuing_authority", None) or "发布机构未登记"),
        document_version=str(getattr(source, "document_version", None) or "文档版本未登记"),
        locators=locators,
        access_mode=str(getattr(access, "mode", "未登记")),
        integrity=integrity,
        license_terms=str(license_uri or "许可条款未登记"),
        url=_knowledge_source_url(source),
        registry_status=str(status or getattr(source, "registry_status", "未登记")),
    )


def _knowledge_topic_projection(view: Any) -> KnowledgeTopicProjection:
    record = view.record
    resolution = view.catalog_resolution
    concept = resolution.concept if resolution.resolved else None
    claims = []
    source_locators: dict[tuple[str, int], list[str]] = {}
    for claim in view.claims:
        ref = claim.ref.key()
        review = view.review_summaries[ref]
        claims.append(
            KnowledgeClaimProjection(
                claim_ref=f"{claim.claim_id}@{claim.claim_version}",
                statement=str(claim.statement),
                supports=tuple(str(item) for item in claim.supports),
                does_not_support=tuple(str(item) for item in claim.does_not_support),
                lifecycle=str(getattr(claim.lifecycle, "value", claim.lifecycle)),
                review_aggregate=str(getattr(review.aggregate, "value", review.aggregate)),
                scope_json=_knowledge_json(claim.applicable_scope),
            )
        )
        for citation in getattr(claim, "citations", ()):
            source_locators.setdefault(citation.source.key(), []).append(
                _knowledge_json(citation.locator)
            )
    sources = tuple(
        _knowledge_source_projection(
            source,
            integrity=str(view.source_content_status[source.ref.key()]),
            locators=tuple(source_locators.get(source.ref.key(), ())),
        )
        for source in view.sources
    )
    bindings = tuple(
        KnowledgeBindingProjection(
            binding_ref=f"{item.binding_id}@{item.binding_version}",
            pathway_scope=f"{item.pathway.pathway_code} | {item.pathway.pathway_version}",
            purpose=str(getattr(item.binding_purpose, "value", item.binding_purpose)),
            artifact_json=_knowledge_json(item.artifact),
        )
        for item in view.bindings
    )
    gaps = tuple(
        KnowledgeGapProjection(
            gap_ref=f"{item.gap_id}@{item.gap_version}",
            reason=str(item.reason),
            gap_kind=str(getattr(item.gap_kind, "value", item.gap_kind)),
            lifecycle=str(item.lifecycle),
            pathway_scope=f"{item.pathway.pathway_code} | {item.pathway.pathway_version}",
        )
        for item in view.gaps
    )
    coding = getattr(concept, "coding", None)
    return KnowledgeTopicProjection(
        topic_id=str(record.symptom_index_id),
        record_version=int(record.record_version),
        mode=str(getattr(view.mode, "value", view.mode)).upper(),
        name=str(concept.preferred_zh) if concept is not None else None,
        catalog_resolved=bool(resolution.resolved),
        catalog_detail=str(resolution.detail),
        catalog_system=str(getattr(coding, "system", "")) or None,
        catalog_code=str(getattr(coding, "code", "")) or None,
        catalog_version=str(getattr(coding, "version", "")) or None,
        claims=tuple(claims),
        supports=tuple(item for claim in claims for item in claim.supports),
        does_not_support=tuple(item for claim in claims for item in claim.does_not_support),
        gaps=gaps,
        coverage_message=(
            "仍有未解决的资料缺口" if gaps else "当前未登记覆盖缺口"
        ),
        sources=sources,
        bindings=bindings,
        manifest_json=_knowledge_json(record),
    )


def project_knowledge_library(
    registry: Any,
    *,
    selected_topic_id: str | None = None,
) -> KnowledgeLibraryProjection:
    """Project an already-loaded offline registry without patient context."""

    topics = tuple(_knowledge_topic_projection(view) for view in registry.symptom_views())
    available = {item.topic_id: item for item in topics}
    selected = selected_topic_id if selected_topic_id in available else None
    if selected is None and topics:
        selected = topics[0].topic_id
    unbound = tuple(
        _knowledge_source_projection(
            source,
            integrity=str(registry.source_content_status[source.ref.key()]),
        )
        for source in registry.sources
        if source.registered_by == "Organization/continucare-m5k-link-only-registration"
    )
    mode = str(getattr(registry.mode, "value", registry.mode)).upper()
    return KnowledgeLibraryProjection(
        mode=mode,
        topics=topics,
        selected_topic_id=selected,
        selected_topic=available.get(selected),
        unbound_sources=unbound,
        independence_notice="这里只说明采集依据，没有对这位患者做过评估。",
        readonly_notice="本页只读，不读取患者故事，不创建记录，不参与本轮完成判定。",
    )


def _nurse_task_note(task: dict) -> str | None:
    notes = task.get("note", [])
    if not isinstance(notes, list):
        return None
    values = [
        str(item.get("text") or "").strip()
        for item in notes
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    return values[-1] if values else None


def _nurse_task_references(task: dict) -> tuple[str, ...]:
    references = {
        str(task.get("reasonReference", {}).get("reference") or "").strip(),
        *(
            str(item.get("valueReference", {}).get("reference") or "").strip()
            for item in task.get("input", [])
            if isinstance(item, dict)
        ),
    }
    return tuple(sorted(item for item in references if item))


def _nurse_sort_key(item: NurseTaskProjection):
    try:
        instant = datetime.fromisoformat(item.submitted_at.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        return (1, item.submitted_at, item.task_id)
    return (0, instant, item.task_id)


def _fail_closed_nurse_task(
    task: dict,
    context: dict,
    *,
    detail: str,
) -> NurseTaskProjection:
    task_id = str(task.get("id") or "unknown-task")
    return NurseTaskProjection(
        task_id=task_id,
        submitted_at=str(task.get("authoredOn") or ""),
        patient_label=str(context.get("patient_label") or "合成患者"),
        queue="completed",
        tone="error",
        status_title="这项记录暂时无法安全处理",
        status_detail=detail,
        original_quote=context.get("original_quote"),
        confirmed_statement=str(
            context.get("confirmed_statement") or "患者已确认的表述暂时无法读取"
        ),
        primary_action=None,
        primary_label=None,
        primary_writes=False,
        secondary_actions=(),
        outcome_label=context.get("outcome_label"),
        review_note=context.get("review_note"),
        communication_text=context.get("communication_text"),
        communication_marker=None,
        stop_reason=None,
        produced=(),
        not_produced=("后续业务动作",),
        history=tuple(context.get("history") or ()),
        technical_references=_nurse_task_references(task),
    )


def _project_nurse_task(
    task: dict,
    context: dict,
    *,
    story_complete: bool,
) -> NurseTaskProjection:
    task_id = str(task.get("id") or "")
    submitted_at = str(task.get("authoredOn") or "")
    status = str(task.get("status") or "")
    if (
        task.get("resourceType") != "Task"
        or not task_id
        or not submitted_at
        or not status
    ):
        return _fail_closed_nurse_task(
            task,
            context,
            detail="任务字段不完整；页面没有推测下一步，也没有继续任何写入。",
        )

    common = {
        "task_id": task_id,
        "submitted_at": submitted_at,
        "patient_label": str(context.get("patient_label") or "合成患者"),
        "original_quote": context.get("original_quote"),
        "confirmed_statement": str(
            context.get("confirmed_statement") or "患者已确认一条随访表述"
        ),
        "outcome_label": context.get("outcome_label"),
        "review_note": context.get("review_note"),
        "communication_text": context.get("communication_text"),
        "history": tuple(context.get("history") or ()),
        "technical_references": _nurse_task_references(task),
    }
    if status in {"requested", "received", "in-progress", "completed"} and (
        not context.get("original_quote")
        or not str(context.get("confirmed_statement") or "").strip()
    ):
        return _fail_closed_nurse_task(
            task,
            context,
            detail="患者确认内容或原话来源不完整；页面没有推测下一步。",
        )
    if story_complete:
        return NurseTaskProjection(
            queue="completed",
            tone="complete",
            status_title="演示记录链已走完",
            status_detail="这只表示合成接力记录已完成；不代表临床完成、治疗完成或风险解除。",
            primary_action=None,
            primary_label=None,
            primary_writes=False,
            secondary_actions=(),
            communication_marker="模拟（未真实发送）" if context.get("communication_text") else None,
            stop_reason=None,
            produced=("患者确认记录", "护士核对历史", "未发送的沟通文字", "复诊速览"),
            not_produced=("临床评估", "真实消息发送"),
            **common,
        )

    action_specs = {
        "requested": (
            "等待接手",
            "这是一条例行记录核对，按最初提交时间进入队列。",
            "acknowledge",
            "接手这项核对",
            (("cancel", "取消任务"),),
        ),
        "received": (
            "已接手",
            "下一步是开始核对患者已经确认的记录。",
            "start",
            "开始核对",
            (("reject", "拒绝处理"), ("cancel", "取消任务")),
        ),
        "in-progress": (
            "正在核对",
            "请选择一个受控结果，并留下本次处理说明。",
            "record_outcome",
            "记录结果并生成沟通文字",
            (("cancel", "取消任务"),),
        ),
    }
    if status in action_specs:
        title, detail, action, label, secondary = action_specs[status]
        return NurseTaskProjection(
            queue="pending",
            tone="active",
            status_title=title,
            status_detail=detail,
            primary_action=action,
            primary_label=label,
            primary_writes=True,
            secondary_actions=secondary,
            communication_marker=None,
            stop_reason=None,
            produced=(),
            not_produced=(),
            **common,
        )

    if status == "completed":
        readiness = context.get("communication_readiness")
        has_pending_brief = bool(context.get("has_pending_brief"))
        if readiness == "pending-approval" and not has_pending_brief:
            return NurseTaskProjection(
                queue="pending",
                tone="caution",
                status_title="沟通文字待核对",
                status_detail="需要先由医生按当前记录明确生成一版复诊速览。查看页面不会自动生成。",
                primary_action="open_doctor",
                primary_label="前往复诊速览",
                primary_writes=False,
                secondary_actions=(),
                communication_marker="模拟（未真实发送）",
                stop_reason=None,
                produced=("护士核对结果", "待人工核对的沟通文字"),
                not_produced=("按当前来源生成的复诊速览", "真实消息发送"),
                **common,
            )
        if readiness == "pending-approval" and has_pending_brief:
            return NurseTaskProjection(
                queue="pending",
                tone="caution",
                status_title="沟通文字待核对",
                status_detail="复诊速览已生成；现在只需人工核对沟通文字。",
                primary_action="approve_draft",
                primary_label="确认文字已核对",
                primary_writes=True,
                secondary_actions=(),
                communication_marker="模拟（未真实发送）",
                stop_reason=None,
                produced=("护士核对结果", "待人工核对的沟通文字", "当前版本复诊速览"),
                not_produced=("真实消息发送",),
                **common,
            )
        if readiness == "ready-to-send":
            return NurseTaskProjection(
                queue="completed",
                tone="complete",
                status_title="核对已完成",
                status_detail="沟通文字已经人工核对；本演示不会发送。",
                primary_action="open_doctor",
                primary_label="前往复诊速览",
                primary_writes=False,
                secondary_actions=(),
                communication_marker="模拟（未真实发送）",
                stop_reason=None,
                produced=("护士核对结果", "已人工核对的沟通文字"),
                not_produced=("真实消息发送",),
                **common,
            )
        return _fail_closed_nurse_task(
            task,
            context,
            detail="已完成任务与沟通文字状态不一致；页面没有推测下一步。",
        )

    if status in {"rejected", "cancelled", "failed", "entered-in-error"}:
        is_error = status in {"failed", "entered-in-error"}
        reason = _nurse_task_note(task) or "未记录"
        title = (
            "记录错误：任务已标记为不应存在"
            if status == "entered-in-error"
            else "任务没有完成，后续流程已停止"
            if status == "failed"
            else "流程已停止"
        )
        detail = (
            "已有历史记录会保留并标明状态；后续业务动作已经停止。"
            if is_error
            else "已有记录继续保留供追溯，不再提供业务动作。"
        )
        has_communication = bool(context.get("communication_text"))
        return NurseTaskProjection(
            queue="completed",
            tone="error" if is_error else "stopped",
            status_title=title,
            status_detail=detail,
            primary_action=None,
            primary_label=None,
            primary_writes=False,
            secondary_actions=(),
            communication_marker=(
                "模拟（未真实发送）" if has_communication else None
            ),
            stop_reason=reason,
            produced=("患者确认记录", "任务历史") + (("既有沟通文字",) if has_communication else ()),
            not_produced=("新的沟通文字", "新的医生速览", "真实消息发送"),
            **common,
        )

    return _fail_closed_nurse_task(
        task,
        context,
        detail="任务状态无法安全映射；页面没有推测下一步，也没有继续任何写入。",
    )


def project_nurse_workbench(
    progress,
    *,
    tasks: tuple[dict, ...] = (),
    task_contexts: dict[str, dict] | None = None,
    selected_task_id: str | None = None,
) -> NurseWorkbenchProjection:
    """Project the nurse page without caching or mutating business state."""

    contexts = task_contexts or {}
    stage = getattr(getattr(progress, "stage", None), "value", None)
    if stage is None:
        stage = str(getattr(progress, "stage", ""))
    integrity_issue = bool(getattr(progress, "integrity_issue", None))
    unknown_stage = stage not in _NURSE_KNOWN_STAGES
    story_complete = stage == "story_complete"
    progress_task_id = str(getattr(progress, "task_id", None) or "")
    expected_task_status = _NURSE_STAGE_TASK_STATUS.get(stage)

    projected = []
    has_stage_task_mismatch = False
    for task in tasks:
        task_id = str(task.get("id") or "")
        context = contexts.get(task_id, {})
        stage_task_mismatch = bool(
            progress_task_id
            and task_id == progress_task_id
            and str(task.get("status") or "") != expected_task_status
        )
        has_stage_task_mismatch = has_stage_task_mismatch or stage_task_mismatch
        if integrity_issue or unknown_stage or stage_task_mismatch:
            item = _fail_closed_nurse_task(
                task,
                context,
                detail=(
                    "这一轮记录存在完整性问题；页面没有继续任何业务动作。"
                    if integrity_issue
                    else "任务状态与当前记录链不一致；页面没有推测下一步，也没有继续任何业务动作。"
                    if stage_task_mismatch
                    else "这一轮状态无法安全映射；页面没有继续任何业务动作。"
                ),
            )
        else:
            item = _project_nurse_task(
                task,
                context,
                story_complete=story_complete,
            )
        projected.append(item)

    pending = tuple(
        sorted(
            (item for item in projected if item.queue == "pending"),
            key=_nurse_sort_key,
        )
    )
    completed = tuple(
        sorted(
            (item for item in projected if item.queue == "completed"),
            key=_nurse_sort_key,
        )
    )
    available_ids = {item.task_id for item in (*pending, *completed)}
    selected = selected_task_id if selected_task_id in available_ids else None
    if selected is None and pending:
        selected = pending[0].task_id
    if selected is None and completed:
        selected = completed[0].task_id

    if integrity_issue or unknown_stage or has_stage_task_mismatch:
        return NurseWorkbenchProjection(
            state="error",
            tone="error",
            notice_title="这一轮记录暂时无法安全读取",
            notice_detail="页面保持只读，没有推测下一步，也没有继续任何业务动作。",
            pending_tasks=pending,
            completed_tasks=completed,
            selected_task_id=selected,
        )
    if not projected:
        return NurseWorkbenchProjection(
            state="empty",
            tone="neutral",
            notice_title="目前没有待核对记录",
            notice_detail="新的例行记录核对会按最初提交时间出现在这里。",
            pending_tasks=(),
            completed_tasks=(),
            selected_task_id=None,
        )
    return NurseWorkbenchProjection(
        state="ready",
        tone="complete" if story_complete else "active",
        notice_title=None,
        notice_detail=None,
        pending_tasks=pending,
        completed_tasks=completed,
        selected_task_id=selected,
    )


def _summary_item_references(item: Any) -> tuple[str, ...]:
    references = []
    for evidence in getattr(item, "evidence_refs", ()):
        resource = getattr(evidence, "resource", None)
        reference = getattr(resource, "reference", None)
        if isinstance(reference, str) and reference:
            references.append(reference)
    return tuple(references)


def _decision_value(review_or_decision: Any) -> str | None:
    decision = getattr(review_or_decision, "decision", review_or_decision)
    value = getattr(decision, "value", decision)
    return str(value) if value is not None else None


def _clean_doctor_clause(value: str) -> str:
    return value.strip().rstrip("。；; ")


def project_doctor_summary_wording(
    summary: Any,
    *,
    confirmed_statement: str | None,
    review: Any | None = None,
    review_source_summary: Any | None = None,
) -> tuple[DoctorWordingItemProjection, ...]:
    """Project two restrained clauses from an exact persisted brief version.

    The generated M5-C Summary is intentionally technical.  This projection
    exposes only the patient-confirmed statement and the completed nursing
    action in natural Chinese.  A stored MODIFY decision may replace the text
    of one of those same Summary items; its section and evidence stay owned by
    the immutable Summary contract.
    """

    if summary is None or getattr(summary, "summary_kind", None) != "manual_review_brief":
        return ()
    statement = _clean_doctor_clause(confirmed_statement or "")
    if not statement:
        return ()
    items = tuple(getattr(summary, "items", ()))
    patient_item = next(
        (
            item
            for item in items
            if getattr(item, "section", None) == "overview"
            and any(
                reference.startswith("QuestionnaireResponse/")
                for reference in _summary_item_references(item)
            )
        ),
        None,
    )
    task_item = next(
        (
            item
            for item in items
            if getattr(item, "section", None) == "tasks_and_actions"
            and any(
                reference.startswith("Task/")
                for reference in _summary_item_references(item)
            )
        ),
        None,
    )
    if patient_item is None or task_item is None:
        return ()

    projected_text = {
        getattr(patient_item, "item_id"): f"患者表示{statement}",
        getattr(task_item, "item_id"): "护士已完成记录核对",
    }
    if _decision_value(review) == "modify" and review_source_summary is not None:
        source_items = {
            getattr(item, "item_id", ""): item
            for item in getattr(review_source_summary, "items", ())
        }
        for item in (patient_item, task_item):
            item_id = getattr(item, "item_id")
            source_item = source_items.get(item_id)
            current_text = str(getattr(item, "text", "")).strip()
            if (
                source_item is not None
                and current_text
                and current_text != str(getattr(source_item, "text", "")).strip()
            ):
                projected_text[item_id] = current_text

    return (
        DoctorWordingItemProjection(
            item_id=getattr(patient_item, "item_id"),
            section=getattr(patient_item, "section"),
            label="患者表述",
            text=projected_text[getattr(patient_item, "item_id")],
        ),
        DoctorWordingItemProjection(
            item_id=getattr(task_item, "item_id"),
            section=getattr(task_item, "section"),
            label="护理动作",
            text=projected_text[getattr(task_item, "item_id")],
        ),
    )


def doctor_summary_text(
    wording_items: tuple[DoctorWordingItemProjection, ...],
) -> str | None:
    clauses = [_clean_doctor_clause(item.text) for item in wording_items]
    if not clauses or any(not item for item in clauses):
        return None
    return f"{'；'.join(clauses)}。尚未提供临床评估。"


def build_doctor_modified_items(
    summary: Any,
    *,
    item_id: str,
    replacement: str,
    allowed_item_ids: tuple[str, ...],
) -> tuple[Any, ...]:
    """Change exactly one allowed Summary item's text and preserve its contract."""

    text = replacement.strip()
    if not text:
        raise ValueError("调整后的措辞不能为空")
    if len(text) > 3000:
        raise ValueError("调整后的措辞过长")
    if item_id not in allowed_item_ids:
        raise ValueError("只能调整当前速览中已有的可见条目")
    source_items = tuple(getattr(summary, "items", ()))
    matches = [item for item in source_items if getattr(item, "item_id", None) == item_id]
    if len(matches) != 1:
        raise ValueError("待调整条目不属于当前速览")
    if str(getattr(matches[0], "text", "")).strip() == text:
        raise ValueError("调整后的措辞必须发生变化")

    result = []
    for item in source_items:
        if getattr(item, "item_id", None) != item_id:
            result.append(item)
            continue
        payload = item.model_dump(mode="python")
        payload["text"] = text
        result.append(type(item).model_validate(payload))
    return tuple(result)


def _doctor_task_action(task: dict[str, Any] | None) -> str:
    status = str((task or {}).get("status") or "")
    return {
        "requested": "等待护士接手记录核对",
        "received": "护士已接手记录核对",
        "accepted": "护士正在核对记录",
        "in-progress": "护士正在核对记录",
        "completed": "护士已完成记录核对",
        "rejected": "护士未接受这项记录核对",
        "cancelled": "这项记录核对已取消",
        "failed": "这项记录核对没有完成",
        "entered-in-error": "这项记录核对已标记为记录错误",
    }.get(status, "尚未开始护理记录核对")


def project_doctor_visit_brief(
    progress: Any,
    *,
    tasks: tuple[dict[str, Any], ...] = (),
    summary: Any | None = None,
    confirmed_statement: str | None = None,
    original_quote: str | None = None,
    nursing_detail: str | None = None,
    stale: bool = False,
    review: Any | None = None,
    review_source_summary: Any | None = None,
    previous_summary_text: str | None = None,
    source_error: str | None = None,
    trace_degraded: bool = False,
    unresolved_references: tuple[str, ...] = (),
    trace_truncated: bool = False,
) -> DoctorVisitBriefProjection:
    """Translate persisted doctor-brief facts without creating a second state."""

    stage = getattr(getattr(progress, "stage", None), "value", None)
    if stage is None:
        stage = str(getattr(progress, "stage", ""))
    generation = getattr(progress, "generation", None)
    integrity_issue = bool(getattr(progress, "integrity_issue", None))
    task_id = str(getattr(progress, "task_id", None) or "")
    task = next((item for item in tasks if str(item.get("id") or "") == task_id), None)
    if task is None and not task_id and len(tasks) == 1:
        task = tasks[0]
        task_id = str(task.get("id") or "")
    expected_task_status = _NURSE_STAGE_TASK_STATUS.get(stage)
    task_mismatch = bool(
        expected_task_status
        and (
            task is None
            or str(task.get("status") or "") != expected_task_status
        )
    )
    unknown_stage = stage not in _NURSE_KNOWN_STAGES
    statement = _clean_doctor_clause(confirmed_statement or "") or None
    quote = original_quote.strip() if original_quote and original_quote.strip() else None
    has_patient_fact = stage not in {"not_started", "candidate_ready", "candidate_unsure", "candidate_rejected"}
    source_missing = bool(has_patient_fact and (not statement or not quote))

    patient_value = statement if has_patient_fact and statement else "尚未形成患者确认记录"
    facts = (
        DoctorFactProjection(
            label="患者确认的表述",
            value=patient_value,
            source_key="patient" if quote and has_patient_fact else None,
        ),
        DoctorFactProjection(
            label="护理动作",
            value=_doctor_task_action(task),
            source_key="nursing" if task is not None else None,
        ),
        DoctorFactProjection(
            label="当前边界",
            value="尚未提供临床评估",
            source_key=None,
        ),
    )
    source_actions = tuple(
        item
        for item in (
            ("patient", "查看患者原话") if quote else None,
            ("nursing", "查看护理动作详情") if task is not None else None,
            ("previous", "查看上一版措辞") if summary is not None else None,
            ("audit", "查看完整接力记录") if generation else None,
        )
        if item is not None
    )
    source_notice_parts = []
    if trace_degraded:
        source_notice_parts.append("部分来源暂时无法读取")
    if unresolved_references:
        source_notice_parts.append("存在尚未解析的来源")
    if trace_truncated:
        source_notice_parts.append("技术来源达到展开上限")
    source_notice = "；".join(source_notice_parts) + "。" if source_notice_parts else None

    common = {
        "facts": facts,
        "summary_id": getattr(summary, "summary_id", None),
        "summary_version": getattr(summary, "version", None),
        "patient_quote": quote,
        "nursing_detail": nursing_detail,
        "previous_summary_text": previous_summary_text,
        "source_actions": source_actions,
        "source_notice": source_notice,
        "primary_task_id": task_id or None,
        "decision_boundary": DOCTOR_DECISION_BOUNDARY,
        "reject_boundary": DOCTOR_REJECT_BOUNDARY,
        "show_audit_link": bool(generation),
        "show_knowledge_link": True,
    }

    def failed(detail: str) -> DoctorVisitBriefProjection:
        return DoctorVisitBriefProjection(
            state="error",
            tone="error",
            notice_title="这一轮记录暂时无法安全读取",
            notice_detail=detail,
            summary_text=None,
            wording_items=(),
            primary_action=None,
            primary_label=None,
            show_decisions=False,
            decision_actions=(),
            recorded_decision=None,
            decision_note=None,
            show_nurse_link=False,
            produced=(),
            not_produced=("新的复诊速览", "新的措辞决定"),
            **common,
        )

    if integrity_issue or unknown_stage or task_mismatch or source_error or source_missing:
        return failed(
            "当前步骤没有完成安全读取；原记录仍会保留，系统没有继续生成速览或保存措辞决定。"
            "请刷新，或前往记录追溯查看停止位置。"
        )

    if stage == "not_started" or not generation:
        return DoctorVisitBriefProjection(
            state="empty",
            tone="neutral",
            notice_title="还没有可生成速览的已完成记录核对。",
            notice_detail="这次合成演示还没有开始。请返回导览准备患者待确认内容。",
            summary_text=None,
            wording_items=(),
            primary_action=None,
            primary_label=None,
            show_decisions=False,
            decision_actions=(),
            recorded_decision=None,
            decision_note=None,
            show_nurse_link=False,
            produced=(),
            not_produced=("患者确认记录", "已完成的记录核对", "复诊速览"),
            **common,
        )

    terminal_specs = {
        "task_rejected": (
            "流程已停止：护士未接受这项核对",
            "患者确认和任务历史继续保留；没有产生后续沟通文字或医生速览。",
            "stopped",
        ),
        "task_cancelled": (
            "流程已停止：这项核对已取消",
            "取消前记录继续保留；没有继续后续业务动作。",
            "stopped",
        ),
        "task_failed": (
            "任务没有完成，后续流程已停止",
            "已有历史记录继续保留；系统没有继续生成沟通文字或医生速览。",
            "error",
        ),
        "task_entered_in_error": (
            "记录错误：任务已标记为不应存在",
            "历史记录会保留并标明状态；该任务不再被当作有效业务记录。",
            "error",
        ),
    }
    if stage in terminal_specs:
        title, detail, tone = terminal_specs[stage]
        reason = _nurse_task_note(task or {}) or "未记录"
        return DoctorVisitBriefProjection(
            state=stage,
            tone=tone,
            notice_title=title,
            notice_detail=f"{detail} 原因：{reason}",
            summary_text=None,
            wording_items=(),
            primary_action=None,
            primary_label=None,
            show_decisions=False,
            decision_actions=(),
            recorded_decision=None,
            decision_note=None,
            show_nurse_link=False,
            produced=("患者确认记录", "停止前的任务历史"),
            not_produced=("新的沟通文字", "新的复诊速览", "临床评估", "真实消息发送"),
            **common,
        )

    completed_task = task is not None and task.get("status") == "completed"
    if summary is None:
        if not completed_task:
            detail = (
                "当前还没有完成的护士记录核对。请先返回当前上游步骤；查看或刷新本页不会生成内容。"
            )
            return DoctorVisitBriefProjection(
                state="waiting_for_task",
                tone="neutral",
                notice_title="还没有可生成速览的已完成记录核对。",
                notice_detail=detail,
                summary_text=None,
                wording_items=(),
                primary_action=None,
                primary_label=None,
                show_decisions=False,
                decision_actions=(),
                recorded_decision=None,
                decision_note=None,
                show_nurse_link=task is not None,
                produced=("患者确认记录",) if statement else (),
                not_produced=("已完成的记录核对", "复诊速览"),
                **common,
            )
        return DoctorVisitBriefProjection(
            state="ready_to_generate",
            tone="active",
            notice_title="还没有可生成的复诊速览",
            notice_detail=(
                "已有一条完成的记录核对。生成速览是明确动作，查看或刷新页面不会自动生成。"
            ),
            summary_text=None,
            wording_items=(),
            primary_action="generate",
            primary_label="按当前记录生成速览",
            show_decisions=False,
            decision_actions=(),
            recorded_decision=None,
            decision_note=None,
            show_nurse_link=False,
            produced=("患者确认记录", "已完成的记录核对"),
            not_produced=("复诊速览", "临床评估", "真实消息发送"),
            **common,
        )

    wording_items = project_doctor_summary_wording(
        summary,
        confirmed_statement=statement,
        review=review,
        review_source_summary=review_source_summary,
    )
    summary_text = doctor_summary_text(wording_items)
    if not completed_task or summary_text is None:
        return failed(
            "当前速览与已完成记录核对的精确来源不一致；旧记录仍会保留，系统没有继续保存措辞决定。"
            "请刷新，或前往记录追溯。"
        )

    if stage == "story_complete":
        if stale:
            return failed(
                "完成状态与当前速览来源不一致；原记录仍会保留，系统没有继续任何业务写入。"
            )
        return DoctorVisitBriefProjection(
            state="story_complete",
            tone="complete",
            notice_title="演示记录链已走完",
            notice_detail=(
                "合成演示 9/9 只表示记录链已完成，不代表临床结论；没有真实发送，也没有临床评估。"
            ),
            summary_text=summary_text,
            wording_items=wording_items,
            primary_action=None,
            primary_label=None,
            show_decisions=False,
            decision_actions=(),
            recorded_decision=None,
            decision_note=None,
            show_nurse_link=False,
            produced=("患者确认记录", "护士核对历史", "未发送的沟通文字", "复诊速览"),
            not_produced=("临床评估", "诊断或风险分级", "真实消息发送", "EMR 写回"),
            **common,
        )

    if stale:
        return DoctorVisitBriefProjection(
            state="stale",
            tone="caution",
            notice_title="这版速览基于较早记录。",
            notice_detail=(
                "患者确认、护理动作或沟通文字已经变化。旧版本继续保留，但不能冒充当前版本。"
            ),
            summary_text=summary_text,
            wording_items=wording_items,
            primary_action="refresh",
            primary_label="按当前记录生成新版本",
            show_decisions=False,
            decision_actions=(),
            recorded_decision=None,
            decision_note=None,
            show_nurse_link=False,
            produced=("旧版复诊速览",),
            not_produced=("按当前记录生成的新版本",),
            **common,
        )

    status = getattr(getattr(summary, "status", None), "value", getattr(summary, "status", None))
    decision = _decision_value(review)
    decision_labels = dict(DOCTOR_DECISION_ACTIONS)
    if status == "safety_reviewed":
        pending = getattr(progress, "communication_readiness", None) == "pending-approval"
        return DoctorVisitBriefProjection(
            state="pending" if pending else "current",
            tone="caution" if pending else "active",
            notice_title="当前速览已生成" if pending else None,
            notice_detail=(
                "沟通文字仍待护士核对；这不等于已发送、可发送或临床结论。"
                if pending
                else None
            ),
            summary_text=summary_text,
            wording_items=wording_items,
            primary_action=None,
            primary_label=None,
            show_decisions=True,
            decision_actions=DOCTOR_DECISION_ACTIONS,
            recorded_decision=None,
            decision_note=None,
            show_nurse_link=pending,
            produced=("当前版本复诊速览",),
            not_produced=("临床评估", "真实消息发送", "EMR 写回"),
            **common,
        )
    if status in {"doctor_reviewed", "rejected"}:
        if decision not in decision_labels:
            return failed(
                "速览状态显示已有措辞决定，但对应的不可变决定记录无法读取；系统没有继续提供写入动作。"
            )
        rejected = status == "rejected"
        return DoctorVisitBriefProjection(
            state="rejected" if rejected else "reviewed",
            tone="stopped" if rejected else "complete",
            notice_title=(
                "未采用这版速览" if rejected else "已记录这版速览的措辞决定"
            ),
            notice_detail=(
                "原始来源和历史版本继续保留；患者确认记录没有被拒绝或删除。"
                if rejected
                else f"当前决定：{decision_labels[decision]}。未写入 EMR，也未形成临床评估。"
            ),
            summary_text=summary_text,
            wording_items=wording_items,
            primary_action=None,
            primary_label=None,
            show_decisions=False,
            decision_actions=(),
            recorded_decision=decision_labels[decision],
            decision_note=getattr(review, "note", None),
            show_nurse_link=False,
            produced=("不可变措辞决定",),
            not_produced=("临床评估", "真实消息发送", "EMR 写回"),
            **common,
        )
    return failed(
        "当前速览状态无法安全映射；原记录仍会保留，系统没有继续提供任何写入动作。"
    )


def patient_recorded_meaning(candidate) -> str:
    """Return one restrained patient-facing meaning from a real candidate.

    The projection deliberately avoids ``patient_message`` because that field can
    contain terminology and implementation language intended for the technical
    demo.  The persisted candidate still remains the sole source of the displayed
    concept, timing and polarity.
    """

    value = (
        candidate.model_dump(mode="python")
        if hasattr(candidate, "model_dump")
        else dict(candidate)
    )
    terminology_match = value.get("terminology_match") or {}
    preferred = str(terminology_match.get("preferred_zh") or "").strip()
    evidence = str(value.get("evidence_text") or "").strip()
    expression = str(
        (value.get("effective_time") or {}).get("expression") or ""
    ).strip()
    answer = value.get("answer")

    if preferred and isinstance(answer, bool):
        verb = "有" if answer else "没有"
        if expression:
            return f"{expression}{verb}{preferred}"
        return f"{verb}{preferred}"
    if preferred:
        return preferred
    if evidence:
        return evidence
    return "这段待确认内容"


def project_patient_followup(
    progress,
    *,
    original_quote: str | None = None,
    recorded_meanings: tuple[str, ...] = (),
    has_round_record: bool = False,
) -> PatientFollowupProjection:
    """Translate persisted facts into the patient page without a second state."""

    stage = getattr(progress.stage, "value", str(progress.stage))
    quote = original_quote.strip() if original_quote and original_quote.strip() else None
    meanings = tuple(item.strip() for item in recorded_meanings if item.strip())
    has_record = bool(has_round_record or progress.generation)
    read_only_boundary = "这不是诊断或风险判断，本演示不会发送消息。"

    common = {
        "original_quote": quote,
        "recorded_meanings": meanings,
        "question": None,
        "consequence": None,
        "decision_actions": (),
        "boundary": read_only_boundary,
        "produced": (),
        "not_produced": (),
        "show_record_link": has_record,
        "show_home_link": not has_record,
        "show_nurse_demo_link": False,
    }

    if progress.integrity_issue:
        return PatientFollowupProjection(
            state="error",
            tone="error",
            notice_title="这一轮记录暂时无法读取。",
            notice_detail=(
                "页面没有继续保存任何决定，原来的本地记录也没有变化。"
                "请先刷新；如仍无法读取，请返回演示首页。"
            ),
            **common,
        )

    if stage == "not_started" or not progress.generation:
        return PatientFollowupProjection(
            state="empty",
            tone="neutral",
            notice_title="目前没有需要您确认的内容",
            notice_detail="新的待确认内容会出现在这里。",
            **common,
        )

    if stage in {"candidate_ready", "candidate_unsure"}:
        unsure = stage == "candidate_unsure"
        return PatientFollowupProjection(
            state=stage,
            tone="caution" if unsure else "active",
            notice_title="这段记录还没有确认。" if unsure else None,
            notice_detail=(
                "您仍可以选择“对，就是这个意思”或“不是这个意思”。"
                "在您明确决定前，不会生成患者确认记录或护士任务。"
                if unsure
                else None
            ),
            original_quote=quote,
            recorded_meanings=meanings,
            question="这和你的意思一致吗？",
            consequence=PATIENT_CONSEQUENCE,
            decision_actions=PATIENT_DECISION_ACTIONS,
            boundary=PATIENT_DECISION_BOUNDARY,
            produced=(),
            not_produced=(),
            show_record_link=True,
            show_home_link=False,
            show_nurse_demo_link=False,
        )

    if stage == "candidate_rejected":
        return PatientFollowupProjection(
            state=stage,
            tone="stopped",
            notice_title="这一轮到这里结束。",
            notice_detail="您选择了“不是这个意思”，系统没有形成患者确认记录。",
            **{
                **common,
                "produced": ("患者原话", "本次决定记录"),
                "not_produced": (
                    "患者确认记录",
                    "护士核对任务",
                    "医生速览",
                    "任何临床评估或消息发送",
                ),
                "show_record_link": True,
                "show_home_link": False,
            },
        )

    if stage in {"patient_confirmed", "task_requested"}:
        meaning = meanings[0] if meanings else "这段表述"
        return PatientFollowupProjection(
            state="confirmed",
            tone="complete",
            notice_title="我们已经保存您的确认。",
            notice_detail=f"您确认的表述：{meaning}。下一步是由护士核对这条记录。",
            **{
                **common,
                "show_record_link": True,
                "show_home_link": False,
                "show_nurse_demo_link": True,
            },
        )

    terminal_copy = {
        "task_rejected": (
            "这条记录没有继续进入后续流程。",
            "护士没有接受这项记录核对。您已确认的内容和已有记录仍然保留。",
            "stopped",
        ),
        "task_cancelled": (
            "这条记录的后续流程已经停止。",
            "这项记录核对已取消。取消前的记录仍然保留。",
            "stopped",
        ),
        "task_failed": (
            "这条记录没有完成后续核对。",
            "任务没有完成，系统没有继续生成后续内容。已有记录仍然保留。",
            "error",
        ),
        "task_entered_in_error": (
            "这项任务已被标记为记录错误。",
            "历史记录会保留并标明状态；这项任务不会继续作为有效记录处理。",
            "error",
        ),
    }
    if stage in terminal_copy:
        title, detail, tone = terminal_copy[stage]
        return PatientFollowupProjection(
            state=stage,
            tone=tone,
            notice_title=title,
            notice_detail=detail,
            **{
                **common,
                "produced": ("患者确认记录", "停止前已经留下的记录"),
                "not_produced": (
                    "后续沟通文字",
                    "新的医生速览",
                    "临床评估或消息发送",
                ),
                "show_record_link": True,
                "show_home_link": False,
            },
        )

    if stage == "story_complete":
        return PatientFollowupProjection(
            state=stage,
            tone="complete",
            notice_title="这一轮记录已经走完。",
            notice_detail=(
                "您的确认、护士核对和复诊速览已经保留。"
                "这不代表已经形成临床评估，也没有发送消息。"
            ),
            **{
                **common,
                "show_record_link": True,
                "show_home_link": False,
            },
        )

    later_copy = {
        "nurse_received": "护士已经接手这条记录，后续只进行记录核对。",
        "nurse_in_progress": "护士正在核对这条记录；这不是风险判断。",
        "communication_pending": "核对结果已经记录，沟通文字仍待人工核对，并且没有发送。",
        "doctor_brief_pending": "复诊速览已按当前记录生成；沟通文字仍待核对，也没有发送。",
        "communication_ready": "沟通文字已经核对；本演示仍然不会发送消息。",
        "doctor_brief_ready": "复诊速览已按当前记录生成；尚未提供临床评估。",
    }
    return PatientFollowupProjection(
        state="read_only",
        tone="neutral",
        notice_title="您的确认记录正在继续处理。",
        notice_detail=later_copy.get(
            stage,
            "当前页面只显示已经留下的记录，没有继续任何患者业务动作。",
        ),
        **{
            **common,
            "show_record_link": True,
            "show_home_link": False,
        },
    )


def _linear_step_states(current_step: int) -> tuple[str, ...]:
    return tuple(
        "complete" if index < current_step else "current" if index == current_step else "upcoming"
        for index in range(1, len(DEMO_GUIDE_STEPS) + 1)
    )


def project_demo_guide(progress) -> DemoGuideProjection:
    """Translate workflow facts into the five-step presenter language.

    This function is intentionally pure: it neither reads nor writes the database,
    and it does not cache a second story state in the browser session.
    """

    if progress.integrity_issue:
        return DemoGuideProjection(
            current_step=5,
            step_states=("unavailable", "unavailable", "unavailable", "unavailable", "current"),
            current_role="演示者",
            status_title="这一轮记录暂时无法读取",
            status_detail="页面没有继续推断故事状态，也没有写入或替换原来的记录。",
            context_lines=(
                ("当前结果", "无法确认这一轮停在哪一步"),
                ("数据处理", "原来的本地记录保持不变"),
                ("当前边界", "没有继续任何角色业务动作"),
            ),
            previous_event="读取本地合成记录时发现完整性问题。",
            next_destination="打开下方“管理本地演示数据”，再明确决定是否替换本轮。",
            next_page=None,
            next_label=None,
            tone="error",
        )

    stage = getattr(progress.stage, "value", str(progress.stage))
    common_boundary = "尚未提供临床评估，也不会真实发送"
    patient_context = (
        ("患者原话", "我今天拉肚子。"),
        ("我们记成了", "今天有腹泻"),
        ("当前边界", "确认表达是否记对，不是诊断或风险判断"),
    )
    nurse_context = (
        ("患者确认的表述", "今天有腹泻"),
        ("任务类型", "例行记录核对"),
        ("当前边界", "这里只核对记录，不判断风险"),
    )
    doctor_context = (
        ("患者确认的表述", "今天有腹泻"),
        ("护理动作", "护士已完成记录核对"),
        ("当前边界", "尚未提供临床评估"),
    )

    if stage == "not_started":
        return DemoGuideProjection(
            current_step=1,
            step_states=_linear_step_states(1),
            current_role="演示者",
            status_title="这次合成演示还没有开始",
            status_detail="开始只会准备待患者确认的内容，不替任何角色作决定。",
            context_lines=(
                ("当前状态", "尚未准备本轮记录"),
                ("下一步", "准备待患者确认的合成内容"),
                ("当前边界", "不替患者、护士或医生作决定"),
            ),
            previous_event="还没有上一步；本轮尚未留下流程记录。",
            next_destination="开始一轮合成演示。",
            next_page=None,
            next_label=None,
            tone="neutral",
        )
    if stage == "candidate_ready":
        return DemoGuideProjection(
            current_step=2,
            step_states=_linear_step_states(2),
            current_role="患者",
            status_title="请确认我们记得是否准确",
            status_detail="待确认内容已经准备好；患者决定前不会创建护士任务。",
            context_lines=patient_context,
            previous_event="已经记录合成患者原话，并准备了待确认的表述。",
            next_destination="前往“我的随访”，由患者明确接受、不确定或拒绝。",
            next_page="pages/1_patient_followup.py",
            next_label="前往我的随访",
            tone="active",
        )
    if stage == "candidate_unsure":
        return DemoGuideProjection(
            current_step=2,
            step_states=_linear_step_states(2),
            current_role="患者",
            status_title="这段记录还没有确认",
            status_detail="患者仍可接受或拒绝；当前没有形成确认记录或护士任务。",
            context_lines=patient_context,
            previous_event="患者选择了“我还不确定”，故事仍停在患者确认。",
            next_destination="返回“我的随访”，由患者明确接受或拒绝。",
            next_page="pages/1_patient_followup.py",
            next_label="返回我的随访",
            tone="caution",
        )
    if stage in {"patient_confirmed", "task_requested"}:
        return DemoGuideProjection(
            current_step=3,
            step_states=_linear_step_states(3),
            current_role="护士",
            status_title="等待护士接手",
            status_detail="患者确认已保存，下一步是例行记录核对，不是风险警报。",
            context_lines=nurse_context,
            previous_event="患者已经确认表述，例行记录核对任务已经准备好。",
            next_destination="前往“随访待办”接手这项核对。",
            next_page="pages/2_nurse_risk_center.py",
            next_label="前往随访待办",
            tone="active",
        )
    if stage == "nurse_received":
        return DemoGuideProjection(
            current_step=3,
            step_states=_linear_step_states(3),
            current_role="护士",
            status_title="护士已接手",
            status_detail="这一步只核对记录，不判断风险，也不提供诊疗建议。",
            context_lines=nurse_context,
            previous_event="护士已经接手这条例行记录核对。",
            next_destination="返回“随访待办”开始核对。",
            next_page="pages/2_nurse_risk_center.py",
            next_label="继续随访核对",
            tone="active",
        )
    if stage == "nurse_in_progress":
        return DemoGuideProjection(
            current_step=3,
            step_states=_linear_step_states(3),
            current_role="护士",
            status_title="正在核对这项记录",
            status_detail="核对结果只描述记录处理，不生成诊断、风险等级或治疗建议。",
            context_lines=nurse_context,
            previous_event="护士已接手并开始核对患者确认的记录。",
            next_destination="返回“随访待办”记录核对结果。",
            next_page="pages/2_nurse_risk_center.py",
            next_label="继续随访核对",
            tone="active",
        )
    if stage == "communication_pending":
        return DemoGuideProjection(
            current_step=4,
            step_states=_linear_step_states(4),
            current_role="医生",
            status_title="核对结果已记录，沟通文字尚待确认",
            status_detail="查看页面不会自动生成速览；生成必须由明确动作触发。",
            context_lines=doctor_context,
            previous_event="护士已记录核对结果，并形成未发送的中性沟通文字。",
            next_destination="前往“复诊速览”，按当前记录明确生成速览。",
            next_page="pages/3_doctor_summary.py",
            next_label="前往复诊速览",
            tone="active",
        )
    if stage == "doctor_brief_pending":
        return DemoGuideProjection(
            current_step=4,
            step_states=_linear_step_states(4),
            current_role="护士",
            status_title="当前速览已生成，沟通文字仍待核对",
            status_detail="速览不是临床结论；沟通文字也没有发送。",
            context_lines=doctor_context,
            previous_event="医生已按当前来源生成一版速览，来源关系保持不变。",
            next_destination="返回“随访待办”核对沟通文字。",
            next_page="pages/2_nurse_risk_center.py",
            next_label="返回随访待办",
            tone="caution",
        )
    if stage == "communication_ready":
        return DemoGuideProjection(
            current_step=4,
            step_states=_linear_step_states(4),
            current_role="医生",
            status_title="沟通文字已核对",
            status_detail="人工核对只推进合成故事；本演示不会发送消息。",
            context_lines=doctor_context,
            previous_event="护士已经核对沟通文字；没有发生真实发送。",
            next_destination="前往“复诊速览”，按当前来源生成或刷新速览。",
            next_page="pages/3_doctor_summary.py",
            next_label="前往复诊速览",
            tone="active",
        )
    if stage == "doctor_brief_ready":
        return DemoGuideProjection(
            current_step=5,
            step_states=_linear_step_states(5),
            current_role="审核者",
            status_title="复诊速览已按当前来源生成",
            status_detail="下一步只回看本地记录，不继续任何角色业务动作。",
            context_lines=(
                ("当前结果", "复诊速览已按最新来源生成"),
                ("记录范围", "患者确认、护理动作与来源关系"),
                ("当前边界", common_boundary),
            ),
            previous_event="医生已按最新来源生成复诊速览。",
            next_destination="前往“记录追溯”解释本轮发生了什么。",
            next_page="pages/4_audit_log.py",
            next_label="查看记录追溯",
            tone="active",
        )

    terminal_specs = {
        "candidate_rejected": (
            ("complete", "stopped", "skipped", "skipped", "current"),
            "本轮已结束：没有形成确认记录",
            "患者明确拒绝了全部待确认内容，本轮不能立即重新表述。",
            "患者已拒绝全部待确认内容，本轮在患者确认处停止。",
            "患者原话与本次决定已保留；没有产生患者确认记录、护士任务或医生速览。",
            "stopped",
        ),
        "task_rejected": (
            ("complete", "complete", "stopped", "skipped", "current"),
            "流程已停止：护士未接受这项核对",
            "已有记录保留；没有产生后续沟通文字或医生速览。",
            "护士明确拒绝了这条例行记录核对。",
            "患者确认和任务历史已保留；后续业务动作没有继续。",
            "stopped",
        ),
        "task_cancelled": (
            ("complete", "complete", "stopped", "skipped", "current"),
            "流程已停止：这项核对已取消",
            "取消前记录保留；没有继续后续业务动作。",
            "这条例行记录核对已被明确取消。",
            "患者确认和取消前记录已保留；没有生成新的沟通文字或医生速览。",
            "stopped",
        ),
        "task_failed": (
            ("complete", "complete", "error", "skipped", "current"),
            "任务没有完成，后续流程已停止",
            "原因：未记录。页面没有推断或补造失败原因。",
            "护士核对任务以失败状态停止。",
            "已有历史记录保留；没有继续生成沟通文字或医生速览。",
            "error",
        ),
        "task_entered_in_error": (
            ("complete", "complete", "error", "skipped", "current"),
            "记录错误：任务已标记为不应存在",
            "原因：未记录。该任务不再被当作有效业务记录。",
            "任务被标记为记录错误，后续业务动作已经停止。",
            "历史记录保留并标明状态；没有继续生成后续内容。",
            "error",
        ),
    }
    if stage in terminal_specs:
        states, title, detail, previous, outcome, tone = terminal_specs[stage]
        return DemoGuideProjection(
            current_step=5,
            step_states=states,
            current_role="审核者",
            status_title=title,
            status_detail=detail,
            context_lines=(
                ("当前结果", outcome),
                ("下一步", "只读查看本轮记录"),
                ("当前边界", common_boundary),
            ),
            previous_event=previous,
            next_destination="前往“记录追溯”查看已经产生和没有产生的内容。",
            next_page="pages/4_audit_log.py",
            next_label="查看记录追溯",
            tone=tone,
        )
    if stage == "story_complete":
        return DemoGuideProjection(
            current_step=5,
            step_states=("complete", "complete", "complete", "complete", "current"),
            current_role="审核者",
            status_title="演示记录链已走完",
            status_detail="本轮只完成合成记录接力，不代表临床结论。",
            context_lines=(
                ("当前结果", "合成演示记录已完成并可追溯"),
                ("已经产生", "患者确认、护士核对、未发送文字、复诊速览"),
                ("没有产生", "临床评估、诊断、风险分级或真实发送"),
            ),
            previous_event="最新来源速览和完整本地追溯记录已经保留。",
            next_destination="前往“记录追溯”解释本轮完成与边界。",
            next_page="pages/4_audit_log.py",
            next_label="查看记录追溯",
            tone="complete",
        )

    return DemoGuideProjection(
        current_step=5,
        step_states=("unavailable", "unavailable", "unavailable", "unavailable", "current"),
        current_role="演示者",
        status_title="这一轮状态暂时无法解释",
        status_detail="页面已停止投影未知状态，没有继续任何角色业务动作。",
        context_lines=(
            ("当前结果", "状态无法安全映射到演示导览"),
            ("数据处理", "没有修改原来的本地记录"),
            ("当前边界", "没有继续业务动作或真实发送"),
        ),
        previous_event="持久化事实没有落入已知的合成演示状态。",
        next_destination="打开下方“管理本地演示数据”，再明确决定是否替换本轮。",
        next_page=None,
        next_label=None,
        tone="error",
    )


def render_demo_guide(
    st,
    progress,
    *,
    render_primary_action: Callable[[], None] | None = None,
) -> DemoGuideProjection:
    """Render the home-only A++ guide while preserving role-page contracts."""

    projection = project_demo_guide(progress)
    state_labels = {
        "complete": "完成",
        "current": "现在",
        "upcoming": "待进行",
        "stopped": "已停止",
        "skipped": "未发生",
        "error": "记录错误",
        "unavailable": "状态无法读取",
    }
    steps = []
    for index, (label, state) in enumerate(
        zip(DEMO_GUIDE_STEPS, projection.step_states), start=1
    ):
        current = ' aria-current="step"' if state == "current" else ""
        steps.append(
            f'<li class="cc-guide-step cc-guide-step--{state}"{current}>'
            f'<span class="cc-guide-index">{index}</span>'
            '<span class="cc-guide-node" aria-hidden="true"></span>'
            f'<span class="cc-guide-label">{html.escape(label)}</span>'
            f'<span class="cc-guide-state">{html.escape(state_labels[state])}</span>'
            "</li>"
        )
    context_rows = "".join(
        "<div class=\"cc-guide-fact\">"
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
        "</div>"
        for label, value in projection.context_lines
    )
    st.markdown(
        f"""
        <section class="cc-guide-stage-head">
          <span>LIVE DEMO</span>
          <h2>当前接力进度</h2>
          <p>沿着同一条合成记录，依次打开三个角色端。</p>
        </section>
        <nav class="cc-guide" aria-label="合成演示五步导览">
          <ol class="cc-guide-steps">{''.join(steps)}</ol>
        </nav>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="cc_demo_guide_layout"):
        st.markdown(
            f"""
            <article class="cc-guide-current cc-guide-current--{projection.tone}" aria-live="polite">
              <div class="cc-guide-current-head">
                <p class="cc-guide-role">当前接力 · {html.escape(projection.current_role)}</p>
                <span>{projection.current_step} / {len(DEMO_GUIDE_STEPS)}</span>
              </div>
              <h2>{html.escape(projection.status_title)}</h2>
              <p class="cc-guide-detail">{html.escape(projection.status_detail)}</p>
              <dl class="cc-guide-facts">{context_rows}</dl>
              <div class="cc-guide-meta">
                <div>
                  <h3>刚刚发生</h3>
                  <p>{html.escape(projection.previous_event)}</p>
                </div>
                <div>
                  <h3>接下来</h3>
                  <p>{html.escape(projection.next_destination)}</p>
                </div>
              </div>
            </article>
            """,
            unsafe_allow_html=True,
        )
        if projection.next_page and projection.next_label:
            with st.container(key="cc_demo_primary_action"):
                st.page_link(
                    projection.next_page,
                    label=projection.next_label,
                    width="stretch",
                )
        elif render_primary_action is not None:
            render_primary_action()
    return projection


def render_disclosure_controls(
    st,
    *,
    query_parameter: str,
    page_path: str,
    options: tuple[tuple[str, str], ...],
    aria_label: str,
    panel_id: str,
    selected: str | None = None,
    stacked: bool = False,
) -> str | None:
    """Render keyboard-native state links with explicit expanded state."""

    query_selected = st.query_params.get(query_parameter)
    if query_selected is not None:
        selected = str(query_selected)
    valid_values = {value for value, _ in options}
    if selected not in valid_values:
        selected = None
    links = []
    for value, label in options:
        active = selected == value
        href = page_path + "?" + urlencode({query_parameter: "" if active else value})
        links.append(
            f'<a class="cc-disclosure-control" target="_top" href="{html.escape(href, quote=True)}" '
            f'aria-expanded="{str(active).lower()}" aria-controls="{html.escape(panel_id, quote=True)}">'
            f"{html.escape(label)}</a>"
        )
    layout_class = " cc-disclosure-controls--stacked" if stacked else ""
    collapsed_anchor = (
        f'<span id="{html.escape(panel_id, quote=True)}" '
        'class="cc-disclosure-anchor" hidden aria-hidden="true"></span>'
        if selected is None
        else ""
    )
    st.markdown(
        f'<nav class="cc-disclosure-controls cc-disclosure-controls--{len(options)}{layout_class}" '
        f'aria-label="{html.escape(aria_label, quote=True)}">{"".join(links)}</nav>'
        f"{collapsed_anchor}",
        unsafe_allow_html=True,
    )
    return selected


def inject_global_styles(st) -> None:
    st.markdown(
        """
        <style>
        :root {
            --cc-bg: #FFFFFF;
            --cc-surface-subtle: #F7F9F9;
            --cc-text: #172126;
            --cc-muted: #5E6B70;
            --cc-border: #D6DEE0;
            --cc-accent: #006D70;
            --cc-accent-strong: #004F52;
            --cc-caution: #A15C00;
            --cc-caution-bg: #FFF7ED;
            --cc-danger: #B42318;
            --cc-danger-bg: #FFF5F4;
        }
        .block-container {max-width: 1180px; padding-top: 2rem; padding-bottom: 4rem;}
        .cc-disclosure-controls {
            display:grid; grid-template-columns:1fr; gap:.5rem; margin:.25rem 0;
        }
        .cc-disclosure-controls--2 {grid-template-columns:repeat(2, minmax(0, 1fr));}
        .cc-disclosure-controls--3 {grid-template-columns:repeat(3, minmax(0, 1fr));}
        .cc-disclosure-controls--stacked {grid-template-columns:1fr;}
        .cc-disclosure-anchor {display:none !important;}
        .cc-disclosure-control {
            display:flex; align-items:center; justify-content:center; min-height:44px;
            padding:.5rem .65rem; border:1px solid var(--cc-accent); border-radius:5px;
            background:var(--cc-bg); color:var(--cc-accent-strong) !important;
            font-size:.94rem; line-height:1.35; font-weight:630; text-align:center;
            text-decoration:none !important; box-shadow:none;
        }
        .cc-disclosure-control:hover {border-color:var(--cc-accent-strong); background:var(--cc-surface-subtle);}
        .cc-disclosure-control:focus-visible {outline:3px solid rgba(0,109,112,.28); outline-offset:2px;}
        .cc-disclosure-control[aria-expanded="true"] {
            border-color:var(--cc-accent-strong); background:var(--cc-surface-subtle); font-weight:720;
        }
        h1, h2, h3 {
            overflow-wrap: anywhere;
            word-break: break-word;
            white-space: normal !important;
            max-width: 100%;
        }
        [data-testid="stHeadingWithActionElements"] {
            white-space: normal !important;
            min-width: 0;
        }
        [data-testid="stAlert"], [data-testid="stExpander"],
        [data-testid="stChatMessage"], [data-testid="stVerticalBlockBorderWrapper"] {
            overflow-wrap: anywhere;
        }
        [data-testid="stChatMessage"] {max-width: 680px;}
        code {white-space: pre-wrap !important; overflow-wrap: anywhere;}
        .cc-mode-chip {
            display:inline-block; padding:.35rem .7rem; border-radius:999px;
            background:#ecfeff; color:#155e75; border:1px solid #a5f3fc;
            font-size:.82rem; font-weight:650; margin:0 .35rem .35rem 0;
        }
        .cc-kicker {color:#0f766e;font-size:.78rem;font-weight:750;letter-spacing:.08em;text-transform:uppercase;}
        .cc-result-title {font-size:1.18rem;font-weight:750;margin:.15rem 0 .4rem;}
        .cc-muted {color:#64748b;font-size:.88rem;}
        .cc-quote {
            padding:.85rem 1rem; border-left:4px solid #14b8a6;
            background:#f0fdfa; border-radius:0 .55rem .55rem 0;
            font-size:1.02rem; line-height:1.75;
        }
        .cc-fact {
            display:inline-block; padding:.34rem .62rem; margin:.15rem .25rem .15rem 0;
            border-radius:.45rem; background:#fff7ed; border:1px solid #fed7aa;
            color:#9a3412; font-size:.86rem; font-weight:650;
        }
        .cc-chain-step {
            padding:.7rem .85rem; margin:.35rem 0; border-radius:.55rem;
            background:#f8fafc; border:1px solid #e2e8f0;
        }
        .cc-demo-header {
            display:grid; grid-template-columns:minmax(19rem, 1.25fr) minmax(16rem, 1fr) minmax(17rem, 1fr);
            gap:1.5rem; align-items:center; padding:.2rem 0 1.1rem;
            border-bottom:1px solid var(--cc-text); color:var(--cc-text);
        }
        .stApp:has(.cc-demo-header) [data-testid="stSidebar"],
        .stApp:has(.cc-demo-header) [data-testid="stHeader"] {display:none !important;}
        .stApp:has(.cc-demo-header) [data-testid="stAppViewContainer"] {margin-left:0 !important;}
        .stApp:has(.cc-demo-header) .block-container {padding-top:1rem;}
        .cc-demo-header h1 {
            margin:0; font-size:clamp(1.65rem, 2.4vw, 2rem); line-height:1.15;
            letter-spacing:-.025em; font-weight:720; white-space:nowrap !important;
        }
        .cc-demo-header h1 span {font-weight:430;}
        .cc-demo-header p {margin:0; font-size:.93rem; line-height:1.55; color:var(--cc-text);}
        .cc-demo-boundary {font-weight:560;}
        .cc-demo-claim {
            margin:.85rem 0 .2rem; font-size:clamp(1.45rem, 2.6vw, 2rem);
            line-height:1.25; letter-spacing:-.03em; color:var(--cc-text); font-weight:680;
        }
        .cc-guide {margin:.8rem 0 .65rem;}
        .cc-guide-steps {
            position:relative; display:grid; grid-template-columns:repeat(5, minmax(0, 1fr));
            gap:1rem; margin:0; padding:0; list-style:none;
        }
        .cc-guide-steps::before {
            content:""; position:absolute; top:2.1rem; left:10%; right:10%;
            height:1px; background:var(--cc-text); z-index:0;
        }
        .cc-guide-step {
            position:relative; display:grid; grid-template-rows:1.15rem 1.05rem auto auto;
            justify-items:center; align-items:center; min-width:0; text-align:center;
            color:var(--cc-muted); z-index:1;
        }
        .cc-guide-index {font-size:1rem; line-height:1; font-weight:650; color:var(--cc-text);}
        .cc-guide-node {
            display:block; width:.78rem; height:.78rem; border-radius:50%;
            border:1.5px solid var(--cc-text); background:var(--cc-bg);
        }
        .cc-guide-label {
            margin-top:.25rem; font-size:.96rem; line-height:1.3; font-weight:640;
            color:var(--cc-text); overflow-wrap:anywhere;
        }
        .cc-guide-state {font-size:.74rem; line-height:1.3; color:var(--cc-muted);}
        .cc-guide-step--current .cc-guide-index,
        .cc-guide-step--current .cc-guide-label,
        .cc-guide-step--current .cc-guide-state {color:var(--cc-accent-strong);}
        .cc-guide-step--current .cc-guide-node {
            width:1rem; height:1rem; border-color:var(--cc-accent); background:var(--cc-accent);
        }
        .cc-guide-step--complete .cc-guide-node {border-color:var(--cc-accent);}
        .cc-guide-step--stopped .cc-guide-node {border-color:var(--cc-caution); background:var(--cc-caution-bg);}
        .cc-guide-step--stopped .cc-guide-state {color:var(--cc-caution); font-weight:650;}
        .cc-guide-step--error .cc-guide-node {border-color:var(--cc-danger); background:var(--cc-danger-bg);}
        .cc-guide-step--error .cc-guide-state {color:var(--cc-danger); font-weight:650;}
        .cc-guide-step--skipped .cc-guide-label,
        .cc-guide-step--skipped .cc-guide-index,
        .cc-guide-step--unavailable .cc-guide-label,
        .cc-guide-step--unavailable .cc-guide-index {color:var(--cc-muted);}
        .st-key-cc_demo_guide_layout {margin-top:.65rem;}
        .st-key-cc_demo_guide_layout [data-testid="stHorizontalBlock"] {gap:2rem;}
        .cc-guide-current {
            border:1px solid var(--cc-accent); border-radius:6px; padding:.85rem 1rem;
            background:var(--cc-bg); min-width:0;
        }
        .cc-guide-current--caution, .cc-guide-current--stopped {border-color:var(--cc-caution);}
        .cc-guide-current--error {border-color:var(--cc-danger); background:var(--cc-danger-bg);}
        .cc-guide-role {
            margin:0 0 .2rem; color:var(--cc-accent-strong); font-size:1.05rem;
            line-height:1.4; font-weight:720;
        }
        .cc-guide-current--caution .cc-guide-role,
        .cc-guide-current--stopped .cc-guide-role {color:var(--cc-caution);}
        .cc-guide-current--error .cc-guide-role {color:var(--cc-danger);}
        .cc-guide-current h2 {margin:.05rem 0 .25rem; font-size:1.28rem; line-height:1.3; color:var(--cc-text);}
        .cc-guide-detail {margin:0 0 .55rem; color:var(--cc-muted); line-height:1.5;}
        .cc-guide-facts {margin:0; border-top:1px solid var(--cc-border);}
        .cc-guide-fact {
            display:grid; grid-template-columns:minmax(8.5rem, .42fr) minmax(0, 1fr);
            gap:1rem; padding:.43rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-guide-fact dt {font-weight:620; color:var(--cc-text);}
        .cc-guide-fact dd {margin:0; color:var(--cc-text); overflow-wrap:anywhere;}
        .cc-guide-meta {
            display:grid; grid-template-columns:1fr 1fr; gap:1.25rem;
            margin-top:.6rem; padding-top:.55rem; border-top:1px solid var(--cc-accent);
        }
        .cc-guide-meta > div {display:grid; grid-template-columns:max-content minmax(0, 1fr); gap:.45rem;}
        .cc-guide-meta h3 {margin:0; font-size:.9rem; color:var(--cc-accent-strong);}
        .cc-guide-meta p {margin:0; color:var(--cc-text); line-height:1.45; overflow-wrap:anywhere;}
        .cc-guide-proof {border-left:1px solid var(--cc-border); padding-left:1.45rem;}
        .cc-guide-proof section + section {margin-top:1rem;}
        .cc-guide-proof h2 {
            margin:0; padding-bottom:.45rem; border-bottom:1px solid var(--cc-accent);
            color:var(--cc-accent-strong); font-size:1.2rem; line-height:1.4;
        }
        .cc-guide-proof ul {list-style:none; margin:0; padding:0;}
        .cc-guide-proof li {
            padding:.45rem .1rem; border-bottom:1px solid var(--cc-border);
            color:var(--cc-text); line-height:1.5;
        }
        .st-key-cc_demo_primary_action {margin-top:.45rem;}
        .st-key-cc_demo_primary_action a {
            min-height:3rem; display:flex; align-items:center; justify-content:center;
            border:1px solid var(--cc-accent) !important; border-radius:5px !important;
            background:var(--cc-accent) !important; color:#fff !important;
            font-size:1rem !important; font-weight:680 !important; text-decoration:none !important;
        }
        .st-key-cc_demo_primary_action a * {color:#fff !important;}
        .st-key-cc_demo_primary_action a:hover {background:var(--cc-accent-strong) !important;}
        .st-key-cc_demo_start_action button,
        .st-key-cc_demo_reset_action button {
            min-height:3rem; border:1px solid var(--cc-accent) !important;
            border-radius:5px; background:var(--cc-accent) !important; color:#fff !important;
            font-size:1rem; font-weight:680;
        }
        .st-key-cc_demo_start_action button:hover,
        .st-key-cc_demo_reset_action button:hover {background:var(--cc-accent-strong) !important;}
        .cc-negative-path {
            display:grid; grid-template-columns:auto 1fr auto; gap:1.25rem; align-items:center;
            padding:1rem 0; border-top:1px solid var(--cc-caution); border-bottom:1px solid var(--cc-caution);
            color:var(--cc-text);
        }
        .cc-negative-path strong {color:var(--cc-caution); font-size:1.05rem;}
        .cc-negative-path p {margin:0; line-height:1.6;}
        .cc-independent-knowledge {
            margin:1.5rem 0 .35rem; padding-top:1rem; border-top:1px solid var(--cc-border);
        }
        .cc-independent-knowledge h2 {margin:0 0 .25rem; font-size:1.1rem; color:var(--cc-text);}
        .cc-independent-knowledge p {margin:0; color:var(--cc-muted); line-height:1.55;}
        .cc-patient-shell {display:none;}
        .stApp:has(.cc-patient-shell) [data-testid="stSidebar"],
        .stApp:has(.cc-patient-shell) [data-testid="stHeader"] {display:none !important;}
        .stApp:has(.cc-patient-shell) [data-testid="stAppViewContainer"] {margin-left:0 !important;}
        .stApp:has(.cc-patient-shell) .block-container {
            max-width:720px; padding:1rem 1.25rem 3rem;
        }
        .stApp:has(.cc-patient-shell) h1 {
            margin:0; padding:.15rem 0 .7rem; border-bottom:1px solid var(--cc-border);
            color:var(--cc-text); font-size:1.9rem; line-height:1.25; font-weight:720;
            letter-spacing:-.025em; text-align:center;
        }
        .stApp:has(.cc-patient-shell) .st-key-cc_patient_page [data-testid="stVerticalBlock"] {
            gap:.72rem;
        }
        .cc-patient-status {
            margin:.1rem 0 .2rem; padding:.7rem .85rem; border-left:3px solid var(--cc-accent);
            background:var(--cc-surface-subtle); color:var(--cc-text);
        }
        .cc-patient-status--caution {border-left-color:var(--cc-caution); background:var(--cc-caution-bg);}
        .cc-patient-status--stopped {border-left-color:var(--cc-caution); background:var(--cc-caution-bg);}
        .cc-patient-status--error {border-left-color:var(--cc-danger); background:var(--cc-danger-bg);}
        .cc-patient-status h2 {margin:0 0 .2rem; font-size:1.28rem; line-height:1.4; color:var(--cc-text);}
        .cc-patient-status p {margin:0; font-size:1rem; line-height:1.6; color:var(--cc-text);}
        .cc-patient-quote {
            margin:.05rem 0; padding:.35rem 0 .6rem; text-align:center;
        }
        .cc-patient-label {
            display:block; margin:0 0 .2rem; color:var(--cc-muted);
            font-family:-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei",
                "Noto Sans CJK SC", sans-serif;
            font-size:.92rem; line-height:1.45; font-weight:560;
        }
        .cc-patient-quote blockquote {
            margin:0; padding:0; border:0; color:#331A1A;
            font-family:"Songti SC", STSong, "Noto Serif CJK SC", serif;
            font-size:clamp(1.9rem, 5vw, 2.35rem); line-height:1.55; font-weight:500;
            letter-spacing:.01em; overflow-wrap:anywhere;
        }
        .cc-patient-meaning {
            margin:0; padding:.55rem 0 .7rem; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border); text-align:center;
        }
        .cc-patient-meaning p {
            margin:.1rem 0 0; color:var(--cc-text); font-size:1.55rem;
            line-height:1.45; font-weight:690; overflow-wrap:anywhere;
        }
        .cc-patient-question {
            margin:.1rem 0 0; color:var(--cc-text); font-size:1.25rem;
            line-height:1.5; font-weight:690; text-align:center;
        }
        .cc-patient-consequence {
            margin:0; padding:.65rem .75rem; border:1px solid #D97706; border-radius:5px;
            background:var(--cc-caution-bg); color:#7A3E00; font-size:.96rem;
            line-height:1.58; font-weight:560;
        }
        .st-key-cc_patient_decisions,
        .st-key-cc_patient_decisions_unsure {margin:.05rem 0;}
        .st-key-cc_patient_decisions [data-testid="stVerticalBlock"],
        .st-key-cc_patient_decisions_unsure [data-testid="stVerticalBlock"] {gap:.5rem;}
        .st-key-cc_patient_decisions button,
        .st-key-cc_patient_decisions_unsure button {
            width:100%; min-height:48px !important; height:auto !important; padding:.6rem .8rem;
            border:1px solid var(--cc-accent) !important; border-radius:5px !important;
            background:var(--cc-bg) !important; color:var(--cc-accent-strong) !important;
            font-size:1rem !important; line-height:1.35 !important; font-weight:650 !important;
            box-shadow:none !important;
        }
        .st-key-cc_patient_decisions button:hover,
        .st-key-cc_patient_decisions_unsure button:hover {
            background:var(--cc-surface-subtle) !important; border-color:var(--cc-accent-strong) !important;
        }
        .stApp:has(.cc-patient-unsure) .st-key-cc_patient_decision_unsure button {
            border-color:var(--cc-caution) !important; background:var(--cc-caution-bg) !important;
            color:#7A3E00 !important;
        }
        .cc-patient-boundary {
            margin:0; padding:.65rem .75rem; border:1px solid var(--cc-border); border-radius:5px;
            background:var(--cc-surface-subtle); color:var(--cc-text);
            font-size:.95rem; line-height:1.58; text-align:center;
        }
        .cc-patient-loading {
            margin:.1rem 0; padding:.65rem .75rem; border-left:3px solid var(--cc-accent);
            background:var(--cc-surface-subtle); color:var(--cc-text); line-height:1.55;
        }
        .cc-patient-outcomes {
            display:grid; grid-template-columns:1fr 1fr; gap:1rem;
            margin:.15rem 0; padding:.75rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border);
        }
        .cc-patient-outcomes h3 {margin:0 0 .25rem; font-size:1rem; color:var(--cc-text);}
        .cc-patient-outcomes ul {margin:0; padding-left:1.15rem; color:var(--cc-text); line-height:1.6;}
        .cc-patient-fixed-note {
            margin:.15rem 0; padding:.65rem 0; color:var(--cc-text);
            border-bottom:1px solid var(--cc-border); line-height:1.6;
        }
        .cc-patient-secondary {
            margin:.1rem 0; padding-top:.25rem;
        }
        .cc-patient-secondary h2 {
            margin:0 0 .25rem; color:var(--cc-text); font-size:1rem; line-height:1.45;
        }
        .cc-patient-secondary p {margin:0; color:var(--cc-muted); font-size:.9rem; line-height:1.55;}
        .st-key-cc_patient_record_link a,
        .st-key-cc_patient_home_link a,
        .st-key-cc_patient_nurse_link a {
            min-height:2.75rem; display:flex; align-items:center; justify-content:flex-start;
            border:0 !important; border-bottom:1px solid var(--cc-border) !important;
            border-radius:0 !important; background:transparent !important;
            color:var(--cc-accent-strong) !important; font-size:.98rem !important;
            font-weight:620 !important; text-decoration:none !important;
        }
        .st-key-cc_patient_record_link a *,
        .st-key-cc_patient_home_link a *,
        .st-key-cc_patient_nurse_link a * {color:var(--cc-accent-strong) !important;}
        .cc-patient-emergency {
            margin:.2rem 0 0; padding:.55rem 0 0; border-top:1px solid var(--cc-text);
            color:var(--cc-text); font-size:.9rem; line-height:1.6;
        }
        .st-key-cc_patient_other_methods {margin-top:.55rem; padding-top:.55rem; border-top:1px solid var(--cc-border);}
        .st-key-cc_patient_other_methods [data-testid="stCheckbox"] label {
            color:var(--cc-muted); font-size:.92rem; font-weight:580;
        }
        .cc-nurse-shell {display:none;}
        .stApp:has(.cc-nurse-shell) [data-testid="stSidebar"],
        .stApp:has(.cc-nurse-shell) [data-testid="stHeader"] {display:none !important;}
        .stApp:has(.cc-nurse-shell) [data-testid="stAppViewContainer"] {margin-left:0 !important;}
        .stApp:has(.cc-nurse-shell) .block-container {
            max-width:1180px; padding:.45rem 1.25rem 3rem;
        }
        .stApp:has(.cc-nurse-shell) h1 {
            margin:0; padding:.1rem 0 .45rem; border-bottom:1px solid var(--cc-text);
            color:var(--cc-text); font-size:1.9rem; line-height:1.25; font-weight:720;
            letter-spacing:-.025em;
        }
        .cc-nurse-boundary {
            margin:.35rem 0 .65rem; padding:.4rem 0; border-bottom:1px solid var(--cc-border);
            color:var(--cc-text); font-size:.95rem; line-height:1.6;
        }
        .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort) {
            gap:0 !important; align-items:stretch;
        }
        .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort)
        > [data-testid="stColumn"]:first-child {
            padding-right:1.15rem; border-right:1px solid var(--cc-border);
        }
        .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort)
        > [data-testid="stColumn"]:last-child {
            padding-left:1.4rem; min-width:0;
        }
        .stApp:has(.cc-nurse-shell) [data-testid="stTabs"] [role="tablist"] {
            gap:1.1rem; border-bottom:1px solid var(--cc-border);
        }
        .stApp:has(.cc-nurse-shell) [data-testid="stTabs"] [role="tab"] {
            min-height:44px; padding:.45rem .05rem; color:var(--cc-muted);
            font-size:.98rem; font-weight:640;
        }
        .stApp:has(.cc-nurse-shell) [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
            color:var(--cc-accent-strong) !important;
        }
        .stApp:has(.cc-nurse-shell) [data-testid="stTabs"]
        [role="tab"][aria-selected="true"] .react-aria-SelectionIndicator {
            background:var(--cc-accent) !important;
        }
        .cc-nurse-sort {
            margin:.7rem 0 .45rem; color:var(--cc-muted); font-size:.88rem; line-height:1.5;
        }
        [class*="st-key-cc_nurse_task_"] button {
            width:100%; min-height:44px !important; height:auto !important;
            padding:.58rem .65rem !important; border:0 !important;
            border-top:1px solid var(--cc-border) !important; border-radius:0 !important;
            background:transparent !important; color:var(--cc-text) !important;
            justify-content:flex-start !important; text-align:left !important;
            font-size:.96rem !important; line-height:1.35 !important; font-weight:650 !important;
            box-shadow:none !important;
        }
        [class*="st-key-cc_nurse_task_"] button:hover {background:var(--cc-surface-subtle) !important;}
        [class*="st-key-cc_nurse_task_selected_"] button {
            background:var(--cc-surface-subtle) !important;
            border-left:3px solid var(--cc-accent) !important;
        }
        [class*="st-key-cc_nurse_disclosure_"] button {
            width:100%; min-height:44px !important; height:auto !important;
            padding:.4rem .25rem !important; border:0 !important;
            border-bottom:1px solid var(--cc-border) !important; border-radius:0 !important;
            background:transparent !important; color:var(--cc-accent-strong) !important;
            font-size:.9rem !important; line-height:1.35 !important; font-weight:620 !important;
            box-shadow:none !important;
        }
        [class*="st-key-cc_nurse_disclosure_active_"] button {
            border-bottom:2px solid var(--cc-accent) !important;
            background:var(--cc-surface-subtle) !important;
        }
        .cc-nurse-task-meta {
            margin:-.25rem 0 .45rem; padding:0 .65rem .45rem;
            color:var(--cc-muted); font-size:.8rem; line-height:1.45;
        }
        .cc-nurse-empty {
            margin:.8rem 0; padding:.85rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border); color:var(--cc-muted); line-height:1.6;
        }
        .cc-nurse-detail-head {
            display:grid; grid-template-columns:minmax(8.5rem, .34fr) minmax(0, 1fr);
            gap:1rem; padding:.55rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-nurse-detail-head dt {font-weight:620; color:var(--cc-muted);}
        .cc-nurse-detail-head dd {
            margin:0; color:var(--cc-text); font-weight:620; overflow-wrap:anywhere;
        }
        .cc-nurse-statement {
            margin:.55rem 0 0; padding:.75rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-nurse-statement span {
            display:block; margin-bottom:.18rem; color:var(--cc-muted);
            font-size:.88rem; font-weight:580;
        }
        .cc-nurse-statement strong {
            color:var(--cc-text); font-size:1.38rem; line-height:1.45; font-weight:690;
        }
        .cc-nurse-status {
            margin:.75rem 0; padding:.58rem .8rem; border-left:3px solid var(--cc-accent);
            background:var(--cc-surface-subtle); color:var(--cc-text);
        }
        .cc-nurse-status--caution, .cc-nurse-status--stopped {
            border-left-color:var(--cc-caution); background:var(--cc-caution-bg);
        }
        .cc-nurse-status--error {border-left-color:var(--cc-danger); background:var(--cc-danger-bg);}
        .cc-nurse-status h2 {margin:0 0 .15rem; font-size:1.25rem; line-height:1.4;}
        .cc-nurse-status p {margin:0; line-height:1.55;}
        .cc-nurse-action-title {
            margin:.8rem 0 .35rem; color:var(--cc-muted); font-size:.88rem; font-weight:620;
        }
        .st-key-cc_nurse_primary button,
        .st-key-cc_nurse_primary_link a {
            width:100%; min-height:48px !important; height:auto !important;
            display:flex; align-items:center; justify-content:center;
            border:1px solid var(--cc-accent) !important; border-radius:5px !important;
            background:var(--cc-accent) !important; color:#fff !important;
            font-size:1rem !important; line-height:1.35 !important; font-weight:680 !important;
            text-decoration:none !important; box-shadow:none !important;
        }
        .st-key-cc_nurse_primary button:hover,
        .st-key-cc_nurse_primary_link a:hover {background:var(--cc-accent-strong) !important;}
        .st-key-cc_nurse_primary_link a * {color:#fff !important;}
        [class*="st-key-cc_nurse_secondary_"] button {
            min-height:44px !important; height:auto !important; border:0 !important;
            border-bottom:1px solid var(--cc-border) !important; border-radius:0 !important;
            background:transparent !important; color:var(--cc-accent-strong) !important;
            font-size:.94rem !important; font-weight:620 !important; box-shadow:none !important;
        }
        .cc-nurse-result-boundary {
            margin:.6rem 0; padding:.62rem .75rem; border:1px solid var(--cc-border);
            background:var(--cc-surface-subtle); color:var(--cc-text); line-height:1.55;
        }
        .cc-nurse-communication {
            display:grid; grid-template-columns:minmax(8.5rem, .34fr) minmax(0, 1fr);
            gap:.2rem 1rem; margin:.5rem 0; padding:.55rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border);
        }
        .cc-nurse-communication h3 {grid-row:1 / span 2; margin:0; font-size:1rem; line-height:1.5;}
        .cc-nurse-communication p {margin:0; color:var(--cc-text); line-height:1.5;}
        .cc-nurse-mock {color:var(--cc-caution) !important; font-size:.88rem; font-weight:650;}
        .cc-nurse-consequence {
            margin:.7rem 0; padding:.7rem .8rem; border:1px solid var(--cc-caution);
            background:var(--cc-caution-bg); color:#7A3E00; line-height:1.6;
        }
        [class*="st-key-cc_nurse_confirm_"] button {
            min-height:46px !important; height:auto !important; border:1px solid var(--cc-caution) !important;
            background:var(--cc-caution) !important; color:#fff !important; font-weight:680 !important;
        }
        .cc-nurse-outcomes {
            display:grid; grid-template-columns:1fr 1fr; gap:1rem;
            margin:.7rem 0; padding:.75rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border);
        }
        .cc-nurse-outcomes h3 {margin:0 0 .25rem; font-size:1rem;}
        .cc-nurse-outcomes ul {margin:0; padding-left:1.15rem; line-height:1.6;}
        .cc-nurse-history {
            display:grid; grid-template-columns:4.5rem minmax(5.5rem, .35fr) minmax(0, 1fr);
            gap:.65rem; padding:.45rem 0; border-bottom:1px solid var(--cc-border);
            color:var(--cc-text); font-size:.9rem; line-height:1.5;
        }
        .cc-nurse-technical {overflow-wrap:anywhere; word-break:break-word;}
        .st-key-cc_nurse_record_link a {
            min-height:44px; display:flex; align-items:center; border:0 !important;
            border-bottom:1px solid var(--cc-border) !important; border-radius:0 !important;
            background:transparent !important; color:var(--cc-accent-strong) !important;
            font-size:.96rem !important; font-weight:620 !important; text-decoration:none !important;
        }
        .st-key-cc_nurse_record_link a * {color:var(--cc-accent-strong) !important;}
        .st-key-cc_nurse_boundary_expander {margin-top:1.25rem; border-top:1px solid var(--cc-border);}
        .cc-doctor-shell {display:none;}
        .stApp:has(.cc-doctor-shell) [data-testid="stSidebar"],
        .stApp:has(.cc-doctor-shell) [data-testid="stHeader"] {display:none !important;}
        .stApp:has(.cc-doctor-shell) [data-testid="stAppViewContainer"] {margin-left:0 !important;}
        .stApp:has(.cc-doctor-shell) .block-container {
            max-width:1180px; padding:.45rem 1.25rem 3rem;
        }
        .stApp:has(.cc-doctor-shell) h1 {
            margin:0; padding:.1rem 0 .45rem; border-bottom:1px solid var(--cc-text);
            color:var(--cc-text); font-size:1.9rem; line-height:1.25; font-weight:720;
            letter-spacing:-.025em;
        }
        .cc-doctor-boundary {
            margin:.35rem 0 .55rem; padding:.38rem 0; border-bottom:1px solid var(--cc-border);
            color:var(--cc-text); font-size:.95rem; line-height:1.55;
        }
        .cc-doctor-feedback {
            margin:.35rem 0; padding:.48rem .7rem; border-left:3px solid var(--cc-accent);
            background:var(--cc-surface-subtle); color:var(--cc-text); line-height:1.5;
        }
        .st-key-cc_doctor_workspace [data-testid="stHorizontalBlock"] {
            gap:1.35rem; align-items:stretch;
        }
        .st-key-cc_doctor_workspace [data-testid="stHorizontalBlock"]
        > [data-testid="stColumn"]:first-child {padding-right:.2rem; min-width:0;}
        .st-key-cc_doctor_workspace [data-testid="stHorizontalBlock"]
        > [data-testid="stColumn"]:last-child {
            padding-left:1rem; border-left:1px solid var(--cc-border); min-width:0;
        }
        .cc-doctor-facts {margin:.1rem 0 .5rem; border-top:1px solid var(--cc-border);}
        .cc-doctor-fact {
            display:grid; grid-template-columns:minmax(10.5rem, .34fr) minmax(0, 1fr);
            gap:1rem; padding:.48rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-doctor-fact dt {color:var(--cc-text); font-weight:620; line-height:1.45;}
        .cc-doctor-fact dd {
            margin:0; color:var(--cc-text); font-size:1.02rem; line-height:1.45;
            font-weight:620; overflow-wrap:anywhere;
        }
        .cc-doctor-fact:last-child dd {color:var(--cc-accent-strong); font-weight:720;}
        .cc-doctor-notice {
            margin:.45rem 0; padding:.55rem .75rem; border-left:3px solid var(--cc-accent);
            background:var(--cc-surface-subtle); color:var(--cc-text);
        }
        .cc-doctor-notice--caution, .cc-doctor-notice--stopped {
            border-left-color:var(--cc-caution); background:var(--cc-caution-bg);
        }
        .cc-doctor-notice--error {border-left-color:var(--cc-danger); background:var(--cc-danger-bg);}
        .cc-doctor-notice h2 {margin:0 0 .12rem; font-size:1.1rem; line-height:1.35;}
        .cc-doctor-notice p {margin:0; color:var(--cc-text); font-size:.92rem; line-height:1.5;}
        .cc-doctor-summary {
            margin:.55rem 0 .45rem; padding:.55rem 0 .65rem;
            border-top:1px solid var(--cc-accent); border-bottom:1px solid var(--cc-border);
        }
        .cc-doctor-summary h2 {margin:0 0 .28rem; font-size:1.08rem; line-height:1.4;}
        .cc-doctor-summary p {
            margin:0; color:var(--cc-text); font-size:1.08rem; line-height:1.65;
            font-weight:520; overflow-wrap:anywhere;
        }
        .cc-doctor-source-title {
            margin:.05rem 0 .35rem; padding-bottom:.42rem; border-bottom:1px solid var(--cc-accent);
            color:var(--cc-accent-strong); font-size:1rem !important; line-height:1.4 !important;
        }
        .stApp:has(.cc-doctor-shell) [class*="st-key-cc_doctor_source_"] button {
            width:100%; min-height:44px !important; height:auto !important;
            padding:.42rem .15rem !important; border:0 !important;
            border-bottom:1px solid var(--cc-border) !important; border-radius:0 !important;
            background:transparent !important; color:var(--cc-accent-strong) !important;
            justify-content:flex-start !important; text-align:left !important;
            font-size:.9rem !important; line-height:1.35 !important; font-weight:620 !important;
            box-shadow:none !important;
        }
        .stApp:has(.cc-doctor-shell) [class*="st-key-cc_doctor_source_active_"] button {
            border-bottom:2px solid var(--cc-accent) !important;
            background:var(--cc-surface-subtle) !important;
        }
        .st-key-cc_doctor_record_link a,
        .st-key-cc_doctor_nurse_link a,
        .st-key-cc_doctor_home_link a,
        .st-key-cc_doctor_knowledge_link a {
            min-height:44px; display:flex; align-items:center; border:0 !important;
            border-bottom:1px solid var(--cc-border) !important; border-radius:0 !important;
            background:transparent !important; color:var(--cc-accent-strong) !important;
            font-size:.94rem !important; line-height:1.35 !important; font-weight:620 !important;
            text-decoration:none !important;
        }
        .st-key-cc_doctor_record_link a *,
        .st-key-cc_doctor_nurse_link a *,
        .st-key-cc_doctor_home_link a *,
        .st-key-cc_doctor_knowledge_link a * {color:var(--cc-accent-strong) !important;}
        .cc-doctor-source-notice {
            margin:.55rem 0 0; color:var(--cc-caution); font-size:.82rem; line-height:1.45;
        }
        .cc-doctor-disclosure {
            margin:.7rem 0 0; padding:.6rem 0 .2rem; border-top:1px solid var(--cc-accent);
        }
        .cc-doctor-disclosure h2 {margin:0; font-size:1.08rem; line-height:1.4; color:var(--cc-text);}
        .cc-doctor-quote {
            margin:.2rem 0; padding:.45rem 0; border:0; color:#331A1A;
            font-family:"Songti SC", STSong, "Noto Serif CJK SC", serif;
            font-size:1.35rem; line-height:1.6; overflow-wrap:anywhere;
        }
        .cc-doctor-source-caption {margin:.05rem 0 .4rem; color:var(--cc-muted); font-size:.88rem;}
        .cc-doctor-source-copy {margin:.15rem 0 .5rem; color:var(--cc-text); line-height:1.65;}
        .st-key-cc_doctor_primary button,
        .st-key-cc_doctor_submit_decision button {
            width:100%; min-height:48px !important; height:auto !important;
            border:1px solid var(--cc-accent) !important; border-radius:5px !important;
            background:var(--cc-accent) !important; color:#fff !important;
            font-size:1rem !important; line-height:1.35 !important; font-weight:680 !important;
            box-shadow:none !important;
        }
        .st-key-cc_doctor_primary button:hover,
        .st-key-cc_doctor_submit_decision button:hover {background:var(--cc-accent-strong) !important;}
        .cc-doctor-decision-head {
            margin:.85rem 0 .25rem; padding-top:.65rem; border-top:1px solid var(--cc-text);
        }
        .cc-doctor-decision-head h2 {margin:0 0 .15rem; font-size:1.2rem; line-height:1.4;}
        .cc-doctor-decision-head p {margin:0; color:var(--cc-text); font-size:.92rem; line-height:1.5;}
        .stApp:has(.cc-doctor-shell) [class*="st-key-cc_doctor_decisions_"]
        [role="radiogroup"] {
            display:grid !important; grid-template-columns:repeat(3, minmax(0, 1fr));
            gap:.55rem !important;
        }
        .stApp:has(.cc-doctor-shell) [class*="st-key-cc_doctor_decisions_"]
        [role="radiogroup"] label {
            min-height:48px; margin:0 !important; padding:.55rem .7rem !important;
            border:1px solid var(--cc-accent); border-radius:5px;
            background:var(--cc-bg); color:var(--cc-accent-strong);
            align-items:center; font-size:.95rem; line-height:1.35; font-weight:620;
        }
        .cc-doctor-reject-boundary {
            margin:.3rem 0 .45rem; color:var(--cc-text); font-size:.9rem; line-height:1.5;
        }
        .cc-doctor-recorded-decision {
            margin:.8rem 0; padding:.65rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border);
        }
        .cc-doctor-recorded-decision h2 {margin:0 0 .2rem; font-size:1.1rem;}
        .cc-doctor-recorded-decision p {margin:.12rem 0; color:var(--cc-text); line-height:1.55;}
        .cc-doctor-outcomes {
            display:grid; grid-template-columns:1fr 1fr; gap:1rem;
            margin:.7rem 0; padding:.7rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border);
        }
        .cc-doctor-outcomes h3 {margin:0 0 .25rem; font-size:1rem;}
        .cc-doctor-outcomes ul {margin:0; padding-left:1.15rem; line-height:1.55;}
        .cc-doctor-knowledge {
            margin:1.2rem 0 0; padding-top:.7rem; border-top:1px solid var(--cc-border);
        }
        .cc-doctor-knowledge h2 {margin:0 0 .15rem; font-size:1rem;}
        .cc-doctor-knowledge p {margin:0; color:var(--cc-muted); font-size:.88rem; line-height:1.5;}
        .cc-audit-shell {display:none;}
        .stApp:has(.cc-audit-shell) [data-testid="stSidebar"],
        .stApp:has(.cc-audit-shell) [data-testid="stHeader"] {display:none !important;}
        .stApp:has(.cc-audit-shell) [data-testid="stAppViewContainer"] {margin-left:0 !important;}
        .stApp:has(.cc-audit-shell) .block-container {
            max-width:1180px; padding:.45rem 1.25rem 3rem;
        }
        .stApp:has(.cc-audit-shell) h1 {
            margin:0; padding:.1rem 0 .45rem; border-bottom:1px solid var(--cc-text);
            color:var(--cc-text); font-size:1.9rem; line-height:1.25; font-weight:720;
            letter-spacing:-.025em;
        }
        .cc-audit-boundary {
            margin:.35rem 0 .7rem; padding:.4rem 0; border-bottom:1px solid var(--cc-border);
            color:var(--cc-text); font-size:.94rem; line-height:1.55;
        }
        .st-key-cc_audit_page [data-testid="stVerticalBlock"] {gap:.65rem;}
        .cc-audit-conclusion {
            margin:0; padding:.8rem 1rem; border:1px solid var(--cc-accent);
            border-left:4px solid var(--cc-accent); background:var(--cc-bg); color:var(--cc-text);
        }
        .cc-audit-conclusion--stopped {border-color:var(--cc-caution); background:var(--cc-caution-bg);}
        .cc-audit-conclusion--error {border-color:var(--cc-danger); background:var(--cc-danger-bg);}
        .cc-audit-label {
            margin:0 0 .12rem !important; color:var(--cc-muted) !important;
            font-size:.82rem !important; font-weight:650; line-height:1.4 !important;
        }
        .cc-audit-conclusion h2 {
            margin:0 0 .45rem; color:var(--cc-text); font-size:1.45rem; line-height:1.35;
        }
        .cc-audit-conclusion--stopped h2 {color:#7A3E00;}
        .cc-audit-conclusion--error h2 {color:var(--cc-danger);}
        .cc-audit-conclusion > p:last-child {margin:.45rem 0 0; line-height:1.6;}
        .cc-audit-reason {
            display:grid; grid-template-columns:8rem minmax(0, 1fr); gap:.8rem;
            padding:.48rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border); line-height:1.5;
        }
        .cc-audit-reason span {color:var(--cc-muted); font-weight:600;}
        .cc-audit-reason strong {font-weight:650; overflow-wrap:anywhere;}
        .cc-audit-products {
            display:grid; grid-template-columns:1fr 1fr; gap:1.5rem;
            margin:.15rem 0; padding:.75rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-audit-products h2 {
            margin:0 0 .35rem; padding-bottom:.35rem; border-bottom:1px solid var(--cc-border);
            color:var(--cc-text); font-size:1.05rem; line-height:1.4;
        }
        .cc-audit-products ul {margin:0; padding-left:1.1rem; color:var(--cc-text); line-height:1.65;}
        .stApp:has(.cc-audit-shell) h2 {
            color:var(--cc-text); font-size:1.15rem; line-height:1.4;
        }
        .cc-audit-empty {
            margin:0; padding:.8rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border); color:var(--cc-muted); line-height:1.6;
        }
        .cc-audit-table-wrap {width:100%; overflow:visible;}
        .cc-audit-table {width:100%; border-collapse:collapse; color:var(--cc-text); table-layout:fixed;}
        .cc-audit-table th, .cc-audit-table td {
            padding:.52rem .55rem; border-bottom:1px solid var(--cc-border);
            text-align:left; vertical-align:top; line-height:1.5; overflow-wrap:anywhere;
        }
        .cc-audit-table th {color:var(--cc-muted); font-size:.84rem; font-weight:650;}
        .cc-audit-table th:first-child, .cc-audit-table td:first-child {width:4rem;}
        .cc-audit-table th:nth-child(2), .cc-audit-table td:nth-child(2) {width:7rem;}
        .cc-audit-table th:last-child, .cc-audit-table td:last-child {width:12.5rem;}
        .cc-audit-disclosure-intro {margin:.35rem 0 0; color:var(--cc-muted); font-size:.88rem; line-height:1.5;}
        .stApp:has(.cc-audit-shell) [class*="st-key-cc_audit_disclosure"] button {
            width:100%; min-height:44px !important; height:auto !important;
            padding:.5rem .65rem !important; border:1px solid var(--cc-accent) !important;
            border-radius:5px !important; background:var(--cc-bg) !important;
            color:var(--cc-accent-strong) !important; font-size:.94rem !important;
            line-height:1.35 !important; font-weight:630 !important; box-shadow:none !important;
        }
        .stApp:has(.cc-audit-shell) [class*="st-key-cc_audit_disclosure_active"] button {
            border-color:var(--cc-accent-strong) !important;
            background:var(--cc-surface-subtle) !important; font-weight:720 !important;
        }
        .cc-audit-disclosure {
            margin:.4rem 0; padding:.75rem 0; border-top:1px solid var(--cc-accent);
            border-bottom:1px solid var(--cc-border); color:var(--cc-text);
        }
        .cc-audit-disclosure h2 {margin:0 0 .35rem; color:var(--cc-accent-strong) !important;}
        .cc-audit-disclosure p {margin:.2rem 0; line-height:1.6;}
        .cc-audit-effects, .cc-audit-relations {margin:.55rem 0 0; padding:0; list-style:none;}
        .cc-audit-effects li {
            display:grid; grid-template-columns:minmax(13rem, .42fr) minmax(0, 1fr);
            gap:.15rem 1rem; padding:.5rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-audit-effects span {line-height:1.55;}
        .cc-audit-effects small {grid-column:2; color:var(--cc-muted); line-height:1.45;}
        .cc-audit-relations li {padding:.52rem 0; border-bottom:1px solid var(--cc-border); line-height:1.55;}
        .cc-audit-technical {margin:.5rem 0; border-top:1px solid var(--cc-border);}
        .cc-audit-technical div {
            display:grid; grid-template-columns:8.5rem minmax(0, 1fr); gap:.8rem;
            padding:.45rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-audit-technical dt {color:var(--cc-muted); font-weight:620;}
        .cc-audit-technical dd {margin:0; min-width:0; overflow-wrap:anywhere;}
        .cc-audit-fixed-boundary {
            margin:1rem 0 .3rem; padding:.65rem 0; border-top:1px solid var(--cc-text);
            border-bottom:1px solid var(--cc-border); color:var(--cc-text); line-height:1.6;
        }
        .st-key-cc_audit_guide_link a {
            min-height:44px; display:flex; align-items:center; border:0 !important;
            border-bottom:1px solid var(--cc-border) !important; border-radius:0 !important;
            background:transparent !important; color:var(--cc-accent-strong) !important;
            font-size:.96rem !important; font-weight:620 !important; text-decoration:none !important;
        }
        .st-key-cc_audit_guide_link a * {color:var(--cc-accent-strong) !important;}
        .cc-knowledge-shell {display:none;}
        .stApp:has(.cc-knowledge-shell) [data-testid="stSidebar"],
        .stApp:has(.cc-knowledge-shell) [data-testid="stHeader"] {display:none !important;}
        .stApp:has(.cc-knowledge-shell) [data-testid="stAppViewContainer"] {margin-left:0 !important;}
        .stApp:has(.cc-knowledge-shell) .block-container {
            max-width:960px; padding:.45rem 1.25rem 3rem;
        }
        .stApp:has(.cc-knowledge-shell) h1 {
            margin:0; padding:.1rem 0 .15rem; color:var(--cc-text);
            font-size:1.9rem; line-height:1.25; font-weight:720; letter-spacing:-.025em;
        }
        .cc-knowledge-subtitle {
            margin:0 0 .55rem; padding-bottom:.5rem; border-bottom:1px solid var(--cc-text);
            color:var(--cc-accent-strong); font-size:1.08rem; line-height:1.5; font-weight:620;
        }
        .cc-knowledge-notices {
            margin:.15rem 0 .35rem; padding:.7rem .85rem; border-left:4px solid #4B5F76;
            background:var(--cc-surface-subtle); color:var(--cc-text);
        }
        .cc-knowledge-notices strong {display:block; font-size:1.05rem; line-height:1.5;}
        .cc-knowledge-notices p {margin:.2rem 0 0; color:var(--cc-text); line-height:1.55;}
        .cc-knowledge-topic-note {margin:.15rem 0 .55rem; color:var(--cc-muted); font-size:.88rem; line-height:1.5;}
        .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_topic [role="radiogroup"] {
            display:grid !important; grid-template-columns:repeat(4, minmax(0, 1fr));
            gap:.55rem !important;
        }
        .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_topic [role="radiogroup"] label {
            min-height:48px; margin:0 !important; padding:.55rem .65rem !important;
            border:1px solid #738397; border-radius:5px; background:var(--cc-bg);
            color:var(--cc-text); align-items:center; font-size:.96rem; line-height:1.35;
            font-weight:620;
        }
        .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_topic [role="radiogroup"] label:has(input:checked) {
            border-color:var(--cc-accent); background:var(--cc-surface-subtle);
            color:var(--cc-accent-strong); font-weight:720;
        }
        .cc-knowledge-topic-head {
            display:flex; align-items:baseline; justify-content:space-between; gap:1rem;
            margin:.7rem 0 .2rem; padding:.55rem 0; border-top:1px solid var(--cc-border);
            border-bottom:1px solid var(--cc-border);
        }
        .cc-knowledge-topic-head h2 {margin:0; color:var(--cc-text); font-size:1.45rem; line-height:1.35;}
        .cc-knowledge-topic-head p {margin:0; color:var(--cc-muted); font-size:.88rem; line-height:1.45;}
        .cc-knowledge-rationale {margin:.55rem 0; padding:.35rem 0 .55rem;}
        .cc-knowledge-rationale h2,
        .cc-knowledge-scope h2,
        .cc-knowledge-coverage h2,
        .cc-knowledge-details-head h2 {
            margin:0 0 .3rem; color:var(--cc-text); font-size:1.05rem; line-height:1.4;
        }
        .cc-knowledge-rationale p {margin:.25rem 0; color:var(--cc-text); line-height:1.65;}
        .cc-knowledge-scope {display:grid; grid-template-columns:1fr 1fr; gap:1.25rem; margin:.45rem 0;}
        .cc-knowledge-supports, .cc-knowledge-limitations {padding:.7rem .8rem; border:1px solid var(--cc-border);}
        .cc-knowledge-supports {border-left:4px solid #4B5F76; background:var(--cc-surface-subtle);}
        .cc-knowledge-limitations {border-left:4px solid var(--cc-caution); background:var(--cc-caution-bg);}
        .cc-knowledge-scope ul {margin:0; padding-left:1.1rem; color:var(--cc-text); line-height:1.65;}
        .cc-knowledge-coverage {
            margin:.7rem 0 .35rem; padding:.65rem .8rem; border-left:4px solid var(--cc-caution);
            background:var(--cc-caution-bg); color:var(--cc-text);
        }
        .cc-knowledge-coverage--none {border-left-color:#4B5F76; background:var(--cc-surface-subtle);}
        .cc-knowledge-coverage strong {display:block; line-height:1.5;}
        .cc-knowledge-coverage p {margin:.2rem 0 0; line-height:1.55;}
        .cc-knowledge-coverage ul {margin:.25rem 0 0; padding-left:1.1rem; line-height:1.6;}
        .cc-knowledge-unassessed {
            margin:.2rem 0 .7rem; padding:.45rem 0; border-bottom:1px solid var(--cc-border);
            color:var(--cc-text); font-size:.92rem; line-height:1.55;
        }
        .cc-knowledge-unresolved, .cc-knowledge-no-claim, .cc-knowledge-load-error {
            margin:.55rem 0; padding:.7rem .8rem; border-left:4px solid var(--cc-caution);
            background:var(--cc-caution-bg); color:var(--cc-text);
        }
        .cc-knowledge-load-error {border-left-color:var(--cc-danger); background:var(--cc-danger-bg);}
        .cc-knowledge-unresolved h2, .cc-knowledge-no-claim h2, .cc-knowledge-load-error h2 {
            margin:0 0 .2rem; font-size:1.08rem; line-height:1.4;
        }
        .cc-knowledge-unresolved p, .cc-knowledge-no-claim p, .cc-knowledge-load-error p {
            margin:0; line-height:1.55;
        }
        .stApp:has(.cc-knowledge-shell) [class*="st-key-cc_knowledge_details"] button {
            width:100%; min-height:46px !important; height:auto !important;
            border:1px solid #4B5F76 !important; border-radius:5px !important;
            background:var(--cc-bg) !important; color:#31445A !important;
            font-size:.96rem !important; line-height:1.35 !important; font-weight:650 !important;
            box-shadow:none !important;
        }
        .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_details_active button {
            background:var(--cc-surface-subtle) !important; font-weight:720 !important;
        }
        .cc-knowledge-details-head {
            margin:.7rem 0 .45rem; padding:.65rem 0 .35rem; border-top:1px solid #4B5F76;
        }
        .cc-knowledge-details-head p {margin:0; color:var(--cc-muted); line-height:1.5;}
        .cc-knowledge-source {
            margin:.55rem 0; padding:.7rem .8rem; border:1px solid #AAB5C2;
            border-left:4px solid #4B5F76; background:#FBFCFD; color:var(--cc-text);
        }
        .cc-knowledge-source h3 {margin:0 0 .4rem; color:#273B52; font-size:1.02rem; line-height:1.4;}
        .cc-knowledge-source dl {margin:0;}
        .cc-knowledge-source dl div {
            display:grid; grid-template-columns:7.5rem minmax(0, 1fr); gap:.7rem;
            padding:.28rem 0; border-bottom:1px solid #DDE3E9;
        }
        .cc-knowledge-source dt {color:var(--cc-muted); font-weight:620;}
        .cc-knowledge-source dd {margin:0; overflow-wrap:anywhere;}
        .cc-knowledge-locator {margin:.45rem 0; color:var(--cc-text);}
        .cc-knowledge-locator strong {display:block; margin-bottom:.2rem;}
        .cc-knowledge-locator pre {
            margin:.25rem 0; padding:.45rem .55rem; white-space:pre-wrap; overflow-wrap:anywhere;
            border:1px solid #DDE3E9; background:#fff; color:var(--cc-text); font-size:.78rem;
        }
        .cc-knowledge-source-link {
            min-height:44px; display:inline-flex; align-items:center; color:#31445A;
            font-weight:650; text-decoration:underline; text-underline-offset:3px;
        }
        .cc-knowledge-history {list-style:none; margin:.4rem 0; padding:0;}
        .cc-knowledge-history li {
            display:flex; justify-content:space-between; gap:1rem; padding:.45rem 0;
            border-bottom:1px solid var(--cc-border); line-height:1.5;
        }
        .cc-knowledge-history span {color:var(--cc-muted); text-align:right;}
        .cc-knowledge-fixed-boundary {
            margin:1rem 0 .3rem; padding:.7rem 0; border-top:1px solid var(--cc-text);
            border-bottom:1px solid var(--cc-border); color:var(--cc-text); line-height:1.65;
        }
        .st-key-cc_knowledge_home_link a {
            min-height:44px; display:flex; align-items:center; border:0 !important;
            border-bottom:1px solid var(--cc-border) !important; border-radius:0 !important;
            background:transparent !important; color:#31445A !important;
            font-size:.96rem !important; font-weight:620 !important; text-decoration:none !important;
        }
        .st-key-cc_knowledge_home_link a * {color:#31445A !important;}
        :where(a, button, input, select, textarea, [tabindex]):focus-visible {
            outline:3px solid color-mix(in srgb, var(--cc-accent) 45%, white) !important;
            outline-offset:3px !important;
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                scroll-behavior:auto !important; animation-duration:.01ms !important;
                animation-iteration-count:1 !important; transition-duration:.01ms !important;
            }
        }
        @media (max-width: 768px) {
            .block-container {padding: 1rem .85rem 3rem;}
            h1 {font-size: 1.75rem !important; line-height: 1.25 !important;}
            h2 {font-size: 1.35rem !important; line-height: 1.3 !important;}
            h3 {font-size: 1.12rem !important; line-height: 1.38 !important;}
            [data-testid="stHorizontalBlock"] {gap: .75rem;}
            [data-testid="stMetric"] {min-width: 0;}
            [data-testid="stButton"] button {min-height: 2.75rem;}
            [data-testid="stChatMessage"] {max-width: 100%;}
            .cc-demo-header {grid-template-columns:1fr; gap:.4rem; padding-bottom:.65rem;}
            .cc-demo-header h1 {font-size:1.45rem; white-space:nowrap !important;}
            .cc-demo-header p {font-size:.82rem; line-height:1.45;}
            .cc-demo-claim {margin-top:.65rem; font-size:1.28rem;}
            .cc-guide {margin-top:1rem;}
            .cc-guide-steps {grid-template-columns:repeat(5, minmax(0, 1fr)); gap:.1rem;}
            .cc-guide-steps::before {
                top:2rem; bottom:auto; left:10%; right:10%; width:auto; height:1px;
            }
            .cc-guide-step {
                grid-template-columns:1fr; grid-template-rows:1rem 1rem auto auto;
                gap:.05rem; justify-items:center; text-align:center;
            }
            .cc-guide-node {justify-self:center;}
            .cc-guide-label {margin:.2rem 0 0; font-size:.78rem; line-height:1.25;}
            .cc-guide-state {text-align:center; font-size:.65rem; line-height:1.2;}
            .st-key-cc_demo_guide_layout [data-testid="stHorizontalBlock"] {flex-direction:column; gap:1rem;}
            .st-key-cc_demo_guide_layout [data-testid="stColumn"] {
                width:100% !important; flex:1 1 auto !important; min-width:0 !important;
            }
            .cc-guide-current {padding:.75rem;}
            .cc-guide-current h2 {font-size:1.2rem;}
            .cc-guide-fact {
                grid-template-columns:minmax(7rem, .42fr) minmax(0, 1fr);
                gap:.55rem; padding:.34rem 0;
            }
            .cc-guide-meta {grid-template-columns:1fr; gap:.9rem;}
            .cc-guide-meta > div {grid-template-columns:max-content minmax(0, 1fr); gap:.45rem;}
            .cc-guide-proof {border-left:0; border-top:1px solid var(--cc-border); padding:1rem 0 0;}
            .cc-negative-path {grid-template-columns:1fr; gap:.45rem;}
            .stApp:has(.cc-patient-shell) .block-container {padding:.35rem 1rem 2.5rem;}
            .stApp:has(.cc-patient-shell) h1 {
                padding:.1rem 0 .5rem; font-size:1.65rem !important; line-height:1.25 !important;
            }
            .stApp:has(.cc-patient-shell) .st-key-cc_patient_page [data-testid="stVerticalBlock"] {
                gap:.55rem;
            }
            .cc-patient-status {padding:.55rem .7rem;}
            .cc-patient-status h2 {font-size:1.12rem !important;}
            .cc-patient-status p {font-size:.94rem; line-height:1.52;}
            .cc-patient-quote {padding:.05rem 0 .2rem;}
            .cc-patient-quote blockquote {font-size:1.8rem; line-height:1.4;}
            .cc-patient-meaning {padding:.25rem 0 .35rem;}
            .cc-patient-meaning p {font-size:1.35rem; line-height:1.4;}
            .cc-patient-question {font-size:1.12rem; line-height:1.42;}
            .cc-patient-consequence {padding:.42rem .65rem; font-size:.88rem; line-height:1.42;}
            .cc-patient-boundary {padding:.45rem .65rem; font-size:.88rem; line-height:1.42;}
            .cc-patient-outcomes {grid-template-columns:1fr; gap:.65rem;}
            .stApp:has(.cc-nurse-shell) .block-container {padding:.35rem 1rem 2.5rem;}
            .stApp:has(.cc-nurse-shell) h1 {
                padding:.1rem 0 .5rem; font-size:1.65rem !important; line-height:1.25 !important;
            }
            .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort) {
                flex-direction:column; gap:.55rem !important;
            }
            .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort)
            > [data-testid="stColumn"] {
                width:100% !important; flex:1 1 auto !important; min-width:0 !important;
                padding-left:0 !important; padding-right:0 !important; border-right:0 !important;
            }
            .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort)
            > [data-testid="stColumn"]:first-child {
                padding-bottom:.45rem; border-bottom:1px solid var(--cc-border);
            }
            .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:not(:has(.cc-nurse-sort)) {
                flex-direction:row !important; gap:.35rem !important;
            }
            .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:not(:has(.cc-nurse-sort))
            > [data-testid="stColumn"] {
                width:auto !important; flex:1 1 0 !important; min-width:0 !important;
                padding-left:0 !important; padding-right:0 !important; border:0 !important;
            }
            .cc-nurse-detail-head {
                grid-template-columns:7.4rem minmax(0, 1fr); gap:.45rem; padding:.4rem 0;
            }
            .cc-nurse-statement {margin:.35rem 0 0; padding:.5rem 0;}
            .cc-nurse-statement strong {font-size:1.25rem;}
            .cc-nurse-status {margin:.4rem 0; padding:.5rem .65rem;}
            .cc-nurse-status h2 {font-size:1.15rem !important;}
            .cc-nurse-action-title {margin:.45rem 0 .25rem;}
            .cc-nurse-communication {grid-template-columns:1fr; gap:.3rem;}
            .cc-nurse-communication h3 {grid-row:auto;}
            .cc-nurse-outcomes {grid-template-columns:1fr; gap:.65rem;}
            .cc-nurse-history {grid-template-columns:4rem minmax(0, 1fr);}
            .cc-nurse-history span:last-child {grid-column:1 / -1;}
            .stApp:has(.cc-doctor-shell) .block-container {padding:.35rem 1rem 2.5rem;}
            .stApp:has(.cc-doctor-shell) h1 {
                padding:.1rem 0 .5rem; font-size:1.65rem !important; line-height:1.25 !important;
            }
            .st-key-cc_doctor_workspace [data-testid="stHorizontalBlock"] {
                flex-direction:column; gap:.55rem;
            }
            .st-key-cc_doctor_workspace [data-testid="stHorizontalBlock"]
            > [data-testid="stColumn"] {
                width:100% !important; flex:1 1 auto !important; min-width:0 !important;
                padding-left:0 !important; padding-right:0 !important; border-left:0 !important;
            }
            .st-key-cc_doctor_workspace [data-testid="stHorizontalBlock"]
            > [data-testid="stColumn"]:last-child {
                margin-top:.25rem; padding-top:.55rem !important; border-top:1px solid var(--cc-border);
            }
            .cc-doctor-fact {grid-template-columns:1fr; gap:.08rem; padding:.42rem 0;}
            .cc-doctor-fact dt {color:var(--cc-muted); font-size:.86rem;}
            .cc-doctor-fact dd {font-size:1rem;}
            .cc-doctor-notice {margin:.35rem 0; padding:.48rem .65rem;}
            .cc-doctor-summary {margin:.4rem 0; padding:.45rem 0 .55rem;}
            .cc-doctor-summary p {font-size:1rem; line-height:1.58;}
            .stApp:has(.cc-doctor-shell) [class*="st-key-cc_doctor_decisions_"]
            [role="radiogroup"] {grid-template-columns:1fr; gap:.45rem !important;}
            .cc-doctor-outcomes {grid-template-columns:1fr; gap:.65rem;}
            .stApp:has(.cc-audit-shell) .block-container,
            .stApp:has(.cc-knowledge-shell) .block-container {padding:.35rem 1rem 2.5rem;}
            .stApp:has(.cc-audit-shell) h1,
            .stApp:has(.cc-knowledge-shell) h1 {
                padding:.1rem 0 .35rem; font-size:1.65rem !important; line-height:1.25 !important;
            }
            .cc-audit-boundary {font-size:.86rem; line-height:1.48;}
            .cc-audit-conclusion {padding:.65rem .75rem;}
            .cc-audit-conclusion h2 {font-size:1.2rem !important;}
            .cc-audit-reason {grid-template-columns:1fr; gap:.1rem;}
            .cc-audit-products {grid-template-columns:1fr; gap:.8rem;}
            .cc-audit-table thead {display:none;}
            .cc-audit-table, .cc-audit-table tbody, .cc-audit-table tr, .cc-audit-table td {
                display:block; width:100% !important;
            }
            .cc-audit-table tr {padding:.45rem 0; border-bottom:1px solid var(--cc-border);}
            .cc-audit-table td {
                display:grid; grid-template-columns:5rem minmax(0, 1fr); gap:.5rem;
                padding:.18rem 0; border:0; line-height:1.45;
            }
            .cc-audit-table td::before {
                content:attr(data-label); color:var(--cc-muted); font-size:.82rem; font-weight:620;
            }
            .stApp:has(.cc-audit-shell) [data-testid="stHorizontalBlock"] {
                flex-direction:column; gap:.4rem;
            }
            .stApp:has(.cc-audit-shell) [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                width:100% !important; flex:1 1 auto !important; min-width:0 !important;
            }
            .cc-audit-effects li {grid-template-columns:1fr; gap:.1rem;}
            .cc-audit-effects small {grid-column:1;}
            .cc-audit-technical div {grid-template-columns:1fr; gap:.08rem;}
            .cc-knowledge-subtitle {font-size:1rem;}
            .cc-knowledge-notices {padding:.6rem .7rem;}
            .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_topic [role="radiogroup"] {
                grid-template-columns:repeat(2, minmax(0, 1fr)); gap:.45rem !important;
            }
            .cc-knowledge-topic-head {display:block;}
            .cc-knowledge-topic-head h2 {font-size:1.25rem !important;}
            .cc-knowledge-topic-head p {margin-top:.2rem;}
            .cc-knowledge-scope {grid-template-columns:1fr; gap:.65rem;}
            .cc-knowledge-source dl div {grid-template-columns:1fr; gap:.08rem;}
            .cc-knowledge-history li {display:block;}
            .cc-knowledge-history span {display:block; margin-top:.1rem; text-align:left;}
        }
        /* Competition product-demo refresh: keep business state, replace the
           former verification-console presentation with role-first surfaces. */
        :root {
            --cc-bg:#FFFFFF;
            --cc-canvas:#F5F8F7;
            --cc-surface-subtle:#EFF6F5;
            --cc-surface-aqua:#E4F2F1;
            --cc-text:#132326;
            --cc-muted:#617073;
            --cc-border:#D9E4E2;
            --cc-accent:#007C7A;
            --cc-accent-strong:#005C5A;
            --cc-caution:#A56612;
            --cc-caution-bg:#FFF7E8;
            --cc-danger:#B42318;
            --cc-danger-bg:#FFF3F1;
            --cc-shadow:0 18px 48px rgba(21,55,57,.08);
        }
        html, body, [class*="css"] {
            font-family:Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
                "SF Pro Display", "PingFang SC", "Microsoft YaHei", sans-serif;
        }
        .stApp {background:var(--cc-canvas); color:var(--cc-text);}
        .block-container {max-width:1280px; padding:1rem 1.5rem 4rem;}
        .cc-role-topbar {
            display:flex; align-items:center; justify-content:space-between; gap:1rem;
            min-height:64px; margin-bottom:1.35rem; padding-bottom:1rem;
            border-bottom:1px solid var(--cc-border);
        }
        .cc-role-topbar-brand {
            display:flex; align-items:center; gap:.65rem; color:var(--cc-text);
            font-size:1.05rem; font-weight:760; letter-spacing:-.02em;
        }
        .cc-role-topbar-brand i,
        .cc-demo-brand-mark {
            width:34px; height:34px; display:inline-flex; align-items:center; justify-content:center;
            border-radius:10px; background:var(--cc-accent); color:#fff;
            font-style:normal; font-size:1rem; font-weight:800;
        }
        .cc-role-topbar-badge,
        .cc-demo-badge {
            display:inline-flex; align-items:center; min-height:30px; padding:.25rem .65rem;
            border:1px solid #BBDAD7; border-radius:999px; background:#F1FAF9;
            color:var(--cc-accent-strong); font-size:.78rem; font-weight:680;
        }
        .cc-role-footer-boundary {
            margin-top:1.5rem; padding:1rem 0; border-top:1px solid var(--cc-border);
            color:var(--cc-muted); font-size:.8rem; line-height:1.55; text-align:center;
        }

        /* Demo home */
        .stApp:has(.cc-demo-header) .block-container {max-width:1280px; padding-top:.65rem;}
        .cc-demo-header {
            display:flex; align-items:center; justify-content:space-between; gap:1rem;
            min-height:64px; padding:.45rem 0 1rem; border:0;
            border-bottom:1px solid var(--cc-border);
        }
        .cc-demo-brand {display:flex; align-items:center; gap:.7rem;}
        .cc-demo-brand strong {font-size:1.22rem; letter-spacing:-.025em;}
        .cc-demo-nav-label {color:var(--cc-muted); font-size:.86rem; font-weight:620;}
        .cc-demo-hero {
            display:grid; grid-template-columns:minmax(0, 1.03fr) minmax(31rem, .97fr);
            gap:4rem; align-items:center; min-height:480px; padding:4.6rem 0 4rem;
        }
        .cc-demo-eyebrow,
        .cc-guide-stage-head > span,
        .cc-role-section-head > p {
            margin:0 0 .75rem; color:var(--cc-accent); font-size:.72rem;
            line-height:1.3; font-weight:780; letter-spacing:.15em;
        }
        .cc-demo-hero h1 {
            max-width:760px; margin:0; color:var(--cc-text);
            font-size:clamp(2.9rem, 5.4vw, 5rem); line-height:1.08;
            letter-spacing:-.065em; font-weight:790;
        }
        .cc-demo-lead {
            max-width:690px; margin:1.5rem 0 0; color:var(--cc-muted);
            font-size:1.12rem; line-height:1.85;
        }
        .cc-demo-chain {
            padding:2rem; border:1px solid var(--cc-border); border-radius:22px;
            background:var(--cc-bg); box-shadow:var(--cc-shadow);
        }
        .cc-demo-chain-roles {
            display:grid; grid-template-columns:1fr auto 1fr auto 1fr;
            gap:.7rem; align-items:center;
        }
        .cc-demo-chain-roles > div {min-width:0; text-align:center;}
        .cc-demo-chain-roles > div > span {
            width:52px; height:52px; display:flex; align-items:center; justify-content:center;
            margin:0 auto .8rem; border-radius:16px; background:var(--cc-surface-aqua);
            color:var(--cc-accent-strong); font-size:.88rem; font-weight:780;
        }
        .cc-demo-chain-roles strong {display:block; font-size:1rem; line-height:1.4;}
        .cc-demo-chain-roles small {
            display:block; margin-top:.25rem; color:var(--cc-muted); line-height:1.45;
        }
        .cc-demo-chain-roles i {color:var(--cc-accent); font-size:1.3rem; font-style:normal;}
        .cc-demo-chain-proof {
            display:grid; grid-template-columns:repeat(4, 1fr); gap:.45rem;
            margin-top:1.7rem; padding-top:1.25rem; border-top:1px solid var(--cc-border);
        }
        .cc-demo-chain-proof span {
            position:relative; padding-top:.65rem; color:var(--cc-accent-strong);
            font-size:.78rem; line-height:1.4; font-weight:670; text-align:center;
        }
        .cc-demo-chain-proof span::before {
            content:""; position:absolute; top:0; left:calc(50% - 3px);
            width:6px; height:6px; border-radius:50%; background:var(--cc-accent);
        }
        .cc-guide-stage-head {
            display:grid; grid-template-columns:1fr auto; align-items:end; gap:.2rem 1.5rem;
            margin:1rem 0 1.25rem; padding-top:2.8rem; border-top:1px solid var(--cc-border);
        }
        .cc-guide-stage-head > span {grid-column:1; margin:0;}
        .cc-guide-stage-head h2 {
            grid-column:1; margin:0; color:var(--cc-text); font-size:2rem; letter-spacing:-.04em;
        }
        .cc-guide-stage-head p {grid-column:2; grid-row:1 / span 2; margin:0; color:var(--cc-muted);}
        .cc-guide {margin:.5rem 0 1.1rem; padding:1.2rem 1.4rem; border-radius:16px; background:#EAF4F3;}
        .cc-guide-steps::before {top:2.08rem; background:#B8D4D2;}
        .cc-guide-index {font-size:.78rem; color:var(--cc-muted);}
        .cc-guide-node {border-color:#83AAA7; background:#fff;}
        .cc-guide-label {font-size:.88rem;}
        .cc-guide-state {font-size:.67rem;}
        .cc-guide-step--complete .cc-guide-node {background:var(--cc-accent); border-color:var(--cc-accent);}
        .st-key-cc_demo_guide_layout {margin-top:0;}
        .cc-guide-current {
            padding:1.5rem 1.6rem; border:1px solid var(--cc-border); border-radius:18px;
            background:var(--cc-bg); box-shadow:0 10px 30px rgba(21,55,57,.05);
        }
        .cc-guide-current--caution, .cc-guide-current--stopped {border-color:#E2C18B;}
        .cc-guide-current--error {border-color:#E8B5AF; background:var(--cc-danger-bg);}
        .cc-guide-current-head {display:flex; align-items:center; justify-content:space-between; gap:1rem;}
        .cc-guide-current-head > span {
            color:var(--cc-muted); font-size:.78rem; font-weight:700; letter-spacing:.08em;
        }
        .cc-guide-role {margin:0; color:var(--cc-accent); font-size:.78rem; letter-spacing:.08em;}
        .cc-guide-current h2 {margin:.55rem 0 .25rem; font-size:1.55rem; letter-spacing:-.025em;}
        .cc-guide-detail {margin:0 0 1rem;}
        .cc-guide-facts {
            display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:.7rem;
            padding:1rem 0; border-top:1px solid var(--cc-border); border-bottom:1px solid var(--cc-border);
        }
        .cc-guide-fact {display:block; padding:0 .8rem; border:0; border-left:2px solid #B8D4D2;}
        .cc-guide-fact dt {color:var(--cc-muted); font-size:.74rem;}
        .cc-guide-fact dd {margin:.3rem 0 0; line-height:1.5; font-weight:620;}
        .cc-guide-meta {margin-top:1rem; padding:0; border:0;}
        .cc-guide-meta > div {display:block;}
        .cc-guide-meta h3 {margin-bottom:.25rem; font-size:.76rem; letter-spacing:.04em;}
        .cc-guide-meta p {color:var(--cc-muted);}
        .st-key-cc_demo_primary_action,
        .st-key-cc_demo_start_action {max-width:380px; margin:.9rem 0 0;}
        .st-key-cc_demo_primary_action a,
        .st-key-cc_demo_start_action button {
            min-height:54px !important; border:0 !important; border-radius:12px !important;
            background:var(--cc-accent) !important; font-size:1rem !important;
            box-shadow:0 8px 20px rgba(0,124,122,.16) !important;
        }
        .cc-role-section-head {
            margin:4.5rem 0 1.2rem; padding-top:3rem; border-top:1px solid var(--cc-border);
        }
        .cc-role-section-head > p {margin-bottom:.45rem;}
        .cc-role-section-head h2 {margin:0; font-size:2rem; letter-spacing:-.04em;}
        .cc-role-section-head > span {display:block; margin-top:.4rem; color:var(--cc-muted);}
        .st-key-cc_role_card_patient,
        .st-key-cc_role_card_nurse,
        .st-key-cc_role_card_doctor {
            height:100%; padding:1.4rem; border:1px solid var(--cc-border); border-radius:18px;
            background:var(--cc-bg); box-shadow:0 10px 28px rgba(21,55,57,.04);
        }
        .cc-role-card-copy {min-height:175px;}
        .cc-role-number {color:var(--cc-accent); font-size:.75rem; font-weight:780; letter-spacing:.1em;}
        .cc-role-card-copy p {margin:1rem 0 .15rem; color:var(--cc-muted); font-size:.82rem;}
        .cc-role-card-copy h3 {margin:0 0 .6rem; font-size:1.32rem; letter-spacing:-.025em;}
        .cc-role-card-copy > span:last-child {color:var(--cc-muted); line-height:1.6;}
        .st-key-cc_role_card_patient a,
        .st-key-cc_role_card_nurse a,
        .st-key-cc_role_card_doctor a {
            min-height:48px; display:flex; align-items:center; justify-content:center;
            border:0 !important; border-radius:10px !important; background:var(--cc-surface-subtle) !important;
            color:var(--cc-accent-strong) !important; font-weight:690 !important; text-decoration:none !important;
        }
        .st-key-cc_role_card_patient a *,
        .st-key-cc_role_card_nurse a *,
        .st-key-cc_role_card_doctor a * {color:var(--cc-accent-strong) !important;}
        .cc-demo-footer {
            display:flex; align-items:center; justify-content:space-between; gap:1rem;
            margin-top:2rem; padding:1.3rem 0; border-top:1px solid var(--cc-border);
            color:var(--cc-muted); font-size:.78rem;
        }
        .cc-demo-footer strong {color:var(--cc-text);}

        /* Patient experience */
        .stApp:has(.cc-patient-shell) {background:#ECF3F2;}
        .stApp:has(.cc-patient-shell) .block-container {
            max-width:720px; margin:1.4rem auto 3rem; padding:1.2rem 2.8rem 2.4rem;
            border:1px solid var(--cc-border); border-radius:28px; background:var(--cc-bg);
            box-shadow:var(--cc-shadow);
        }
        .stApp:has(.cc-patient-shell) h1 {
            margin:0; padding:.25rem 0 .3rem; border:0; text-align:left;
            font-size:2.1rem; letter-spacing:-.04em;
        }
        .cc-patient-progress {margin:.15rem 0 1.4rem; color:var(--cc-muted); font-size:.82rem;}
        .cc-patient-progress i {
            position:relative; display:block; height:5px; margin-top:.65rem;
            border-radius:5px; background:#DDEBE9; overflow:hidden;
        }
        .cc-patient-progress i::after {
            content:""; position:absolute; inset:0 auto 0 0; width:34%; background:var(--cc-accent);
        }
        .cc-patient-prompt {display:grid; grid-template-columns:48px 1fr; gap:1rem; align-items:start; margin:1.7rem 0;}
        .cc-patient-prompt > span {
            width:48px; height:48px; display:flex; align-items:center; justify-content:center;
            border-radius:16px; background:var(--cc-surface-aqua); color:var(--cc-accent);
            font-family:Georgia, serif; font-size:2.4rem; line-height:1;
        }
        .cc-patient-prompt h2 {margin:0; font-size:1.65rem; letter-spacing:-.03em;}
        .cc-patient-prompt p {margin:.35rem 0 0; color:var(--cc-muted); font-size:1rem;}
        .cc-patient-quote {display:flex; flex-direction:column; align-items:flex-end; margin:.5rem 0 1.25rem; padding:0; text-align:left;}
        .cc-patient-quote .cc-patient-label {margin-right:.3rem;}
        .cc-patient-quote blockquote {
            max-width:82%; margin:0; padding:1rem 1.2rem; border:0; border-radius:18px 18px 4px 18px;
            background:var(--cc-surface-aqua); color:var(--cc-text);
            font-family:inherit; font-size:1.2rem; line-height:1.55; font-weight:620;
        }
        .cc-patient-meaning {
            display:grid; grid-template-columns:52px minmax(0, 1fr); gap:1rem; align-items:center;
            margin:0 0 1rem; padding:1.2rem; border:1px solid #BBDAD7; border-radius:18px;
            background:#F7FBFA; text-align:left;
        }
        .cc-patient-meaning-mark {
            width:52px; height:52px; display:flex; align-items:center; justify-content:center;
            border-radius:17px; background:var(--cc-surface-aqua); color:var(--cc-accent);
            font-size:1.55rem; font-weight:780;
        }
        .cc-patient-meaning .cc-patient-label {margin:0 0 .2rem; text-align:left;}
        .cc-patient-meaning strong {display:block; color:var(--cc-text); font-size:1.28rem; line-height:1.45;}
        .cc-patient-meaning p {margin:.35rem 0 0; color:var(--cc-muted); font-size:.9rem; line-height:1.5;}
        .cc-patient-status {
            margin:.8rem 0; padding:.9rem 1rem; border:0; border-radius:14px;
            background:var(--cc-surface-subtle);
        }
        .cc-patient-status--caution, .cc-patient-status--stopped {background:var(--cc-caution-bg);}
        .st-key-cc_patient_decisions button,
        .st-key-cc_patient_decisions_unsure button {
            min-height:56px !important; border-radius:12px !important; font-size:1.02rem !important;
        }
        .st-key-cc_patient_decision_accept button {
            border-color:var(--cc-accent) !important; background:var(--cc-accent) !important;
            color:#fff !important; box-shadow:0 8px 20px rgba(0,124,122,.15) !important;
        }
        .cc-patient-boundary {
            margin:.7rem 0 0; padding:.55rem 0; border:0; background:transparent;
            color:var(--cc-muted); font-size:.8rem;
        }
        .cc-patient-boundary::before {content:"🔒"; margin-right:.35rem; filter:grayscale(1);}
        .cc-patient-emergency {
            margin-top:1.25rem; padding-top:.85rem; border-top:1px solid var(--cc-border);
            color:var(--cc-muted); font-size:.78rem; line-height:1.55;
        }

        /* Nurse experience */
        .stApp:has(.cc-nurse-shell) .block-container {max-width:1280px; padding:1rem 1.5rem 4rem;}
        .stApp:has(.cc-nurse-shell) h1 {
            margin:0; padding:0; border:0; font-size:2.15rem; letter-spacing:-.045em;
        }
        .cc-nurse-count {
            padding:.75rem 1rem; border:1px solid var(--cc-border); border-radius:12px;
            background:var(--cc-bg); color:var(--cc-muted); text-align:center;
        }
        .cc-nurse-count strong {color:var(--cc-accent); font-size:1.2rem;}
        .st-key-cc_nurse_workspace {margin-top:1.4rem;}
        .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort) {
            gap:1.4rem !important;
        }
        .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort)
        > [data-testid="stColumn"]:first-child {
            padding:1rem; border:1px solid var(--cc-border); border-radius:18px; background:var(--cc-bg);
        }
        .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort)
        > [data-testid="stColumn"]:last-child {
            padding:0; border:0; min-width:0;
        }
        .cc-nurse-sort {margin:.15rem 0 .65rem; font-size:.78rem;}
        [class*="st-key-cc_nurse_task_"] button {
            padding:.8rem .8rem .2rem !important; border:0 !important; border-radius:12px 12px 0 0 !important;
            background:transparent !important; font-size:1rem !important;
        }
        [class*="st-key-cc_nurse_task_selected_"] button {background:var(--cc-surface-subtle) !important; border-left:3px solid var(--cc-accent) !important;}
        .cc-nurse-task-meta {
            margin:0 0 .45rem; padding:.25rem .8rem .8rem; border-radius:0 0 12px 12px;
            color:var(--cc-muted); background:transparent;
        }
        .cc-nurse-review-head {
            display:flex; align-items:flex-start; justify-content:space-between; gap:1rem;
            margin:0 0 1rem; padding:1.2rem 1.3rem; border:1px solid var(--cc-border);
            border-radius:16px; background:var(--cc-bg);
        }
        .cc-nurse-review-head > div {order:-1;}
        .cc-nurse-review-head h2 {margin:0; font-size:1.45rem; letter-spacing:-.03em;}
        .cc-nurse-review-head p {margin:.35rem 0 0; color:var(--cc-muted); line-height:1.55;}
        .cc-nurse-review-badge {
            flex:0 0 auto; padding:.38rem .65rem; border:1px solid #E5C48E;
            border-radius:999px; background:var(--cc-caution-bg); color:var(--cc-caution);
            font-size:.76rem; font-weight:720;
        }
        .cc-nurse-review-badge--complete {border-color:#BBDAD7; background:#F0F8F7; color:var(--cc-accent-strong);}
        .cc-nurse-evidence-grid {display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:.8rem;}
        .cc-nurse-evidence-grid article {
            min-height:150px; padding:1rem; border:1px solid var(--cc-border);
            border-radius:14px; background:var(--cc-bg);
        }
        .cc-nurse-evidence-grid article > span {color:var(--cc-muted); font-size:.78rem; font-weight:650;}
        .cc-nurse-evidence-grid article > p {margin:1.5rem 0 0; font-size:1.12rem; line-height:1.55; font-weight:650;}
        .cc-nurse-why {
            display:grid; grid-template-columns:36px 1fr; gap:.8rem; align-items:center;
            margin:1rem 0; padding:.85rem 1rem; border:1px solid var(--cc-border);
            border-radius:12px; background:#F9FBFA;
        }
        .cc-nurse-why > span {
            width:32px; height:32px; display:flex; align-items:center; justify-content:center;
            border:1px solid #A8CFCC; border-radius:50%; color:var(--cc-accent); font-weight:760;
        }
        .cc-nurse-why p {margin:.15rem 0 0; color:var(--cc-muted); line-height:1.45;}
        .cc-nurse-communication {
            display:block; margin:1rem 0; padding:1rem; border:1px solid var(--cc-border);
            border-radius:14px; background:var(--cc-bg);
        }
        .cc-nurse-communication h3 {margin:0 0 .7rem; font-size:1rem;}
        .cc-nurse-communication p {padding:.65rem .75rem; border-radius:10px; background:var(--cc-surface-subtle);}
        .cc-nurse-communication .cc-nurse-mock {
            display:inline-block; margin-top:.55rem; padding:.2rem .5rem; background:#F2F4F4;
            color:var(--cc-muted) !important; font-size:.72rem;
        }
        .cc-nurse-action-title {margin:1rem 0 .35rem; color:var(--cc-muted); font-size:.76rem; letter-spacing:.04em;}
        .st-key-cc_nurse_primary button,
        .st-key-cc_nurse_primary_link a {
            min-height:52px !important; border:0 !important; border-radius:12px !important;
            box-shadow:0 8px 20px rgba(0,124,122,.14) !important;
        }

        /* Doctor experience */
        .stApp:has(.cc-doctor-shell) .block-container {max-width:1280px; padding:1rem 1.5rem 4rem;}
        .stApp:has(.cc-doctor-shell) h1 {
            margin:0; padding:0; border:0; font-size:2.15rem; letter-spacing:-.045em;
        }
        .cc-doctor-patient-strip {
            display:flex; align-items:center; gap:1rem; margin:1.4rem 0 1.2rem;
            padding:1rem 1.2rem; border:1px solid var(--cc-border); border-radius:16px;
            background:var(--cc-bg);
        }
        .cc-doctor-patient-strip > span {
            width:46px; height:46px; display:flex; align-items:center; justify-content:center;
            border-radius:15px; background:var(--cc-surface-aqua); color:var(--cc-accent-strong); font-weight:780;
        }
        .cc-doctor-patient-strip small {display:block; color:var(--cc-muted); font-size:.74rem;}
        .cc-doctor-patient-strip strong {display:block; margin-top:.15rem; font-size:1.05rem;}
        .cc-doctor-patient-strip p {margin:0 0 0 auto; color:var(--cc-muted); font-size:.82rem;}
        .st-key-cc_doctor_workspace [data-testid="stHorizontalBlock"] {gap:1.4rem;}
        .st-key-cc_doctor_workspace [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child {
            padding:0; border:0;
        }
        .cc-doctor-brief-card {
            padding:1.3rem 1.4rem; border:1px solid var(--cc-border); border-radius:18px;
            background:var(--cc-bg); box-shadow:0 12px 32px rgba(21,55,57,.055);
        }
        .cc-doctor-brief-card > header {display:flex; align-items:center; gap:1rem; padding-bottom:1rem; border-bottom:1px solid var(--cc-border);}
        .cc-doctor-brief-card > header > span {
            width:56px; height:56px; display:flex; align-items:center; justify-content:center;
            border-radius:18px; background:var(--cc-surface-aqua); color:var(--cc-accent);
            font-size:1.25rem; font-weight:790;
        }
        .cc-doctor-brief-card > header p {margin:0; color:var(--cc-muted); font-size:.68rem; letter-spacing:.12em;}
        .cc-doctor-brief-card > header h2 {margin:.1rem 0 0; font-size:1.75rem; letter-spacing:-.035em;}
        .cc-doctor-facts {margin:0; border:0;}
        .cc-doctor-fact {
            display:grid; grid-template-columns:42px minmax(0, 1fr); gap:1rem;
            padding:1.05rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-doctor-fact:last-child {border-bottom:0;}
        .cc-doctor-fact > span {
            width:38px; height:38px; display:flex; align-items:center; justify-content:center;
            border-radius:13px; background:var(--cc-surface-subtle); color:var(--cc-accent); font-weight:780;
        }
        .cc-doctor-fact dt {color:var(--cc-muted); font-size:.78rem; font-weight:650;}
        .cc-doctor-fact dd {margin:.28rem 0 0; font-size:1.04rem; line-height:1.5; font-weight:650;}
        .cc-doctor-fact:last-child > span {background:var(--cc-caution-bg); color:var(--cc-caution);}
        .cc-doctor-fact:last-child dd {color:var(--cc-text);}
        .cc-doctor-notice {margin:1rem 0; padding:.8rem 1rem; border:0; border-radius:12px;}
        .cc-doctor-summary {
            margin:1rem 0; padding:1rem 1.1rem; border:1px solid var(--cc-border);
            border-radius:14px; background:var(--cc-bg);
        }
        .cc-doctor-evidence-head {margin:0 0 1rem;}
        .cc-doctor-evidence-head p {margin:0; color:var(--cc-accent); font-size:.66rem; letter-spacing:.12em; font-weight:760;}
        .cc-doctor-evidence-head h2 {margin:.2rem 0 0; font-size:1.45rem;}
        .cc-doctor-evidence-chain {
            position:relative; margin:0 0 1rem; padding:1.1rem 1rem; list-style:none;
            border:1px solid var(--cc-border); border-radius:16px; background:var(--cc-bg);
        }
        .cc-doctor-evidence-chain::before {
            content:""; position:absolute; top:37px; bottom:37px; left:29px; width:1px; background:#A9CFCC;
        }
        .cc-doctor-evidence-chain li {position:relative; display:flex; align-items:center; gap:.8rem; min-height:58px;}
        .cc-doctor-evidence-chain li > span {
            width:38px; height:38px; display:flex; align-items:center; justify-content:center;
            border:4px solid #fff; border-radius:50%; background:var(--cc-surface-aqua);
            color:var(--cc-accent); font-size:.68rem; font-weight:760; z-index:1;
        }
        .cc-doctor-evidence-chain strong {font-size:.9rem;}
        .cc-doctor-source-title {margin:1rem 0 .25rem; padding:0; border:0; color:var(--cc-muted); font-size:.76rem !important;}
        .stApp:has(.cc-doctor-shell) [class*="st-key-cc_doctor_source_"] button {
            min-height:42px !important; padding:.35rem .1rem !important; font-size:.82rem !important;
        }
        .st-key-cc_doctor_primary button {min-height:52px !important; border:0 !important; border-radius:12px !important;}

        /* Knowledge Center: governed sources, terminology, review and release.
           Symptoms are catalog entries, never top-level Knowledge categories. */
        .stApp:has(.cc-knowledge-shell) .block-container {
            max-width:1280px; padding:.7rem 1.5rem 3.5rem;
        }
        .cc-kc-hero {
            display:grid; grid-template-columns:minmax(0, 1.35fr) minmax(340px, .65fr);
            gap:3rem; align-items:end; padding:3.6rem 0 2.8rem;
            border-bottom:1px solid var(--cc-border);
        }
        .cc-kc-kicker,
        .cc-kc-section-head > div > span {
            color:var(--cc-accent); font-size:.68rem; line-height:1.4;
            font-weight:790; letter-spacing:.14em;
        }
        .stApp:has(.cc-knowledge-shell) .cc-kc-hero h1 {
            max-width:760px; margin:.75rem 0 1.1rem; padding:0;
            color:var(--cc-text); font-size:clamp(2.8rem, 5.3vw, 5.1rem);
            line-height:1.03; font-weight:770; letter-spacing:-.055em;
        }
        .cc-kc-hero-copy > p {
            max-width:720px; margin:0; color:var(--cc-muted);
            font-size:1.05rem; line-height:1.75;
        }
        .cc-kc-flow {
            position:relative; margin:0; padding:1.25rem 1.3rem; list-style:none;
            border:1px solid var(--cc-border); border-radius:18px; background:var(--cc-bg);
            box-shadow:var(--cc-shadow);
        }
        .cc-kc-flow::before {
            content:""; position:absolute; top:38px; bottom:38px; left:33px;
            width:1px; background:#B6C8C6;
        }
        .cc-kc-flow li {
            position:relative; display:grid; grid-template-columns:42px minmax(0, 1fr);
            gap:.85rem; align-items:center; min-height:64px;
        }
        .cc-kc-flow li > span {
            width:40px; height:40px; display:flex; align-items:center; justify-content:center;
            border:4px solid var(--cc-bg); border-radius:50%; background:#EDF1F0;
            color:var(--cc-muted); font-size:.67rem; font-weight:780; z-index:1;
        }
        .cc-kc-flow li.is-ready > span {background:var(--cc-surface-aqua); color:var(--cc-accent);}
        .cc-kc-flow strong {display:block; font-size:.92rem;}
        .cc-kc-flow small {display:block; margin-top:.15rem; color:var(--cc-muted); line-height:1.4;}
        .cc-kc-metrics {
            display:grid; grid-template-columns:repeat(4, minmax(0, 1fr));
            gap:1rem; margin:1.2rem 0 2.2rem;
        }
        .cc-kc-metrics article,
        .cc-kc-term-summary article {
            display:flex; align-items:baseline; gap:.55rem; padding:1rem 1.1rem;
            border:1px solid var(--cc-border); border-radius:14px; background:var(--cc-bg);
        }
        .cc-kc-metrics strong,
        .cc-kc-term-summary strong {color:var(--cc-accent); font-size:1.55rem; line-height:1;}
        .cc-kc-metrics span,
        .cc-kc-term-summary span {color:var(--cc-muted); font-size:.78rem; line-height:1.4;}
        .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_section [role="radiogroup"] {
            display:grid !important; grid-template-columns:repeat(4, minmax(0, 1fr));
            gap:.65rem !important; margin-bottom:1.4rem;
        }
        .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_section [role="radiogroup"] label {
            min-height:50px; margin:0 !important; padding:.65rem .8rem !important;
            border:1px solid var(--cc-border); border-radius:12px; background:var(--cc-bg);
            color:var(--cc-text); align-items:center; justify-content:center;
            font-size:.92rem; line-height:1.3; font-weight:670;
        }
        .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_section [role="radiogroup"] label:has(input:checked) {
            border-color:var(--cc-accent); background:var(--cc-surface-aqua);
            color:var(--cc-accent-strong); box-shadow:0 8px 20px rgba(0,124,122,.08);
        }
        .cc-kc-section-head {
            display:flex; align-items:end; justify-content:space-between; gap:2rem;
            margin:1.8rem 0 1rem; padding-bottom:.8rem; border-bottom:1px solid var(--cc-border);
        }
        .cc-kc-section-head h2 {margin:.22rem 0 0; font-size:1.85rem; letter-spacing:-.035em;}
        .cc-kc-section-head > p {max-width:560px; margin:0; color:var(--cc-muted); line-height:1.55; text-align:right;}
        .cc-kc-group-grid {
            display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:1rem;
        }
        .cc-kc-group-card {
            min-height:310px; padding:1.2rem; border:1px solid var(--cc-border);
            border-radius:16px; background:var(--cc-bg);
        }
        .cc-kc-group-card > span,
        .cc-kc-source-main > span,
        .cc-kc-term-detail > div:first-child > span,
        .cc-kc-gate-card > span,
        .cc-kc-release-status > span {
            color:var(--cc-accent); font-size:.69rem; font-weight:750; letter-spacing:.08em;
        }
        .cc-kc-group-card h2 {margin:.5rem 0 .35rem; font-size:1.22rem;}
        .cc-kc-group-card > p {min-height:42px; margin:0; color:var(--cc-muted); font-size:.86rem; line-height:1.5;}
        .cc-kc-group-card ul {margin:1rem 0 0; padding:0; list-style:none;}
        .cc-kc-group-card li {
            padding:.5rem 0; border-top:1px solid var(--cc-border);
            color:var(--cc-text); font-size:.82rem; line-height:1.4;
        }
        .stApp:has(.cc-knowledge-shell) [class*="st-key-cc_knowledge_source_policy"],
        .stApp:has(.cc-knowledge-shell) [class*="st-key-cc_knowledge_term"] {margin-top:1.2rem;}
        .cc-kc-source-detail {
            display:grid; grid-template-columns:minmax(0, 1.4fr) minmax(220px, .6fr);
            gap:1rem; margin:.7rem 0 1.5rem; padding:1.25rem;
            border:1px solid var(--cc-border); border-radius:18px; background:var(--cc-bg);
        }
        .cc-kc-source-main h3,
        .cc-kc-term-detail h3 {margin:.45rem 0 .25rem; font-size:1.35rem; line-height:1.35;}
        .cc-kc-source-main p,
        .cc-kc-term-detail > div:first-child > p {margin:0; color:var(--cc-muted); line-height:1.5;}
        .cc-kc-source-main small {display:block; margin-top:.5rem; color:var(--cc-muted); overflow-wrap:anywhere;}
        .cc-kc-source-status {
            padding:1rem; border-radius:13px; background:var(--cc-caution-bg);
        }
        .cc-kc-source-status strong {display:block; color:var(--cc-caution);}
        .cc-kc-source-status span {display:block; margin-top:.35rem; color:var(--cc-muted); font-size:.82rem;}
        .cc-kc-source-allowed,
        .cc-kc-source-boundary {padding-top:1rem; border-top:1px solid var(--cc-border);}
        .cc-kc-source-allowed ul {margin:.45rem 0 0; padding-left:1.1rem; line-height:1.65;}
        .cc-kc-source-boundary p {margin:.45rem 0 0; color:var(--cc-muted); line-height:1.65;}
        .cc-kc-term-summary {
            display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:.85rem;
        }
        .cc-kc-term-summary article {align-items:center; min-height:72px;}
        .cc-kc-term-summary article.is-caution {background:var(--cc-caution-bg);}
        .cc-kc-term-summary article.is-caution strong {color:var(--cc-caution);}
        .cc-kc-term-detail {
            display:grid; grid-template-columns:minmax(0, 1fr) max-content; gap:1.1rem 2rem;
            margin:.7rem 0; padding:1.3rem; border:1px solid var(--cc-border);
            border-radius:18px; background:var(--cc-bg);
        }
        .cc-kc-term-state {
            align-self:start; padding:.5rem .7rem; border-radius:999px;
            background:var(--cc-caution-bg); color:var(--cc-caution); font-size:.76rem;
        }
        .cc-kc-term-detail > section {padding-top:1rem; border-top:1px solid var(--cc-border);}
        .cc-kc-term-detail h4 {margin:0 0 .45rem; font-size:.9rem;}
        .cc-kc-term-detail ul {margin:0; padding-left:1.1rem; line-height:1.65;}
        .cc-kc-term-detail section p {margin:0; color:var(--cc-muted); line-height:1.65;}
        .cc-kc-inline-boundary {
            margin:.7rem 0 1.2rem; padding:.85rem 1rem; border-radius:12px;
            background:var(--cc-surface-subtle); color:var(--cc-text); line-height:1.55;
        }
        .cc-kc-gate-grid {
            display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:.85rem;
        }
        .cc-kc-gate-card {
            min-height:180px; padding:1.05rem; border:1px solid var(--cc-border);
            border-radius:15px; background:var(--cc-bg);
        }
        .cc-kc-gate-card h3 {margin:.55rem 0 .75rem; font-size:1.05rem;}
        .cc-kc-gate-card ul {margin:0; padding:0; list-style:none;}
        .cc-kc-gate-card li {padding:.32rem 0; color:var(--cc-muted); font-size:.82rem;}
        .cc-kc-gate-card li::before {content:"✓"; margin-right:.45rem; color:var(--cc-accent);}
        .cc-kc-release-card {
            display:grid; grid-template-columns:minmax(280px, .72fr) minmax(0, 1.28fr);
            gap:1.2rem; padding:1.25rem; border:1px solid var(--cc-border);
            border-radius:18px; background:var(--cc-bg);
        }
        .cc-kc-release-status {padding:1.1rem; border-radius:14px; background:var(--cc-caution-bg);}
        .cc-kc-release-status strong {display:block; margin:.55rem 0; color:var(--cc-caution); font-size:1.2rem;}
        .cc-kc-release-status p {margin:0; color:var(--cc-muted);}
        .cc-kc-gap-list {margin:0; padding:0; list-style:none;}
        .cc-kc-gap-list li {
            display:grid; grid-template-columns:42px minmax(0, 1fr); gap:.75rem;
            align-items:center; padding:.6rem 0; border-bottom:1px solid var(--cc-border);
        }
        .cc-kc-gap-list li:last-child {border-bottom:0;}
        .cc-kc-gap-list strong {color:var(--cc-accent); font-size:1.1rem;}
        .cc-kc-gap-list span {line-height:1.45;}
        .cc-kc-release-boundaries {
            display:grid; grid-template-columns:1fr 1fr; gap:1rem; margin:1rem 0;
        }
        .cc-kc-release-boundaries article {
            padding:1.1rem; border:1px solid var(--cc-border); border-radius:15px; background:var(--cc-bg);
        }
        .cc-kc-release-boundaries article:last-child {background:var(--cc-caution-bg);}
        .cc-kc-release-boundaries h3 {margin:0 0 .55rem; font-size:1.05rem;}
        .cc-kc-release-boundaries ul {margin:0; padding-left:1.1rem; line-height:1.7;}
        .cc-kc-footer {
            margin:2rem 0 .5rem; padding:1rem 0; border-top:1px solid var(--cc-text);
            border-bottom:1px solid var(--cc-border); color:var(--cc-muted); line-height:1.65;
        }

        @media (max-width: 900px) {
            .cc-demo-hero {grid-template-columns:1fr; gap:2.5rem; min-height:0; padding:3rem 0;}
            .cc-demo-hero h1 {font-size:clamp(2.5rem, 10vw, 4rem);}
            .cc-guide-facts {grid-template-columns:1fr;}
            .cc-guide-fact {padding:.45rem .8rem;}
            .cc-role-section-head {margin-top:3.2rem;}
            .cc-nurse-evidence-grid {grid-template-columns:1fr;}
            .cc-nurse-evidence-grid article {min-height:0;}
            .cc-nurse-evidence-grid article > p {margin:.65rem 0 0;}
            .cc-kc-hero {grid-template-columns:1fr; gap:1.5rem; padding:2.6rem 0 2rem;}
            .cc-kc-group-grid {grid-template-columns:1fr;}
            .cc-kc-group-card {min-height:0;}
            .cc-kc-gate-grid {grid-template-columns:repeat(2, minmax(0, 1fr));}
        }
        @media (max-width: 768px) {
            .block-container {padding:.6rem 1rem 2.5rem;}
            .cc-role-topbar {min-height:54px; margin-bottom:1rem;}
            .cc-role-topbar-badge {font-size:.7rem;}
            .cc-demo-header {min-height:56px;}
            .cc-demo-nav-label {display:none;}
            .cc-demo-brand strong {font-size:1.08rem;}
            .cc-demo-hero {padding:2.4rem 0 2.8rem;}
            .cc-demo-hero h1 {font-size:2.65rem !important; line-height:1.08 !important;}
            .cc-demo-lead {font-size:1rem; line-height:1.7;}
            .cc-demo-chain {padding:1.2rem; border-radius:16px;}
            .cc-demo-chain-roles {grid-template-columns:1fr; gap:.75rem;}
            .cc-demo-chain-roles i {transform:rotate(90deg);}
            .cc-demo-chain-roles > div {display:grid; grid-template-columns:44px 1fr; gap:.1rem .8rem; text-align:left;}
            .cc-demo-chain-roles > div > span {grid-row:1 / span 2; width:44px; height:44px; margin:0;}
            .cc-demo-chain-proof {grid-template-columns:repeat(2, 1fr);}
            .cc-guide-stage-head {display:block; padding-top:2rem;}
            .cc-guide-stage-head h2 {font-size:1.6rem !important;}
            .cc-guide-stage-head p {margin-top:.35rem;}
            .cc-guide {padding:1rem .35rem; overflow-x:auto;}
            .cc-guide-steps {min-width:520px;}
            .cc-guide-current {padding:1.1rem;}
            .cc-guide-current-head {align-items:flex-start;}
            .cc-guide-current h2 {font-size:1.3rem !important;}
            .cc-guide-facts {display:block;}
            .cc-guide-meta {grid-template-columns:1fr;}
            .cc-role-section-head h2 {font-size:1.55rem !important;}
            .cc-demo-footer {display:block;}
            .cc-demo-footer span {display:block; margin-top:.35rem;}
            .stApp:has(.cc-patient-shell) .block-container {
                margin:0; padding:.7rem 1.1rem 2.5rem; border:0; border-radius:0; box-shadow:none;
            }
            .stApp:has(.cc-patient-shell) h1 {font-size:1.9rem !important;}
            .cc-patient-prompt {grid-template-columns:42px 1fr; margin:1.35rem 0;}
            .cc-patient-prompt > span {width:42px; height:42px;}
            .cc-patient-prompt h2 {font-size:1.35rem !important;}
            .cc-patient-quote blockquote {max-width:90%; font-size:1.05rem;}
            .cc-patient-meaning {grid-template-columns:44px 1fr; padding:1rem;}
            .cc-patient-meaning-mark {width:44px; height:44px;}
            .cc-patient-meaning strong {font-size:1.1rem;}
            .stApp:has(.cc-nurse-shell) .block-container,
            .stApp:has(.cc-doctor-shell) .block-container {padding:.7rem 1rem 2.5rem;}
            .stApp:has(.cc-nurse-shell) h1,
            .stApp:has(.cc-doctor-shell) h1 {font-size:1.85rem !important;}
            .cc-nurse-count {padding:.6rem; font-size:.8rem;}
            .st-key-cc_nurse_workspace [data-testid="stHorizontalBlock"]:has(.cc-nurse-sort)
            > [data-testid="stColumn"]:first-child {padding:.7rem; border-radius:14px;}
            .cc-nurse-review-head {display:block; padding:1rem;}
            .cc-nurse-review-badge {display:inline-block; margin-top:.75rem;}
            .cc-doctor-patient-strip p {display:none;}
            .cc-doctor-brief-card {padding:1rem;}
            .cc-doctor-brief-card > header h2 {font-size:1.45rem !important;}
            .cc-doctor-fact {grid-template-columns:38px 1fr; gap:.75rem;}
            .stApp:has(.cc-knowledge-shell) .block-container {padding:.7rem 1rem 2.5rem;}
            .stApp:has(.cc-knowledge-shell) .cc-kc-hero h1 {
                font-size:2.55rem !important; line-height:1.08 !important;
            }
            .cc-kc-metrics,
            .cc-kc-term-summary {grid-template-columns:repeat(2, minmax(0, 1fr));}
            .stApp:has(.cc-knowledge-shell) .st-key-cc_knowledge_section [role="radiogroup"] {
                grid-template-columns:repeat(2, minmax(0, 1fr));
            }
            .cc-kc-section-head {display:block;}
            .cc-kc-section-head > p {margin-top:.4rem; text-align:left;}
            .cc-kc-source-detail,
            .cc-kc-release-card,
            .cc-kc-release-boundaries {grid-template-columns:1fr;}
            .cc-kc-term-detail {grid-template-columns:1fr;}
            .cc-kc-term-state {justify-self:start;}
            .cc-kc-gate-grid {grid-template-columns:1fr;}
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_mode_badges(st) -> None:
    model_label = html.escape(semantic_model_label())
    st.markdown(
        f"""
        <span class="cc-mode-chip">本地稳定演示</span>
        <span class="cc-mode-chip">{model_label}</span>
        <span class="cc-mode-chip">Safety Agent v4 · 规则 + 可选 MiMo Critic</span>
        <span class="cc-mode-chip">SQLite 持久化</span>
        <span class="cc-mode-chip">外部适配器默认离线 · 未联调</span>
        """,
        unsafe_allow_html=True,
    )


def render_competition_progress(st, progress, *, show_next: bool = True) -> None:
    """Render persisted-fact milestones without caching a second UI state."""

    st.markdown("## 完整比赛 Demo 进度")
    completed = sum(
        bool(progress.milestones.get(step)) for step, _ in COMPETITION_STEP_LABELS
    )
    st.progress(
        completed / len(COMPETITION_STEP_LABELS),
        text=f"持久化事实已完成 {completed}/{len(COMPETITION_STEP_LABELS)} 项",
    )
    for offset in range(0, len(COMPETITION_STEP_LABELS), 3):
        row = COMPETITION_STEP_LABELS[offset : offset + 3]
        columns = st.columns(len(row))
        for column, (step, label) in zip(columns, row):
            with column:
                if progress.milestones.get(step):
                    st.success(f"✓ {label}")
                else:
                    st.info(f"○ {label}")
    if progress.integrity_issue:
        st.error(progress.integrity_issue)
    if progress.knowledge_available:
        st.caption("Knowledge CURRENT registry：可用（独立只读，不参与临床进度判定）")
    elif progress.knowledge_error:
        st.warning(progress.knowledge_error)
    if progress.is_terminal:
        if progress.stage.value == "story_complete":
            st.success(f"流程终态：{progress.terminal_reason}")
        else:
            st.warning(f"流程终态：{progress.terminal_reason}")
    if show_next and progress.generation and progress.is_terminal:
        with st.container(border=True):
            st.markdown(f"**{progress.next_label}**")
            st.caption(progress.next_help)
            st.page_link(
                progress.next_page,
                label=f"{progress.next_label} →",
                icon="🧾",
            )
            st.page_link(
                "app.py",
                label="返回首页（不会自动重新开始） →",
                icon="↩️",
            )
    elif show_next and progress.generation:
        with st.container(border=True):
            st.markdown(f"**推荐下一步：{progress.next_label}**")
            st.caption(progress.next_help)
            st.page_link(
                progress.next_page,
                label=f"{progress.next_label} →",
                icon="🧭",
            )
    render_integration_status(st)
    if progress.is_terminal:
        current_url = getattr(getattr(st, "context", None), "url", None)
        current_path = urlparse(current_url).path.rstrip("/") if current_url else ""
        is_home = bool(current_url) and current_path == ""
        is_audit = bool(current_url) and current_path.endswith(
            ("/audit_log", "/records")
        )
        if not (is_home or is_audit):
            st.stop()


def render_integration_status(st) -> None:
    """Render one pure config projection; this performs no auth or health check."""

    from continucare.adapters.factory import read_adapter_statuses

    statuses = read_adapter_statuses()
    st.markdown("### 可选外部适配器状态")
    labels = {
        "feishu": ("飞书", "未进行真实租户联调"),
        "aily": ("Aily", "未进行真实 API 调用"),
        "bitable": ("Bitable", "未写入外部数据"),
    }
    for capability in ("feishu", "aily", "bitable"):
        status = statuses[capability]
        title, honest_boundary = labels[capability]
        if status.selected_mode == "mock":
            mode_text = "Mock fallback"
        elif status.selected_mode == "disabled":
            mode_text = "disabled"
        elif status.external_calls_allowed:
            mode_text = "test_tenant 已配置（本轮未验证）"
        else:
            mode_text = "test_tenant fail-closed"
        missing = (
            f" · 缺少配置：{', '.join(status.missing_config_keys)}"
            if status.missing_config_keys
            else ""
        )
        st.caption(
            f"{title}：{mode_text} / {honest_boundary}{missing} · "
            "live_tenant_verified=false · production_ready=false"
        )


def clear_demo_session_state(st) -> None:
    """Drop browser-only widget/navigation hints after an explicit reset."""

    prefixes = (
        "care::",
        "semantic::",
        "manual_",
        "competition::",
        "cc_patient_",
        "cc_nurse_",
        "cc_doctor_",
    )
    exact = {"care_submission_notice"}
    for key in list(st.session_state):
        if key in exact or key.startswith(prefixes):
            del st.session_state[key]


def semantic_model_label() -> str:
    from continucare.care_agent.model_api import build_model_adapter

    adapter = build_model_adapter()
    if adapter.configured:
        return f"MiMo {adapter.config.model_name} 已启用"
    return "Care Agent 语义 Mock 回退"

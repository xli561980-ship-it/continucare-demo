"""Outcome-first nurse work queue for deterministic workflow Alerts."""

from __future__ import annotations

import html
from datetime import datetime, timezone

import streamlit as st

from continucare.adapters.mock_notifier import MockNotifier
from continucare.adapters.sqlite_store import SQLiteStore
from continucare.config import get_settings
from continucare.db import utc_now_iso
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.models import AlertStatus
from continucare.presentation import (
    alert_next_step,
    alert_status_text,
    build_l5_governance_for_patient,
    build_latest_l5_submission_view,
    observation_evidence_text,
    owner_text,
)
from continucare.services.alerts import AlertService
from continucare.services.manual_review_workflow import ManualReviewWorkflowService
from continucare.layer4.manual_reviews import (
    PENDING_APPROVAL,
    READY_TO_SEND,
    ManualReviewQueue,
    communication_readiness,
)
from continucare.layer4.storage import Layer4SQLiteStore
from continucare.services.competition_demo import (
    demo_write_guard,
    read_competition_demo,
)
from continucare.ui import (
    inject_global_styles,
    render_competition_progress,
    render_l5_governance_panel,
    render_l5_submission_panel,
    render_mode_badges,
)


def _sla_text(due_at: str | None) -> str:
    if not due_at:
        return "未设置"
    due = datetime.fromisoformat(due_at)
    remaining = due - datetime.now(timezone.utc)
    seconds = int(remaining.total_seconds())
    if seconds <= 0:
        return "需立即处理"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours} 小时 {minutes} 分钟"


def _alert_evidence(store, alert):
    message = None
    observations = []
    for ref in alert.evidence_refs:
        if ref.startswith(("message-", "message_")):
            message = store.get_message(ref)
        elif ref.startswith(("observation-", "observation_")):
            item = store.get_observation(ref)
            if item:
                observations.append(item)
    return message, observations


def _render_task_reason(store, alert):
    message, observations = _alert_evidence(store, alert)
    st.markdown("**患者原话**")
    if message:
        st.markdown(
            f'<div class="cc-quote">{html.escape(message.message_text)}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.warning("原始消息记录缺失。")

    st.markdown("**为什么进入工作队列**")
    if observations:
        for observation in observations:
            st.markdown(f"- {observation_evidence_text(observation)}")
    st.caption(f"确定性规则：{alert.trigger_rule_id} · {alert.trigger_reason}")


def _task_output(task, code):
    for item in task.get("output", []):
        codes = {
            coding.get("code")
            for coding in item.get("type", {}).get("coding", [])
        }
        if code in codes:
            return (
                item.get("valueCode")
                or item.get("valueString")
                or item.get("valueReference", {}).get("reference")
            )
    return None


TASK_STATUS_LABELS = {
    "requested": "待接收",
    "received": "已接收",
    "accepted": "已接受",
    "in-progress": "处理中",
    "completed": "处理完成",
    "rejected": "已拒绝",
    "cancelled": "已取消",
}

OUTCOME_LABELS = {
    "evidence_consistent": "已核对证据，记录一致",
    "clarification_needed": "已核对证据，需要后续补充说明",
}


def _guarded_write(action, /, *args, **kwargs):
    with demo_write_guard(
        settings.db_path,
        expected_generation=progress.generation,
    ):
        return action(*args, **kwargs)


st.set_page_config(
    page_title="护士任务中心 · ContinuCare",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.title("护士任务中心")

settings = get_settings()
progress = read_competition_demo(settings.db_path)
render_competition_progress(st, progress)
if not settings.db_path.is_file():
    st.info("尚未开始完整比赛 Demo。")
    st.page_link("app.py", label="返回首页开始 Demo →", icon="🏠")
    st.stop()

store = SQLiteStore(settings.db_path, initialize=False)
alert_service = AlertService(store, MockNotifier())
manual_repository = Layer4SQLiteStore(settings.db_path, initialize=False)
manual_service = ManualReviewWorkflowService(store, layer4_store=manual_repository)
manual_tasks = ManualReviewQueue(manual_repository).list_for_patient(DEMO_PATIENT_ID)
with st.expander("工程追溯：中国知识版本、原始回答与标准化 Observation"):
    render_l5_governance_panel(
        st, build_l5_governance_for_patient(store, DEMO_PATIENT_ID)
    )
    render_l5_submission_panel(
        st, build_latest_l5_submission_view(store, DEMO_PATIENT_ID)
    )
all_alerts = store.list_alerts()
active_alerts = [item for item in all_alerts if item.status != AlertStatus.RESOLVED]
resolved_alerts = [item for item in all_alerts if item.status == AlertStatus.RESOLVED]

st.markdown("## 患者确认触发的人工复核")
st.caption(
    "该队列与临床规则/Alert 队列分离。处理结果只用于合成演示；"
    "沟通草稿必须人工批准，本页面不提供发送能力。"
)
if not manual_tasks:
    st.info("尚无患者明确确认后创建的护士人工复核任务。")
for task in manual_tasks:
    response_ref = task["reasonReference"]["reference"]
    response_id = response_ref.split("/", 1)[1]
    message = store.get_message(response_id)
    observations = store.list_observations_for_message(response_id)
    response = store.get_questionnaire_response(response_id)
    with st.container(border=True):
        st.markdown("### 人工复核患者已确认报告")
        st.caption(
            f"FHIR Task/{task['id']} · 状态 "
            f"{TASK_STATUS_LABELS.get(task['status'], task['status'])} · "
            "优先级 routine · 临床评估 not_assessed"
        )
        st.markdown("**患者原话**")
        if message:
            st.info(message.message_text)
        else:
            st.warning("原始患者消息缺失，任务不能继续处理。")
        st.markdown("**患者确认结果与证据链**")
        if response:
            st.write(
                f"QuestionnaireResponse/{response_id} · 状态 {response.get('status', '—')}"
            )
        for observation in observations:
            st.markdown(f"- {observation_evidence_text(observation)}")
            st.caption(
                f"Observation/{observation.observation_id} → derivedFrom {response_ref}"
            )
        st.info("这里只核对患者自述与确认事实，不形成诊断、风险等级或治疗建议。")

        with st.expander("查看 Task 处理历史与证据引用"):
            history = sorted(
                (
                    item
                    for item in manual_repository.list_fhir_resources(
                        patient_id=DEMO_PATIENT_ID,
                        resource_type="Task",
                        current_only=False,
                    )
                    if item["id"] == task["id"]
                ),
                key=lambda item: int(item["meta"]["versionId"]),
            )
            for version in history:
                st.write(
                    f"v{version['meta']['versionId']} · "
                    f"{TASK_STATUS_LABELS.get(version['status'], version['status'])} · "
                    f"{version['meta']['lastUpdated']}"
                )
            st.code("\n".join(sorted({response_ref, *[f'Observation/{item.observation_id}' for item in observations]})), language=None)

        status = task["status"]
        note = ""
        if status in {"requested", "received", "in-progress"}:
            note = st.text_area(
                "护士处理记录（当前动作必填）",
                key=f"manual_note_{task['id']}_{status}",
                value={
                    "requested": "已收到合成人工复核任务。",
                    "received": "接受并开始核对合成证据。",
                    "in-progress": "已逐字核对患者原话、确认结果与最终证据链。",
                }[status],
                placeholder="仅记录合成人工处理事实；不要填写诊断、风险等级、治疗或改药建议。",
            )
        try:
            if status == "requested":
                acknowledge, cancel = st.columns(2)
                if acknowledge.button(
                    "确认收到任务", key=f"manual_ack_{task['id']}", width="stretch"
                ):
                    _guarded_write(
                        manual_service.acknowledge,
                        patient_id=DEMO_PATIENT_ID,
                        task_id=task["id"],
                        note=note,
                        occurred_at=utc_now_iso(),
                    )
                    st.rerun()
                if cancel.button(
                    "取消任务", key=f"manual_cancel_requested_{task['id']}", width="stretch"
                ):
                    _guarded_write(
                        manual_service.cancel,
                        patient_id=DEMO_PATIENT_ID,
                        task_id=task["id"],
                        note=note,
                        occurred_at=utc_now_iso(),
                    )
                    st.rerun()
            elif status == "received":
                start, reject, cancel = st.columns(3)
                if start.button(
                    "接受并开始复核", key=f"manual_start_{task['id']}", width="stretch"
                ):
                    _guarded_write(
                        manual_service.start,
                        patient_id=DEMO_PATIENT_ID,
                        task_id=task["id"],
                        note=note,
                        occurred_at=utc_now_iso(),
                    )
                    st.rerun()
                if reject.button(
                    "拒绝任务", key=f"manual_reject_{task['id']}", width="stretch"
                ):
                    _guarded_write(
                        manual_service.reject,
                        patient_id=DEMO_PATIENT_ID,
                        task_id=task["id"],
                        note=note,
                        occurred_at=utc_now_iso(),
                    )
                    st.rerun()
                if cancel.button(
                    "取消任务", key=f"manual_cancel_received_{task['id']}", width="stretch"
                ):
                    _guarded_write(
                        manual_service.cancel,
                        patient_id=DEMO_PATIENT_ID,
                        task_id=task["id"],
                        note=note,
                        occurred_at=utc_now_iso(),
                    )
                    st.rerun()
            elif status == "in-progress":
                outcome = st.selectbox(
                    "受控处理结果",
                    options=list(OUTCOME_LABELS),
                    format_func=lambda value: OUTCOME_LABELS[value],
                    key=f"manual_outcome_{task['id']}",
                )
                complete, cancel = st.columns(2)
                if complete.button(
                    "记录结果并生成沟通草稿",
                    key=f"manual_complete_{task['id']}",
                    width="stretch",
                ):
                    _guarded_write(
                        manual_service.record_outcome,
                        patient_id=DEMO_PATIENT_ID,
                        task_id=task["id"],
                        outcome=outcome,
                        note=note,
                        occurred_at=utc_now_iso(),
                    )
                    st.rerun()
                if cancel.button(
                    "取消任务", key=f"manual_cancel_progress_{task['id']}", width="stretch"
                ):
                    _guarded_write(
                        manual_service.cancel,
                        patient_id=DEMO_PATIENT_ID,
                        task_id=task["id"],
                        note=note,
                        occurred_at=utc_now_iso(),
                    )
                    st.rerun()
            elif status == "completed":
                outcome = _task_output(task, "review-outcome")
                review_note = _task_output(task, "review-note")
                st.success(f"处理结果：{OUTCOME_LABELS.get(outcome, outcome or '已记录')}")
                if review_note:
                    st.write(f"护士记录：{review_note}")
                communications = manual_service.list_communications_for_task(
                    DEMO_PATIENT_ID, task["id"]
                )
                if not communications:
                    st.error("处理结果缺少沟通草稿，请查看审计记录。")
                for communication in communications:
                    readiness = communication_readiness(communication)
                    st.markdown("**合成、非诊断性沟通草稿**")
                    st.info(communication["payload"][0]["contentString"])
                    if readiness == PENDING_APPROVAL:
                        st.warning("待人工批准 · 尚不可发送 · 未实际发送")
                        approval_note = st.text_area(
                            "人工批准记录（必填）",
                            key=f"manual_approval_note_{communication['id']}",
                            value="已逐字核对合成草稿，明确批准进入 ready-to-send；仍未发送。",
                            placeholder="例如：已逐字核对合成草稿，明确批准进入可发送状态。",
                        )
                        if st.button(
                            "明确批准进入可发送状态",
                            key=f"manual_approve_{communication['id']}",
                            type="primary",
                            width="stretch",
                        ):
                            _guarded_write(
                                manual_service.approve_draft,
                                patient_id=DEMO_PATIENT_ID,
                                task_id=task["id"],
                                communication_id=communication["id"],
                                note=approval_note,
                                occurred_at=utc_now_iso(),
                            )
                            st.rerun()
                        st.page_link(
                            "pages/3_doctor_summary.py",
                            label="先前往医生端生成 pending 简报 →",
                            icon="📋",
                        )
                    elif readiness == READY_TO_SEND:
                        st.success("已人工批准：ready-to-send（尚未发送）")
                        st.page_link(
                            "pages/3_doctor_summary.py",
                            label="下一步：前往医生端刷新 ready 简报 →",
                            icon="📋",
                        )
                    else:
                        st.error("草稿批准状态无效。")
            elif status in {"rejected", "cancelled"}:
                st.info("任务已进入终态；未创建沟通草稿，也没有发送副作用。")
        except (LookupError, ValueError) as exc:
            st.error(str(exc))

st.divider()
st.markdown("## 已获批规则产生的 Alert 任务（独立队列）")
active_metric, approved_metric, done_metric = st.columns(3)
active_metric.metric("待处理任务", len(active_alerts))
approved_metric.metric("当前获批临床规则", 0)
done_metric.metric("已完成任务", len(resolved_alerts))

active_tab, completed_tab = st.tabs(
    [f"待处理任务（{len(active_alerts)}）", f"已完成任务（{len(resolved_alerts)}）"]
)

with active_tab:
    if not active_alerts:
        st.info(
            "当前工作队列为空。路径仍处于临床审核草案，"
            "不会根据患者文本自动产生分级任务。"
        )

    for index, alert in enumerate(active_alerts, start=1):
        with st.container(border=True):
            st.markdown(
                f'<div class="cc-kicker">任务 {index} · {alert.severity}</div>',
                unsafe_allow_html=True,
            )
            heading, sla = st.columns([3, 1])
            with heading:
                st.markdown("### 按获批规则要求复核本次患者报告")
                st.caption(
                    f"责任角色：{owner_text(alert.owner_role)} · "
                    f"当前状态：{alert_status_text(alert)}"
                )
            with sla:
                st.metric("剩余 SLA", _sla_text(alert.sla_due_at))

            _render_task_reason(store, alert)

            st.markdown("**你需要完成的下一步**")
            st.info(alert_next_step(alert))

            note = st.text_area(
                "处理记录（关闭任务时必填）",
                key=f"note_{alert.alert_id}",
                placeholder="例如：已完成合成演示复核并记录结果。不要填写真实患者信息。",
            )
            acknowledge, escalate, resolve = st.columns(3)
            try:
                if acknowledge.button(
                    "确认收到任务", key=f"ack_{alert.alert_id}", width="stretch"
                ):
                    _guarded_write(alert_service.acknowledge, alert.alert_id, note)
                    st.rerun()
                if escalate.button(
                    "升级医生复核", key=f"escalate_{alert.alert_id}", width="stretch"
                ):
                    _guarded_write(alert_service.escalate, alert.alert_id, note)
                    st.rerun()
                if resolve.button(
                    "记录结果并完成", key=f"resolve_{alert.alert_id}", width="stretch"
                ):
                    _guarded_write(alert_service.resolve, alert.alert_id, note)
                    st.rerun()
            except ValueError as exc:
                st.error(str(exc))

            with st.expander("模拟飞书通知预览（Mock，未真实发送）"):
                st.warning(
                    f"{alert.severity} 医护任务\n\n"
                    f"{alert.title}\n\n"
                    f"责任角色：{owner_text(alert.owner_role)} · SLA：{_sla_text(alert.sla_due_at)}"
                )
                st.caption("此卡片只是本地 Mock 展示，未配置 Token，也未完成飞书联调。")

            with st.expander("查看技术记录"):
                st.write(f"Alert ID：`{alert.alert_id}`")
                st.write(f"规则 ID：`{alert.trigger_rule_id}`")
                st.write("Evidence refs：")
                st.code("\n".join(alert.evidence_refs), language=None)

with completed_tab:
    if not resolved_alerts:
        st.info("还没有已完成任务。只有获批规则产生的任务才会进入这里。")
    for alert in resolved_alerts:
        message, observations = _alert_evidence(store, alert)
        actions = store.list_alert_actions(alert.alert_id)
        with st.container(border=True):
            st.markdown(
                f'<div class="cc-kicker">{alert.severity} · 已完成</div>',
                unsafe_allow_html=True,
            )
            st.markdown("### 处理结果已进入医生复诊简报")
            st.success(alert.resolution_reason or "任务已完成并留痕。")
            if message:
                st.markdown(
                    f'<div class="cc-quote">{html.escape(message.message_text)}</div>',
                    unsafe_allow_html=True,
                )
            st.caption(f"完成时间：{alert.resolved_at or '—'}")
            st.markdown("**处理时间线**")
            for action in actions:
                action_label = {
                    "acknowledge": "确认收到",
                    "escalate_to_doctor": "升级医生",
                    "resolve": "记录结果并完成",
                }.get(action.action_type, action.action_type)
                st.write(f"- {action.created_at} · {action_label}：{action.note}")

with st.expander("演示模式说明"):
    render_mode_badges(st)
    st.caption("通知为 Mock；任务、处理记录和审计事件为真实本地持久化。")

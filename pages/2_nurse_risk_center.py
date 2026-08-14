"""Outcome-first nurse work queue for deterministic workflow Alerts."""

from __future__ import annotations

import html
from datetime import datetime, timezone

import streamlit as st

from continucare.adapters.mock_notifier import MockNotifier
from continucare.adapters.sqlite_store import SQLiteStore
from continucare.config import get_settings
from continucare.models import AlertStatus
from continucare.presentation import (
    alert_next_step,
    alert_status_text,
    observation_evidence_text,
    owner_text,
)
from continucare.services.alerts import AlertService
from continucare.ui import inject_global_styles, render_mode_badges


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


st.set_page_config(
    page_title="护士任务中心 · ContinuCare",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.title("护士任务中心")
st.error("仅使用合成数据 · 这里展示的是医护工作任务，不是诊断结论")

store = SQLiteStore(get_settings().db_path)
service = AlertService(store, MockNotifier())
all_alerts = store.list_alerts()
active_alerts = [item for item in all_alerts if item.status != AlertStatus.RESOLVED]
resolved_alerts = [item for item in all_alerts if item.status == AlertStatus.RESOLVED]

st.markdown("## 已获批规则产生的任务")
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
                    service.acknowledge(alert.alert_id, note)
                    st.rerun()
                if escalate.button(
                    "升级医生复核", key=f"escalate_{alert.alert_id}", width="stretch"
                ):
                    service.escalate(alert.alert_id, note)
                    st.rerun()
                if resolve.button(
                    "记录结果并完成", key=f"resolve_{alert.alert_id}", width="stretch"
                ):
                    service.resolve(alert.alert_id, note)
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

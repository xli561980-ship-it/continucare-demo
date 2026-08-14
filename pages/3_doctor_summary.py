"""Doctor-facing, outcome-first pre-visit evidence brief."""

from __future__ import annotations

import html
from datetime import datetime

import streamlit as st

from continucare.adapters.mock_extractor import MockExtractor
from continucare.adapters.mock_notifier import MockNotifier
from continucare.adapters.sqlite_store import SQLiteStore
from continucare.config import get_settings
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.models import AlertStatus
from continucare.presentation import (
    alert_status_text,
    observation_evidence_text,
    observation_text,
    owner_text,
)
from continucare.services.summaries import SummaryService
from continucare.ui import inject_global_styles, render_mode_badges


def _in_period(timestamp: str, start: str, end: str) -> bool:
    item_date = datetime.fromisoformat(timestamp).date().isoformat()
    return start <= item_date <= end


def _source_message(store, alert):
    for ref in alert.evidence_refs:
        if ref.startswith(("message-", "message_")):
            return store.get_message(ref)
    return None


def _alert_observations(store, alert):
    observations = []
    for ref in alert.evidence_refs:
        if ref.startswith(("observation-", "observation_")):
            item = store.get_observation(ref)
            if item:
                observations.append(item)
    return observations


def _human_trigger(alert) -> str:
    return alert.trigger_reason


def _render_alert_evidence_story(store, alert):
    message = _source_message(store, alert)
    observations = _alert_observations(store, alert)
    actions = store.list_alert_actions(alert.alert_id)

    st.markdown('<div class="cc-chain-step"><b>1 · 患者原话</b></div>', unsafe_allow_html=True)
    if message:
        st.markdown(
            f'<div class="cc-quote">{html.escape(message.message_text)}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.warning("原始消息记录缺失。")

    st.markdown('<div class="cc-chain-step"><b>2 · 形成患者报告事实</b></div>', unsafe_allow_html=True)
    for observation in observations:
        st.write(f"- {observation_evidence_text(observation)}")

    st.markdown('<div class="cc-chain-step"><b>3 · 确定性规则创建医护任务</b></div>', unsafe_allow_html=True)
    st.write(f"{alert.severity} · {_human_trigger(alert)}")
    st.caption(
        f"规则 {alert.trigger_rule_id} · 技术条件 {alert.trigger_reason} · "
        f"责任角色 {owner_text(alert.owner_role)} · "
        f"当前状态 {alert_status_text(alert)}"
    )

    st.markdown('<div class="cc-chain-step"><b>4 · 医护处理结果</b></div>', unsafe_allow_html=True)
    if actions:
        for action in actions:
            label = {
                "acknowledge": "确认收到",
                "escalate_to_doctor": "升级医生",
                "resolve": "记录结果并完成",
            }.get(action.action_type, action.action_type)
            st.write(f"- {action.created_at} · {label}：{action.note}")
    else:
        st.warning("任务尚无医护处理记录。")

    with st.expander("查看技术 evidence_refs"):
        st.code("\n".join(alert.evidence_refs), language=None)


st.set_page_config(
    page_title="医生复诊前简报 · ContinuCare",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.title("医生复诊前简报")
st.error("仅使用合成数据 · 不生成诊断、治疗或用药建议 · 默认不写入 EMR")

store = SQLiteStore(get_settings().db_path)
service = SummaryService(store, MockExtractor(), MockNotifier())
patient = store.get_patient(DEMO_PATIENT_ID)

header, generate_col = st.columns([3, 1])
with header:
    st.markdown("### 复诊前 30 秒：重点、处理结果、待确认项")
    if patient:
        st.caption(
            f"{patient.display_name} · Pathway {patient.pathway_code} · "
            f"下次复诊 {patient.next_visit_date}"
        )
with generate_col:
    if st.button("生成 / 刷新简报", type="primary", width="stretch"):
        service.generate(DEMO_PATIENT_ID)
        st.rerun()

summary = store.get_latest_summary(DEMO_PATIENT_ID)
if summary is None:
    with st.container(border=True):
        st.markdown('<div class="cc-kicker">简报尚未生成</div>', unsafe_allow_html=True)
        st.markdown("### 生成后，这里不会只显示一段总结文字")
        preview_one, preview_two, preview_three = st.columns(3)
        preview_one.info("**本期重点**\n\n患者报告发生了什么变化")
        preview_two.success("**已完成处理**\n\n护士任务的状态与最终记录")
        preview_three.warning("**复诊待确认**\n\n数据缺失、未关闭任务和患者问题")
        st.caption("先从首页载入合成场景，在护士端完成处理，再点击“生成 / 刷新简报”。")
    st.stop()

messages = [
    item
    for item in store.list_messages(DEMO_PATIENT_ID)
    if _in_period(item.submitted_at, summary.period_start, summary.period_end)
]
observations = [
    item
    for item in store.list_observations(DEMO_PATIENT_ID)
    if _in_period(item.effective_time, summary.period_start, summary.period_end)
]
alerts = [
    item
    for item in store.list_alerts(DEMO_PATIENT_ID)
    if _in_period(item.created_at, summary.period_start, summary.period_end)
]
resolved_alerts = [item for item in alerts if item.status == AlertStatus.RESOLVED]
unresolved_alerts = [item for item in alerts if item.status != AlertStatus.RESOLVED]
submitted_dates = {datetime.fromisoformat(item.submitted_at).date() for item in messages}
missing_days = max(0, 14 - len(submitted_dates))
content = summary.summary_json

st.success(
    f"复诊简报已生成 · 覆盖 {summary.period_start} 至 {summary.period_end} · "
    "所有关键内容都可以回看证据"
)

report_metric, fact_metric, task_metric, missing_metric = st.columns(4)
report_metric.metric("患者随访", f"{len(messages)} 次")
fact_metric.metric("患者报告事实", f"{len(observations)} 条")
task_metric.metric("医护任务", f"{len(resolved_alerts)}/{len(alerts)} 已完成")
missing_metric.metric("缺少记录", f"{missing_days} 天")

st.markdown("## 医生 30 秒读完")
focus_col, handled_col, confirm_col = st.columns(3)

with focus_col:
    with st.container(border=True):
        st.markdown('<div class="cc-kicker">本期重点</div>', unsafe_allow_html=True)
        st.markdown("### 患者报告了什么")
        if content.key_changes:
            for item in content.key_changes[:4]:
                st.write(f"- {item.text}")
        else:
            st.write("本期没有形成有证据支持的关键变化。")
        st.caption("只展示患者报告事实，不生成诊断。")

with handled_col:
    with st.container(border=True):
        st.markdown('<div class="cc-kicker">已完成处理</div>', unsafe_allow_html=True)
        st.markdown("### 医护团队做了什么")
        if alerts:
            for alert in alerts:
                st.write(f"**{alert.severity} · {alert_status_text(alert)}**")
                if alert.resolution_reason:
                    st.write(alert.resolution_reason)
                elif alert.status == AlertStatus.ESCALATED:
                    st.write("任务已升级医生，尚未关闭。")
                else:
                    st.write("任务尚未形成最终处理结果。")
        else:
            st.write("本期患者报告未创建额外医护任务。")
        st.caption("任务状态与护士处理记录来自数据库。")

with confirm_col:
    with st.container(border=True):
        st.markdown('<div class="cc-kicker">复诊待确认</div>', unsafe_allow_html=True)
        st.markdown("### 复诊时还要看什么")
        if unresolved_alerts:
            st.write(f"- 仍有 {len(unresolved_alerts)} 个医护任务未关闭。")
        if content.patient_questions:
            for item in content.patient_questions:
                st.write(f"- {item.text}")
        if missing_days:
            st.write(f"- 14 天窗口内有 {missing_days} 天没有随访记录。")
        if not unresolved_alerts and not content.patient_questions and not missing_days:
            st.write("当前没有额外待确认项。")
        st.caption("这里记录待核对事项，不给出调整建议。")

st.markdown("## 医护任务的最终结果")
if not alerts:
    st.info("本期没有创建医护任务。患者报告事实仍保留在下方随访时间线中。")
for alert in alerts:
    with st.container(border=True):
        status_col, owner_col = st.columns([3, 1])
        with status_col:
            st.markdown(f"### {alert.severity} 工作流 · {alert_status_text(alert)}")
            st.write(f"触发原因：{_human_trigger(alert)}")
            if alert.resolution_reason:
                st.success(f"最终处理结果：{alert.resolution_reason}")
            else:
                st.warning("尚未形成最终处理结果。")
        with owner_col:
            st.metric("当前责任角色", owner_text(alert.owner_role))

        with st.expander("展开：这条结果是怎么形成的？", expanded=True):
            _render_alert_evidence_story(store, alert)

st.markdown("## 14 天患者报告时间线")
if observations:
    st.dataframe(
        [
            {
                "日期": datetime.fromisoformat(item.effective_time).date().isoformat(),
                "患者报告事实": observation_text(item),
                "原文证据": item.evidence_text,
                "来源": "患者本人",
            }
            for item in observations
        ],
        hide_index=True,
        width="stretch",
    )
else:
    st.caption("当前窗口没有患者报告事实。")

with st.expander("查看完整摘要中的缺失数据和其他待确认内容"):
    st.markdown("**患者主要问题**")
    if content.patient_questions:
        for item in content.patient_questions:
            st.write(f"- {item.text}")
    else:
        st.caption("本期没有记录患者问题。")
    st.markdown("**缺失数据**")
    for item in content.missing_data:
        st.write(f"- {item.text}")
    st.markdown("**医生待确认**")
    if content.doctor_to_confirm:
        for item in content.doctor_to_confirm:
            st.write(f"- {item.text}")
    else:
        st.caption("本期没有其他有证据支持的待确认项。")

st.markdown("## 审阅留痕")
if summary.reviewed_at:
    st.success(f"医生已审阅：{summary.reviewed_at} · 未写入 EMR")
elif st.button("标记这份简报已审阅", type="primary", width="stretch"):
    service.review(summary.summary_id)
    st.rerun()
else:
    st.caption("点击后只写入本地审计事件，不会写入 EMR。")

with st.expander("演示模式说明"):
    render_mode_badges(st)
    st.caption("摘要由本地模板生成；飞书通知为 Mock，未完成真实联调。")

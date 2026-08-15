"""A++ nurse workbench for patient-confirmed routine record checks."""

from __future__ import annotations

import html
from datetime import datetime

import streamlit as st

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.config import get_settings
from continucare.db import utc_now_iso
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.layer4.manual_reviews import (
    ManualReviewQueue,
    communication_readiness,
)
from continucare.layer4.storage import Layer4SQLiteStore
from continucare.services.competition_demo import (
    CompetitionDemoConflict,
    demo_write_guard,
    read_competition_demo,
)
from continucare.services.manual_review_workflow import ManualReviewWorkflowService
from continucare.ui import (
    NURSE_RESULT_BOUNDARY,
    NURSE_ROLE_BOUNDARY,
    NURSE_STOP_CONSEQUENCE,
    NurseTaskProjection,
    inject_global_styles,
    patient_recorded_meaning,
    project_nurse_workbench,
    render_disclosure_controls,
)


TASK_STATUS_LABELS = {
    "requested": "等待接手",
    "received": "已接手",
    "accepted": "已接受",
    "in-progress": "正在核对",
    "completed": "核对已完成",
    "rejected": "已拒绝",
    "cancelled": "已取消",
    "failed": "未完成",
    "entered-in-error": "记录错误",
}


OUTCOME_LABELS = {
    "evidence_consistent": "记录一致",
    "clarification_needed": "需要补充说明",
}


def _stage_value(progress) -> str:
    return getattr(progress.stage, "value", str(progress.stage))


def _task_output(task: dict, code: str) -> str | None:
    matches = []
    for item in task.get("output", []):
        codes = {
            coding.get("code")
            for coding in item.get("type", {}).get("coding", [])
        }
        if code in codes:
            matches.append(
                item.get("valueCode")
                or item.get("valueString")
                or item.get("valueReference", {}).get("reference")
            )
    return str(matches[0]) if len(matches) == 1 and matches[0] else None


def _patient_quote(response: dict | None) -> str | None:
    if not response:
        return None
    values = [
        answer.get("valueString")
        for item in response.get("item", [])
        if item.get("linkId") == "free-text-report"
        for answer in item.get("answer", [])
        if isinstance(answer.get("valueString"), str)
        and answer.get("valueString", "").strip()
    ]
    return values[0].strip() if len(values) == 1 else None


def _format_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "提交时间未记录"
    return parsed.strftime("%Y-%m-%d %H:%M")


def _guarded_write(action, /, *args, **kwargs):
    with demo_write_guard(
        settings.db_path,
        expected_generation=progress.generation,
    ):
        return action(*args, **kwargs)


def _run_action(action, *, feedback: str, **kwargs) -> None:
    try:
        _guarded_write(action, **kwargs)
    except CompetitionDemoConflict:
        st.error("这轮演示已在另一个页面重新开始。当前页面没有继续写入，请刷新后再操作。")
    except (LookupError, ValueError) as exc:
        st.error(str(exc))
    else:
        st.session_state["cc_nurse_notice"] = feedback
        st.session_state.pop("cc_nurse_confirm_action", None)
        st.rerun()


def _build_task_contexts(
    *,
    store: SQLiteStore,
    repository: Layer4SQLiteStore,
    service: ManualReviewWorkflowService,
    tasks: tuple[dict, ...],
) -> dict[str, dict]:
    patient = store.get_patient(DEMO_PATIENT_ID)
    patient_label = patient.display_name if patient else "合成患者"
    meanings = []
    if progress.run_id:
        run = store.get_agent_run(progress.run_id)
        if run:
            meanings = [
                patient_recorded_meaning(item)
                for item in run.output_json.get("candidates", [])
            ]
    confirmed_statement = "；".join(item for item in meanings if item).strip()
    contexts: dict[str, dict] = {}
    for task in tasks:
        task_id = str(task.get("id") or "")
        response_ref = str(task.get("reasonReference", {}).get("reference") or "")
        response_id = (
            response_ref.split("/", 1)[1]
            if response_ref.startswith("QuestionnaireResponse/")
            else ""
        )
        response = store.get_questionnaire_response(response_id) if response_id else None
        communications = service.list_communications_for_task(
            DEMO_PATIENT_ID,
            task_id,
        )
        communication = communications[0] if len(communications) == 1 else None
        communication_text = None
        if communication:
            payload = communication.get("payload", [])
            if len(payload) == 1:
                communication_text = payload[0].get("contentString")
        history = sorted(
            (
                item
                for item in repository.list_fhir_resources(
                    patient_id=DEMO_PATIENT_ID,
                    resource_type="Task",
                    current_only=False,
                )
                if item.get("id") == task_id
            ),
            key=lambda item: int(item.get("meta", {}).get("versionId", "0")),
        )
        outcome = _task_output(task, "review-outcome")
        contexts[task_id] = {
            "patient_label": patient_label,
            "original_quote": _patient_quote(response),
            "confirmed_statement": confirmed_statement,
            "outcome_label": OUTCOME_LABELS.get(outcome, outcome),
            "review_note": _task_output(task, "review-note"),
            "communication_text": communication_text,
            "communication_readiness": (
                communication_readiness(communication) if communication else None
            ),
            "has_pending_brief": (
                progress.task_id == task_id
                and _stage_value(progress) == "doctor_brief_pending"
            ),
            "history": tuple(
                (
                    f"v{item.get('meta', {}).get('versionId', '—')}",
                    TASK_STATUS_LABELS.get(
                        str(item.get("status") or ""),
                        str(item.get("status") or "状态未记录"),
                    ),
                    str(item.get("meta", {}).get("lastUpdated") or "时间未记录"),
                )
                for item in history
            ),
        }
    return contexts


def _render_queue(
    tasks: tuple[NurseTaskProjection, ...],
    *,
    selected_task_id: str | None,
    prefix: str,
) -> None:
    if not tasks:
        st.markdown(
            '<div class="cc-nurse-empty">这里暂时没有记录。</div>',
            unsafe_allow_html=True,
        )
        return
    for index, item in enumerate(tasks):
        selected = item.task_id == selected_task_id
        key_prefix = "cc_nurse_task_selected" if selected else "cc_nurse_task"
        if st.button(
            item.patient_label,
            key=f"{key_prefix}_{prefix}_{index}",
            width="stretch",
        ):
            st.session_state["cc_nurse_selected_task"] = item.task_id
            st.session_state.pop("cc_nurse_confirm_action", None)
            st.rerun()
        st.markdown(
            '<div class="cc-nurse-task-meta">'
            f"新确认记录 · {html.escape(item.status_title)}<br>"
            f"{html.escape(_format_time(item.submitted_at))}"
            "</div>",
            unsafe_allow_html=True,
        )


def _render_disclosure(task: NurseTaskProjection, *, area: str) -> None:
    choice = st.query_params.get("cc_nurse_disclosure")
    options = (
        (("patient", "查看原始来源详情"),)
        if area == "source"
        else (("history", "查看先前动作"), ("technical", "技术详情"))
    )
    panel_id = (
        "cc-nurse-source-panel" if area == "source" else "cc-nurse-record-panel"
    )
    selected = render_disclosure_controls(
        st,
        query_parameter="cc_nurse_disclosure",
        page_path="/nurse_risk_center",
        options=options,
        selected=str(choice) if choice is not None else None,
        aria_label="护士任务来源" if area == "source" else "护士任务进一步查看",
        panel_id=panel_id,
    )
    if area == "source" and selected == "patient":
        quote = task.original_quote or "患者原话暂时无法读取。"
        st.markdown(
            f'<div id="{panel_id}" class="cc-nurse-result-boundary">'
            f"<strong>患者在本轮确认</strong><br>{html.escape(quote)}"
            "</div>",
            unsafe_allow_html=True,
        )
    elif area == "record" and selected == "history":
        history = "".join(
            (
                '<div class="cc-nurse-history">'
                f"<strong>{html.escape(version)}</strong>"
                f"<span>{html.escape(status)}</span>"
                f"<span>{html.escape(occurred_at)}</span>"
                "</div>"
            )
            for version, status, occurred_at in task.history
        )
        if not history:
            history = "<p>还没有先前动作。</p>"
        st.markdown(
            f'<section id="{panel_id}" aria-label="先前动作">{history}</section>',
            unsafe_allow_html=True,
        )
    elif area == "record" and selected == "technical":
        st.markdown(
            f'<div id="{panel_id}" class="cc-nurse-technical">'
            f"Task：<code>{html.escape(task.task_id)}</code>"
            "</div>",
            unsafe_allow_html=True,
        )
        for reference in task.technical_references:
            st.code(reference, language=None)


def _render_communication(task: NurseTaskProjection) -> None:
    if not task.communication_text:
        return
    marker = "未发送"
    title = "沟通文字草稿"
    st.markdown(
        '<section class="cc-nurse-communication">'
        f"<h3>{html.escape(title)}</h3>"
        f"<p>{html.escape(task.communication_text)}</p>"
        f'<p class="cc-nurse-mock">{html.escape(marker)}</p>'
        "</section>",
        unsafe_allow_html=True,
    )


def _render_primary_action(task: NurseTaskProjection) -> None:
    if task.primary_action is None:
        return
    st.markdown(
        '<div class="cc-nurse-action-title">当前唯一主动作</div>',
        unsafe_allow_html=True,
    )
    if task.primary_action == "open_doctor":
        with st.container(key="cc_nurse_primary_link"):
            st.page_link(
                "pages/3_doctor_summary.py",
                label="进入复诊准备",
                width="stretch",
            )
        return

    action_notes = {
        "acknowledge": "已接手这条例行记录核对。",
        "start": "已开始核对患者确认的记录。",
        "record_outcome": "已核对患者原话、确认结果与最终记录。",
        "approve_draft": "已逐字核对这段沟通文字。",
    }
    if task.primary_action == "record_outcome":
        outcome = st.radio(
            "核对结果",
            options=tuple(OUTCOME_LABELS),
            format_func=lambda value: OUTCOME_LABELS[value],
            key=f"cc_nurse_outcome_{task.task_id}",
            horizontal=True,
        )
    else:
        outcome = None
    note = action_notes[task.primary_action]
    with st.container(key="cc_nurse_primary"):
        display_label = {
            "acknowledge": "开始核对记录",
            "start": "核对记录与来源",
            "record_outcome": "保存核对结果",
            "approve_draft": "确认文字已核对",
        }.get(task.primary_action, task.primary_label or "继续")
        clicked = st.button(
            display_label,
            key=f"cc_nurse_primary_button_{task.task_id}_{task.primary_action}",
            type="primary",
            width="stretch",
        )
    if task.primary_action == "approve_draft":
        st.caption("这一步只确认文字可进入后续演示流程，不会发送给患者。")
    if not clicked:
        return
    common = {
        "patient_id": DEMO_PATIENT_ID,
        "task_id": task.task_id,
        "note": note,
        "occurred_at": utc_now_iso(),
    }
    if task.primary_action == "acknowledge":
        _run_action(
            manual_service.acknowledge,
            feedback="已接手，下一步开始核对。",
            **common,
        )
    elif task.primary_action == "start":
        _run_action(
            manual_service.start,
            feedback="已开始核对。",
            **common,
        )
    elif task.primary_action == "record_outcome":
        _run_action(
            manual_service.record_outcome,
            feedback="结果已保存，文字仍待人工核对。",
            outcome=outcome,
            **common,
        )
    elif task.primary_action == "approve_draft":
        communications = manual_service.list_communications_for_task(
            DEMO_PATIENT_ID,
            task.task_id,
        )
        if len(communications) != 1:
            st.error("沟通文字状态无法安全确认；页面没有继续写入。")
            return
        _run_action(
            manual_service.approve_draft,
            feedback="文字已核对；模拟，未真实发送。",
            communication_id=communications[0]["id"],
            **common,
        )


def _render_secondary_actions(task: NurseTaskProjection) -> None:
    if not task.secondary_actions:
        return
    columns = st.columns(len(task.secondary_actions))
    for column, (action, label) in zip(columns, task.secondary_actions):
        with column, st.container(key=f"cc_nurse_secondary_{action}"):
            if st.button(
                label,
                key=f"cc_nurse_secondary_button_{task.task_id}_{action}",
                width="stretch",
            ):
                st.session_state["cc_nurse_confirm_action"] = (
                    f"{task.task_id}:{action}"
                )
                st.rerun()

    selection = st.session_state.get("cc_nurse_confirm_action")
    if selection not in {
        f"{task.task_id}:{action}" for action, _ in task.secondary_actions
    }:
        return
    action = selection.rsplit(":", 1)[1]
    label = dict(task.secondary_actions)[action]
    st.markdown(
        f'<div class="cc-nurse-consequence">{html.escape(NURSE_STOP_CONSEQUENCE)}</div>',
        unsafe_allow_html=True,
    )
    note = st.text_area(
        "停止处理说明（必填）",
        value="",
        key=f"cc_nurse_stop_note_{task.task_id}_{action}",
        placeholder="请说明为什么停止后续处理。",
    )
    with st.container(key=f"cc_nurse_confirm_{action}"):
        confirmed = st.button(
            f"确认{label}",
            key=f"cc_nurse_confirm_button_{task.task_id}_{action}",
            width="stretch",
            disabled=not note.strip(),
        )
    if confirmed:
        _run_action(
            manual_service.reject if action == "reject" else manual_service.cancel,
            feedback="流程已停止；已有记录仍保留供追溯。",
            patient_id=DEMO_PATIENT_ID,
            task_id=task.task_id,
            note=note,
            occurred_at=utc_now_iso(),
        )


def _render_outcomes(task: NurseTaskProjection) -> None:
    if task.stop_reason:
        st.markdown(f"**原因：** {task.stop_reason}")
    if not task.produced and not task.not_produced:
        return
    produced = "".join(f"<li>{html.escape(item)}</li>" for item in task.produced)
    not_produced = "".join(
        f"<li>{html.escape(item)}</li>" for item in task.not_produced
    )
    st.markdown(
        '<div class="cc-nurse-outcomes">'
        f"<section><h3>已经产生</h3><ul>{produced or '<li>无</li>'}</ul></section>"
        f"<section><h3>没有产生</h3><ul>{not_produced or '<li>无</li>'}</ul></section>"
        "</div>",
        unsafe_allow_html=True,
    )


def _render_detail(task: NurseTaskProjection | None) -> None:
    if task is None:
        st.markdown(
            '<div class="cc-nurse-empty">选择一条记录后，这里会显示当前动作。</div>',
            unsafe_allow_html=True,
        )
        return
    review_label = "待人工核对" if task.queue == "pending" else task.status_title
    original_quote = task.original_quote or "患者原话暂时无法读取"
    st.markdown(
        f"""
        <section class="cc-nurse-review-head" aria-live="polite">
          <span class="cc-nurse-review-badge cc-nurse-review-badge--{html.escape(task.tone)}">{html.escape(review_label)}</span>
          <div>
            <h2>核对记录与来源</h2>
            <p>{html.escape(task.status_detail)}</p>
          </div>
        </section>
        <section class="cc-nurse-evidence-grid" aria-label="记录核对证据">
          <article>
            <span>患者原话</span>
            <p>“{html.escape(original_quote)}”</p>
          </article>
          <article>
            <span>已确认记录</span>
            <p>{html.escape(task.confirmed_statement)}</p>
          </article>
          <article>
            <span>来源</span>
            <p>患者本人确认</p>
          </article>
        </section>
        <section class="cc-nurse-why">
          <span aria-hidden="true">?</span>
          <div><strong>为什么交给我</strong><p>确认记录与患者原话一致，并保留来源。</p></div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    _render_communication(task)
    _render_primary_action(task)
    _render_disclosure(task, area="source")
    if task.outcome_label:
        st.markdown(f"**核对结果：** {task.outcome_label}")
    if task.review_note:
        st.caption(f"处理说明：{task.review_note}")
    _render_secondary_actions(task)
    _render_outcomes(task)
    _render_disclosure(task, area="record")
    with st.container(key="cc_nurse_record_link"):
        st.page_link(
            "pages/4_audit_log.py",
            label="查看完整接力记录",
            width="stretch",
        )


st.set_page_config(
    page_title="随访待办 · ContinuCare",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.markdown('<span class="cc-nurse-shell" aria-hidden="true"></span>', unsafe_allow_html=True)

settings = get_settings()
progress = read_competition_demo(settings.db_path)
manual_service = None
tasks: tuple[dict, ...] = ()
contexts: dict[str, dict] = {}
if settings.db_path.is_file():
    store = SQLiteStore(settings.db_path, initialize=False)
    repository = Layer4SQLiteStore(settings.db_path, initialize=False)
    manual_service = ManualReviewWorkflowService(store, layer4_store=repository)
    tasks = tuple(ManualReviewQueue(repository).list_for_patient(DEMO_PATIENT_ID))
    contexts = _build_task_contexts(
        store=store,
        repository=repository,
        service=manual_service,
        tasks=tasks,
    )

selected_hint = st.session_state.get("cc_nurse_selected_task")
projection = project_nurse_workbench(
    progress,
    tasks=tasks,
    task_contexts=contexts,
    selected_task_id=selected_hint,
)
if projection.selected_task_id:
    st.session_state["cc_nurse_selected_task"] = projection.selected_task_id

st.markdown(
    """
    <header class="cc-role-topbar">
      <span class="cc-role-topbar-brand"><i aria-hidden="true">C</i> ContinuCare</span>
      <span class="cc-role-topbar-badge">合成数据演示</span>
    </header>
    """,
    unsafe_allow_html=True,
)
title_column, count_column = st.columns([4, 1], vertical_alignment="center")
with title_column:
    st.title("随访待办")
    st.caption("核对记录与来源，再交给复诊准备。")
with count_column:
    pending_count = len(projection.pending_tasks)
    st.markdown(
        f'<div class="cc-nurse-count"><strong>{pending_count}</strong> 条待核对</div>',
        unsafe_allow_html=True,
    )

notice = st.session_state.pop("cc_nurse_notice", None)
if notice:
    st.info(notice)
if projection.notice_title:
    tone_class = "error" if projection.tone == "error" else "neutral"
    st.markdown(
        f'<section class="cc-nurse-status cc-nurse-status--{tone_class}">'
        f"<h2>{html.escape(projection.notice_title)}</h2>"
        f"<p>{html.escape(projection.notice_detail or '')}</p>"
        "</section>",
        unsafe_allow_html=True,
    )

all_tasks = (*projection.pending_tasks, *projection.completed_tasks)
selected_task = next(
    (item for item in all_tasks if item.task_id == projection.selected_task_id),
    None,
)
with st.container(key="cc_nurse_workspace"):
    queue_column, detail_column = st.columns([3, 7], gap="small")
    with queue_column:
        st.markdown(
            '<div class="cc-nurse-sort">按提交时间排序 · 最早提交的记录在前</div>',
            unsafe_allow_html=True,
        )
        queue_labels = [
            f"待处理（{len(projection.pending_tasks)}）",
            f"已处理（{len(projection.completed_tasks)}）",
        ]
        pending_tab, completed_tab = st.tabs(
            queue_labels,
            default=(
                queue_labels[0] if projection.pending_tasks else queue_labels[1]
            ),
            key="cc_nurse_queue_tabs",
        )
        with pending_tab:
            _render_queue(
                projection.pending_tasks,
                selected_task_id=projection.selected_task_id,
                prefix="pending",
            )
        with completed_tab:
            _render_queue(
                projection.completed_tasks,
                selected_task_id=projection.selected_task_id,
                prefix="completed",
            )
    with detail_column:
        _render_detail(selected_task)

with st.expander("演示边界", expanded=False):
    st.caption(NURSE_ROLE_BOUNDARY)
    st.caption(NURSE_RESULT_BOUNDARY)
    if progress.alert_count == 0 and progress.approved_clinical_rule_count == 0:
        st.caption("临床警报未启用：0 条获批规则，0 条 Alert。")
    else:
        st.caption(
            f"当前记录有 {progress.approved_clinical_rule_count} 条获批规则、"
            f"{progress.alert_count} 条 Alert；不与例行记录核对队列合并。"
        )
    st.caption("仅使用合成数据；页面没有真实发送或真实外部写入。")

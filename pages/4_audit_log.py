"""A++ read-only record trace projected from durable local facts."""

from __future__ import annotations

import html
import json
import sqlite3

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.config import get_settings
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.layer4.storage import Layer4SQLiteStore
from continucare.services.competition_demo import read_competition_demo
from continucare.ui import (
    AuditTrailProjection,
    inject_global_styles,
    project_audit_trail,
    render_disclosure_controls,
)


AUDIT_BOUNDARY = "合成数据 · 无临床评估 · 无风险分级 · 无真实发送 · 外部系统为 Mock/disabled。"


def _guide_link(label: str = "返回演示首页") -> None:
    try:
        st.page_link("app.py", label=label, width="stretch")
    except (StreamlitPageNotFoundError, KeyError):
        st.markdown(f"[{label}](/)")


def _list_markup(items: tuple[str, ...], *, empty: str) -> str:
    values = items or (empty,)
    return "".join(f"<li>{html.escape(item)}</li>" for item in values)


def _render_conclusion(projection: AuditTrailProjection) -> None:
    st.markdown(
        f"""
        <section class="cc-audit-conclusion cc-audit-conclusion--{projection.tone}" aria-live="polite">
          <p class="cc-audit-label">当前结论</p>
          <h2>{html.escape(projection.title)}</h2>
          <div class="cc-audit-reason">
            <span>直接原因</span>
            <strong>原因：{html.escape(projection.reason)}</strong>
          </div>
          <p>{html.escape(projection.explanation)}</p>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_products(projection: AuditTrailProjection) -> None:
    st.markdown(
        f"""
        <section class="cc-audit-products" aria-label="本轮产生与未产生的内容">
          <div>
            <h2>已经产生</h2>
            <ul>{_list_markup(projection.produced, empty="尚未留下业务记录")}</ul>
          </div>
          <div>
            <h2>没有产生</h2>
            <ul>{_list_markup(projection.not_produced, empty="没有可安全确认的未产生项")}</ul>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_actions(projection: AuditTrailProjection) -> None:
    st.markdown("## 参与者动作")
    if not projection.actions:
        st.markdown(
            '<p class="cc-audit-empty">这一轮还没有留下流程动作；页面没有补造默认步骤。</p>',
            unsafe_allow_html=True,
        )
        return
    rows = "".join(
        "<tr>"
        f"<td data-label=\"序号\">{item.sequence}</td>"
        f"<td data-label=\"参与者\">{html.escape(item.participant)}</td>"
        f"<td data-label=\"动作\">{html.escape(item.action)}</td>"
        f"<td data-label=\"时间\">{html.escape(item.time)}</td>"
        "</tr>"
        for item in projection.actions
    )
    st.markdown(
        f"""
        <div class="cc-audit-table-wrap">
          <table class="cc-audit-table">
            <thead><tr><th>序号</th><th>参与者</th><th>动作</th><th>时间</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_disclosure_controls() -> str | None:
    options = (
        ("why", "为什么停在这里"),
        ("relations", "查看资源关系"),
        ("technical", "查看技术详情"),
    )
    return render_disclosure_controls(
        st,
        query_parameter="cc_audit_disclosure",
        page_path="/audit_log",
        options=options,
        aria_label="记录追溯进一步查看",
        panel_id="cc-audit-disclosure-panel",
    )


def _render_why(projection: AuditTrailProjection) -> None:
    rows = "".join(
        "<li>"
        f"<strong>{item.sequence}. {html.escape(item.participant)} · {html.escape(item.action)}</strong>"
        f"<span>{html.escape(item.effect)}</span>"
        + (
            f"<small>{html.escape(item.before_state)} → {html.escape(item.after_state)}</small>"
            if item.before_state and item.after_state
            else ""
        )
        + "</li>"
        for item in projection.actions
    )
    st.markdown(
        f"""
        <section id="cc-audit-disclosure-panel" class="cc-audit-disclosure" aria-label="停止或完成原因说明">
          <h2>为什么停在这里</h2>
          <p><strong>原因：{html.escape(projection.reason)}</strong></p>
          <p>{html.escape(projection.explanation)}</p>
          <ol class="cc-audit-effects">{rows}</ol>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_relations(projection: AuditTrailProjection) -> None:
    rows = "".join(
        f"<li>{html.escape(item)}</li>" for item in projection.resource_relations
    )
    if not rows:
        rows = "<li>当前没有可安全连接的资源关系。</li>"
    st.markdown(
        f"""
        <section id="cc-audit-disclosure-panel" class="cc-audit-disclosure" aria-label="资源关系">
          <h2>资源关系</h2>
          <p>这里只用业务语言展示已经存在的关系，不把审计事件本身当作临床事实。</p>
          <ol class="cc-audit-relations">{rows}</ol>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_technical(projection: AuditTrailProjection) -> None:
    st.markdown(
        '<section id="cc-audit-disclosure-panel" class="cc-audit-disclosure"><h2>技术详情</h2>'
        '<p>以下字段用于排查和资料审计；默认不展开原始 JSON。</p></section>',
        unsafe_allow_html=True,
    )
    if not projection.actions:
        st.info("当前没有技术事件记录。")
        return
    labels = tuple(
        f"{item.sequence}. {item.participant} · {item.action} · {item.time}"
        for item in projection.actions
    )
    selected_label = st.selectbox("选择一项动作记录", labels)
    item = projection.actions[labels.index(selected_label)]
    st.markdown(
        f"""
        <dl class="cc-audit-technical">
          <div><dt>event_type</dt><dd><code>{html.escape(item.event_type)}</code></dd></div>
          <div><dt>event_id</dt><dd><code>{html.escape(item.event_id)}</code></dd></div>
          <div><dt>entity</dt><dd><code>{html.escape(item.entity_type)} / {html.escape(item.entity_id)}</code></dd></div>
          <div><dt>resource type</dt><dd><code>{html.escape(item.resource_type)}</code></dd></div>
          <div><dt>version</dt><dd><code>{html.escape(item.resource_version or '未记录')}</code></dd></div>
          <div><dt>Provenance</dt><dd><code>{html.escape('；'.join(item.provenance_refs) or '未关联')}</code></dd></div>
        </dl>
        """,
        unsafe_allow_html=True,
    )
    with st.expander("查看原始 details JSON"):
        st.code(
            json.dumps(item.details_json, ensure_ascii=False, indent=2, sort_keys=True),
            language="json",
        )


st.set_page_config(
    page_title="记录追溯 · ContinuCare",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.markdown('<div class="cc-audit-shell" aria-hidden="true"></div>', unsafe_allow_html=True)
st.title("记录追溯")
st.markdown(
    f'<p class="cc-audit-boundary">{html.escape(AUDIT_BOUNDARY)}</p>',
    unsafe_allow_html=True,
)

settings = get_settings()
progress = read_competition_demo(settings.db_path)
events = ()
tasks = ()
provenances = ()
if settings.db_path.is_file():
    try:
        events = tuple(
            SQLiteStore(settings.db_path, initialize=False).list_audit_events(
                DEMO_PATIENT_ID
            )
        )
        repository = Layer4SQLiteStore(settings.db_path, initialize=False)
        tasks = tuple(
            repository.list_fhir_resources(
                patient_id=DEMO_PATIENT_ID,
                resource_type="Task",
            )
        )
        provenances = tuple(
            repository.list_fhir_resources(
                patient_id=DEMO_PATIENT_ID,
                resource_type="Provenance",
                current_only=False,
            )
        )
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        progress = progress.model_copy(
            update={"integrity_issue": "audit projection source unavailable"}
        )

projection = project_audit_trail(
    progress,
    events=events,
    tasks=tasks,
    provenances=provenances,
)

with st.container(key="cc_audit_page"):
    _render_conclusion(projection)
    _render_products(projection)
    _render_actions(projection)
    st.markdown(
        '<p class="cc-audit-disclosure-intro">需要核对时再进入下一层；三个入口一次只展开一个。</p>',
        unsafe_allow_html=True,
    )
    disclosure = _render_disclosure_controls()
    if disclosure == "why":
        _render_why(projection)
    elif disclosure == "relations":
        _render_relations(projection)
    elif disclosure == "technical":
        _render_technical(projection)

    st.markdown(
        '<p class="cc-audit-fixed-boundary">流程记录说明谁在何时做过什么；它不证明临床事实成立。'
        '本页只读，查看、刷新和展开不会写入数据库。</p>',
        unsafe_allow_html=True,
    )
    if projection.show_guide_link:
        with st.container(key="cc_audit_guide_link"):
            _guide_link()

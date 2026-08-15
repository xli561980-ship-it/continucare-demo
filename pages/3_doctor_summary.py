"""A++ doctor visit brief projected from immutable persisted facts."""

from __future__ import annotations

import html
import json
import sqlite3

import streamlit as st

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.config import get_settings
from continucare.db import utc_now_iso
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.errors import ConcurrentWriteConflict
from continucare.layer4 import (
    BRIEF_SUMMARY_KIND,
    DoctorReviewService,
    DoctorWorkbenchService,
    Layer4InputReader,
    Layer4SQLiteStore,
    ManualReviewBriefService,
    WorkbenchAccessContext,
    WorkbenchPurpose,
    WorkbenchRole,
)
from continucare.layer4.contracts import DoctorReviewDecision
from continucare.pathways import load_builtin_pathways
from continucare.services.competition_demo import (
    CompetitionDemoConflict,
    CompetitionDemoProgress,
    demo_write_guard,
    read_competition_demo,
)
from continucare.ui import (
    DOCTOR_DECISION_ACTIONS,
    DOCTOR_ROLE_BOUNDARY,
    build_doctor_modified_items,
    doctor_summary_text,
    inject_global_styles,
    patient_recorded_meaning,
    project_doctor_summary_wording,
    project_doctor_visit_brief,
    render_disclosure_controls,
)


DOCTOR_REFERENCE = "Practitioner/synthetic-doctor-review"


def _access() -> WorkbenchAccessContext:
    return WorkbenchAccessContext(
        actor_reference=DOCTOR_REFERENCE,
        role=WorkbenchRole.DOCTOR,
        purpose=WorkbenchPurpose.TREATMENT,
        permitted_patient_ids=[DEMO_PATIENT_ID],
        identity_verified=True,
    )


def _version_number(value: str) -> tuple[int, str]:
    try:
        return int(value), value
    except ValueError:
        return -1, value


def _patient_quote(response: dict | None) -> str | None:
    if not response:
        return None
    items = [
        item
        for item in response.get("item", [])
        if item.get("linkId") == "free-text-report"
    ]
    if len(items) != 1:
        return None
    answers = items[0].get("answer", [])
    if len(answers) != 1:
        return None
    value = answers[0].get("valueString")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _confirmed_statement(store: SQLiteStore, progress) -> str | None:
    if not progress.run_id:
        return None
    run = store.get_agent_run(progress.run_id)
    if run is None:
        return None
    meanings = [
        patient_recorded_meaning(item)
        for item in run.output_json.get("candidates", [])
    ]
    value = "；".join(item for item in meanings if item).strip()
    return value or None


def _task_quote(store: SQLiteStore, task: dict | None) -> str | None:
    reference = str((task or {}).get("reasonReference", {}).get("reference") or "")
    if not reference.startswith("QuestionnaireResponse/"):
        return None
    response = store.get_questionnaire_response(reference.split("/", 1)[1])
    return _patient_quote(response)


def _find_review(repository: Layer4SQLiteStore, summary):
    if summary is None:
        return None
    matches = [
        item
        for item in repository.list_contracts(
            "doctor_review",
            patient_id=DEMO_PATIENT_ID,
            current_only=False,
        )
        if item.result_summary_id == summary.summary_id
        and item.result_summary_version == summary.version
    ]
    return max(matches, key=lambda item: item.reviewed_at, default=None)


def _source_summary(repository: Layer4SQLiteStore, review):
    if review is None:
        return None
    return repository.get_contract(
        "summary_draft",
        review.summary_id,
        version=review.summary_version,
    )


def _previous_summary(repository: Layer4SQLiteStore, summary):
    if summary is None:
        return None
    candidates = [
        item
        for item in repository.list_contracts(
            "summary_draft",
            patient_id=DEMO_PATIENT_ID,
            current_only=False,
        )
        if item.summary_id == summary.summary_id
        and _version_number(item.version) < _version_number(summary.version)
    ]
    return max(candidates, key=lambda item: _version_number(item.version), default=None)


def _guarded_generate(briefs, *, task_id: str, progress) -> None:
    with demo_write_guard(
        settings.db_path,
        expected_generation=progress.generation,
    ):
        briefs.generate(
            patient_id=DEMO_PATIENT_ID,
            task_id=task_id,
            generated_at=utc_now_iso(),
        )


def _guarded_review(
    briefs,
    repository,
    *,
    summary,
    progress,
    decision: DoctorReviewDecision,
    note: str | None,
    modified_items=None,
) -> None:
    reviewed_at = utc_now_iso()
    with demo_write_guard(
        settings.db_path,
        expected_generation=progress.generation,
    ):
        latest_progress = read_competition_demo(settings.db_path)
        if latest_progress.is_terminal:
            raise ConcurrentWriteConflict("这轮记录已进入只读终态，请刷新后查看。")
        current = repository.get_contract("summary_draft", summary.summary_id)
        if current is None or current.version != summary.version:
            raise ConcurrentWriteConflict("这版速览已经变化，请刷新后再决定。")
        if briefs.is_stale(summary, as_of=reviewed_at):
            raise ConcurrentWriteConflict(
                "患者确认、护理动作或沟通文字已经变化；请先按当前记录生成新版本。"
            )
        DoctorReviewService(repository).review(
            summary_id=summary.summary_id,
            summary_version=summary.version,
            reviewer_reference=DOCTOR_REFERENCE,
            decision=decision,
            reviewed_at=reviewed_at,
            note=note,
            modified_items=modified_items,
        )


def _show_feedback() -> None:
    value = st.session_state.pop("cc_doctor_feedback", None)
    if value:
        st.markdown(
            f'<p class="cc-doctor-feedback" role="status">{html.escape(value)}</p>',
            unsafe_allow_html=True,
        )


def _render_notice(projection) -> None:
    if not projection.notice_title:
        return
    detail = (
        f"<p>{html.escape(projection.notice_detail)}</p>"
        if projection.notice_detail
        else ""
    )
    st.markdown(
        f"""
        <section class="cc-doctor-notice cc-doctor-notice--{projection.tone}" aria-live="polite">
          <h2>{html.escape(projection.notice_title)}</h2>
          {detail}
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_facts(projection) -> None:
    display_labels = {
        "患者确认的表述": "本轮患者确认",
        "护理动作": "护理接力",
        "当前边界": "复诊时需要补充",
    }
    icons = {
        "患者确认的表述": "✓",
        "护理动作": "↗",
        "当前边界": "!",
    }
    display_values = {
        "尚未提供临床评估": "尚无临床评估，需进一步询问",
        "护士已完成记录核对": "记录已由护士核对；沟通文字未发送",
    }
    rows = "".join(
        "<article class=\"cc-doctor-fact\">"
        f"<span aria-hidden=\"true\">{icons.get(item.label, '·')}</span>"
        "<div>"
        f"<dt>{html.escape(display_labels.get(item.label, item.label))}</dt>"
        f"<dd>{html.escape(display_values.get(item.value, item.value))}</dd>"
        "</div></article>"
        for item in projection.facts
    )
    st.markdown(
        f"""
        <section class="cc-doctor-brief-card" aria-label="30 秒速览">
          <header><span aria-hidden="true">30</span><div><p>PRE-VISIT BRIEF</p><h2>30 秒速览</h2></div></header>
          <dl class="cc-doctor-facts">{rows}</dl>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_source_rail(projection) -> str | None:
    st.markdown(
        """
        <section class="cc-doctor-evidence-head">
          <p>EVIDENCE CHAIN</p>
          <h2>证据链</h2>
        </section>
        <ol class="cc-doctor-evidence-chain">
          <li><span>01</span><strong>患者原话</strong></li>
          <li><span>02</span><strong>本人确认</strong></li>
          <li><span>03</span><strong>护士核对</strong></li>
        </ol>
        <p class="cc-doctor-source-title">查看来源详情</p>
        """,
        unsafe_allow_html=True,
    )
    source_options = tuple(
        (key, label) for key, label in projection.source_actions if key != "audit"
    )
    selected = render_disclosure_controls(
        st,
        query_parameter="cc_doctor_source",
        page_path="/doctor_summary",
        options=source_options,
        aria_label="复诊速览来源",
        panel_id="cc-doctor-source-panel",
        stacked=True,
    )
    for key, label in projection.source_actions:
        if key == "audit":
            with st.container(key="cc_doctor_record_link"):
                st.page_link("pages/4_audit_log.py", label=label, width="stretch")
    if projection.source_notice:
        st.markdown(
            f'<p class="cc-doctor-source-notice">{html.escape(projection.source_notice)}</p>',
            unsafe_allow_html=True,
        )
    return selected


def _render_technical_details(summary, trace) -> None:
    if summary is None:
        return
    with st.expander("技术详情"):
        st.caption(
            f"Summary/{summary.summary_id} · 版本 {summary.version} · "
            f"状态 {summary.status.value}"
        )
        references = sorted(
            {
                (
                    evidence.resource.reference
                    if not evidence.resource.version_id
                    else f"{evidence.resource.reference}/_history/{evidence.resource.version_id}"
                )
                for item in summary.items
                for evidence in item.evidence_refs
            }
        )
        st.code("\n".join(references), language=None)
        if trace is None:
            st.caption("完整技术来源当前不可用；系统没有补造内容。")
            return
        if trace.degraded:
            st.error("部分技术来源暂时无法读取。")
        if trace.unresolved_references:
            st.warning("存在尚未解析的技术来源。")
            st.code("\n".join(trace.unresolved_references), language=None)
        if trace.truncated:
            st.warning("技术来源达到展开上限，当前展示不完整。")
        if trace.artifacts:
            options = [item.reference for item in trace.artifacts]
            selected = st.selectbox(
                "查看一条原始记录",
                options=options,
                key=f"cc_doctor_technical_{summary.summary_id}_{summary.version}",
            )
            artifact = next(item for item in trace.artifacts if item.reference == selected)
            st.code(
                json.dumps(artifact.payload, ensure_ascii=False, indent=2),
                language="json",
            )


def _render_source_detail(selected, projection, summary, trace) -> None:
    if selected is None:
        return
    labels = dict(projection.source_actions)
    st.markdown(
        f'<section id="cc-doctor-source-panel" class="cc-doctor-disclosure">'
        f'<h2>{html.escape(labels[selected])}</h2></section>',
        unsafe_allow_html=True,
    )
    if selected == "patient":
        quote = projection.patient_quote or "患者原话暂时无法读取。"
        st.markdown(
            f"""
            <blockquote class="cc-doctor-quote">“{html.escape(quote)}”</blockquote>
            <p class="cc-doctor-source-caption">患者在本轮明确确认的原话来源。</p>
            """,
            unsafe_allow_html=True,
        )
    elif selected == "nursing":
        st.markdown(
            f'<p class="cc-doctor-source-copy">{html.escape(projection.nursing_detail or "护理动作详情暂时无法读取。")}</p>',
            unsafe_allow_html=True,
        )
    elif selected == "previous":
        st.markdown(
            f'<p class="cc-doctor-source-copy">{html.escape(projection.previous_summary_text or "当前没有更早一版措辞。")}</p>',
            unsafe_allow_html=True,
        )
    _render_technical_details(summary, trace)


def _render_outcomes(projection) -> None:
    if not projection.produced and not projection.not_produced:
        return
    produced = "".join(f"<li>{html.escape(item)}</li>" for item in projection.produced)
    not_produced = "".join(
        f"<li>{html.escape(item)}</li>" for item in projection.not_produced
    )
    st.markdown(
        f"""
        <div class="cc-doctor-outcomes">
          <section><h3>已经产生</h3><ul>{produced or '<li>无</li>'}</ul></section>
          <section><h3>没有产生</h3><ul>{not_produced or '<li>无</li>'}</ul></section>
        </div>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="复诊准备 · ContinuCare",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.markdown('<span class="cc-doctor-shell" aria-hidden="true"></span>', unsafe_allow_html=True)
st.markdown(
    """
    <header class="cc-role-topbar">
      <span class="cc-role-topbar-brand"><i aria-hidden="true">C</i> ContinuCare</span>
      <span class="cc-role-topbar-badge">合成数据演示</span>
    </header>
    """,
    unsafe_allow_html=True,
)
st.title("复诊准备")
st.caption("在接诊前，先看清已确认事实、护理接力和仍需补充的信息。")
_show_feedback()

settings = get_settings()
source_error = None
try:
    progress = read_competition_demo(settings.db_path)
except (LookupError, OSError, ValueError, sqlite3.Error):
    progress = CompetitionDemoProgress(
        integrity_issue="复诊来源完整性检查未通过",
    )
    source_error = "复诊来源完整性检查未通过"
tasks: tuple[dict, ...] = ()
summary = None
previous_text = None
review = None
review_source = None
statement = None
quote = None
nursing_detail = None
stale = False
trace = None
view_degraded = False
patient_label = "合成患者"

if settings.db_path.is_file():
    try:
        store = SQLiteStore(settings.db_path, initialize=False)
        repository = Layer4SQLiteStore(settings.db_path, initialize=False)
        patient = store.get_patient(DEMO_PATIENT_ID)
        if patient is None:
            raise ValueError("synthetic patient source missing")
        patient_label = patient.display_name
        pathway = load_builtin_pathways().get(patient.pathway_code)
        briefs = ManualReviewBriefService(
            store,
            repository,
            pathway_code=pathway.code,
            pathway_version=pathway.version,
        )
        workbench = DoctorWorkbenchService(
            Layer4InputReader(store),
            repository,
            pathway_code=pathway.code,
            pathway_version=pathway.version,
            summary_kind=BRIEF_SUMMARY_KIND,
        )
        as_of = utc_now_iso()
        view = workbench.query(
            patient_id=DEMO_PATIENT_ID,
            access=_access(),
            as_of=as_of,
            generated_at=as_of,
        )
        view_degraded = view.degraded
        summary = view.summary
        tasks = tuple(briefs.list_manual_tasks(DEMO_PATIENT_ID, as_of=as_of))
        task = next(
            (item for item in tasks if item.get("id") == progress.task_id),
            tasks[0] if len(tasks) == 1 else None,
        )
        statement = _confirmed_statement(store, progress)
        quote = _task_quote(store, task)
        if task and task.get("status") == "completed":
            snapshot = briefs.inspect(
                patient_id=DEMO_PATIENT_ID,
                task_id=task["id"],
                as_of=as_of,
            )
            quote = snapshot.quote
            readiness = (
                "沟通文字仍待护士核对。"
                if snapshot.readiness == "pending-approval"
                else "沟通文字已经护士核对；本演示没有发送。"
            )
            nursing_detail = f"受控处理结果：{snapshot.outcome_label}。{readiness}"
        elif task:
            nursing_detail = f"当前护理动作：{task.get('status') or '状态未记录'}。"

        if summary is not None:
            stale = briefs.is_stale(summary, as_of=as_of)
            review = _find_review(repository, summary)
            review_source = _source_summary(repository, review)
            previous = _previous_summary(repository, summary)
            if previous is not None:
                previous_review = _find_review(repository, previous)
                previous_source = _source_summary(repository, previous_review)
                previous_text = doctor_summary_text(
                    project_doctor_summary_wording(
                        previous,
                        confirmed_statement=statement,
                        review=previous_review,
                        review_source_summary=previous_source,
                    )
                )
            root = (
                f"urn:continucare:summary:{summary.summary_id}:version:{summary.version}"
            )
            try:
                trace = workbench.trace_evidence(
                    patient_id=DEMO_PATIENT_ID,
                    access=_access(),
                    root_reference=root,
                    as_of=as_of,
                    max_depth=10,
                    max_nodes=200,
                )
            except (LookupError, OSError, ValueError, sqlite3.Error):
                view_degraded = True
    except (LookupError, OSError, ValueError, sqlite3.Error) as exc:
        source_error = str(exc)

projection = project_doctor_visit_brief(
    progress,
    tasks=tasks,
    summary=summary,
    confirmed_statement=statement,
    original_quote=quote,
    nursing_detail=nursing_detail,
    stale=stale,
    review=review,
    review_source_summary=review_source,
    previous_summary_text=previous_text,
    source_error=source_error,
    trace_degraded=view_degraded or bool(trace and trace.degraded),
    unresolved_references=tuple(trace.unresolved_references) if trace else (),
    trace_truncated=bool(trace and trace.truncated),
)
patient_initial = html.escape(patient_label[:1] or "患")

st.markdown(
    f"""
    <section class="cc-doctor-patient-strip">
      <span aria-hidden="true">{patient_initial}</span>
      <div><small>本轮复诊准备</small><strong>{html.escape(patient_label)}</strong></div>
      <p>记录来源完整可回看</p>
    </section>
    """,
    unsafe_allow_html=True,
)

with st.container(key="cc_doctor_workspace"):
    main_column, source_column = st.columns([3.2, 1.15], gap="large", vertical_alignment="top")
    with main_column:
        _render_facts(projection)
        _render_notice(projection)
        if projection.summary_text:
            st.markdown(
                f"""
                <section class="cc-doctor-summary" aria-live="polite">
                  <h2>当前速览</h2>
                  <p>{html.escape(projection.summary_text)}</p>
                </section>
                """,
                unsafe_allow_html=True,
            )
        if projection.primary_action and projection.primary_label:
            with st.container(key="cc_doctor_primary"):
                primary_label = (
                    "生成本轮速览"
                    if projection.primary_action == "generate"
                    else "按最新记录刷新速览"
                )
                if st.button(primary_label, type="primary", width="stretch"):
                    try:
                        with st.spinner("正在按当前来源生成速览……"):
                            _guarded_generate(
                                briefs,
                                task_id=projection.primary_task_id,
                                progress=progress,
                            )
                    except CompetitionDemoConflict:
                        st.error(
                            "这轮演示已在另一个页面重新开始。当前页面没有继续写入，请刷新后再操作。"
                        )
                    except (LookupError, ValueError) as exc:
                        st.error(str(exc))
                    else:
                        st.session_state["cc_doctor_feedback"] = (
                            "已按当前来源生成新一版速览。旧版本仍保留。"
                            if projection.primary_action == "refresh"
                            else "已按当前记录生成速览。"
                        )
                        st.rerun()
    with source_column:
        selected_source = _render_source_rail(projection)
        if projection.show_nurse_link:
            with st.container(key="cc_doctor_nurse_link"):
                st.page_link(
                    "pages/2_nurse_risk_center.py",
                    label="返回随访待办核对文字",
                    width="stretch",
                )

_render_source_detail(selected_source, projection, summary, trace)

if projection.show_decisions and summary is not None:
    st.markdown(
        """
        <section class="cc-doctor-decision-head">
          <h2>这版速览的措辞</h2>
          <p>以上只调整速览的文字表达，不等于临床评估。</p>
        </section>
        """,
        unsafe_allow_html=True,
    )
    decision = st.radio(
        "选择一项措辞决定",
        options=[value for value, _ in DOCTOR_DECISION_ACTIONS],
        format_func=dict(DOCTOR_DECISION_ACTIONS).__getitem__,
        index=None,
        horizontal=True,
        key=f"cc_doctor_decisions_{summary.summary_id}_{summary.version}",
    )
    st.markdown(
        '<p class="cc-doctor-reject-boundary">不采用只影响这段速览文字，不改变患者确认的记录。</p>',
        unsafe_allow_html=True,
    )
    note = None
    modified_items = None
    selection_error = None
    if decision == "modify":
        options = [item.item_id for item in projection.wording_items]
        selected_item_id = st.selectbox(
            "选择要调整的现有条目",
            options=options,
            format_func=lambda value: next(
                f"{item.label}：{item.text}"
                for item in projection.wording_items
                if item.item_id == value
            ),
            key=f"cc_doctor_modify_item_{summary.summary_id}_{summary.version}",
        )
        selected_item = next(
            item for item in projection.wording_items if item.item_id == selected_item_id
        )
        replacement = st.text_area(
            "调整后的措辞",
            value=selected_item.text,
            key=f"cc_doctor_replacement_{summary.summary_id}_{summary.version}_{selected_item_id}",
        )
        note = st.text_area(
            "调整说明（必填）",
            key=f"cc_doctor_modify_note_{summary.summary_id}_{summary.version}",
        )
        if replacement.strip() == selected_item.text.strip():
            selection_error = "调整后的措辞必须与当前显示不同。"
        else:
            try:
                modified_items = list(
                    build_doctor_modified_items(
                        summary,
                        item_id=selected_item_id,
                        replacement=replacement,
                        allowed_item_ids=tuple(item.item_id for item in projection.wording_items),
                    )
                )
            except ValueError as exc:
                selection_error = str(exc)
    elif decision == "reject":
        note = st.text_area(
            "不采用说明（必填）",
            key=f"cc_doctor_reject_note_{summary.summary_id}_{summary.version}",
        )

    if decision:
        with st.container(key="cc_doctor_submit_decision"):
            if st.button("记录这项措辞决定", width="stretch"):
                if decision in {"modify", "reject"} and not (note or "").strip():
                    st.error("请先填写说明。")
                elif selection_error:
                    st.error(selection_error)
                else:
                    try:
                        _guarded_review(
                            briefs,
                            repository,
                            summary=summary,
                            progress=progress,
                            decision=DoctorReviewDecision(decision),
                            note=(note or "").strip() or None,
                            modified_items=modified_items,
                        )
                    except (CompetitionDemoConflict, ConcurrentWriteConflict) as exc:
                        st.error(str(exc))
                    except (LookupError, ValueError) as exc:
                        st.error(str(exc))
                    else:
                        st.session_state["cc_doctor_feedback"] = "已记录这版速览的措辞决定。"
                        st.rerun()

if projection.recorded_decision:
    detail = (
        f"<p>说明：{html.escape(projection.decision_note)}</p>"
        if projection.decision_note
        else ""
    )
    st.markdown(
        f"""
        <section class="cc-doctor-recorded-decision">
          <h2>已记录的措辞决定</h2>
          <p>{html.escape(projection.recorded_decision)}</p>
          {detail}
          <p>未写入 EMR，也未形成临床评估。</p>
        </section>
        """,
        unsafe_allow_html=True,
    )

if projection.state in {
    "story_complete",
    "task_rejected",
    "task_cancelled",
    "task_failed",
    "task_entered_in_error",
}:
    _render_outcomes(projection)

if projection.state == "story_complete":
    with st.container(key="cc_doctor_home_link"):
        st.page_link(
            "app.py",
            label="返回演示导览，按需明确重新开始",
            width="stretch",
        )

if projection.show_knowledge_link:
    st.markdown(
        """
        <section class="cc-doctor-knowledge">
          <h2>独立资料</h2>
          <p>只读，不携带患者上下文，不参与本轮状态。</p>
        </section>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="cc_doctor_knowledge_link"):
        st.page_link(
            "pages/5_knowledge_evidence.py",
            label="打开 Knowledge 中心",
            width="stretch",
        )

st.markdown(
    f'<footer class="cc-role-footer-boundary">{html.escape(DOCTOR_ROLE_BOUNDARY)} · 合成数据演示，不真实发送。</footer>',
    unsafe_allow_html=True,
)

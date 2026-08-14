"""Read-only M5-C Doctor Workbench with explicit controlled actions."""

from __future__ import annotations

import json

import streamlit as st

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.config import get_settings
from continucare.db import utc_now_iso
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.layer4 import (
    BRIEF_SUMMARY_KIND,
    DoctorReviewService,
    DoctorWorkbenchService,
    Layer4InputReader,
    Layer4SQLiteStore,
    ManualReviewBriefService,
    SummaryEvidenceItem,
    WorkbenchAccessContext,
    WorkbenchPurpose,
    WorkbenchRole,
)
from continucare.layer4.contracts import DoctorReviewDecision, SummaryDraftStatus
from continucare.layer4.manual_reviews import SEND_ENABLED
from continucare.pathways import load_builtin_pathways
from continucare.presentation import (
    build_l5_governance_for_patient,
    build_latest_l5_submission_view,
)
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


DOCTOR_REFERENCE = "Practitioner/synthetic-doctor-review"


def _access() -> WorkbenchAccessContext:
    return WorkbenchAccessContext(
        actor_reference=DOCTOR_REFERENCE,
        role=WorkbenchRole.DOCTOR,
        purpose=WorkbenchPurpose.TREATMENT,
        permitted_patient_ids=[DEMO_PATIENT_ID],
        identity_verified=True,
    )


def _versioned_evidence(evidence) -> str:
    if evidence.resource.reference.startswith("urn:"):
        return evidence.resource.reference
    if evidence.resource.version_id:
        return (
            f"{evidence.resource.reference}/_history/"
            f"{evidence.resource.version_id}"
        )
    return evidence.resource.reference


def _task_id(summary) -> str | None:
    ids = {
        evidence.resource.reference.split("/", 2)[1]
        for item in summary.items
        for evidence in item.evidence_refs
        if evidence.resource.reference.startswith("Task/")
    }
    return next(iter(ids)) if len(ids) == 1 else None


def _section_label(section: str) -> str:
    return {
        "overview": "患者原话与 completed QuestionnaireResponse",
        "key_changes": "final Observation 与 derivedFrom",
        "tasks_and_actions": "护士受控处理与 Communication readiness",
        "doctor_to_confirm": "医生待确认",
    }.get(section, section)


def _guarded_write(action, /, *args, **kwargs):
    with demo_write_guard(
        settings.db_path,
        expected_generation=progress.generation,
    ):
        return action(*args, **kwargs)


st.set_page_config(
    page_title="医生复诊前简报 · ContinuCare",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.title("医生复诊前简报")
st.error("仅使用合成数据 · 不生成诊断、风险、阈值、治疗或用药建议 · 不写入 EMR")

settings = get_settings()
progress = read_competition_demo(settings.db_path)
render_competition_progress(st, progress)
if not settings.db_path.is_file():
    st.info("尚未开始完整比赛 Demo。")
    st.page_link("app.py", label="返回首页开始 Demo →", icon="🏠")
    st.stop()

store = SQLiteStore(settings.db_path, initialize=False)
repository = Layer4SQLiteStore(settings.db_path, initialize=False)
patient = store.get_patient(DEMO_PATIENT_ID)
pathway = load_builtin_pathways().get(patient.pathway_code if patient else "GLP1-14D")
if patient is not None:
    with st.expander("工程追溯：中国知识版本、原始回答与标准化 Observation"):
        render_l5_governance_panel(
            st, build_l5_governance_for_patient(store, DEMO_PATIENT_ID)
        )
        render_l5_submission_panel(
            st, build_latest_l5_submission_view(store, DEMO_PATIENT_ID)
        )
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
access = _access()
as_of = utc_now_iso()
view = workbench.query(
    patient_id=DEMO_PATIENT_ID,
    access=access,
    as_of=as_of,
    generated_at=as_of,
)
manual_tasks = briefs.list_manual_tasks(DEMO_PATIENT_ID, as_of=as_of)

header, status_col = st.columns([3, 1])
with header:
    st.markdown("### 复诊前 30 秒：患者原话、最终事实、人工处理与发送准备度")
    if patient:
        st.caption(
            f"{patient.display_name} · Pathway {pathway.code}|{pathway.version} · "
            f"下次复诊 {patient.next_visit_date}"
        )
with status_col:
    st.metric("临床评估", "not_assessed")

alert_count = len(store.list_alerts(DEMO_PATIENT_ID))
metric_one, metric_two, metric_three, metric_four = st.columns(4)
metric_one.metric("Alert", alert_count)
metric_two.metric("获批 ClinicalRule", 0)
metric_three.metric("M6 clinical-rule Task", len(view.tasks))
metric_four.metric("发送能力", "关闭" if not SEND_ENABLED else "开启")
st.caption("M6 Task 列表继续只正向选择 clinical-rule Task；manual-review Task 不进入该列表。")

completed_tasks = [item for item in manual_tasks if item.get("status") == "completed"]
summary = view.summary
if summary is None:
    with st.container(border=True):
        st.markdown("### M5-C 简报尚未生成")
        if completed_tasks:
            st.write("已找到处理完成的 manual-review Task。生成是明确动作；页面加载和刷新本身不写入。")
            selected_task_id = st.selectbox(
                "选择受控人工复核 Task",
                options=[item["id"] for item in completed_tasks],
            )
            if st.button("明确生成 M5-C 证据简报", type="primary", width="stretch"):
                try:
                    _guarded_write(
                        briefs.generate,
                        patient_id=DEMO_PATIENT_ID,
                        task_id=selected_task_id,
                        generated_at=utc_now_iso(),
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
        elif manual_tasks:
            current = manual_tasks[0]
            st.info(
                f"Task/{current['id']} 当前为 {current['status']}；"
                "只有 completed 且证据完整的任务才能生成简报。"
            )
        else:
            st.info("尚无患者明确确认后创建的 manual-review Task。请先完成 M5-A 与 M5-B。")
    st.stop()

task_id = _task_id(summary)
stale = task_id is None or briefs.is_stale(summary, as_of=as_of)
if stale:
    st.warning(
        "这份简报是不可变版本快照；当前 Task、Communication 或证据版本已变化。"
        "请明确生成新版本后再依据最新 readiness 工作。"
    )
else:
    st.success("当前简报与已保存的来源版本一致。页面查询保持只读。")

st.caption(
    f"Summary/{summary.summary_id} · v{summary.version} · {summary.status.value} · "
    f"as-of {summary.period_end} · source digest {summary.source_evidence_digest}"
)
if task_id and st.button("明确生成 / 刷新为当前来源版本", width="stretch"):
    try:
        _guarded_write(
            briefs.generate,
            patient_id=DEMO_PATIENT_ID,
            task_id=task_id,
            generated_at=utc_now_iso(),
        )
    except ValueError as exc:
        st.error(str(exc))
    else:
        st.rerun()

if progress.communication_readiness == "pending-approval":
    st.page_link(
        "pages/2_nurse_risk_center.py",
        label="下一步：返回护士端人工批准草稿 →",
        icon="🧭",
    )
elif progress.communication_readiness == "ready-to-send" and not stale:
    next_audit, next_knowledge = st.columns(2)
    with next_audit:
        st.page_link(
            "pages/4_audit_log.py",
            label="查看完整审计链 →",
            icon="🧾",
        )
    with next_knowledge:
        st.page_link(
            "pages/5_knowledge_evidence.py",
            label="独立查看腹泻采集依据 →",
            icon="📚",
        )

st.markdown("## 确定性证据简报")
st.caption(
    "正文由本地固定模板和 canonical text 生成；患者原话逐字显示。"
    "AuditEvent 只证明流程发生，不作为以下临床事实的 evidence reference。"
)
for section in ("overview", "key_changes", "tasks_and_actions", "doctor_to_confirm"):
    section_items = [item for item in summary.items if item.section == section]
    if not section_items:
        continue
    st.markdown(f"### {_section_label(section)}")
    for item in section_items:
        with st.container(border=True):
            if item is summary.items[0]:
                st.info(item.text)
                st.caption("患者原话 · 逐字证据")
            else:
                st.write(item.text)
            with st.expander("逐项查看精确 evidence reference"):
                for evidence in item.evidence_refs:
                    st.code(_versioned_evidence(evidence), language=None)
                    st.caption(
                        f"role={evidence.role.value} · "
                        f"effective={evidence.effective_start or '—'}"
                    )

root = f"urn:continucare:summary:{summary.summary_id}:version:{summary.version}"
trace = workbench.trace_evidence(
    patient_id=DEMO_PATIENT_ID,
    access=access,
    root_reference=root,
    as_of=as_of,
    max_depth=10,
    max_nodes=200,
)
st.markdown("## 完整来源、版本、Provenance 与审计链")
if trace.degraded:
    st.error("证据来源暂时不可用；系统没有补造内容。")
if trace.unresolved_references:
    st.warning("存在 unresolved evidence reference：")
    st.code("\n".join(trace.unresolved_references), language=None)
if trace.truncated:
    st.warning("证据图达到展开上限，当前展示不完整。")

fhir_artifacts = [
    item for item in trace.artifacts if item.artifact_type.value == "fhir_resource"
]
application_artifacts = [
    item for item in trace.artifacts if item.artifact_type.value == "application_record"
]
for label, artifacts in (
    ("FHIR 与 Provenance 资源", fhir_artifacts),
    ("流程审计与应用记录（非临床事实）", application_artifacts),
):
    with st.expander(f"{label} · {len(artifacts)} 项"):
        for artifact in artifacts:
            st.markdown(
                f"**{artifact.reference}** · {artifact.resource_type or artifact.record_type} "
                f"v{artifact.version or '—'}"
            )
            if artifact.record_type == "audit_event":
                st.caption("流程证据：证明动作发生，不证明患者临床状态。")
            st.code(
                json.dumps(artifact.payload, ensure_ascii=False, indent=2),
                language="json",
            )

with st.expander("查看证据图关系"):
    for edge in trace.edges:
        st.write(
            f"{edge.source_reference} --{edge.relation}→ {edge.target_reference}"
        )

st.markdown("## 医生受控审阅")
if summary.status == SummaryDraftStatus.SAFETY_REVIEWED:
    st.caption("审阅只作用于当前不可变版本；生成新来源版本不会迁移旧决定。")
    note = st.text_area(
        "审阅说明",
        placeholder="拒绝或修改时必填。不要添加没有来源证据的新临床结论。",
    )
    decision = st.radio(
        "决定",
        options=["accept", "modify", "reject"],
        format_func=lambda value: {
            "accept": "接受当前版本",
            "modify": "修改已有措辞（保留原证据）",
            "reject": "拒绝当前版本",
        }[value],
        horizontal=True,
    )
    replacement = None
    selected_item_id = None
    if decision == "modify":
        selected_item_id = st.selectbox(
            "选择要修改的条目",
            options=[item.item_id for item in summary.items],
            format_func=lambda value: next(
                item.text for item in summary.items if item.item_id == value
            ),
        )
        original = next(item for item in summary.items if item.item_id == selected_item_id)
        replacement = st.text_area("修改后措辞", value=original.text)
    if st.button("明确提交医生审阅", type="primary", width="stretch"):
        try:
            modified_items = None
            if decision == "modify":
                modified_items = [
                    SummaryEvidenceItem(
                        item_id=item.item_id,
                        section=item.section,
                        text=(replacement if item.item_id == selected_item_id else item.text),
                        evidence_refs=item.evidence_refs,
                        requires_doctor_confirmation=item.requires_doctor_confirmation,
                    )
                    for item in summary.items
                ]
            _guarded_write(
                DoctorReviewService(repository).review,
                summary_id=summary.summary_id,
                summary_version=summary.version,
                reviewer_reference=DOCTOR_REFERENCE,
                decision=DoctorReviewDecision(decision),
                reviewed_at=utc_now_iso(),
                note=note or None,
                modified_items=modified_items,
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.rerun()
elif summary.status == SummaryDraftStatus.DOCTOR_REVIEWED:
    st.success("医生已接受或修改此精确版本；未写入 EMR。")
else:
    st.warning("医生已拒绝此精确版本；原始证据和历史版本仍保留。")

st.markdown("## Timeline 读取边界")
st.info(
    "M5-C 不在页面加载或生成时重建 Clinical Memory。下方 Timeline 可能为空或过时，"
    "因此不作为本简报事实来源；请以每条 Summary evidence reference 和上方证据图为准。"
)
st.caption(f"当前只读 Timeline 事件数：{len(view.timeline)}")

with st.expander("演示模式说明"):
    render_mode_badges(st)
    st.caption(
        "controlled LLM 未实例化、未调用；Communication 始终未发送；"
        "所有身份和患者数据均为合成。"
    )

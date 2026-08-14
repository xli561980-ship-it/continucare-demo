"""Questionnaire-driven synthetic patient follow-up (Layer 2)."""

from __future__ import annotations

import html
import json
from typing import Any

import streamlit as st

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.agents.contracts import (
    CandidateIssueAction,
    SemanticResult,
    SemanticStatus,
)
from continucare.care_agent import CareAgentService
from continucare.care_engine import CareEngine
from continucare.config import get_settings
from continucare.demo_data import DEMO_PATIENT_ID, STRUCTURED_SCENARIOS
from continucare.fhir.questionnaires import (
    flatten_questionnaire_items,
    visible_questionnaire_items,
)
from continucare.fhir.r4 import FHIRValidationError
from continucare.fhir.terminology import UCUM
from continucare.presentation import observation_text
from continucare.models import CareSessionStatus
from continucare.ui import inject_global_styles, render_mode_badges


PRESETS = {
    "轻度恶心": STRUCTURED_SCENARIOS["恶心记录"],
    "呕吐与摄入": STRUCTURED_SCENARIOS["呕吐与摄入记录"],
    "仅保留原文": STRUCTURED_SCENARIOS["仅保留患者原文"],
}


def _widget_key(session_id: str, link_id: str) -> str:
    return f"care::{session_id}::{link_id}"


def _set_widget_answers(session_id: str, answers: dict[str, Any]) -> None:
    for item in flatten_questionnaire_items(questionnaire.get("item", [])):
        key = _widget_key(session_id, item["linkId"])
        st.session_state[key] = _widget_value(item, answers.get(item["linkId"]))


def _widget_value(item: dict[str, Any], value: Any) -> Any:
    if item["type"] == "quantity" and isinstance(value, dict):
        return value.get("value")
    return value


def _read_widget_answer(item: dict[str, Any], session_id: str) -> Any:
    key = _widget_key(session_id, item["linkId"])
    saved = session.answers.get(item["linkId"])
    initial = _widget_value(item, saved)
    item_type = item["type"]

    if item_type == "boolean":
        if key not in st.session_state:
            st.session_state[key] = initial
        return st.radio(
            item.get("text", item["linkId"]),
            options=[None, True, False],
            format_func=lambda value: {None: "暂不回答", True: "是", False: "否"}[value],
            horizontal=True,
            key=key,
        )

    if item_type == "choice":
        options = [
            option["valueCoding"]
            for option in item.get("answerOption", [])
            if "valueCoding" in option
        ]
        option_by_code = {option["code"]: option for option in options}
        if key not in st.session_state:
            st.session_state[key] = initial
        return st.radio(
            item.get("text", item["linkId"]),
            options=[None, *option_by_code],
            format_func=lambda code: (
                "暂不回答"
                if code is None
                else _choice_display(option_by_code[code])
            ),
            horizontal=True,
            key=key,
        )

    if item_type == "integer":
        kwargs = {"value": initial} if key not in st.session_state else {}
        return st.number_input(
            item.get("text", item["linkId"]),
            min_value=0,
            step=1,
            key=key,
            **kwargs,
        )

    if item_type == "decimal":
        kwargs = {"value": initial} if key not in st.session_state else {}
        return st.number_input(
            item.get("text", item["linkId"]),
            step=0.1,
            key=key,
            **kwargs,
        )

    if item_type == "quantity":
        kwargs = {"value": initial} if key not in st.session_state else {}
        value = st.number_input(
            item.get("text", item["linkId"]),
            min_value=0,
            step=50,
            key=key,
            **kwargs,
        )
        st.caption("单位由当前 Pathway 锁定为 mL；不确定时可以留空。")
        if value is None:
            return None
        return {"value": value, "unit": "mL", "system": UCUM, "code": "mL"}

    if item_type in {"text", "string"}:
        kwargs = {"value": initial or ""} if key not in st.session_state else {}
        return st.text_area(
            item.get("text", item["linkId"]),
            placeholder="请只输入合成演示内容",
            height=110,
            key=key,
            **kwargs,
        )

    st.warning(f"当前界面尚未支持题型：{item_type}")
    return None


def _choice_display(coding: dict[str, Any]) -> str:
    translations = {"Mild": "轻度", "Moderate": "中度", "Severe": "重度"}
    display = coding.get("display") or coding["code"]
    return translations.get(display, display)


def _has_answer(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _render_latest_submission() -> None:
    messages = store.list_messages(DEMO_PATIENT_ID)
    if not messages:
        st.info("尚未提交随访。完成下方问题后，这里会显示系统实际保存的标准记录。")
        return
    message = messages[0]
    observations = store.list_observations_for_message(message.message_id)
    response = store.get_questionnaire_response(message.message_id)
    with st.container(border=True):
        heading, state = st.columns([3, 1])
        with heading:
            st.markdown('<div class="cc-kicker">最近一次提交</div>', unsafe_allow_html=True)
            st.markdown("### 回答已保存，临床风险尚未评估")
            st.caption(message.submitted_at)
        with state:
            st.metric("临床分级", "未评估")

        fact_col, next_col = st.columns([3, 2])
        with fact_col:
            st.markdown("**系统记录的患者报告事实**")
            if observations:
                for observation in observations:
                    source_label = (
                        "患者自述新症状"
                        if observation.evidence.source_kind
                        == "patient_reported_new"
                        else "Pathway 已确认监测项"
                    )
                    st.markdown(
                        f"- {observation_text(observation)} · `{source_label}`"
                    )
            else:
                st.caption("原始回答已经保存，本次没有形成当前映射范围内的 Observation。")
        with next_col:
            st.markdown("**安全边界**")
            st.info("当前没有获批临床规则，因此不生成风险等级、报警或治疗建议。")

        with st.expander("查看本次 QuestionnaireResponse 与来源链"):
            st.markdown("**患者提交内容**")
            st.markdown(
                '<div class="cc-quote">'
                + "<br>".join(html.escape(line) for line in message.message_text.splitlines())
                + "</div>",
                unsafe_allow_html=True,
            )
            if response:
                st.caption(
                    f"Questionnaire：{response['questionnaire']} · "
                    f"QuestionnaireResponse/{response['id']}"
                )
            st.dataframe(
                [
                    {
                        "患者报告事实": observation_text(item),
                        "FHIR code": item.code,
                        "结构化值": item.value_display,
                        "来源": f"QuestionnaireResponse/{item.message_id}",
                        "记录分栏": (
                            "患者自述新症状"
                            if item.evidence.source_kind == "patient_reported_new"
                            else "Pathway 已确认监测项"
                        ),
                        "术语匹配": (
                            (
                                f"{item.evidence.terminology_match.get('catalog_id')} "
                                f"v{item.evidence.terminology_match.get('catalog_version')}"
                            )
                            if item.evidence.terminology_match
                            else "—"
                        ),
                        "确认级别": item.confidence_tier.value,
                    }
                    for item in observations
                ],
                hide_index=True,
                width="stretch",
            )


def _semantic_state_key(name: str, *parts: str) -> str:
    return "::".join(("semantic", name, session.session_id, *parts))


def _render_conversation_assist() -> None:
    """Hybrid chat/card UX; only a patient action can update the Layer-2 draft."""

    st.markdown("### 先用一句话告诉我今天的情况")
    st.caption(
        "自然语言用于发现事实和检索术语；按钮用于确认含义。只有确认后，才会写入问卷草稿或“患者自述新症状”。"
    )
    with st.chat_message("assistant"):
        st.write(
            "您可以像聊天一样描述，例如：过去24小时吐了2次，现在有点恶心。"
        )
        st.caption(
            f"当前按患者时区 {agent_service.patient_timezone} 解析“今天/昨天”；"
            "也可直接回答上一轮的“是的/不是/不确定”。"
        )
        st.caption(
            "短期记忆覆盖本次每日随访的全部轮次；提交后形成的 Observation "
            "进入跨日长期记录，不能反推为今天仍有症状。"
        )
        st.caption("请勿输入真实患者信息；当前仅用于合成数据演示。")

    run_key = _semantic_state_key("latest_run")
    run_id = st.session_state.get(run_key)
    recent_records = list(reversed(store.list_agent_runs(session.session_id)[:5]))
    for prior in recent_records:
        if prior.run_id == run_id:
            continue
        prior_result = SemanticResult.model_validate(prior.output_json)
        with st.chat_message("user"):
            st.write(prior.input_text)
        with st.chat_message("assistant"):
            if prior_result.context_resolution is not None:
                st.write(prior_result.context_resolution.explanation)
            elif prior_result.clarifications:
                st.write(prior_result.clarifications[0].prompt)
            elif prior_result.candidates:
                st.write(
                    f"已安全整理 {len(prior_result.candidates)} 项候选，等待患者确认。"
                )
            else:
                st.write("这一轮没有形成可写入的结构化候选。")
    if run_id:
        record = store.get_agent_run(run_id)
        if record and record.session_id == session.session_id:
            result = SemanticResult.model_validate(record.output_json)
            _render_semantic_result(record, result)

    text_key = _semantic_state_key("input")
    patient_text = st.text_area(
        "输入身体状态",
        placeholder="例如：过去24小时我吐了2次，现在有点恶心。",
        height=92,
        key=text_key,
        label_visibility="collapsed",
    )
    analyze_clicked = st.button(
        "让 Care Agent 帮我整理",
        type="primary",
        width="stretch",
        key=_semantic_state_key("analyze"),
    )
    if analyze_clicked:
        if not patient_text.strip():
            st.warning("请先输入一段合成的身体状态描述。")
        else:
            try:
                interaction = agent_service.analyze(session.session_id, patient_text)
                st.session_state[run_key] = interaction.result.run_id
                st.rerun()
            except ValueError as exc:
                st.warning(str(exc))


def _render_semantic_result(record, result: SemanticResult) -> None:
    with st.chat_message("user"):
        st.write(record.input_text)
    with st.chat_message("assistant"):
        if result.mode == "local_semantic_mock":
            st.caption("本地语义 Mock · Safety Agent v4 硬规则已检查")
        else:
            stage_by_name = {item.stage: item for item in result.stage_traces}
            safety_mode = stage_by_name.get("safety_critic")
            language_mode = stage_by_name.get("language_rewrite")
            stage_labels = ["Safety Agent v4 已检查"]
            if safety_mode and safety_mode.mode == "model_api:xiaomi_mimo":
                stage_labels.append("MiMo Safety Critic 已复核")
            if language_mode and language_mode.details.get("rewritten_count", 0):
                stage_labels.append("亲和力表达已优化")
            st.caption(
                f"小米 MiMo {record.model_name or ''} · JSON mode · "
                + " · ".join(stage_labels)
            )

        if result.status == SemanticStatus.BLOCKED:
            st.warning(_human_reason(result) or "这段文字不能安全转换为健康记录。")
            return

        if result.status == SemanticStatus.CONTEXT_RESOLVED:
            resolution = result.context_resolution
            if resolution is not None:
                st.success(resolution.explanation)
                if resolution.applied_link_ids:
                    st.caption(
                        "已写入问卷草稿："
                        + "、".join(resolution.applied_link_ids)
                    )
            _render_stage_traces(result)
            return

        confirmed_key = _semantic_state_key("confirmed", result.run_id)
        confirmed = set(st.session_state.get(confirmed_key, []))
        available = [
            item for item in result.candidates if item.candidate_id not in confirmed
        ]
        if available:
            st.markdown("**请确认我整理的内容**")
            selected: list[str] = []
            for candidate in available:
                with st.container(border=True):
                    st.write(candidate.patient_message)
                    st.caption(f"依据原话：‘{candidate.evidence_text}’")
                    if candidate.terminology_match is not None:
                        match = candidate.terminology_match
                        origin_label = (
                            "患者自述新症状"
                            if candidate.origin.value == "patient_reported_new"
                            else "Pathway 已确认监测项"
                        )
                        st.caption(
                            f"仓库匹配：{match.catalog_id} v{match.catalog_version} · "
                            f"{match.coding.system} | {match.coding.code} · "
                            f"{origin_label}"
                        )
                    if st.checkbox(
                        "这项记录正确",
                        value=True,
                        key=_semantic_state_key("select", candidate.candidate_id),
                    ):
                        selected.append(candidate.candidate_id)
            confirm_col, modify_col = st.columns([1.4, 1])
            with confirm_col:
                if st.button(
                    "确认所选记录",
                    type="primary",
                    width="stretch",
                    key=_semantic_state_key("confirm", result.run_id),
                    disabled=not selected,
                ):
                    try:
                        updated = agent_service.confirm_candidates(result.run_id, selected)
                        st.session_state[confirmed_key] = [*confirmed, *selected]
                        _set_widget_answers(session.session_id, updated.answers)
                        st.rerun()
                    except ValueError as exc:
                        st.warning(str(exc))
            with modify_col:
                st.caption("如有不准确，请取消勾选，或在下方完整问卷中修改。")
        elif result.candidates:
            st.success("这些候选已由您确认，并保存到下方问卷草稿。")

        resolved_key = _semantic_state_key("resolved", result.run_id)
        resolved = set(st.session_state.get(resolved_key, []))
        pending_clarifications = [
            item
            for item in result.clarifications
            if item.clarification_id not in resolved
        ]
        for clarification in pending_clarifications:
            st.markdown("**还需要确认一个细节**")
            st.write(clarification.prompt)
            columns = st.columns(len(clarification.options))
            for column, option in zip(columns, clarification.options):
                with column:
                    if st.button(
                        option.label,
                        width="stretch",
                        key=_semantic_state_key(
                            "clarify",
                            clarification.clarification_id,
                            option.option_id,
                        ),
                    ):
                        try:
                            updated = agent_service.resolve_clarification(
                                result.run_id,
                                clarification.clarification_id,
                                option.option_id,
                            )
                            st.session_state[resolved_key] = [
                                *resolved,
                                clarification.clarification_id,
                            ]
                            _set_widget_answers(session.session_id, updated.answers)
                            st.rerun()
                        except ValueError as exc:
                            st.warning(str(exc))
        if result.clarifications and not pending_clarifications:
            st.info("澄清已处理；只有您明确确认的内容才进入问卷草稿。")

        if result.status == SemanticStatus.NO_MATCH:
            st.info(_human_reason(result) or "暂时没有可安全结构化的明确事实。")
            verbatim_key = _semantic_state_key("verbatim", result.run_id)
            if st.session_state.get(verbatim_key):
                st.success("原话已保存到问卷草稿，未生成结构化临床事实。")
            elif st.button(
                "确认仅保存原话",
                width="stretch",
                key=_semantic_state_key("save_original", result.run_id),
            ):
                try:
                    updated = agent_service.confirm_original_text(result.run_id)
                    st.session_state[verbatim_key] = True
                    _set_widget_answers(session.session_id, updated.answers)
                    st.rerun()
                except ValueError as exc:
                    st.warning(str(exc))

        if result.reported_symptom_mentions:
            st.warning(
                "以下原话尚未在当前 GLP-1 术语目录中唯一命中，系统不会猜代码："
                + "、".join(
                    f"“{item.evidence_text}”"
                    for item in result.reported_symptom_mentions
                )
                + "。原话会随本次记录保留，待医生或术语人员复核。"
            )

        _render_candidate_issues(result)
        _render_stage_traces(result)


def _render_candidate_issues(result: SemanticResult) -> None:
    if not result.candidate_issues:
        return
    st.markdown("**Safety Agent 的处理说明**")
    for issue in result.candidate_issues:
        with st.container(border=True):
            if issue.action == CandidateIssueAction.CLARIFICATION_REQUIRED:
                st.info(f"需要患者确认：{issue.field_label}")
            else:
                st.warning(f"未采用模型候选：{issue.field_label}")
            st.write(issue.explanation)
            st.caption(
                f"模型尝试整理为：{_display_issue_answer(issue.proposed_answer)}"
            )
            st.caption(f"引用原话：‘{issue.evidence_text}’")
            st.caption(
                "判断依据："
                + "；".join(_display_reason_code(code) for code in issue.reason_codes)
            )


def _render_stage_traces(result: SemanticResult) -> None:
    if not result.stage_traces:
        return
    with st.expander("查看 Agent 分阶段记录"):
        st.dataframe(
            [
                {
                    "阶段": trace.stage,
                    "Agent": trace.agent_name,
                    "模式": trace.mode,
                    "状态": trace.status,
                    "Prompt": trace.prompt_version or "—",
                    "Token": (trace.model_usage or {}).get("total_tokens", 0),
                    "延迟(ms)": trace.latency_ms or 0,
                }
                for trace in result.stage_traces
            ],
            hide_index=True,
            width="stretch",
        )


def _display_issue_answer(answer: Any) -> str:
    if answer is True:
        return "是"
    if answer is False:
        return "否"
    if isinstance(answer, dict) and "value" in answer:
        return f"{answer['value']} {answer.get('unit', '')}".strip()
    return str(answer)


def _display_reason_code(code: str) -> str:
    labels = {
        "time_window_not_explicit": "需要明确是否覆盖完整过去24小时",
        "current_status_not_explicit": "需要明确是否为当前情况",
        "evidence_concept_mismatch": "引用原话与目标症状不一致",
        "evidence_negation_mismatch": "有/无判断与患者原话不一致",
        "subject_not_patient": "描述对象不是患者本人",
        "invalid_evidence_span": "引用内容无法在患者原话中找到",
    }
    return labels.get(code, "候选未通过当前安全规则")


def _human_reason(result: SemanticResult) -> str | None:
    return next(
        (
            reason
            for reason in reversed(result.ignored_reasons)
            if any("\u4e00" <= char <= "\u9fff" for char in reason)
        ),
        None,
    )


def _render_pending_new_symptoms() -> None:
    reports = store.list_active_symptom_reports(session.session_id)
    if not reports:
        return
    st.markdown("#### 本次已确认的扩展症状")
    st.caption("这些项目不改写锁定问卷；提交时会生成同样受校验的 FHIR Observation。")
    st.dataframe(
        [
            {
                "分栏": (
                    "患者自述新症状"
                    if item.source_kind == "patient_reported_new"
                    else "Pathway 已确认监测项"
                ),
                "症状": item.preferred_zh,
                "FHIR system": item.coding["system"],
                "code": item.coding["code"],
                "目录": (
                    f"{item.terminology_match['catalog_id']} "
                    f"v{item.terminology_match['catalog_version']}"
                ),
                "患者原话": item.evidence_text,
                "报告时间": item.reported_at,
            }
            for item in reports
        ],
        hide_index=True,
        width="stretch",
    )


st.set_page_config(
    page_title="患者随访 · ContinuCare",
    page_icon="💬",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.title("患者随访（合成数据）")
st.error("仅用于合成数据演示 · 不提供诊断、治疗或用药建议 · 不是急救通道")

settings = get_settings()
store = SQLiteStore(settings.db_path)
patient = store.get_patient(DEMO_PATIENT_ID)
if patient is None:
    st.error("合成患者初始化失败，请返回首页重置 Demo。")
    st.stop()

engine = CareEngine(store)
session = engine.start_or_resume(
    DEMO_PATIENT_ID,
    allow_repeat_same_day=False,
    patient_timezone=settings.patient_timezone,
)
questionnaire = engine.questionnaire_for_session(session)
agent_service = CareAgentService(store, care_engine=engine)

notice = st.session_state.pop("care_submission_notice", None)
if notice:
    st.success(notice)

st.markdown("## 最近一次随访结果")
_render_latest_submission()

if session.status == CareSessionStatus.COMPLETED:
    st.info(
        "患者当地日期的今日随访已经完成。短期对话已关闭，确认后的 Observation "
        "已进入长期记录；下一当地日期会自动开启新的每日会话。"
    )
    st.stop()

st.markdown("## 完成本次随访")
profile, form_area = st.columns([1, 2.15], gap="large")
with profile:
    with st.container(border=True):
        st.markdown('<div class="cc-kicker">版本已锁定</div>', unsafe_allow_html=True)
        st.markdown(f"### {patient.display_name}")
        st.write(f"Pathway：`{session.pathway_code}`")
        st.write(f"版本：`{session.pathway_version}`")
        st.write(f"问卷：`{session.questionnaire_version}`")
        st.write(f"下次复诊：{patient.next_visit_date}")
        st.caption(f"会话：{session.session_id} · 合成患者")
    with st.container(border=True):
        st.markdown("#### 填写说明")
        st.write("问题和选项来自已锁定的 FHIR Questionnaire，不由模型临时生成。")
        st.write("不确定的数量可以留空，并在补充说明中保留原话。")
        st.write("保存草稿后，可在同一设备继续填写。")

with form_area:
    with st.container(border=True):
        _render_conversation_assist()
        _render_pending_new_symptoms()

    st.markdown("### 或直接填写完整问卷")
    st.caption("对话整理的确认结果会同步到这里；完整问卷始终是可检查、可修改的兜底入口。")
    st.markdown(f"### {questionnaire.get('title', '患者报告采集')}")
    st.caption(questionnaire.get("description", ""))
    preset_columns = st.columns(len(PRESETS))
    for column, (label, preset) in zip(preset_columns, PRESETS.items()):
        with column:
            if st.button(f"载入：{label}", width="stretch", key=f"preset::{label}"):
                engine.save_draft(session.session_id, preset)
                _set_widget_answers(session.session_id, preset)
                st.rerun()

    answers: dict[str, Any] = {}
    all_items = flatten_questionnaire_items(questionnaire.get("item", []))
    for item in all_items:
        if item["type"] in {"display", "group"}:
            continue
        visible_ids = {
            question["linkId"]
            for question in visible_questionnaire_items(questionnaire, answers)
        }
        if item["linkId"] not in visible_ids:
            st.session_state.pop(
                _widget_key(session.session_id, item["linkId"]), None
            )
            continue
        with st.container(border=True):
            value = _read_widget_answer(item, session.session_id)
            if _has_answer(value):
                answers[item["linkId"]] = value
            if item.get("required"):
                st.caption("必填")
            elif item["type"] != "quantity":
                st.caption("可选；不确定时可以暂不回答")

    visible_count = len(visible_questionnaire_items(questionnaire, answers))
    answered_count = sum(_has_answer(value) for value in answers.values())
    st.progress(
        answered_count / visible_count if visible_count else 0,
        text=f"已回答 {answered_count} / 当前可见 {visible_count} 个问题",
    )

    save_col, submit_col, stop_col = st.columns([1, 1.4, 1])
    with save_col:
        save_clicked = st.button("保存草稿", width="stretch")
    with submit_col:
        submit_clicked = st.button(
            "确认并提交", type="primary", width="stretch"
        )
    with stop_col:
        stop_clicked = st.button("放弃本次", width="stretch")

    try:
        if save_clicked:
            engine.save_draft(session.session_id, answers)
            st.success("草稿已保存。问题版本和当前答案均已锁定。")
        if submit_clicked:
            result = engine.complete(session.session_id, answers)
            st.session_state["care_submission_notice"] = (
                "提交成功：完整 QuestionnaireResponse 已保存，形成 "
                f"{len(result.observations)} 条患者确认的 Observation；临床风险未评估。"
            )
            st.rerun()
        if stop_clicked:
            engine.stop(session.session_id)
            st.session_state["care_submission_notice"] = "本次草稿已停止，未形成临床事实。"
            st.rerun()
    except (ValueError, FHIRValidationError) as exc:
        st.warning(str(exc))

with st.expander("第二层与第三层执行边界"):
    render_mode_badges(st)
    st.write("第三层只生成候选与澄清问题，不能直接写 FHIR、生成风险等级或创建 Alert。")
    st.write("患者确认后，答案才交给第二层完成问卷校验、条件分支和 Observation 映射。")
    if agent_service.agent.model_adapter.configured:
        st.write(
            "小米 MiMo API 已配置；模型只生成候选，异常时自动回退本地语义 Mock。"
        )
    else:
        st.write("MiMo API Key 未配置；系统使用本地语义 Mock 安全回退。")
    st.caption("当前 Pathway 为 draft / synthetic_only / not_reviewed。")

with st.expander("查看当前 GLP-1 症状术语目录"):
    catalog = agent_service.terminology.catalog
    st.caption(
        f"{catalog.catalog_id} v{catalog.version} · {catalog.status} · "
        f"SNOMED CT {catalog.code_system_release.rsplit('/', 1)[-1]}"
    )
    st.warning(catalog.completeness_statement)
    st.dataframe(
        [
            {
                "中文概念": item.preferred_zh,
                "SNOMED CT": item.coding.code,
                "英文显示": item.coding.display,
                "类别": item.category,
                "中文别名": "、".join(item.aliases_zh),
                "审核状态": item.approval_status,
            }
            for item in catalog.concepts
        ],
        hide_index=True,
        width="stretch",
    )

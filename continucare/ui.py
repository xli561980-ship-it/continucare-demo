"""Shared visual safety cues and responsive layout rules."""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from continucare.presentation import L5GovernanceView, L5SubmissionView


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
    "患者表达",
    "患者确认",
    "护士核对",
    "医生速览",
    "记录追溯",
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
    "确认的是您说的话有没有记对，不是确认诊断。本演示不会发送消息。"
)


PATIENT_EMERGENCY_NOTICE = (
    "这里不是急救通道。如情况紧急，请立即联系当地急救服务或前往急诊，"
    "不要在这里等待回复。"
)


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
                "请先刷新；如仍无法读取，请返回合成演示导览。"
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
            question="这和您想表达的是同一个意思吗？",
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
            next_destination="前往“护士工作台”接手这项核对。",
            next_page="pages/2_nurse_risk_center.py",
            next_label="前往护士工作台",
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
            next_destination="返回“护士工作台”开始核对。",
            next_page="pages/2_nurse_risk_center.py",
            next_label="继续护士核对",
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
            next_destination="返回“护士工作台”记录受控结果。",
            next_page="pages/2_nurse_risk_center.py",
            next_label="继续护士核对",
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
            next_destination="返回“护士工作台”核对沟通文字。",
            next_page="pages/2_nurse_risk_center.py",
            next_label="返回护士工作台",
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
        "complete": "已完成",
        "current": "当前步骤",
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
    proof_rows = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in (
            "同一条记录，按角色只显示当前所需",
            "每条交接内容都能一跳回到来源",
            "停止路径同样说明原因和未产生的内容",
        )
    )
    non_claim_rows = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in (
            "没有真实患者",
            "没有临床评估、诊断或风险分级",
            "没有真实发送或真实外部集成",
        )
    )
    st.markdown(
        f"""
        <nav class="cc-guide" aria-label="合成演示五步导览">
          <ol class="cc-guide-steps">{''.join(steps)}</ol>
        </nav>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="cc_demo_guide_layout"):
        main_column, proof_column = st.columns(
            [2, 0.95], gap="large", vertical_alignment="top"
        )
        with main_column:
            st.markdown(
                f"""
                <article class="cc-guide-current cc-guide-current--{projection.tone}" aria-live="polite">
                  <p class="cc-guide-role">当前演示角色：{html.escape(projection.current_role)}</p>
                  <h2>{html.escape(projection.status_title)}</h2>
                  <p class="cc-guide-detail">{html.escape(projection.status_detail)}</p>
                  <dl class="cc-guide-facts">{context_rows}</dl>
                  <div class="cc-guide-meta">
                    <div>
                      <h3>上一步发生了什么</h3>
                      <p>{html.escape(projection.previous_event)}</p>
                    </div>
                    <div>
                      <h3>下一步去哪里</h3>
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
        with proof_column:
            st.markdown(
                f"""
                <aside class="cc-guide-proof" aria-label="演示能力边界">
                  <section>
                    <h2>这一分钟证明什么</h2>
                    <ul>{proof_rows}</ul>
                  </section>
                  <section>
                    <h2>不声称什么</h2>
                    <ul>{non_claim_rows}</ul>
                  </section>
                </aside>
                """,
                unsafe_allow_html=True,
            )
    return projection


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
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_l5_governance_panel(st, view: "L5GovernanceView") -> None:
    """Render the same release, scope and review boundary for every L5 role."""

    st.error(" · ".join(view.disclaimers))
    with st.container(border=True):
        st.markdown("#### 中国知识版本、适用范围与审核状态")
        version_col, review_col = st.columns(2)
        with version_col:
            st.write(f"Pathway：`{view.pathway_code}` v`{view.pathway_version}`")
            st.write(f"中国知识库 Release：`{view.knowledge_release_id}`")
            st.write(f"知识发布状态：`{view.knowledge_status}`")
        with review_col:
            st.write(f"Pathway 状态：`{view.pathway_status}`")
            st.write(f"当前审核状态：**{view.review_status}**")
            st.write("临床规则：`not_assessed`（不创建临床 Alert）")
        st.markdown("**产品范围**")
        for product in view.products:
            st.write(f"- {product}")
        st.markdown("**适应证范围**")
        st.write("、".join(view.indications) or "未声明")
        st.markdown("**数据来源**")
        for source in view.data_sources:
            st.write(f"- {source}")


def render_l5_submission_panel(
    st,
    submission: "L5SubmissionView | None",
    *,
    title: str = "最近一次原始回答与标准化 Observation",
) -> None:
    """Render raw answers and persisted Observation trace without inference."""

    st.markdown(f"### {title}")
    if submission is None:
        st.info("尚无已完成的版本锁定随访；因此没有原始回答或标准化 Observation。")
        return
    st.caption(
        f"QuestionnaireResponse/{submission.response_id} · "
        f"{submission.response_status} · {submission.authored} · "
        f"{submission.questionnaire}"
    )
    st.markdown("**原始患者回答（FHIR value[x]）**")
    st.dataframe(
        list(submission.raw_answer_rows),
        hide_index=True,
        width="stretch",
    )
    st.markdown("**标准化 Observation 与 L1 追溯**")
    if submission.observation_rows:
        st.dataframe(
            list(submission.observation_rows),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("原始回答已保存；本次没有形成发布映射范围内的 Observation。")
    with st.expander("查看原始 FHIR JSON"):
        st.markdown("**QuestionnaireResponse**")
        st.json(submission.response_resource)
        for resource in submission.observation_resources:
            st.markdown(f"**Observation/{resource['id']}**")
            st.json(resource)


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
        is_audit = bool(current_url) and current_path.endswith("/audit_log")
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

    prefixes = ("care::", "semantic::", "manual_", "competition::")
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

"""ContinuCare Streamlit entry point."""

from __future__ import annotations

import streamlit as st

from continucare.config import get_settings
from continucare.demo_data import SCENARIOS
from continucare.knowledge import load_builtin_bundle
from continucare.navigation import ROLE_ROUTES
from continucare.services.competition_demo import (
    CompetitionDemoStartError,
    load_technical_demo_atomically,
    read_competition_demo,
    start_competition_demo,
)
from continucare.ui import (
    clear_demo_session_state,
    inject_global_styles,
    render_demo_guide,
    render_integration_status,
)


def _start_new_demo(db_path) -> None:
    with st.spinner("正在准备新的合成演示，请勿重复操作……"):
        try:
            start_competition_demo(db_path)
        except CompetitionDemoStartError:
            st.error("新一轮暂时无法开始，原来的演示记录没有被替换。请重试。")
            return
    clear_demo_session_state(st)
    st.session_state["competition::notice"] = (
        "新的合成演示已经准备好。下一步由患者确认待确认内容。"
    )
    st.rerun()


def _render_start_action(db_path) -> None:
    with st.container(key="cc_demo_start_action"):
        if st.button(
            "开始一轮合成演示",
            type="primary",
            width="stretch",
            key="competition::start",
        ):
            _start_new_demo(db_path)


st.set_page_config(
    page_title="ContinuCare｜院外随访接力",
    layout="wide",
    initial_sidebar_state="collapsed",
)

settings = get_settings()
inject_global_styles(st)
progress = read_competition_demo(settings.db_path)
try:
    load_builtin_bundle()
    knowledge_available = True
except Exception:
    knowledge_available = False

st.markdown(
    """
    <header class="cc-demo-header">
      <div class="cc-demo-brand">
        <span class="cc-demo-brand-mark" aria-hidden="true">C</span>
        <strong>ContinuCare</strong>
        <span class="cc-demo-badge">合成数据演示</span>
      </div>
      <span class="cc-demo-nav-label">院外随访接力</span>
    </header>
    <section class="cc-demo-hero">
      <div class="cc-demo-hero-copy">
        <p class="cc-demo-eyebrow">PATIENT-REPORTED FOLLOW-UP</p>
        <h1>让院外一句话，<br>变成复诊前可追溯的记录</h1>
        <p class="cc-demo-lead">ContinuCare 把患者自由表达整理为待确认事实，
        并在患者、护士、医生之间完成有来源的接力。</p>
      </div>
      <div class="cc-demo-chain" aria-label="ContinuCare 三端接力">
        <div class="cc-demo-chain-roles">
          <div><span>01</span><strong>患者端</strong><small>说出今天的情况</small></div>
          <i aria-hidden="true">→</i>
          <div><span>02</span><strong>护士端</strong><small>核对记录与来源</small></div>
          <i aria-hidden="true">→</i>
          <div><span>03</span><strong>医生端</strong><small>查看复诊前速览</small></div>
        </div>
        <div class="cc-demo-chain-proof">
          <span>一句原话</span><span>本人确认</span><span>人工核对</span><span>复诊速览</span>
        </div>
      </div>
    </section>
    """,
    unsafe_allow_html=True,
)

notice = st.session_state.pop("competition::notice", None)
if notice:
    st.info(notice)

render_demo_guide(
    st,
    progress,
    render_primary_action=(
        (lambda: _render_start_action(settings.db_path))
        if not progress.generation and not progress.integrity_issue
        else None
    ),
)

st.markdown(
    """
    <section class="cc-role-section-head">
      <p>THREE ROLE EXPERIENCES</p>
      <h2>三种角色，只看此刻需要的内容</h2>
      <span>三个固定地址共享同一条合成记录，适合分设备演示。</span>
    </section>
    """,
    unsafe_allow_html=True,
)
role_copy = {
    "patient": ("01", "像聊天一样表达", "确认系统是否准确理解自己的原话。", "进入患者随访"),
    "nurse": ("02", "核对记录与来源", "并排查看患者原话、确认记录和确认来源。", "进入随访待办"),
    "doctor": ("03", "30 秒复诊准备", "查看已确认事实、护理接力与仍需补充的信息。", "进入复诊准备"),
}
role_columns = st.columns(len(ROLE_ROUTES), gap="medium")
for column, route in zip(role_columns, ROLE_ROUTES):
    number, title, description, action = role_copy[route.url_path]
    with column, st.container(key=f"cc_role_card_{route.url_path}"):
        st.markdown(
            f"""
            <article class="cc-role-card-copy">
              <span class="cc-role-number">{number}</span>
              <p>{route.title}</p>
              <h3>{title}</h3>
              <span>{description}</span>
            </article>
            """,
            unsafe_allow_html=True,
        )
        st.page_link(
            route.source,
            label=action,
            icon=route.icon,
            width="stretch",
        )

with st.expander(
    "再用 20 秒看负向路径",
    expanded=False,
    key="cc_demo_negative_path",
):
    st.markdown(
        """
        <div class="cc-negative-path">
          <strong>患者全部拒绝</strong>
          <p>本轮结束 → 不产生患者确认记录、护士任务或医生速览 → 查看记录追溯</p>
          <p>本轮不能立即重新表述。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown(
    """
    <section class="cc-independent-knowledge">
      <h2>独立只读资料</h2>
      <p>Knowledge 不携带患者上下文，不参与五步故事或本轮完成度。</p>
    </section>
    """,
    unsafe_allow_html=True,
)
st.page_link(
    "pages/5_knowledge_evidence.py",
    label="打开独立 Knowledge 资料库",
    width="content",
)
if not knowledge_available:
    st.caption("Knowledge CURRENT registry 暂不可用；这不改变本轮故事事实。")

with st.expander(
    "技术详情：外部适配器与当前配置",
    expanded=False,
    key="cc_demo_technical_details",
):
    st.caption(
        "以下内容只读取本地配置，不认证、不探活、不联网；Mock/disabled 状态按事实显示。"
    )
    render_integration_status(st)

has_existing_story = bool(progress.generation or progress.integrity_issue)
with st.expander(
    "管理本地演示数据",
    expanded=False,
    key="cc_demo_data_management",
):
    if has_existing_story:
        st.warning(
            "重新开始会替换本轮全部本地合成演示数据，包括记录追溯。"
            "代码和独立 Knowledge 资料不受影响。"
        )
    else:
        st.caption("当前没有已开始的五步故事；下方旧技术演示仍会替换本地合成数据。")

    replace_confirmed = st.checkbox(
        "我知道当前这轮合成演示记录会被替换。",
        key="competition::reset_consent",
    )
    if has_existing_story:
        with st.container(key="cc_demo_reset_action"):
            if st.button(
                "替换并开始新一轮",
                type="primary",
                width="stretch",
                disabled=not replace_confirmed,
                key="competition::restart",
            ):
                _start_new_demo(settings.db_path)

    st.divider()
    st.markdown("#### 旧技术演示")
    st.caption(
        "这些 fixtures 只用于次级技术检查，不属于五步故事；载入时同样会替换当前本地合成数据。"
    )
    scenario_title = st.selectbox(
        "选择旧技术 fixture",
        options=tuple(SCENARIOS),
        key="competition::technical_fixture",
    )
    st.code(SCENARIOS[scenario_title], language=None)
    if st.button(
        f"明确替换并载入 {scenario_title}",
        width="stretch",
        disabled=not replace_confirmed,
        key="competition::technical_load",
    ):
        try:
            load_technical_demo_atomically(settings.db_path, scenario_title)
        except CompetitionDemoStartError as exc:
            st.error(str(exc))
        else:
            clear_demo_session_state(st)
            st.session_state["competition::notice"] = (
                f"已载入旧技术演示“{scenario_title}”；它不属于五步故事。"
            )
            st.rerun()

st.caption("当前展示合成数据下的端到端产品流程，不代表临床试点或真实系统接入。")
st.markdown(
    """
    <footer class="cc-demo-footer">
      <strong>ContinuCare</strong>
      <span>合成数据演示 · 不提供诊断或治疗建议 · 不真实发送</span>
    </footer>
    """,
    unsafe_allow_html=True,
)

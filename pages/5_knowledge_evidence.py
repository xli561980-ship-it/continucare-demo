"""A++ independent, read-only Knowledge library backed by the offline bundle."""

from __future__ import annotations

import html

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from continucare.knowledge import KnowledgeBundleError, LoadMode, load_builtin_bundle
from continucare.ui import (
    KnowledgeLibraryProjection,
    KnowledgeSourceProjection,
    KnowledgeTopicProjection,
    inject_global_styles,
    project_knowledge_library,
    render_disclosure_controls,
)


KNOWLEDGE_BOUNDARIES = (
    "不是诊断。",
    "不是风险等级。",
    "不授权运行时动作。",
    "不包含真实患者数据。",
    "浏览不会写数据库、调用模型或创建临床资源。",
)


def _home_link(label: str = "返回演示首页") -> None:
    try:
        st.page_link("app.py", label=label, width="stretch")
    except (StreamlitPageNotFoundError, KeyError):
        st.markdown(f"[{label}](/)")


def _source_link(source: KnowledgeSourceProjection) -> str:
    if not source.url:
        return ""
    return (
        f'<a class="cc-knowledge-source-link" href="{html.escape(source.url, quote=True)}" '
        'target="_blank" rel="noopener noreferrer">打开官方来源</a>'
    )


def _render_source(source: KnowledgeSourceProjection) -> None:
    locators = "".join(
        f"<pre>{html.escape(item)}</pre>" for item in source.locators
    ) or "<p>Locator 未登记</p>"
    st.markdown(
        f"""
        <article class="cc-knowledge-source">
          <h3>{html.escape(source.title)}</h3>
          <dl>
            <div><dt>发布机构</dt><dd>{html.escape(source.issuing_authority)}</dd></div>
            <div><dt>文档版本</dt><dd>{html.escape(source.document_version)}</dd></div>
            <div><dt>Access</dt><dd><code>{html.escape(source.access_mode)}</code></dd></div>
            <div><dt>Integrity</dt><dd><code>{html.escape(source.integrity)}</code></dd></div>
            <div><dt>License</dt><dd>{html.escape(source.license_terms)}</dd></div>
          </dl>
          <div class="cc-knowledge-locator"><strong>Locator</strong>{locators}</div>
          {_source_link(source)}
        </article>
        """,
        unsafe_allow_html=True,
    )


def _render_topic_selector(projection: KnowledgeLibraryProjection) -> str | None:
    if not projection.topics:
        return None
    topic_map = {item.topic_id: item for item in projection.topics}
    options = tuple(topic_map)
    selected = projection.selected_topic_id
    index = options.index(selected) if selected in options else 0
    return st.radio(
        "四个内置主题",
        options,
        index=index,
        format_func=lambda item: topic_map[item].name or "名称未解析",
        horizontal=True,
        key="cc_knowledge_topic",
        help="主题来自稳定的离线 registry 顺序，不读取患者上下文，也不表示排名。",
    )


def _render_claims(topic: KnowledgeTopicProjection) -> None:
    if not topic.catalog_resolved:
        st.markdown(
            '<section class="cc-knowledge-unresolved" role="status">'
            '<h2>当前主题的资料目录名称未解析</h2>'
            '<p>页面不会补造中文名、编码或适用范围；精确状态保留在来源与版本中。</p>'
            '</section>',
            unsafe_allow_html=True,
        )
    if not topic.claims:
        st.markdown(
            '<section class="cc-knowledge-no-claim"><h2>当前主题没有已登记的支持声明</h2>'
            '<p>可以查看来源，或切换其他主题。本页不会据此生成患者判断。</p></section>',
            unsafe_allow_html=True,
        )
        return
    statements = "".join(
        f"<p>{html.escape(item.statement)}</p>" for item in topic.claims
    )
    supports = "".join(f"<li>{html.escape(item)}</li>" for item in topic.supports)
    limitations = "".join(
        f"<li>{html.escape(item)}</li>" for item in topic.does_not_support
    )
    st.markdown(
        f"""
        <section class="cc-knowledge-rationale">
          <h2>为什么记录</h2>
          <div>{statements}</div>
        </section>
        <section class="cc-knowledge-scope" aria-label="支持与不支持范围">
          <div class="cc-knowledge-supports">
            <h2>支持什么</h2>
            <ul>{supports}</ul>
          </div>
          <div class="cc-knowledge-limitations">
            <h2>不支持什么</h2>
            <ul>{limitations}</ul>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_coverage(topic: KnowledgeTopicProjection) -> None:
    if topic.gaps:
        reasons = "".join(f"<li>{html.escape(item.reason)}</li>" for item in topic.gaps)
        detail = f"<ul>{reasons}</ul>"
        tone = "gap"
    else:
        detail = "<p>这不表示资料完整、临床充分或已经验证。</p>"
        tone = "none"
    st.markdown(
        f"""
        <section class="cc-knowledge-coverage cc-knowledge-coverage--{tone}">
          <h2>CoverageGap</h2>
          <strong>{html.escape(topic.coverage_message)}</strong>
          {detail}
        </section>
        <p class="cc-knowledge-unassessed">未评估边界：这些资料没有用于判断任何一位患者，也不表示来源存在就适用于当前患者。</p>
        """,
        unsafe_allow_html=True,
    )


def _render_technical(topic: KnowledgeTopicProjection) -> None:
    with st.expander("目录、Claim 与 Binding 技术字段"):
        if topic.catalog_resolved:
            st.markdown(
                f"Catalog code：`{topic.catalog_system or '未登记'} | "
                f"{topic.catalog_code or '未登记'} | {topic.catalog_version or '未登记'}`"
            )
        else:
            st.warning(f"catalog unresolved：{topic.catalog_detail}")
        for claim in topic.claims:
            st.markdown(f"**Claim `{claim.claim_ref}`**")
            st.write(
                f"Lifecycle：`{claim.lifecycle}` · Review aggregate："
                f"`{claim.review_aggregate}`"
            )
            st.code(claim.scope_json, language="json")
        if not topic.bindings:
            st.info("当前主题没有 exact Binding。")
        for binding in topic.bindings:
            st.markdown(f"**Binding `{binding.binding_ref}`**")
            st.write(
                f"Pathway scope：`{binding.pathway_scope}` · "
                f"Purpose：`{binding.purpose}`"
            )
            st.code(binding.artifact_json, language="json")
        st.markdown(
            "**Binding 固定边界：** `informational_only` · `runtime_authority=none` · "
            "不授权 Task、Observation、Summary 或 ClinicalRule。"
        )
        for gap in topic.gaps:
            st.markdown(
                f"CoverageGap `{gap.gap_ref}` · `{gap.gap_kind}` · "
                f"`{gap.lifecycle}` · `{gap.pathway_scope}`"
            )
    with st.expander("查看精确 manifest JSON"):
        st.code(topic.manifest_json, language="json")


def _render_historical(selected_topic_id: str | None) -> None:
    st.markdown("### CURRENT / HISTORICAL")
    st.caption(
        "CURRENT 是当前离线选择；HISTORICAL 中失效或未解析记录只用于资料审计，"
        "不代表当前可用，也不参与患者判断。"
    )
    try:
        historical = project_knowledge_library(
            load_builtin_bundle(mode=LoadMode.HISTORICAL),
            selected_topic_id=selected_topic_id,
        )
    except KnowledgeBundleError:
        st.info("历史资料暂时无法读取；当前资料页仍保持只读且不影响患者故事。")
        return
    rows = "".join(
        "<li>"
        f"<strong>{html.escape(item.name or '名称未解析')}</strong>"
        f"<span>{html.escape(item.mode)} · v{item.record_version} · "
        f"{'已精确解析' if item.catalog_resolved else '未解析，仅供审计'}</span>"
        "</li>"
        for item in historical.topics
    )
    st.markdown(
        f'<ul class="cc-knowledge-history">{rows}</ul>',
        unsafe_allow_html=True,
    )


def _render_sources(
    projection: KnowledgeLibraryProjection,
    topic: KnowledgeTopicProjection,
) -> None:
    st.markdown(
        '<section id="cc-knowledge-sources-panel" class="cc-knowledge-details-head">'
        '<h2>来源与版本</h2>'
        '<p>链接只在您明确点击后打开；页面加载不会访问官方来源 URL。</p></section>',
        unsafe_allow_html=True,
    )
    if not topic.sources:
        st.info("当前主题没有已绑定的官方来源；这不改变页面的只读边界。")
    for source in topic.sources:
        _render_source(source)
    _render_technical(topic)
    _render_historical(projection.selected_topic_id)
    st.markdown("### 未绑定的 link-only 来源")
    st.caption(
        "以下来源没有绑定到当前四个主题或 Pathway；来源存在不等于适用于当前患者，"
        "也不形成临床判断。"
    )
    for source in projection.unbound_sources:
        _render_source(source)


st.set_page_config(
    page_title="Knowledge 资料库 · ContinuCare",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.markdown(
    '<div class="cc-knowledge-shell" aria-hidden="true"></div>',
    unsafe_allow_html=True,
)
st.title("Knowledge 资料库")
st.markdown('<p class="cc-knowledge-subtitle">症状采集参考</p>', unsafe_allow_html=True)

try:
    current_registry = load_builtin_bundle(mode=LoadMode.CURRENT)
    projection = project_knowledge_library(current_registry)
except KnowledgeBundleError:
    st.markdown(
        '<section class="cc-knowledge-load-error" role="alert">'
        '<h2>资料库暂时无法读取</h2>'
        '<p>这不会影响患者故事或完成状态；页面没有回退读取患者数据库，也没有请求网络。</p>'
        '</section>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<p class="cc-knowledge-fixed-boundary">' + " ".join(KNOWLEDGE_BOUNDARIES) + "</p>",
        unsafe_allow_html=True,
    )
    _home_link()
    st.stop()

st.markdown(
    f"""
    <section class="cc-knowledge-notices" aria-label="资料库独立性说明">
      <strong>{html.escape(projection.independence_notice)}</strong>
      <p>{html.escape(projection.readonly_notice)}</p>
    </section>
    <p class="cc-knowledge-topic-note">四个主题来自当前离线资料顺序；不是排名、覆盖率目标、固定分母或完整症状库。</p>
    """,
    unsafe_allow_html=True,
)

selected_topic_id = _render_topic_selector(projection)
projection = project_knowledge_library(
    current_registry,
    selected_topic_id=selected_topic_id,
)
topic = projection.selected_topic

if topic is None:
    st.markdown(
        '<section class="cc-knowledge-no-claim"><h2>当前没有可显示的内置主题</h2>'
        '<p>资料库保持只读；这不影响患者故事或完成状态。</p></section>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        f'<header class="cc-knowledge-topic-head"><h2>{html.escape(topic.name or "名称未解析")}</h2>'
        '<p>以下内容来自精确离线资料关系，不携带患者上下文。</p></header>',
        unsafe_allow_html=True,
    )
    _render_claims(topic)
    _render_coverage(topic)
    details_open = st.query_params.get("cc_knowledge_details") == "sources"
    render_disclosure_controls(
        st,
        query_parameter="cc_knowledge_details",
        page_path="/knowledge_evidence",
        options=(
            (
                "sources",
                "收起来源与版本" if details_open else "查看来源与版本",
            ),
        ),
        aria_label="Knowledge 来源与版本",
        panel_id="cc-knowledge-sources-panel",
    )
    if details_open:
        _render_sources(projection, topic)

st.markdown(
    '<p class="cc-knowledge-fixed-boundary">' + " ".join(KNOWLEDGE_BOUNDARIES) + "</p>",
    unsafe_allow_html=True,
)
with st.container(key="cc_knowledge_home_link"):
    _home_link()

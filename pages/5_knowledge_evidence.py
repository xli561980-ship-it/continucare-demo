"""Read-only product view over the governed Knowledge Ops read models."""

from __future__ import annotations

import html
from collections import Counter
from typing import Any

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

from continucare.knowledge.ops.models import KnowledgeOpsManifestError
from continucare.knowledge.ops.read_model import load_builtin_ops_read_model
from continucare.ui import inject_global_styles


KNOWLEDGE_BOUNDARY = (
    "Knowledge 当前只提供来源、术语和治理状态的只读说明；"
    "不读取患者记录，也不直接匹配患者表达；患者端只能生成待确认候选，"
    "须经本人确认后写入。Knowledge 不授权诊断、分诊、治疗或临床规则。"
)

SECTION_LABELS = {
    "sources": "来源库",
    "terminology": "术语治理",
    "review": "审核流程",
    "release": "发布状态",
}

SOURCE_GROUP_LABELS = {
    "regulatory": ("监管与药品资料", "官方药品与监管信息的来源策略"),
    "standards": ("医学标准与量表", "术语、互操作标准和标准化量表"),
    "research": ("患者教育与研究", "患者教育、文献和开放获取元数据"),
    "other": ("其他已登记来源", "等待产品分类更新的来源策略"),
}

SOURCE_TYPE_GROUPS = {
    "regulatory": (
        "regulatory_product_information",
        "regulatory_product_information_metadata",
        "regulatory_safety_communication",
    ),
    "standards": (
        "terminology_standard",
        "interoperability_standard",
        "standard_instrument",
    ),
    "research": (
        "patient_education_metadata",
        "peer_reviewed_study",
        "peer_reviewed_study_metadata",
        "open_access_article_license_metadata",
    ),
}

SOURCE_TYPE_LABELS = {
    "regulatory_product_information": "监管药品资料",
    "regulatory_product_information_metadata": "监管药品资料元数据",
    "regulatory_safety_communication": "监管安全通告",
    "terminology_standard": "医学术语标准",
    "interoperability_standard": "互操作标准",
    "standard_instrument": "标准化量表",
    "patient_education_metadata": "患者教育元数据",
    "peer_reviewed_study": "同行评议研究",
    "peer_reviewed_study_metadata": "同行评议研究元数据",
    "open_access_article_license_metadata": "开放获取文献许可元数据",
}

LICENSE_LABELS = {
    "needs_verification": "复用权利待核验",
    "registration_required": "需要注册与许可核验",
    "license_required": "需要正式许可",
    "verified_restricted": "已核验但限制使用",
    "verified_open": "已核验开放使用",
}

OPERATION_LABELS = {
    "register_link_metadata": "登记官方链接与元数据",
    "discover_metadata": "发现公开元数据",
    "fetch_for_change_detection": "用于变更检测",
    "persist_snapshot": "保存来源快照",
    "persist_full_text": "保存完整正文",
    "short_quote": "引用短句",
    "translate": "翻译",
    "adapt": "改编为产品内容",
    "redistribute": "再分发",
    "create_mapping": "创建术语映射",
    "commercial_use": "商业使用",
    "model_training": "模型训练",
    "vector_index": "正文向量化",
}

GATE_LABELS = {
    "source_promotion": "来源入库",
    "content_persistence": "正文保存",
    "terminology_mapping_promotion": "术语映射",
    "translation_promotion": "翻译内容",
    "clinical_claim_approval": "临床声明",
    "binding_approval": "适用范围绑定",
    "patient_content_approval": "患者内容",
    "knowledge_release": "版本发布",
}

ROLE_LABELS = {
    "knowledge_curator": "知识管理员",
    "rights_officer": "版权审核",
    "terminologist": "术语专家",
    "clinical_reviewer": "临床审核",
    "pharmacist": "药师审核",
}

GAP_LABELS = {
    "live_validation_not_attempted": "真实来源联通尚未验证",
    "rights_unresolved": "内容复用权利尚未确认",
    "cold_import_socket_proof_pending": "离线导入边界仍待证明",
    "terminology_alias_review_pending": "患者表达与术语关系尚待审核",
}


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _label(labels: dict[str, str], value: Any, fallback: str) -> str:
    return labels.get(_enum_value(value), fallback)


def _home_link() -> None:
    try:
        st.page_link("app.py", label="返回演示首页", width="stretch")
    except (StreamlitPageNotFoundError, KeyError):
        st.markdown("[返回演示首页](/)")


def _source_group(policy: Any) -> str:
    source_types = {_enum_value(item) for item in policy.source_types}
    for group, known_types in SOURCE_TYPE_GROUPS.items():
        if source_types.intersection(known_types):
            return group
    return "other"


def _terminology_roles(ops: Any) -> tuple[str, ...]:
    gate = next(
        (
            item
            for item in ops.review_gates
            if _enum_value(item.gate) == "terminology_mapping_promotion"
        ),
        None,
    )
    if gate is None:
        return ()
    return tuple(
        _label(ROLE_LABELS, role, "待配置审核角色") for role in gate.required_roles
    )


def _render_boundary_footer() -> None:
    st.markdown(
        f'<footer class="cc-kc-footer">{html.escape(KNOWLEDGE_BOUNDARY)}</footer>',
        unsafe_allow_html=True,
    )


def _render_hero(ops: Any) -> None:
    metrics = (
        (len(ops.source_policies), "条来源策略"),
        (len(ops.review_gates), "道人工审核门"),
        (len(ops.governance_readiness.open_gaps), "项待补治理证据"),
        (len(ops.production_releases), "个正式发布版本"),
    )
    metric_html = "".join(
        f"<article><strong>{value}</strong><span>{html.escape(label)}</span></article>"
        for value, label in metrics
    )
    st.markdown(
        f"""
        <section class="cc-kc-hero">
          <div class="cc-kc-hero-copy">
            <span class="cc-kc-kicker">KNOWLEDGE OPERATIONS · READ ONLY</span>
            <h1>让每条知识都能回答<br>“从哪里来、谁审核过”</h1>
            <p>这里展示 ContinuCare 已建立的来源策略、术语目录、人工审核门和发布边界。
            当前是离线治理准备状态，不读取患者故事，也不自动形成临床结论。</p>
          </div>
          <ol class="cc-kc-flow" aria-label="Knowledge 治理流程">
            <li class="is-ready"><span>01</span><div><strong>来源登记</strong><small>版本与使用边界</small></div></li>
            <li class="is-ready"><span>02</span><div><strong>证据候选</strong><small>保留定位与追溯</small></div></li>
            <li><span>03</span><div><strong>人工审核</strong><small>正式审核尚未完成</small></div></li>
            <li><span>04</span><div><strong>版本发布</strong><small>当前尚无正式发布版本</small></div></li>
          </ol>
        </section>
        <section class="cc-kc-metrics">{metric_html}</section>
        """,
        unsafe_allow_html=True,
    )


def _render_sources(ops: Any) -> None:
    groups: dict[str, list[Any]] = {key: [] for key in SOURCE_GROUP_LABELS}
    for policy in ops.source_policies:
        groups[_source_group(policy)].append(policy)
    group_cards = []
    for key, (title, description) in SOURCE_GROUP_LABELS.items():
        if not groups[key]:
            continue
        names = "".join(
            f"<li>{html.escape(item.display_name)}</li>" for item in groups[key]
        )
        group_cards.append(
            '<article class="cc-kc-group-card">'
            f"<span>{len(groups[key])} 条策略</span>"
            f"<h2>{html.escape(title)}</h2>"
            f"<p>{html.escape(description)}</p>"
            f"<ul>{names}</ul>"
            "</article>"
        )
    st.markdown(
        '<header class="cc-kc-section-head"><div><span>SOURCE LIBRARY</span>'
        '<h2>来源库</h2></div><p>展示已登记的来源策略，不等于已经抓取或获准复用正文。</p></header>'
        f'<section class="cc-kc-group-grid">{"".join(group_cards)}</section>',
        unsafe_allow_html=True,
    )

    policy_map = {
        f"{item.policy_id}@{item.policy_version}": item
        for item in ops.source_policies
    }
    selected_key = st.selectbox(
        "查看具体来源策略",
        tuple(policy_map),
        format_func=lambda key: (
            f"{policy_map[key].display_name} · v{policy_map[key].policy_version}"
        ),
        key="cc_knowledge_source_policy",
    )
    selected = policy_map[selected_key]
    allowed = tuple(
        _label(OPERATION_LABELS, item.operation, "待配置操作名称")
        for item in selected.operation_rules
        if _enum_value(item.decision) == "allow"
    )
    allowed_html = "".join(f"<li>{html.escape(item)}</li>" for item in allowed)
    source_types = " · ".join(
        _label(SOURCE_TYPE_LABELS, item, "其他已登记来源类型")
        for item in selected.source_types
    )
    st.markdown(
        f"""
        <section class="cc-kc-source-detail">
          <div class="cc-kc-source-main">
            <span>当前来源策略</span>
            <h3>{html.escape(selected.display_name)}</h3>
            <p>{html.escape(selected.issuing_authority)}</p>
            <small>{html.escape(source_types)}</small>
          </div>
          <div class="cc-kc-source-status">
            <strong>{html.escape(_label(LICENSE_LABELS, selected.license_posture, "许可状态待配置"))}</strong>
            <span>真实网络：未启用</span>
          </div>
          <div class="cc-kc-source-allowed">
            <strong>当前明确允许</strong>
            <ul>{allowed_html or '<li>暂无可自动执行的操作</li>'}</ul>
          </div>
          <div class="cc-kc-source-boundary">
            <strong>当前不代表</strong>
            <p>不代表已保存来源正文、取得商业复用许可，或形成任何诊断与临床建议。</p>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_terminology(ops: Any) -> None:
    alias_gap = next(
        (
            item
            for item in ops.governance_readiness.open_gaps
            if _enum_value(item.gap_kind) == "terminology_alias_review_pending"
        ),
        None,
    )
    roles = _terminology_roles(ops)
    role_count = str(len(roles)) if roles else "待配置"
    role_names = "、".join(roles) if roles else "正式审核角色尚未配置"
    if alias_gap is None:
        catalog_version = "—"
        catalog_caption = "当前清单未提供目录版本"
        concept_count = "—"
        concept_caption = "当前清单未报告待复核概念"
        gap_state = "当前清单未报告待处理术语缺口"
        gap_detail = (
            "缺口消失本身不等于自动获批；仍须以正式 Knowledge Release、"
            "版本化消费合同和独立 UI 审核为准。"
        )
    else:
        subject = alias_gap.subject
        catalog_version = f"v{subject.catalog_version}"
        catalog_caption = "当前版本化术语目录"
        concept_count = str(len(subject.concept_refs))
        concept_caption = "个既有概念等待正式复核"
        gap_state = "治理缺口仍待处理"
        gap_detail = (
            "未完成正式术语审核、内容权利确认和后续版本发布，"
            "Knowledge 不能用这些表达直接匹配患者文本。"
        )
    st.markdown(
        f"""
        <header class="cc-kc-section-head"><div><span>TERMINOLOGY GOVERNANCE</span>
        <h2>术语治理</h2></div><p>术语目录属于 Knowledge 资产；具体症状不是一级分类。</p></header>
        <section class="cc-kc-term-summary">
          <article><strong>{html.escape(catalog_version)}</strong><span>{html.escape(catalog_caption)}</span></article>
          <article><strong>{html.escape(concept_count)}</strong><span>{html.escape(concept_caption)}</span></article>
          <article><strong>{html.escape(role_count)}</strong><span>类正式审核角色要求</span></article>
          <article class="is-caution"><strong>候选建议</strong><span>患者口语标准化模式</span></article>
        </section>
        <section class="cc-kc-term-detail">
          <div>
            <span>当前术语资产</span>
            <h3>核心症状术语目录</h3>
            <p>Core Symptom Catalog · {html.escape(catalog_version)}</p>
          </div>
          <strong class="cc-kc-term-state">{html.escape(gap_state)}</strong>
          <section><h4>已经建立</h4><ul>
            <li>版本化目录与精确术语引用</li>
            <li>患者表达的技术边界审计</li>
            <li>审核角色：{html.escape(role_names)}</li>
          </ul></section>
          <section><h4>当前产品模式</h4><p>{html.escape(gap_detail)}患者端仅由受控语义层生成待确认候选，必须经本人确认后才写入记录。</p></section>
        </section>
        <p class="cc-kc-inline-boundary">患者口语标准化：候选建议模式 · 本人确认后写入 · Knowledge 不直接匹配患者文本。系统不会根据单一症状推断用药背景。</p>
        """,
        unsafe_allow_html=True,
    )


def _render_review(ops: Any) -> None:
    cards = []
    for gate in ops.review_gates:
        roles = "".join(
            f"<li>{html.escape(_label(ROLE_LABELS, role, '待配置审核角色'))}</li>"
            for role in gate.required_roles
        )
        gate_value = _enum_value(gate.gate)
        cards.append(
            '<article class="cc-kc-gate-card">'
            "<span>必须人工审批</span>"
            f"<h3>{html.escape(_label(GATE_LABELS, gate_value, '待配置审核门'))}</h3>"
            f"<ul>{roles}</ul>"
            "</article>"
        )
    st.markdown(
        '<header class="cc-kc-section-head"><div><span>HUMAN REVIEW GATES</span>'
        '<h2>审核流程</h2></div><p>模型输出和测试事件都不能代替正式审核人。</p></header>'
        f'<section class="cc-kc-gate-grid">{"".join(cards)}</section>',
        unsafe_allow_html=True,
    )


def _render_release(ops: Any) -> None:
    gap_counts = Counter(item.gap_kind for item in ops.governance_readiness.open_gaps)
    gaps = "".join(
        f"<li><strong>{count}</strong><span>{html.escape(_label(GAP_LABELS, kind, '待配置治理事项'))}</span></li>"
        for kind, count in gap_counts.items()
    )
    st.markdown(
        f"""
        <header class="cc-kc-section-head"><div><span>RELEASE READINESS</span>
        <h2>发布状态</h2></div><p>能力已经构建，但正式知识发布门禁尚未解除。</p></header>
        <section class="cc-kc-release-card">
          <div class="cc-kc-release-status">
            <span>当前状态</span><strong>治理准备中 · 尚未正式发布</strong>
            <p>知识包 v{html.escape(str(ops.bundle_version))} · 仅用于治理准备</p>
          </div>
          <ul class="cc-kc-gap-list">{gaps}</ul>
        </section>
        <section class="cc-kc-release-boundaries">
          <article><h3>现在可以做</h3><ul>
            <li>只读展示版本化来源策略和术语条目</li>
            <li>说明每项能力还缺哪些治理证据</li>
            <li>离线回放审核与发布门禁</li>
          </ul></article>
          <article><h3>现在不能做</h3><ul>
            <li>不能把未审核患者表达作为直接匹配规则</li>
            <li>不能把资料自动变成临床规则或患者建议</li>
            <li>不能声称已有正式 Knowledge Release</li>
          </ul></article>
        </section>
        <p class="cc-kc-inline-boundary">患者口语标准化：候选建议模式 · 本人确认后写入 · Knowledge 运行时权限：无</p>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="Knowledge 中心 · ContinuCare",
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_global_styles(st)
st.markdown('<span class="cc-knowledge-shell" aria-hidden="true"></span>', unsafe_allow_html=True)
st.markdown(
    """
    <header class="cc-role-topbar">
      <span class="cc-role-topbar-brand"><i aria-hidden="true">C</i> ContinuCare</span>
      <span class="cc-role-topbar-badge">离线只读 · 合成演示</span>
    </header>
    """,
    unsafe_allow_html=True,
)

try:
    ops_read_model = load_builtin_ops_read_model()
except (KnowledgeOpsManifestError, ValueError):
    st.error("Knowledge 治理资料未能通过完整性校验；页面保持只读且不回退到旧症状 fixture。")
    _render_boundary_footer()
    _home_link()
    st.stop()

_render_hero(ops_read_model)

selected_section = st.radio(
    "Knowledge 模块",
    tuple(SECTION_LABELS),
    format_func=lambda key: SECTION_LABELS[key],
    horizontal=True,
    key="cc_knowledge_section",
    help="分类对应 Knowledge 的真实数据层；症状只在术语目录中作为条目出现。",
)

if selected_section == "sources":
    _render_sources(ops_read_model)
elif selected_section == "terminology":
    _render_terminology(ops_read_model)
elif selected_section == "review":
    _render_review(ops_read_model)
else:
    _render_release(ops_read_model)

_render_boundary_footer()
with st.container(key="cc_knowledge_home_link"):
    _home_link()

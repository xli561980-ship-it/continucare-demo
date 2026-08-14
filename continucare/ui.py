"""Shared visual safety cues and responsive layout rules."""

from __future__ import annotations

import html


def inject_global_styles(st) -> None:
    st.markdown(
        """
        <style>
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
        @media (max-width: 768px) {
            .block-container {padding: 1rem .85rem 3rem;}
            h1 {font-size: 1.75rem !important; line-height: 1.25 !important;}
            h2 {font-size: 1.35rem !important; line-height: 1.3 !important;}
            h3 {font-size: 1.12rem !important; line-height: 1.38 !important;}
            [data-testid="stHorizontalBlock"] {gap: .75rem;}
            [data-testid="stMetric"] {min-width: 0;}
            [data-testid="stButton"] button {min-height: 2.75rem;}
            [data-testid="stChatMessage"] {max-width: 100%;}
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_mode_badges(st) -> None:
    model_label = html.escape(semantic_model_label())
    st.markdown(
        f"""
        <span class="cc-mode-chip">本地稳定演示</span>
        <span class="cc-mode-chip">{model_label}</span>
        <span class="cc-mode-chip">Safety Agent v4 · 规则 + 可选 MiMo Critic</span>
        <span class="cc-mode-chip">SQLite 持久化</span>
        <span class="cc-mode-chip">飞书通知 Mock · 未联调</span>
        """,
        unsafe_allow_html=True,
    )


def semantic_model_label() -> str:
    from continucare.care_agent.model_api import build_model_adapter

    adapter = build_model_adapter()
    if adapter.configured:
        return f"MiMo {adapter.config.model_name} 已启用"
    return "Care Agent 语义 Mock 回退"

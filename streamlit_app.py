"""Hidden Streamlit router exposing stable, role-specific demo URLs."""

from __future__ import annotations

import streamlit as st

from continucare.navigation import APP_ROUTES


pages = [
    st.Page(
        route.source,
        title=route.title,
        icon=route.icon,
        url_path=route.url_path,
        default=route.default,
        visibility="hidden",
    )
    for route in APP_ROUTES
]

selected_page = st.navigation(pages, position="hidden")
selected_page.run()

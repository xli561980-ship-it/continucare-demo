"""Single-source navigation contract for the separated demo role portals."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppRoute:
    route_id: str
    source: str
    title: str
    icon: str
    url_path: str | None = None
    default: bool = False


APP_ROUTES = (
    AppRoute(
        route_id="demo",
        source="app.py",
        title="演示控制台",
        icon=":material/hub:",
        default=True,
    ),
    AppRoute(
        route_id="patient",
        source="pages/1_patient_followup.py",
        title="患者端",
        icon=":material/person:",
        url_path="patient",
    ),
    AppRoute(
        route_id="nurse",
        source="pages/2_nurse_risk_center.py",
        title="护士端",
        icon=":material/medical_services:",
        url_path="nurse",
    ),
    AppRoute(
        route_id="doctor",
        source="pages/3_doctor_summary.py",
        title="医生端",
        icon=":material/clinical_notes:",
        url_path="doctor",
    ),
    AppRoute(
        route_id="records",
        source="pages/4_audit_log.py",
        title="记录追溯",
        icon=":material/fact_check:",
        url_path="records",
    ),
    AppRoute(
        route_id="knowledge",
        source="pages/5_knowledge_evidence.py",
        title="Knowledge 资料库",
        icon=":material/menu_book:",
        url_path="knowledge",
    ),
)

ROLE_ROUTES = tuple(
    route for route in APP_ROUTES if route.route_id in {"patient", "nurse", "doctor"}
)

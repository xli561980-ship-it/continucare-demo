"""Reusable Streamlit component; intentionally not wired into app navigation."""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from continucare.case_intake.models import (
    CaseAuthor,
    CaseRecord,
    ClinicianAuthoredText,
    ManualCaseInput,
)
from continucare.case_intake.service import CaseIntakeService


def _lines(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in value.splitlines() if line.strip())


def _note(
    text: str,
    *,
    author: CaseAuthor,
    authored_at: datetime,
) -> ClinicianAuthoredText | None:
    value = text.strip()
    if not value:
        return None
    return ClinicianAuthoredText(
        text=value,
        practitioner_id=author.practitioner_id,
        authored_at=authored_at,
    )


def render_manual_case_intake(
    service: CaseIntakeService,
    *,
    patient_id: str,
    author: CaseAuthor,
    pathway_code: str | None = None,
    pathway_version: str | None = None,
    key: str = "case_intake_manual",
) -> CaseRecord | None:
    """Render a text-only synthetic case form and return a saved record.

    The caller owns feature gating and page placement. This component never
    initializes Case Intake, changes navigation, or writes patient-confirmed
    QuestionnaireResponse/Observation resources.
    """

    st.subheader("病例接入（合成数据）")
    st.caption(
        "医生录入与患者本人确认的随访事实严格分开。"
        "本组件不生成诊断、风险等级、处方或治疗建议。"
    )
    with st.form(key):
        encounter_id = st.text_input("就诊 ID", key=f"{key}::encounter")
        encounter_date = st.date_input("就诊日期", key=f"{key}::date")
        chief_complaint = st.text_area("主诉", key=f"{key}::complaint")
        present_illness = st.text_area("现病史（可选）", key=f"{key}::illness")
        past_history = st.text_area("既往史（可选）", key=f"{key}::history")
        medications = st.text_area(
            "用药史（可选，每行一项）", key=f"{key}::medications"
        )
        allergies = st.text_area(
            "过敏史（可选，每行一项）", key=f"{key}::allergies"
        )
        test_summary = st.text_area(
            "检查摘要（可选，仅录入已有合成结果）", key=f"{key}::tests"
        )
        assessment = st.text_area(
            "医生评估（可选，标记为医生手工录入）",
            key=f"{key}::assessment",
        )
        plan = st.text_area(
            "随访计划（可选，标记为医生手工录入）", key=f"{key}::plan"
        )
        submitted = st.form_submit_button("保存合成病例", type="primary")

    if not submitted:
        return None

    recorded_at = datetime.now(timezone.utc)
    try:
        record = service.create_manual(
            ManualCaseInput(
                patient_id=patient_id,
                encounter_id=encounter_id,
                pathway_code=pathway_code,
                pathway_version=pathway_version,
                author=author,
                recorded_at=recorded_at,
                encounter_date=encounter_date,
                chief_complaint=chief_complaint,
                present_illness=present_illness or None,
                past_history=past_history or None,
                medication_history=_lines(medications),
                allergy_history=_lines(allergies),
                test_summary=test_summary or None,
                clinician_assessment=_note(
                    assessment, author=author, authored_at=recorded_at
                ),
                followup_plan=_note(plan, author=author, authored_at=recorded_at),
            )
        )
    except (LookupError, RuntimeError, ValueError) as exc:
        st.error(f"病例未保存：{exc}")
        return None
    st.success(f"病例已保存：{record.case_id} · v{record.version}")
    return record

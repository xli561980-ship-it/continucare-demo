"""Synthetic-only fixtures for FHIR collection and traceability demos."""

from continucare.fhir.terminology import UCUM

DEMO_PATIENT_ID = "P-DEMO-001"

NAUSEA_MESSAGE = "今天有点恶心，但是能正常喝水，没有吐。"
QUANTIFIED_MESSAGE = "今天吐了一次，估计过去24小时喝水800毫升。"
UNSTRUCTURED_MESSAGE = "我现在胸口很痛，还有点喘不过气。"
# The competition story must stay inside the active CN Layer-3 fixed
# Questionnaire whitelist.  Newly reported symptoms such as “拉肚子” remain
# raw, unstructured evidence until a governed dynamic terminology release is
# available; “恶心” is a fixed coded Questionnaire item and can therefore
# produce the single patient-confirmable synthetic candidate used by the demo.
MANUAL_REVIEW_MESSAGE = "我今天有恶心。"

SCENARIOS = {
    "恶心记录": NAUSEA_MESSAGE,
    "呕吐与摄入记录": QUANTIFIED_MESSAGE,
    "仅保留患者原文": UNSTRUCTURED_MESSAGE,
}

STRUCTURED_SCENARIOS = {
    "恶心记录": {
        "nausea-present": True,
        "nausea-severity": "LA6752-5",
        "vomiting-count-24h": 0,
        "fluid-intake-24h-estimated": {
            "value": 1500,
            "unit": "mL",
            "system": UCUM,
            "code": "mL",
        },
        "abdominal-pain-present": False,
        "free-text-report": "今天有一点恶心，其他情况为合成演示。",
    },
    "呕吐与摄入记录": {
        "nausea-present": False,
        "vomiting-count-24h": 1,
        "fluid-intake-24h-estimated": {
            "value": 800,
            "unit": "mL",
            "system": UCUM,
            "code": "mL",
        },
        "abdominal-pain-present": False,
        "free-text-report": QUANTIFIED_MESSAGE,
    },
    "仅保留患者原文": {
        "free-text-report": f"{UNSTRUCTURED_MESSAGE}（合成演示）"
    },
}

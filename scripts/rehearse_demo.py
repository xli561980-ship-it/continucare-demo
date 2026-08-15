"""Run the FHIR-native synthetic story three times without external services."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from continucare.adapters.mock_extractor import MockExtractor
from continucare.adapters.mock_notifier import MockNotifier
from continucare.adapters.sqlite_store import SQLiteStore
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.services.demo_scenarios import load_scenario
from continucare.services.summaries import SummaryService


def rehearse_once(db_path: Path) -> None:
    nausea = load_scenario(db_path, "恶心记录")
    assert nausea.decision.severity == "not_assessed" and nausea.alert is None
    assert {item.code for item in nausea.extraction.observations} == {"422587007"}

    quantified = load_scenario(db_path, "呕吐与摄入记录")
    assert quantified.decision.severity == "not_assessed"
    assert quantified.alert is None
    assert {item.code for item in quantified.extraction.observations} == {
        "94070-0",
        "75301-2",
    }
    store = SQLiteStore(db_path)
    response = store.get_questionnaire_response(quantified.message.message_id)
    assert response["resourceType"] == "QuestionnaireResponse"
    summaries = SummaryService(store, MockExtractor(), MockNotifier())
    summary = summaries.generate(DEMO_PATIENT_ID)
    summaries.review(summary.summary_id)
    assert SQLiteStore(db_path).get_summary(summary.summary_id).status == "reviewed"

    unstructured = load_scenario(db_path, "仅保留患者原文")
    assert unstructured.decision.severity == "not_assessed"
    assert unstructured.alert is None
    assert unstructured.extraction.observations == []
    assert SQLiteStore(db_path).get_questionnaire_response(
        unstructured.message.message_id
    )["resourceType"] == "QuestionnaireResponse"


def main() -> None:
    with TemporaryDirectory(prefix="continucare-rehearsal-") as directory:
        for run in range(1, 4):
            rehearse_once(Path(directory) / f"run-{run}.db")
            print(f"rehearsal {run}/3: passed")


if __name__ == "__main__":
    main()

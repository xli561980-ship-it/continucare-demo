"""One-click, synthetic-only scenario reset and loading."""

from __future__ import annotations

from pathlib import Path

from continucare.adapters.mock_extractor import MockExtractor
from continucare.adapters.mock_notifier import MockNotifier
from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_engine import CareEngine, CareSubmissionResult
from continucare.care_agent import CareAgentService, SemanticInteraction
from continucare.care_agent.model_api import (
    SemanticModelConfig,
    UnconfiguredModelAdapter,
)
from continucare.db import reset_demo
from continucare.demo_data import (
    DEMO_PATIENT_ID,
    MANUAL_REVIEW_MESSAGE,
    SCENARIOS,
    STRUCTURED_SCENARIOS,
)
from continucare.services.alerts import AlertService
from continucare.services.extraction import ExtractionService
from continucare.services.followup import FollowUpService
from continucare.services.workflow import FollowUpWorkflow, WorkflowResult


def load_scenario(db_path: Path | str, scenario_label: str) -> WorkflowResult:
    if scenario_label not in SCENARIOS:
        raise ValueError(f"未知合成场景：{scenario_label}")
    reset_demo(db_path)
    store = SQLiteStore(db_path)
    workflow = FollowUpWorkflow(
        FollowUpService(store),
        ExtractionService(store, MockExtractor()),
        AlertService(store, MockNotifier()),
    )
    return workflow.submit(DEMO_PATIENT_ID, SCENARIOS[scenario_label])


def load_layer2_scenario(
    db_path: Path | str, scenario_label: str
) -> CareSubmissionResult:
    """Reset and load a Questionnaire-driven scenario without Mock extraction."""

    if scenario_label not in STRUCTURED_SCENARIOS:
        raise ValueError(f"未知合成场景：{scenario_label}")
    reset_demo(db_path)
    store = SQLiteStore(db_path)
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    return engine.complete(session.session_id, STRUCTURED_SCENARIOS[scenario_label])


def load_manual_review_scenario(db_path: Path | str) -> SemanticInteraction:
    """Reset and create a local-only Layer-3 candidate, without releasing it."""

    reset_demo(db_path)
    store = SQLiteStore(db_path)
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    agent = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        patient_timezone="Asia/Shanghai",
    )
    return agent.analyze(session.session_id, MANUAL_REVIEW_MESSAGE)

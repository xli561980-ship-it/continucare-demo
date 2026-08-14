from __future__ import annotations

import pytest

from scripts.rehearse_demo import rehearse_once


@pytest.mark.parametrize("run_number", [1, 2, 3])
def test_three_consecutive_demo_rehearsals(tmp_path, run_number):
    rehearse_once(tmp_path / f"rehearsal-{run_number}.db")


def test_one_click_scenario_reset_removes_previous_story(tmp_path):
    from continucare.adapters.sqlite_store import SQLiteStore
    from continucare.services.demo_scenarios import load_scenario

    db_path = tmp_path / "scenarios.db"
    quantified = load_scenario(db_path, "呕吐与摄入记录")
    assert quantified.alert is None

    nausea = load_scenario(db_path, "恶心记录")
    store = SQLiteStore(db_path)

    assert nausea.alert is None
    assert store.list_alerts() == []
    assert len(store.list_messages("P-DEMO-001")) == 1


def test_layer2_one_click_scenario_uses_structured_questionnaire(tmp_path):
    from continucare.services.demo_scenarios import load_layer2_scenario

    db_path = tmp_path / "layer2-scenario.db"
    result = load_layer2_scenario(db_path, "呕吐与摄入记录")

    assert {item.code for item in result.observations} == {"94070-0", "75301-2"}
    assert result.questionnaire_response["status"] == "completed"
    assert result.session.pathway_version == "1.0.0"

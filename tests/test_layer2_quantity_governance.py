from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_engine import CareEngine
from continucare.care_engine.mapping import map_response_to_observations
from continucare.db import connect
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.fhir.questionnaires import build_questionnaire_response
from continucare.fhir.r4 import FHIRValidationError
from continucare.fhir.terminology import UCUM
from continucare.models import CareSessionStatus
from continucare.pathways import (
    load_glp1_observation_mapping,
    load_glp1_questionnaire,
)


_LINK_ID = "fluid-intake-24h-estimated"
_COMPLETION_TABLES = (
    ("care_sessions", "session_id"),
    ("followup_messages", "message_id"),
    ("fhir_questionnaire_responses", "resource_id"),
    ("fhir_observations", "observation_id"),
    ("observation_evidence", "observation_id"),
    ("audit_events", "event_id"),
    ("alerts", "alert_id"),
)


def _quantity(**overrides: Any) -> dict[str, Any]:
    quantity: dict[str, Any] = {
        "value": 800,
        "unit": "mL",
        "system": UCUM,
        "code": "mL",
    }
    quantity.update(overrides)
    return quantity


def _answers(quantity: Any) -> dict[str, Any]:
    return {_LINK_ID: quantity}


def _built_quantity(quantity: Any) -> dict[str, Any]:
    response = build_questionnaire_response(
        questionnaire=load_glp1_questionnaire(),
        response_id="response-quantity-governance",
        patient_id=DEMO_PATIENT_ID,
        authored="2026-08-01T10:00:00+00:00",
        answers=_answers(quantity),
    )
    response_item = next(
        item for item in response["item"] if item["linkId"] == _LINK_ID
    )
    return response_item["answer"][0]["valueQuantity"]


def _policy_with_quantity_codes(*codes: str):
    policy = load_glp1_observation_mapping()
    return policy.model_copy(
        update={
            "mappings": [
                mapping.model_copy(
                    update={"accepted_quantity_unit_codes": list(codes)}
                )
                if mapping.link_id == _LINK_ID
                else mapping
                for mapping in policy.mappings
            ]
        }
    )


def _completion_snapshot(store: SQLiteStore) -> dict[str, list[dict[str, Any]]]:
    with connect(store.db_path) as connection:
        return {
            table: [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY {order_by}"
                ).fetchall()
            ]
            for table, order_by in _COMPLETION_TABLES
        }


def _assert_in_progress_without_submission(
    store: SQLiteStore, session_id: str
) -> None:
    session = store.get_care_session(session_id)
    assert session is not None
    assert session.status == CareSessionStatus.IN_PROGRESS
    assert session.questionnaire_response_id is None
    assert session.completed_at is None
    assert store.list_messages(DEMO_PATIENT_ID) == []
    assert store.list_observations(DEMO_PATIENT_ID) == []
    assert store.list_alerts(DEMO_PATIENT_ID) == []
    assert all(
        event.event_type != "questionnaire_response_completed"
        for event in store.list_audit_events(DEMO_PATIENT_ID)
    )


def test_shipped_quantity_policy_allows_only_the_millilitre_code():
    policy = load_glp1_observation_mapping()
    mapping = next(item for item in policy.mappings if item.link_id == _LINK_ID)

    assert mapping.kind == "millilitres_per_24_hours"
    assert mapping.accepted_quantity_unit_codes == ["mL"]


def test_quantity_code_must_remain_allowed_by_the_mapping_policy():
    questionnaire = load_glp1_questionnaire()
    response = build_questionnaire_response(
        questionnaire=questionnaire,
        response_id="response-policy-allowlist",
        patient_id=DEMO_PATIENT_ID,
        authored="2026-08-01T10:00:00+00:00",
        answers=_answers(_quantity()),
    )
    with pytest.raises(FHIRValidationError):
        map_response_to_observations(
            response=response,
            questionnaire=questionnaire,
            policy=_policy_with_quantity_codes("L"),
        )


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param(_quantity(unit="mL "), id="unit-whitespace-is-not-trimmed"),
        pytest.param(_quantity(code="L"), id="fixed-code-is-not-relaxed"),
        pytest.param(
            _quantity(value=0.8, unit="L", code="L"),
            id="litres-are-not-converted",
        ),
    ],
)
def test_quantity_identity_remains_exact_when_policy_allowlist_is_expanded(
    quantity,
):
    assert _built_quantity(quantity) == quantity
    questionnaire = load_glp1_questionnaire()
    response = build_questionnaire_response(
        questionnaire=questionnaire,
        response_id="response-expanded-policy",
        patient_id=DEMO_PATIENT_ID,
        authored="2026-08-01T10:00:00+00:00",
        answers=_answers(quantity),
    )

    with pytest.raises(FHIRValidationError):
        map_response_to_observations(
            response=response,
            questionnaire=questionnaire,
            policy=_policy_with_quantity_codes("mL", "L"),
        )


def test_exact_governed_quantity_completes_with_normalized_24_hour_output(tmp_path):
    store = SQLiteStore(tmp_path / "quantity-valid.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    quantity = _quantity()

    result = engine.complete(session.session_id, _answers(quantity))

    assert result.session.status == CareSessionStatus.COMPLETED
    assert len(result.observations) == 1
    observation = result.observations[0].resource
    assert observation["code"]["coding"][0]["code"] == "75301-2"
    assert observation["valueQuantity"] == {
        "value": 800,
        "unit": "mL/24 hours",
        "system": UCUM,
        "code": "mL/(24.h)",
    }
    assert math.isfinite(observation["valueQuantity"]["value"])
    assert quantity["code"] != observation["valueQuantity"]["code"]
    assert observation["derivedFrom"] == [
        {
            "reference": (
                f"QuestionnaireResponse/{result.questionnaire_response['id']}"
            )
        }
    ]
    period = observation["effectivePeriod"]
    assert period["end"] == result.questionnaire_response["authored"]
    assert datetime.fromisoformat(period["end"]) - datetime.fromisoformat(
        period["start"]
    ) == timedelta(hours=24)
    assert store.list_alerts(DEMO_PATIENT_ID) == []


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param(_quantity(system="https://example.test/units"), id="wrong-system"),
        pytest.param(_quantity(unit="杯"), id="wrong-display-unit"),
        pytest.param(_quantity(code="L"), id="wrong-code"),
        pytest.param(_quantity(comparator="<"), id="comparator"),
        pytest.param(_quantity(id="quantity-1"), id="element-id"),
    ],
)
def test_mapper_rejects_ungoverned_quantity_without_completion_side_effects(
    tmp_path, quantity
):
    assert _built_quantity(quantity) == quantity
    store = SQLiteStore(tmp_path / "quantity-mapper-rejected.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    before = _completion_snapshot(store)

    with pytest.raises(FHIRValidationError):
        engine.complete(session.session_id, _answers(quantity))

    assert _completion_snapshot(store) == before
    _assert_in_progress_without_submission(store, session.session_id)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="not-a-number"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_mapper_rejects_non_finite_quantity_without_completion_side_effects(
    tmp_path, value
):
    quantity = _quantity(value=value)
    built_quantity = _built_quantity(quantity)
    if math.isnan(value):
        assert math.isnan(built_quantity["value"])
    else:
        assert built_quantity["value"] == value
    store = SQLiteStore(tmp_path / "quantity-non-finite.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    before = _completion_snapshot(store)

    with pytest.raises(
        FHIRValidationError,
        match="requires a finite quantity value",
    ):
        engine.complete(session.session_id, _answers(quantity))

    assert _completion_snapshot(store) == before
    _assert_in_progress_without_submission(store, session.session_id)


def test_finite_negative_quantity_keeps_the_existing_rejection_path(tmp_path):
    quantity = _quantity(value=-1)
    assert _built_quantity(quantity) == quantity
    store = SQLiteStore(tmp_path / "quantity-finite-negative.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    before = _completion_snapshot(store)

    with pytest.raises(FHIRValidationError, match="cannot be negative"):
        engine.complete(session.session_id, _answers(quantity))

    assert _completion_snapshot(store) == before
    _assert_in_progress_without_submission(store, session.session_id)


def test_huge_integer_never_escapes_as_an_overflow_error():
    questionnaire = load_glp1_questionnaire()
    try:
        response = build_questionnaire_response(
            questionnaire=questionnaire,
            response_id="response-huge-integer",
            patient_id=DEMO_PATIENT_ID,
            authored="2026-08-01T10:00:00+00:00",
            answers=_answers(_quantity(value=10**400)),
        )
        map_response_to_observations(
            response=response,
            questionnaire=questionnaire,
            policy=load_glp1_observation_mapping(),
        )
    except OverflowError:
        pytest.fail("finite quantity gate must not raise OverflowError")
    except FHIRValidationError:
        pass


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param(800, id="not-a-dict"),
        pytest.param(
            {"value": 800, "unit": "mL", "code": "mL"},
            id="missing-system",
        ),
        pytest.param(_quantity(value="800"), id="string-value"),
        pytest.param(_quantity(value=True), id="boolean-value"),
    ],
)
def test_builder_rejects_malformed_quantity_without_completion_side_effects(
    tmp_path, quantity
):
    with pytest.raises(FHIRValidationError):
        _built_quantity(quantity)

    store = SQLiteStore(tmp_path / "quantity-builder-rejected.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    before = _completion_snapshot(store)

    with pytest.raises(FHIRValidationError):
        engine.complete(session.session_id, _answers(quantity))

    assert _completion_snapshot(store) == before
    _assert_in_progress_without_submission(store, session.session_id)


def test_draft_may_retain_ungoverned_quantity_but_completion_still_fails_closed(
    tmp_path,
):
    store = SQLiteStore(tmp_path / "quantity-draft-boundary.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    answers = _answers(_quantity(system="https://example.test/units"))

    draft = engine.save_draft(session.session_id, answers)
    assert draft.answers == answers
    before = _completion_snapshot(store)

    with pytest.raises(FHIRValidationError):
        engine.complete(session.session_id, answers)

    assert _completion_snapshot(store) == before
    assert store.get_care_session(session.session_id).answers == answers
    audit_types = [
        event.event_type for event in store.list_audit_events(DEMO_PATIENT_ID)
    ]
    assert audit_types.count("care_session_draft_saved") == 1
    assert "questionnaire_response_completed" not in audit_types


def test_rejected_quantity_can_be_corrected_and_completed_once(tmp_path):
    store = SQLiteStore(tmp_path / "quantity-recovery.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)

    with pytest.raises(FHIRValidationError):
        engine.complete(
            session.session_id,
            _answers(_quantity(system="https://example.test/units")),
        )

    result = engine.complete(session.session_id, _answers(_quantity()))
    snapshot = _completion_snapshot(store)

    assert result.session.status == CareSessionStatus.COMPLETED
    assert len(snapshot["followup_messages"]) == 1
    assert len(snapshot["fhir_questionnaire_responses"]) == 1
    assert len(snapshot["fhir_observations"]) == 1
    assert len(snapshot["observation_evidence"]) == 1
    assert len(snapshot["alerts"]) == 0
    assert [
        event.event_type for event in store.list_audit_events(DEMO_PATIENT_ID)
    ].count("questionnaire_response_completed") == 1

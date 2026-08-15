from __future__ import annotations

import pytest
from continucare.fhir.observations import build_patient_reported_observation
from continucare.fhir.terminology import BODY_WEIGHT, NAUSEA_FINDING
from continucare.layer4 import (
    ClinicalStateService,
    Layer4InputSnapshot,
    Layer4SQLiteStore,
    MetricStateStatus,
    StateMetricDefinition,
    TrendCalculationStatus,
    TrendDirection,
)
from continucare.layer4.contracts import (
    ResourceReference,
    RevisionLink,
    RevisionRelationship,
)


PATIENT_ID = "P-DEMO-001"
PATHWAY_CODE = "GLP1-14D"
PATHWAY_VERSION = "1.0.0"
AS_OF = "2026-08-02T12:00:00+00:00"
UCUM = "http://unitsofmeasure.org"


class MutableInputReader:
    def __init__(self, snapshot: Layer4InputSnapshot):
        self.snapshot = snapshot

    def read(
        self, patient_id: str, *, pathway_code: str, pathway_version: str
    ) -> Layer4InputSnapshot:
        assert patient_id == self.snapshot.patient_id
        assert pathway_code == self.snapshot.pathway_code
        assert pathway_version == self.snapshot.pathway_version
        return self.snapshot


def _quantity_observation(
    observation_id: str,
    *,
    effective_time: str,
    issued_time: str | None = None,
    value: int | float,
    unit: str = "kg",
    unit_system: str = UCUM,
    version_id: str | None = None,
) -> dict:
    resource = build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=PATIENT_ID,
        questionnaire_response_id=f"response-{observation_id}-{version_id or '1'}",
        effective_time=effective_time,
        issued_time=issued_time,
        code=BODY_WEIGHT,
        value_element="valueQuantity",
        value={
            "value": value,
            "unit": unit,
            "system": unit_system,
            "code": unit,
        },
    )
    if version_id is not None:
        resource["meta"] = {
            "versionId": version_id,
            "lastUpdated": issued_time or effective_time,
        }
    return resource


def _boolean_observation(
    observation_id: str, *, effective_time: str, value: bool
) -> dict:
    return build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=PATIENT_ID,
        questionnaire_response_id=f"response-{observation_id}",
        effective_time=effective_time,
        code=NAUSEA_FINDING,
        value_element="valueBoolean",
        value=value,
    )


def _definition(
    metric_id: str = "body-weight",
    *,
    lookback_hours: int = 72,
    stale_after_hours: int = 24,
    trend_window_hours: int = 72,
) -> StateMetricDefinition:
    return StateMetricDefinition(
        metric_id=metric_id,
        version="1.0.0",
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
        code_system=BODY_WEIGHT.system,
        code=BODY_WEIGHT.code,
        display="Body weight",
        unit="kg",
        unit_system=UCUM,
        lookback_hours=lookback_hours,
        stale_after_hours=stale_after_hours,
        trend_window_hours=trend_window_hours,
    )


def _nausea_definition() -> StateMetricDefinition:
    return StateMetricDefinition(
        metric_id="nausea-present",
        version="1.0.0",
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
        code_system=NAUSEA_FINDING.system,
        code=NAUSEA_FINDING.code,
        display="Nausea present",
        lookback_hours=72,
        stale_after_hours=24,
        trend_window_hours=72,
    )


def _snapshot(observations: list[dict]) -> Layer4InputSnapshot:
    return Layer4InputSnapshot(
        patient_id=PATIENT_ID,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
        observations=observations,
        assembled_at=AS_OF,
    )


def _service(tmp_path, snapshot: Layer4InputSnapshot):
    repository = Layer4SQLiteStore(tmp_path / "clinical-state.db")
    reader = MutableInputReader(snapshot)
    service = ClinicalStateService(
        reader,
        repository,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
    )
    return service, repository, reader


def test_snapshot_distinguishes_current_stale_and_unknown_last_known_state(tmp_path):
    current = _quantity_observation(
        "weight-current", effective_time="2026-08-02T11:00:00+00:00", value=71
    )
    stale = _quantity_observation(
        "weight-stale", effective_time="2026-08-01T00:00:00+00:00", value=70
    )
    nausea = _boolean_observation(
        "nausea-stale", effective_time="2026-08-01T00:00:00+00:00", value=True
    )
    service, _, _ = _service(tmp_path, _snapshot([stale, current, nausea]))
    missing_definition = _nausea_definition().model_copy(
        update={"metric_id": "unknown-metric", "code": "unknown-code"}
    )

    result = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition(), _nausea_definition(), missing_definition],
        as_of=AS_OF,
    )

    states = {item.metric_id: item for item in result.states}
    assert states["body-weight"].status == MetricStateStatus.CURRENT
    assert states["body-weight"].latest_value == 71
    assert states["nausea-present"].status == MetricStateStatus.STALE
    assert states["nausea-present"].latest_value is True
    assert states["nausea-present"].age_hours == 36
    assert states["unknown-metric"].status == MetricStateStatus.UNKNOWN
    assert states["unknown-metric"].latest_value is None
    assert states["unknown-metric"].evidence_refs == []


def test_numeric_trend_uses_clinical_time_for_late_arrival_and_has_no_clinical_label(
    tmp_path,
):
    recent = _quantity_observation(
        "weight-recent", effective_time="2026-08-02T10:00:00+00:00", value=72
    )
    late_arrival = _quantity_observation(
        "weight-late",
        effective_time="2026-08-01T10:00:00+00:00",
        issued_time="2026-08-02T11:00:00+00:00",
        value=70,
    )
    service, _, _ = _service(tmp_path, _snapshot([recent, late_arrival]))

    result = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition()],
        as_of=AS_OF,
    )

    trend = result.trends[0]
    assert trend.status == TrendCalculationStatus.CALCULATED
    assert trend.direction == TrendDirection.INCREASING
    assert trend.first_value == "70"
    assert trend.last_value == "72"
    assert trend.delta == "2"
    assert [item.resource.reference for item in trend.evidence_refs] == [
        "Observation/weight-late",
        "Observation/weight-recent",
    ]
    assert "improv" not in trend.model_dump_json().lower()
    assert "wors" not in trend.model_dump_json().lower()


def test_unit_mismatch_is_not_converted_or_silently_skipped(tmp_path):
    kilograms = _quantity_observation(
        "weight-kg", effective_time="2026-08-01T10:00:00+00:00", value=70
    )
    grams = _quantity_observation(
        "weight-g",
        effective_time="2026-08-02T10:00:00+00:00",
        value=71000,
        unit="g",
    )
    service, _, _ = _service(tmp_path, _snapshot([kilograms, grams]))

    result = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition()],
        as_of=AS_OF,
    )

    assert result.states[0].status == MetricStateStatus.UNKNOWN
    assert result.states[0].reason_codes == ["unit_mismatch"]
    assert result.trends[0].status == TrendCalculationStatus.UNIT_MISMATCH
    assert result.trends[0].direction is None
    assert result.trends[0].delta is None
    assert len(result.trends[0].evidence_refs) == 2


def test_conflicting_same_period_values_do_not_choose_state_or_trend(tmp_path):
    first = _quantity_observation(
        "weight-conflict-1", effective_time="2026-08-02T10:00:00+00:00", value=70
    )
    second = _quantity_observation(
        "weight-conflict-2", effective_time="2026-08-02T10:00:00+00:00", value=72
    )
    service, _, _ = _service(tmp_path, _snapshot([second, first]))

    result = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition()],
        as_of=AS_OF,
    )

    assert result.states[0].status == MetricStateStatus.CONFLICT
    assert result.states[0].latest_value is None
    assert len(result.states[0].evidence_refs) == 2
    assert result.trends[0].status == TrendCalculationStatus.CONFLICT
    assert result.trends[0].direction is None


def test_revision_predecessor_is_excluded_from_state_and_trend(tmp_path):
    version_1 = _quantity_observation(
        "weight-revised",
        effective_time="2026-08-02T10:00:00+00:00",
        value=70,
        version_id="1",
    )
    version_2 = _quantity_observation(
        "weight-revised",
        effective_time="2026-08-02T10:00:00+00:00",
        value=71,
        version_id="2",
    )
    service, repository, _ = _service(tmp_path, _snapshot([version_1, version_2]))
    repository.save_contract(
        RevisionLink(
            link_id="revision-weight-1-2",
            patient_id=PATIENT_ID,
            predecessor=ResourceReference(
                reference="Observation/weight-revised", version_id="1"
            ),
            successor=ResourceReference(
                reference="Observation/weight-revised", version_id="2"
            ),
            relationship=RevisionRelationship.CORRECTS,
            reason="Synthetic corrected measurement.",
            actor_reference="Practitioner/tester",
            provenance_reference="Provenance/revision-weight-1-2",
            created_at="2026-08-02T11:00:00+00:00",
        )
    )

    result = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition()],
        as_of=AS_OF,
    )

    assert result.states[0].status == MetricStateStatus.CURRENT
    assert result.states[0].latest_value == 71
    assert result.states[0].latest_observation.version_id == "2"
    assert result.trends[0].status == TrendCalculationStatus.INSUFFICIENT_DATA
    assert result.trends[0].point_count == 1
    assert result.source_observation_refs == [
        ResourceReference(reference="Observation/weight-revised", version_id="2")
    ]


def test_future_effective_or_future_issued_observation_is_excluded(tmp_path):
    future_effective = _quantity_observation(
        "weight-future-effective",
        effective_time="2026-08-03T10:00:00+00:00",
        issued_time="2026-08-02T11:00:00+00:00",
        value=70,
    )
    future_issued = _quantity_observation(
        "weight-future-issued",
        effective_time="2026-08-02T10:00:00+00:00",
        issued_time="2026-08-02T13:00:00+00:00",
        value=71,
    )
    service, _, _ = _service(
        tmp_path, _snapshot([future_effective, future_issued])
    )

    result = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition()],
        as_of=AS_OF,
    )

    assert result.states[0].status == MetricStateStatus.UNKNOWN
    assert result.trends[0].status == TrendCalculationStatus.INSUFFICIENT_DATA
    assert result.source_observation_refs == []


def test_snapshot_is_idempotent_and_late_source_creates_new_version(tmp_path):
    recent = _quantity_observation(
        "weight-version-recent",
        effective_time="2026-08-02T10:00:00+00:00",
        value=72,
    )
    service, repository, reader = _service(tmp_path, _snapshot([recent]))

    first = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition()],
        as_of=AS_OF,
    )
    repeated = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition()],
        as_of=AS_OF,
        generated_at="2026-08-02T12:05:00+00:00",
    )
    late = _quantity_observation(
        "weight-version-late",
        effective_time="2026-08-01T10:00:00+00:00",
        issued_time="2026-08-02T11:30:00+00:00",
        value=70,
    )
    reader.snapshot = _snapshot([recent, late])
    changed = service.build(
        patient_id=PATIENT_ID,
        definitions=[_definition()],
        as_of=AS_OF,
        generated_at="2026-08-02T12:10:00+00:00",
    )

    assert repeated == first
    assert first.version == "1"
    assert changed.snapshot_id == first.snapshot_id
    assert changed.version == "2"
    assert changed.trends[0].status == TrendCalculationStatus.CALCULATED
    assert repository.get_contract(
        "state_snapshot", first.snapshot_id, version="1"
    ) == first
    assert repository.get_contract("state_snapshot", first.snapshot_id) == changed
    provenance_id = changed.provenance_refs[0].reference.removeprefix("Provenance/")
    provenance = repository.get_fhir_resource("Provenance", provenance_id)
    assert provenance is not None
    assert any(
        item["what"]["reference"].startswith("Observation/")
        for item in provenance["entity"]
    )


def test_non_numeric_metric_keeps_state_but_refuses_trend(tmp_path):
    nausea = _boolean_observation(
        "nausea-current", effective_time="2026-08-02T11:00:00+00:00", value=False
    )
    service, _, _ = _service(tmp_path, _snapshot([nausea]))

    result = service.build(
        patient_id=PATIENT_ID,
        definitions=[_nausea_definition()],
        as_of=AS_OF,
    )

    assert result.states[0].status == MetricStateStatus.CURRENT
    assert result.states[0].latest_value is False
    assert result.trends[0].status == TrendCalculationStatus.INSUFFICIENT_DATA
    assert result.trends[0].reason_codes == ["non_numeric_or_missing_value"]


def test_snapshot_contract_rejects_pathway_mismatch(tmp_path):
    service, _, _ = _service(tmp_path, _snapshot([]))
    wrong = _definition().model_copy(update={"pathway_version": "2.0.0"})

    with pytest.raises(ValueError, match="pathway does not match"):
        service.build(patient_id=PATIENT_ID, definitions=[wrong], as_of=AS_OF)


def test_state_snapshot_bundle_rolls_back_and_replays_after_commit(tmp_path):
    observation = _quantity_observation(
        "weight-state-atomic",
        effective_time="2026-08-02T10:00:00+00:00",
        value=72,
    )
    service, repository, _ = _service(tmp_path, _snapshot([observation]))
    arguments = {
        "patient_id": PATIENT_ID,
        "definitions": [_definition()],
        "as_of": AS_OF,
        "generated_at": "2026-08-02T12:05:00+00:00",
    }

    def rollback_fault(stage):
        if stage == "state:after_provenance":
            raise RuntimeError("fault:state:after_provenance")

    repository._provenance_contract_bundle_fault = rollback_fault
    with pytest.raises(RuntimeError, match="state:after_provenance"):
        service.build(**arguments)
    assert repository.list_contracts("state_snapshot", patient_id=PATIENT_ID) == []
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    ) == []

    def commit_fault(stage):
        if stage == "state:after_commit":
            raise RuntimeError("fault:state:after_commit")

    repository._provenance_contract_bundle_fault = commit_fault
    with pytest.raises(RuntimeError, match="state:after_commit"):
        service.build(**arguments)
    committed = repository.list_contracts(
        "state_snapshot", patient_id=PATIENT_ID
    )
    assert len(committed) == 1
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Provenance",
            current_only=False,
        )
    ) == 1

    repository._provenance_contract_bundle_fault = lambda stage: None
    replay = service.build(**arguments)
    assert replay == committed[0]
    assert len(
        repository.list_contracts("state_snapshot", patient_id=PATIENT_ID)
    ) == 1

from __future__ import annotations

import pytest

from continucare.db import reset_demo
from continucare.fhir.observations import (
    build_patient_reported_observation,
    per_day_quantity,
)
from continucare.fhir.terminology import VOMITING_COUNT_24H
from continucare.knowledge import load_cn_glp1_release
from continucare.layer4 import (
    ClinicalStateService,
    Layer4InputSnapshot,
    Layer4SQLiteStore,
    MetricStateStatus,
    StateWindowPolicy,
    bind_l1_state_metric_definitions,
)


POLICIES = {
    "current": StateWindowPolicy(
        lookback_hours=72,
        stale_after_hours=24,
        trend_window_hours=72,
    ),
    "previous_24_hours": StateWindowPolicy(
        lookback_hours=72,
        stale_after_hours=24,
        trend_window_hours=72,
    ),
}


class _SnapshotReader:
    def __init__(self, snapshot: Layer4InputSnapshot):
        self.snapshot = snapshot

    def read(
        self,
        patient_id: str,
        *,
        pathway_code: str,
        pathway_version: str,
    ) -> Layer4InputSnapshot:
        assert patient_id == self.snapshot.patient_id
        assert pathway_code == self.snapshot.pathway_code
        assert pathway_version == self.snapshot.pathway_version
        return self.snapshot


def test_l1_runtime_metrics_bind_to_layer4_without_second_metric_catalog():
    release = load_cn_glp1_release().release

    result = bind_l1_state_metric_definitions(
        release,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
        window_policies=POLICIES,
    )

    definitions = {item.metric_id: item for item in result.definitions}
    assert set(definitions) == {
        "abdominal_pain_present_now",
        "nausea_severity_current",
        "nausea_present_now",
        "vomiting_count_24h",
        "fluid_intake_24h_estimated",
    }
    assert result.skipped_runtime_metric_ids == ()
    nausea = definitions["nausea_present_now"]
    assert nausea.code_system == "http://snomed.info/sct"
    assert nausea.code == "422587007"
    abdominal_pain = definitions["abdominal_pain_present_now"]
    assert abdominal_pain.code_system == "http://snomed.info/sct"
    assert abdominal_pain.code == "21522001"
    vomiting = definitions["vomiting_count_24h"]
    assert vomiting.version == release.manifest.release_id
    assert vomiting.code_system == "http://loinc.org"
    assert vomiting.code == "94070-0"
    assert vomiting.unit == "/d"
    assert vomiting.unit_system == "http://unitsofmeasure.org"


def test_l1_l4_binding_fails_closed_without_declared_recency_policy():
    release = load_cn_glp1_release().release

    with pytest.raises(ValueError, match="no L4 window policy"):
        bind_l1_state_metric_definitions(
            release,
            pathway_code="GLP1-14D",
            pathway_version="1.0.0",
            window_policies={"current": POLICIES["current"]},
        )


def test_l1_l4_binding_rejects_a_metric_that_allows_clinical_interpretation():
    release = load_cn_glp1_release().release.model_copy(deep=True)
    metric = next(item for item in release.metrics if item.runtime_eligible)
    object.__setattr__(metric, "clinical_interpretation_allowed", True)

    with pytest.raises(ValueError, match="allows clinical interpretation"):
        bind_l1_state_metric_definitions(
            release,
            pathway_code="GLP1-14D",
            pathway_version="1.0.0",
            window_policies=POLICIES,
        )


@pytest.mark.parametrize(
    ("observation_release_id", "expected_status"),
    [
        ("cn-glp1-l1-v1.0.3", MetricStateStatus.CURRENT),
        ("cn-glp1-l1-v0.9.0", MetricStateStatus.UNKNOWN),
        (None, MetricStateStatus.UNKNOWN),
    ],
)
def test_l1_release_build_only_consumes_observations_from_the_same_release(
    tmp_path,
    observation_release_id,
    expected_status,
):
    release = load_cn_glp1_release().release
    patient_id = "P-DEMO-001"
    observation = build_patient_reported_observation(
        observation_id="vomiting-release-boundary",
        patient_id=patient_id,
        questionnaire_response_id="qr-release-boundary",
        effective_time="2026-08-14T08:00:00+00:00",
        code=VOMITING_COUNT_24H,
        value_element="valueQuantity",
        value=per_day_quantity(2, unit="events/day"),
    )
    reference = "Observation/vomiting-release-boundary/_history/1"
    snapshot = Layer4InputSnapshot(
        patient_id=patient_id,
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
        observations=[observation],
        observation_knowledge_release_ids={reference: observation_release_id},
        assembled_at="2026-08-14T09:00:00+00:00",
    )
    database_path = tmp_path / f"{observation_release_id or 'missing'}.db"
    reset_demo(database_path)
    service = ClinicalStateService(
        _SnapshotReader(snapshot),
        Layer4SQLiteStore(database_path),
        pathway_code="GLP1-14D",
        pathway_version="1.0.0",
    )

    result = service.build_from_l1_release(
        patient_id=patient_id,
        release=release,
        window_policies=POLICIES,
        as_of="2026-08-14T09:00:00+00:00",
    )

    states = {item.metric_id: item for item in result.states}
    assert states["vomiting_count_24h"].status == expected_status
    if expected_status == MetricStateStatus.UNKNOWN:
        assert states["vomiting_count_24h"].reason_codes == [
            "no_eligible_observation"
        ]
        assert result.source_observation_refs == []

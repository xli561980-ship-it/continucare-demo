from __future__ import annotations

import json

import pytest

from continucare.agents.errors import ModelRequestError
from continucare.care_agent.model_api import SemanticModelConfig
from continucare.fhir.observations import build_patient_reported_observation
from continucare.fhir.terminology import BODY_WEIGHT, NAUSEA_FINDING
from continucare.layer4 import (
    ClinicalMemoryService,
    ClinicalStateService,
    ControlledSummaryService,
    ControlledSummaryStatus,
    Layer4InputSnapshot,
    Layer4SQLiteStore,
    MiMoControlledSummaryAdapter,
    StateMetricDefinition,
    SummaryAgentDecision,
    SummaryAgentTask,
    SummaryModelOutcome,
    SummaryOutlineGroup,
    UnconfiguredSummaryModelAdapter,
)


PATIENT_ID = "P-DEMO-001"
PATHWAY_CODE = "GLP1-14D"
PATHWAY_VERSION = "1.0.0"
PERIOD_START = "2026-08-01T00:00:00+00:00"
PERIOD_END = "2026-08-02T12:00:00+00:00"
GENERATED_AT = "2026-08-02T12:01:00+00:00"
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


class FakeSummaryAdapter:
    VERSION = "fake-controlled-summary-v1"

    def __init__(self, decision=None, *, error: Exception | None = None):
        self.config = SemanticModelConfig(
            provider="synthetic_provider",
            model_name="synthetic-summary-model",
            base_url="https://example.invalid/v1",
            summary_llm_enabled=True,
            summary_prompt_version="synthetic-summary-prompt-v1",
        )
        self.decision = decision
        self.error = error
        self.tasks: list[SummaryAgentTask] = []

    @property
    def configured(self) -> bool:
        return True

    def organize(self, task: SummaryAgentTask) -> SummaryModelOutcome:
        self.tasks.append(task)
        if self.error is not None:
            raise self.error
        decision = self.decision(task) if self.decision else _all_facts_outline(task)
        return SummaryModelOutcome(
            decision=decision,
            provider=self.config.provider,
            model_name=self.config.model_name or "missing",
            prompt_version=self.config.summary_prompt_version,
            agent_version=self.VERSION,
            model_usage={"prompt_tokens": 100, "completion_tokens": 20},
            provider_request_id="synthetic-request-1",
            latency_ms=5,
            attempt_count=1,
        )


def _all_facts_outline(task: SummaryAgentTask) -> SummaryAgentDecision:
    sections: dict[str, list[str]] = {}
    for fact in task.ledger.facts:
        sections.setdefault(fact.section, []).append(fact.fact_id)
    return SummaryAgentDecision(
        groups=[
            SummaryOutlineGroup(
                group_id=f"group-{index}", section=section, fact_ids=fact_ids
            )
            for index, (section, fact_ids) in enumerate(sections.items(), start=1)
        ]
    )


def _weight(observation_id: str, effective_time: str, value: float) -> dict:
    return build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=PATIENT_ID,
        questionnaire_response_id=f"response-{observation_id}",
        effective_time=effective_time,
        code=BODY_WEIGHT,
        value_element="valueQuantity",
        value={
            "value": value,
            "unit": "kg",
            "system": UCUM,
            "code": "kg",
        },
    )


def _nausea(observation_id: str, effective_time: str, value: bool) -> dict:
    return build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=PATIENT_ID,
        questionnaire_response_id=f"response-{observation_id}",
        effective_time=effective_time,
        code=NAUSEA_FINDING,
        value_element="valueBoolean",
        value=value,
    )


def _definitions() -> list[StateMetricDefinition]:
    return [
        StateMetricDefinition(
            metric_id="body-weight",
            version="1.0.0",
            pathway_code=PATHWAY_CODE,
            pathway_version=PATHWAY_VERSION,
            code_system=BODY_WEIGHT.system,
            code=BODY_WEIGHT.code,
            display="Body weight",
            unit="kg",
            unit_system=UCUM,
            lookback_hours=72,
            stale_after_hours=24,
            trend_window_hours=72,
        ),
        StateMetricDefinition(
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
        ),
        StateMetricDefinition(
            metric_id="doctor-defined-metric-x",
            version="7.2.0",
            pathway_code=PATHWAY_CODE,
            pathway_version=PATHWAY_VERSION,
            code_system="urn:synthetic:doctor-metric",
            code="metric-x",
            display="Doctor-defined metric X",
            lookback_hours=72,
            stale_after_hours=24,
            trend_window_hours=72,
        ),
    ]


def _scenario(tmp_path, adapter=None):
    observations = [
        _weight("weight-1", "2026-08-01T10:00:00+00:00", 70),
        _weight("weight-2", "2026-08-02T10:00:00+00:00", 71),
        _nausea("nausea-1", "2026-08-02T09:00:00+00:00", True),
    ]
    reader = MutableInputReader(
        Layer4InputSnapshot(
            patient_id=PATIENT_ID,
            pathway_code=PATHWAY_CODE,
            pathway_version=PATHWAY_VERSION,
            observations=observations,
            assembled_at=PERIOD_END,
        )
    )
    repository = Layer4SQLiteStore(tmp_path / "controlled-summary.db")
    memory = ClinicalMemoryService(
        reader,
        repository,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
    )
    states = ClinicalStateService(
        reader,
        repository,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
    )
    memory.rebuild(PATIENT_ID)
    state = states.build(
        patient_id=PATIENT_ID,
        definitions=_definitions(),
        as_of=PERIOD_END,
    )
    service = ControlledSummaryService(
        memory,
        repository,
        model_adapter=adapter or FakeSummaryAdapter(),
    )
    return reader, repository, memory, states, state, service


def _generate(service: ControlledSummaryService, generated_at: str = GENERATED_AT):
    return service.generate(
        patient_id=PATIENT_ID,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        generated_at=generated_at,
    )


def test_dynamic_metric_count_uses_fact_ids_and_local_canonical_text(tmp_path):
    adapter = FakeSummaryAdapter()
    _, repository, _, _, state, service = _scenario(tmp_path, adapter)

    outcome = _generate(service)

    assert outcome.status == ControlledSummaryStatus.LLM_ASSISTED
    assert outcome.summary.generation_mode == "llm_assisted"
    assert outcome.summary.model_name == "synthetic-summary-model"
    assert outcome.summary.source_state_snapshot_reference.endswith(
        f":{state.version}"
    )
    task = adapter.tasks[0]
    texts = [fact.canonical_text for fact in task.ledger.facts]
    assert any("Body weight" in item for item in texts)
    assert any("Nausea present" in item for item in texts)
    assert any("Doctor-defined metric X" in item for item in texts)
    assert len(task.ledger.facts) == outcome.fact_count
    rendered_lines = [
        line for item in outcome.summary.items for line in item.text.splitlines()
    ]
    assert sorted(rendered_lines) == sorted(texts)
    assert set(outcome.summary.source_fact_ids) == {
        fact.fact_id for fact in task.ledger.facts
    }
    provenance_id = outcome.summary.provenance_refs[0].reference.removeprefix(
        "Provenance/"
    )
    provenance = repository.get_fhir_resource("Provenance", provenance_id)
    assert provenance is not None
    assert any(
        item["what"]["reference"] == outcome.summary.source_state_snapshot_reference
        for item in provenance["entity"]
    )


@pytest.mark.parametrize(
    "bad_decision",
    [
        lambda task: SummaryAgentDecision(
            groups=[
                SummaryOutlineGroup(
                    group_id="unknown",
                    section="overview",
                    fact_ids=["invented-fact-id"],
                )
            ]
        ),
        lambda task: SummaryAgentDecision(groups=[]),
        lambda task: SummaryAgentDecision(
            groups=[
                SummaryOutlineGroup(
                    group_id="wrong-section",
                    section=(
                        "conflicts"
                        if task.ledger.facts[0].section != "conflicts"
                        else "overview"
                    ),
                    fact_ids=[task.ledger.facts[0].fact_id],
                )
            ]
        ),
        lambda task: SummaryAgentDecision(
            groups=[
                SummaryOutlineGroup(
                    group_id="duplicate-1",
                    section=task.ledger.facts[0].section,
                    fact_ids=[task.ledger.facts[0].fact_id],
                ),
                SummaryOutlineGroup(
                    group_id="duplicate-2",
                    section=task.ledger.facts[0].section,
                    fact_ids=[task.ledger.facts[0].fact_id],
                ),
            ]
        ),
    ],
    ids=["unknown-id", "mandatory-omission", "section-change", "duplicate"],
)
def test_invalid_model_outline_is_rejected_with_deterministic_fallback(
    tmp_path, bad_decision
):
    adapter = FakeSummaryAdapter(bad_decision)
    _, _, _, _, _, service = _scenario(tmp_path, adapter)

    outcome = _generate(service)

    assert outcome.status == ControlledSummaryStatus.DETERMINISTIC_FALLBACK
    assert outcome.reason_codes == ["summary_model_output_rejected"]
    assert outcome.summary.generation_mode == "deterministic"
    assert outcome.summary.model_name is None
    assert outcome.summary.model_usage is None
    assert len(outcome.summary.items) == outcome.fact_count
    assert all(item.evidence_refs for item in outcome.summary.items)


def test_model_request_failure_and_unconfigured_model_have_explicit_fallbacks(tmp_path):
    failing = FakeSummaryAdapter(error=ModelRequestError("synthetic timeout"))
    _, _, _, _, _, failure_service = _scenario(tmp_path / "failure", failing)
    failed = _generate(failure_service)

    config = SemanticModelConfig(summary_llm_enabled=False)
    unconfigured = UnconfiguredSummaryModelAdapter(config)
    _, _, _, _, _, offline_service = _scenario(
        tmp_path / "offline", unconfigured
    )
    offline = _generate(offline_service)

    assert failed.reason_codes == ["summary_model_request_failed"]
    assert offline.reason_codes == ["summary_model_not_configured"]
    assert failed.summary.fallback_reason_codes == failed.reason_codes
    assert offline.summary.fallback_reason_codes == offline.reason_codes
    assert failed.fact_count == offline.fact_count


def test_fact_capacity_gate_falls_back_without_truncating_or_calling_model(tmp_path):
    adapter = FakeSummaryAdapter()
    _, _, _, _, _, service = _scenario(tmp_path, adapter)
    service.MAX_LLM_FACTS = 2

    outcome = _generate(service)

    assert outcome.reason_codes == ["fact_ledger_limit_exceeded"]
    assert outcome.fact_count > service.MAX_LLM_FACTS
    assert len(outcome.summary.items) == outcome.fact_count
    assert len(outcome.summary.source_fact_ids) == outcome.fact_count
    assert adapter.tasks == []

    size_adapter = FakeSummaryAdapter()
    _, _, _, _, _, size_service = _scenario(tmp_path / "size", size_adapter)
    size_service.MAX_LLM_INPUT_CHARS = 10
    size_outcome = _generate(size_service)

    assert size_outcome.reason_codes == ["fact_ledger_size_exceeded"]
    assert len(size_outcome.summary.items) == size_outcome.fact_count
    assert size_adapter.tasks == []


def test_same_fact_ledger_is_idempotent_and_changed_snapshot_creates_version(tmp_path):
    adapter = FakeSummaryAdapter()
    reader, repository, memory, states, first_state, service = _scenario(
        tmp_path, adapter
    )

    first = _generate(service)
    repeated = _generate(service, "2026-08-02T12:02:00+00:00")
    reader.snapshot = reader.snapshot.model_copy(
        update={
            "observations": [
                *reader.snapshot.observations,
                _weight("weight-3", "2026-08-02T11:00:00+00:00", 72),
            ]
        }
    )
    memory.rebuild(PATIENT_ID)
    changed_state = states.build(
        patient_id=PATIENT_ID,
        definitions=_definitions(),
        as_of=PERIOD_END,
        generated_at="2026-08-02T12:02:30+00:00",
    )
    changed = _generate(service, "2026-08-02T12:03:00+00:00")

    assert repeated.summary == first.summary
    assert len(adapter.tasks) == 2
    assert first.summary.version == "1"
    assert first_state.snapshot_id == changed_state.snapshot_id
    assert changed_state.version == "2"
    assert changed.summary.version == "2"
    assert changed.summary.source_state_snapshot_reference.endswith(":2")
    assert repository.get_contract(
        "summary_draft", first.summary.summary_id, version="1"
    ) == first.summary


def test_mimo_adapter_uses_json_mode_and_retries_schema_only(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_MIMO_KEY", "test-only-key")
    config = SemanticModelConfig(
        provider="xiaomi_mimo",
        model_name="mimo-v2.5",
        base_url="https://api.xiaomimimo.com/v1",
        api_key_env="SYNTHETIC_MIMO_KEY",
        summary_llm_enabled=True,
        summary_prompt_version="mimo-summary-outline-v1",
    )
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            content = {"groups": [], "summary_text": "不允许的自由文本"}
        else:
            content = {"groups": []}
        return {
            "id": f"request-{len(calls)}",
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }

    adapter = MiMoControlledSummaryAdapter(config, transport=transport)
    task = SummaryAgentTask.model_validate(
        {
            "task_id": "summary-task-empty",
            "ledger": {
                "ledger_id": "ledger-empty",
                "patient_id": PATIENT_ID,
                "pathway_code": PATHWAY_CODE,
                "pathway_version": PATHWAY_VERSION,
                "period_start": PERIOD_START,
                "period_end": PERIOD_END,
                "assembled_at": GENERATED_AT,
                "facts": [],
            },
        }
    )

    outcome = adapter.organize(task)

    assert adapter.configured is True
    assert outcome.attempt_count == 2
    assert outcome.model_usage == {"prompt_tokens": 20, "completion_tokens": 4}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["temperature"] == 0
    assert "only group and order fact_id" in calls[0]["messages"][0]["content"]
    assert "STRICT RETRY" in calls[1]["messages"][0]["content"]


def test_mimo_adapter_retries_semantically_invalid_fact_coverage(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_MIMO_KEY", "test-only-key")
    config = SemanticModelConfig(
        provider="xiaomi_mimo",
        model_name="mimo-v2.5",
        base_url="https://api.xiaomimimo.com/v1",
        api_key_env="SYNTHETIC_MIMO_KEY",
        summary_llm_enabled=True,
        summary_prompt_version="mimo-summary-outline-v1",
    )
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        content = (
            {"groups": []}
            if len(calls) == 1
            else {
                "groups": [
                    {
                        "group_id": "group-1",
                        "section": "key_changes",
                        "fact_ids": ["fact-required"],
                    }
                ]
            }
        )
        return {
            "id": f"request-{len(calls)}",
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }

    task = SummaryAgentTask.model_validate(
        {
            "task_id": "summary-task-required",
            "ledger": {
                "ledger_id": "ledger-required",
                "patient_id": PATIENT_ID,
                "pathway_code": PATHWAY_CODE,
                "pathway_version": PATHWAY_VERSION,
                "period_start": PERIOD_START,
                "period_end": PERIOD_END,
                "assembled_at": GENERATED_AT,
                "facts": [
                    {
                        "fact_id": "fact-required",
                        "kind": "metric_state",
                        "section": "key_changes",
                        "canonical_text": "Synthetic required fact.",
                        "evidence_refs": [
                            {
                                "evidence_id": "evidence-required",
                                "resource": {
                                    "reference": "Observation/required",
                                    "version_id": "1",
                                },
                                "role": "source",
                            }
                        ],
                    }
                ],
            },
        }
    )

    outcome = MiMoControlledSummaryAdapter(config, transport=transport).organize(task)

    assert outcome.attempt_count == 2
    assert outcome.decision.groups[0].fact_ids == ["fact-required"]
    assert "mandatory fact_id" in calls[1]["messages"][0]["content"]

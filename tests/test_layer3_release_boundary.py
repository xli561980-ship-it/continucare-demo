from __future__ import annotations

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_agent.agent import CareSemanticAgent
from continucare.care_agent.mimo_adapter import MiMoSemanticAdapter
from continucare.care_agent.mimo_enhancements import (
    MiMoLanguageRewriter,
    MiMoSafetyCritic,
)
from continucare.care_agent.model_api import SemanticModelConfig
from continucare.care_agent.release import LAYER3_RELEASE
from continucare.care_agent.safety import SafetyAgent
from continucare.care_engine import CareEngine
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.fhir.r4 import FHIR_R4_VERSION
from continucare.layer4 import Layer4InputReader
from continucare.models import Observation
from continucare.terminology import RepositoryTerminologyBackend


def _complete_answers() -> dict:
    return {
        "nausea-present": True,
        "nausea-severity": "LA6752-5",
        "vomiting-count-24h": 2,
        "abdominal-pain-present": False,
        "free-text-report": "合成数据。",
    }


def test_layer3_release_manifest_matches_runtime_versions():
    config = SemanticModelConfig()
    catalog = RepositoryTerminologyBackend().catalog

    assert LAYER3_RELEASE.fhir_version == FHIR_R4_VERSION
    assert LAYER3_RELEASE.care_agent_version == CareSemanticAgent.VERSION
    assert LAYER3_RELEASE.safety_agent_version == SafetyAgent.VERSION
    assert LAYER3_RELEASE.model_adapter_version == MiMoSemanticAdapter.VERSION
    assert LAYER3_RELEASE.safety_critic_version == MiMoSafetyCritic.VERSION
    assert LAYER3_RELEASE.language_rewriter_version == MiMoLanguageRewriter.VERSION
    assert LAYER3_RELEASE.extraction_prompt_version == config.prompt_version
    assert LAYER3_RELEASE.safety_prompt_version == config.safety_prompt_version
    assert LAYER3_RELEASE.language_prompt_version == config.language_prompt_version
    assert LAYER3_RELEASE.terminology_catalog_id == catalog.catalog_id
    assert LAYER3_RELEASE.terminology_catalog_version == catalog.version


def test_layer4_reader_consumes_only_final_resources_and_audit(tmp_path):
    store = SQLiteStore(tmp_path / "layer4-boundary.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    completed = engine.complete(session.session_id, _complete_answers())

    # If the boundary accidentally starts reading semantic internals, fail loudly.
    store.list_agent_runs = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Layer 4 must not read AgentRun or Mock output")
    )
    store.list_observations = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("Layer 4 must use only final Observation resources")
    )
    snapshot = Layer4InputReader(store).read(
        DEMO_PATIENT_ID,
        pathway_code=session.pathway_code,
        pathway_version=session.pathway_version,
        assembled_at="2026-08-02T12:00:00+00:00",
    )

    assert snapshot.contract_version == LAYER3_RELEASE.version
    assert [item["id"] for item in snapshot.questionnaire_responses] == [
        completed.questionnaire_response["id"]
    ]
    assert all(item["resourceType"] == "Observation" for item in snapshot.observations)
    assert {item["id"] for item in snapshot.observations} == {
        item.observation_id for item in completed.observations
    }
    assert snapshot.audit_events
    assert set(snapshot.model_dump()) == {
        "contract_version",
        "patient_id",
        "pathway_code",
        "pathway_version",
        "questionnaire_responses",
        "observations",
        "audit_events",
        "assembled_at",
    }


def test_layer4_reader_rejects_non_final_observation(tmp_path):
    store = SQLiteStore(tmp_path / "layer4-non-final.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    completed = engine.complete(session.session_id, _complete_answers())
    observation = completed.observations[0]
    preliminary_resource = observation.as_fhir()
    preliminary_resource["status"] = "preliminary"
    preliminary = Observation(
        resource=preliminary_resource,
        evidence=observation.evidence,
    )
    store.list_final_observations = lambda *_args, **_kwargs: [preliminary]

    with pytest.raises(ValueError, match="only accepts final Observation"):
        Layer4InputReader(store).read(
            DEMO_PATIENT_ID,
            pathway_code=session.pathway_code,
            pathway_version=session.pathway_version,
        )

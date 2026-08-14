"""Immutable metadata for the accepted Layer-3 engineering baseline."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Layer3ReleaseManifest:
    release_id: str
    version: str
    rollback_ref: str
    fhir_version: str
    fhir_schema_url: str
    fhir_schema_sha256: str
    care_agent_version: str
    safety_agent_version: str
    model_adapter_version: str
    safety_critic_version: str
    language_rewriter_version: str
    model_name: str
    extraction_prompt_version: str
    safety_prompt_version: str
    language_prompt_version: str
    knowledge_release_id: str
    terminology_catalog_id: str
    terminology_catalog_version: str
    terminology_catalog_sha256: str
    offline_case_set: str
    live_case_set: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


LAYER3_RELEASE = Layer3ReleaseManifest(
    release_id="continucare-layer3-v1.0.3",
    version="1.0.3",
    rollback_ref="previous-engineering-validated-release",
    fhir_version="4.0.1",
    fhir_schema_url="https://hl7.org/fhir/R4/fhir.schema.json.zip",
    fhir_schema_sha256=(
        "75e5560da3cf503895a44c8ca7af17a83b4cca6c2cb5ba1883d2aec0d1cb5ac6"
    ),
    care_agent_version="care-agent-v2",
    safety_agent_version="safety-agent-hybrid-v4",
    model_adapter_version="xiaomi-mimo-openai-v4",
    safety_critic_version="mimo-safety-critic-v2",
    language_rewriter_version="mimo-language-rewriter-v1",
    model_name="mimo-v2.5",
    extraction_prompt_version="mimo-semantic-extraction-v4",
    safety_prompt_version="mimo-safety-critic-v2",
    language_prompt_version="mimo-language-rewrite-v1",
    knowledge_release_id="cn-glp1-l1-v1.0.3",
    terminology_catalog_id="continucare-cn-glp1-l1-terminology-whitelist",
    terminology_catalog_version="cn-glp1-l1-v1.0.3",
    terminology_catalog_sha256=(
        "310949e60a36f15c14af71d6399533f8a2717d551c19176b07a148339dd6ee2e"
    ),
    offline_case_set="semantic_cases_v1.json",
    live_case_set="mimo_live_cases_v1.json",
)

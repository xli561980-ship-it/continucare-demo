"""Strict contracts for an auditable L1 knowledge release."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Coding(StrictModel):
    system: str
    code: str
    display: str
    version: str | None = None


class UnitCoding(StrictModel):
    system: str
    code: str


class SourceRecord(StrictModel):
    source_id: str
    authority: str
    jurisdiction: str
    source_type: str
    title: str
    document_number: str | None = None
    language: str
    publication_date: str | None = None
    effective_date: str | None = None
    retrieved_at: str
    canonical_url: str
    local_path: str | None = None
    sha256: str | None = None
    license_status: Literal["verified", "restricted", "unknown"]
    verification_status: Literal["verified", "partially_verified", "unverified"]
    usage: Literal[
        "data_contract_standard",
        "validated_patient_instrument",
        "grading_reference_only",
        "background_comparison_only",
        "signal_research_only",
        "license_record_only",
        "translation_validation_record",
        "cn_product_approval_evidence",
        "cn_product_label",
        "cn_guideline_context",
        "engineering_data_collection_contract",
    ]
    runtime_eligible: bool
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: str | None = None

    @model_validator(mode="after")
    def verified_local_sources_have_hashes(self) -> "SourceRecord":
        if self.local_path and not self.sha256:
            raise ValueError("a local source requires sha256")
        if self.sha256 and len(self.sha256) != 64:
            raise ValueError("sha256 must be a 64-character digest")
        if self.usage in {
            "background_comparison_only",
            "grading_reference_only",
            "signal_research_only",
            "license_record_only",
            "translation_validation_record",
            "cn_guideline_context",
        } and self.runtime_eligible:
            raise ValueError(f"{self.usage} source cannot be runtime eligible")
        return self


class IndicationPopulationScope(StrictModel):
    indication: str
    populations: list[str] = Field(min_length=1)
    qualifiers: list[str] = Field(min_length=1)


class ProductRecord(StrictModel):
    product_id: str
    jurisdiction: Literal["CN"] = "CN"
    active_ingredient: str
    brand_name_zh: str
    agonist_type: Literal[
        "single_glp1_ra", "dual_gip_glp1_agonist", "other_multi_agonist"
    ]
    marketing_authorization_holder: str | None = None
    dosage_form: str | None = None
    strengths: list[str] = Field(default_factory=list)
    strength: str | None = None
    administration_route: str | None = None
    approval_numbers: list[str] = Field(default_factory=list)
    approval_number: str | None = None
    approval_source_ids: list[str] = Field(default_factory=list)
    approval_status: Literal["approved", "withdrawn", "uncertain"]
    approved_indications: list[str] = Field(default_factory=list)
    approved_populations: list[str] = Field(default_factory=list)
    indication_population_scopes: list[IndicationPopulationScope] = Field(
        min_length=1
    )
    label_source_id: str | None = None
    label_version: str | None = None
    verified_at: str
    verification_status: Literal["verified", "incomplete", "unverified"]

    @model_validator(mode="after")
    def product_authorization_is_atomic(self) -> "ProductRecord":
        if self.verification_status == "verified":
            if not self.strength or not self.approval_number:
                raise ValueError("verified product requires atomic strength and approval_number")
            if self.strengths != [self.strength]:
                raise ValueError("verified product strengths must contain the atomic strength")
            if self.approval_numbers != [self.approval_number]:
                raise ValueError(
                    "verified product approval_numbers must contain the atomic approval_number"
                )
        if self.approval_status == "approved" and not self.approval_source_ids:
            raise ValueError("approved product requires approval_source_ids")
        scoped_indications = {item.indication for item in self.indication_population_scopes}
        if scoped_indications != set(self.approved_indications):
            raise ValueError("indication_population_scopes must cover approved_indications")
        scoped_populations = {
            population
            for item in self.indication_population_scopes
            for population in item.populations
        }
        if not scoped_populations <= set(self.approved_populations):
            raise ValueError("scoped populations must be registered on the product")
        return self


class EvidenceLocator(StrictModel):
    section: str
    subsection: str | None = None
    page: int | None = Field(default=None, ge=1)


class EvidenceClaim(StrictModel):
    claim_id: str
    source_id: str
    product_scope_kind: Literal[
        "product_specific",
        "all_registered_products",
        "background_not_product_scoped",
    ]
    product_ids: list[str] = Field(default_factory=list)
    indications: list[str] = Field(default_factory=list)
    populations: list[str] = Field(default_factory=list)
    locator: EvidenceLocator
    normalized_claim: str
    claim_type: Literal[
        "supports_data_collection",
        "supports_question_wording",
        "supports_terminology_binding",
        "candidate_grading_reference",
        "background_product_safety",
        "signal_context_only",
        "supports_product_scope",
        "supports_cn_gi_monitoring_domain",
    ]
    allowed_use: list[str] = Field(min_length=1)
    prohibited_inference: list[str] = Field(min_length=1)
    runtime_eligible: bool
    review_status: Literal[
        "engineering_reviewed", "unreviewed", "pending_clinical_review"
    ]

    @model_validator(mode="after")
    def product_scope_is_explicit(self) -> "EvidenceClaim":
        if self.product_scope_kind == "product_specific" and not self.product_ids:
            raise ValueError("product-specific evidence requires product_ids")
        if self.product_scope_kind != "product_specific" and self.product_ids:
            raise ValueError(
                "generic/background evidence must express scope through "
                "product_scope_kind, not product_ids"
            )
        if (
            self.product_scope_kind == "background_not_product_scoped"
            and self.runtime_eligible
        ):
            raise ValueError("background evidence cannot be runtime eligible")
        return self


class MetricDefinition(StrictModel):
    metric_id: str
    display_zh: str
    clinical_intent: str
    data_type: Literal["boolean", "coded", "integer", "quantity", "text"]
    time_window: str
    product_scope: list[str] = Field(default_factory=list)
    indication_scope: list[str] = Field(default_factory=list)
    population_scope: list[str] = Field(default_factory=list)
    observation_code: Coding | None = None
    allowed_units: list[UnitCoding] = Field(default_factory=list)
    evidence_claim_ids: list[str] = Field(min_length=1)
    missing_behavior: Literal["do_not_create_observation"]
    conflict_behavior: Literal["require_clarification"]
    trend_eligible: bool
    clinical_interpretation_allowed: bool = False
    runtime_eligible: bool
    approval_status: Literal["engineering_validated", "background_only"]

    @model_validator(mode="after")
    def quantities_require_ucum(self) -> "MetricDefinition":
        if self.data_type == "quantity" and not self.allowed_units:
            raise ValueError("quantity metrics require allowed units")
        if any(unit.system != "http://unitsofmeasure.org" for unit in self.allowed_units):
            raise ValueError("metric units must use UCUM")
        if self.clinical_interpretation_allowed:
            raise ValueError("this unapproved release cannot allow clinical interpretation")
        return self


class PatientContent(StrictModel):
    content_id: str
    link_id: str
    locale: Literal["zh-CN"] = "zh-CN"
    text: str
    purpose: Literal["data_collection"] = "data_collection"
    metric_id: str | None = None
    evidence_claim_ids: list[str] = Field(min_length=1)
    item_type: Literal["boolean", "choice", "integer", "quantity", "text"]
    answers: list[str] = Field(default_factory=list)
    medical_advice: bool = False
    approval_status: Literal["engineering_reviewed"] = "engineering_reviewed"
    runtime_pathway_bound: bool = True

    @model_validator(mode="after")
    def choices_have_answers(self) -> "PatientContent":
        if self.item_type == "choice" and not self.answers:
            raise ValueError("choice content requires answers")
        if self.medical_advice:
            raise ValueError("patient content cannot contain medical advice")
        return self


class DataQualityRule(StrictModel):
    rule_id: str
    rule_type: Literal["data_quality"] = "data_quality"
    condition: str
    action: Literal["request_clarification", "reject_invalid_resource"]
    clinical_risk_level: None = None


class TerminologyEntry(StrictModel):
    metric_id: str
    entry_type: Literal["observation_code", "answer_value_set", "unit"]
    coding: Coding
    local_aliases_zh: list[str] = Field(default_factory=list)
    edition: str
    jurisdiction_applicability: str
    license_status: str
    value_set_id: str | None = None
    allowed_codes: list[Coding] = Field(default_factory=list)
    runtime_eligible: bool
    validated_on: str


class ProductCoverage(StrictModel):
    product_id: str
    agonist_type: Literal[
        "single_glp1_ra", "dual_gip_glp1_agonist", "other_multi_agonist"
    ]
    indication_scope: list[str] = Field(min_length=1)
    verification_status: Literal["verified", "incomplete", "unverified"]
    label_source_id: str | None = None
    runtime_pathway_bound: bool
    coverage_note: str


class MetricCoverage(StrictModel):
    metric_id: str
    questionnaire_link_ids: list[str] = Field(default_factory=list)
    evidence_claim_ids: list[str] = Field(min_length=1)
    cn_runtime_claim_ids: list[str] = Field(default_factory=list)
    observation_mapping_bound: bool
    synthetic_runtime_eligible: bool
    clinical_runtime_eligible: Literal[False] = False
    coverage_note: str


class CoverageGap(StrictModel):
    gap_id: str
    category: Literal[
        "source", "product", "evidence", "terminology", "clinical_review", "runtime"
    ]
    status: Literal["open", "blocked_external", "planned"]
    description: str
    affected_ids: list[str] = Field(default_factory=list)
    required_action: str
    blocks_clinical_use: bool = True


class CoverageReport(StrictModel):
    report_id: str
    release_id: str
    generated_at: str
    jurisdiction: Literal["CN"] = "CN"
    coverage_status: Literal["incomplete"] = "incomplete"
    source_count: int = Field(ge=0)
    verified_source_count: int = Field(ge=0)
    cn_source_count: int = Field(ge=0)
    product_record_count: int = Field(ge=0)
    verified_product_record_count: int = Field(ge=0)
    incomplete_product_record_count: int = Field(ge=0)
    evidence_claim_count: int = Field(ge=0)
    runtime_evidence_claim_count: int = Field(ge=0)
    metric_count: int = Field(ge=0)
    runtime_metric_count: int = Field(ge=0)
    clinical_rule_count: int = Field(ge=0)
    compiled_artifacts: list[str] = Field(min_length=1)
    fhir_r4_schema_status: Literal["verified"] = "verified"
    production_clinical_runtime_eligible: Literal[False] = False
    products: list[ProductCoverage] = Field(min_length=1)
    metrics: list[MetricCoverage] = Field(min_length=1)
    gaps: list[CoverageGap] = Field(min_length=1)


class ReleaseManifest(StrictModel):
    release_id: str
    jurisdiction: Literal["CN"] = "CN"
    created_at: str
    status: Literal["draft_candidate", "engineering_validated"] = (
        "engineering_validated"
    )
    synthetic_only: Literal[True] = True
    source_registry_sha256: str
    product_registry_sha256: str
    evidence_claims_sha256: str
    metric_definitions_sha256: str
    terminology_manifest_sha256: str
    patient_content_sha256: str
    data_quality_rules_sha256: str
    clinical_rules_sha256: str
    coverage_report_sha256: str
    questionnaire_sha256: str
    plan_definition_sha256: str
    observation_mapping_sha256: str
    clinical_approval: None = None
    known_limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def digests_are_sha256(self) -> "ReleaseManifest":
        for name, value in self.model_dump().items():
            if name.endswith("_sha256") and len(value) != 64:
                raise ValueError(f"{name} must be a 64-character digest")
        return self


class KnowledgeRelease(StrictModel):
    manifest: ReleaseManifest
    sources: list[SourceRecord]
    products: list[ProductRecord]
    evidence_claims: list[EvidenceClaim]
    metrics: list[MetricDefinition]
    terminology: list[TerminologyEntry]
    patient_content: list[PatientContent]
    data_quality_rules: list[DataQualityRule]
    clinical_rules: list[dict]
    coverage: CoverageReport

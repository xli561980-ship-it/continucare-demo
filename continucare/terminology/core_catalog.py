"""Shared, non-pathway Core Symptom Catalog v2 readiness model."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from continucare.terminology.catalog import load_glp1_symptom_catalog


CORE_CATALOG_V2_FILE = Path(__file__).parent / "data" / "core_symptom_catalog_v2.json"
BenchmarkId = Annotated[
    str, StringConstraints(pattern=r"^core-symptom-[a-z][a-z0-9-]{0,63}$")
]
SafeCatalogId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
]

BENCHMARK_KEYS = (
    "nausea",
    "vomiting",
    "diarrhea",
    "abdominal-pain",
    "constipation",
    "bloating",
    "decreased-appetite",
    "fatigue",
    "dizziness",
    "dyspnea",
    "chest-pain",
    "rash",
)
_FORBIDDEN_CLINICAL_FIELDS = frozenset(
    {
        "red_flag",
        "triage",
        "severity",
        "severity_tier",
        "risk_tier",
        "diagnosis",
        "treatment",
        "recommendation",
        "clinical_rule",
    }
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class CoreSymptomAlias(_StrictFrozenModel):
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    language: Literal["zh-CN", "en"]
    alias_source: Literal[
        "glp1_catalog_v1_preferred",
        "benchmark_seed",
        "internal_candidate_seed",
    ]
    review_status: Literal[
        "inherited_prototype_unverified_for_v2",
        "pending_terminologist_review",
    ]


class CoreSymptomRecord(_StrictFrozenModel):
    benchmark_id: BenchmarkId
    benchmark_key: SafeCatalogId
    preferred_zh: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    preferred_en: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    concept_status: Literal["reused_concept", "alias_candidate", "internal_candidate"]
    existing_concept_ref: SafeCatalogId | None = None
    candidate_target_ref: SafeCatalogId | None = None
    aliases: tuple[CoreSymptomAlias, ...] = Field(min_length=2)
    mapping_status: Literal[
        "inherited_v1_reference_unverified",
        "pending_unverified",
    ]
    semantic_boundary_codes: tuple[SafeCatalogId, ...] = Field(min_length=1)
    terminology_review_status: Literal["pending_terminologist_review"] = (
        "pending_terminologist_review"
    )
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_candidate_semantics(self) -> "CoreSymptomRecord":
        if self.benchmark_id != f"core-symptom-{self.benchmark_key}":
            raise ValueError("benchmark ID must be derived from its stable key")
        if len(self.aliases) != len({(item.language, item.value) for item in self.aliases}):
            raise ValueError("Core Symptom aliases must be unique per language")
        if len(self.semantic_boundary_codes) != len(set(self.semantic_boundary_codes)):
            raise ValueError("semantic boundary codes must be unique")
        if self.concept_status == "reused_concept":
            if self.existing_concept_ref is None or self.candidate_target_ref is not None:
                raise ValueError("reused concept requires one existing concept reference")
            if self.mapping_status != "inherited_v1_reference_unverified":
                raise ValueError("reused concept must preserve unverified v1 reference status")
        elif self.concept_status == "alias_candidate":
            if self.existing_concept_ref is not None or self.candidate_target_ref is None:
                raise ValueError("alias candidate requires one candidate target only")
            if self.mapping_status != "pending_unverified":
                raise ValueError("alias candidate mapping must remain pending")
        elif self.existing_concept_ref is not None or self.candidate_target_ref is not None:
            raise ValueError("internal candidate cannot claim an external or existing mapping")
        return self


class CoreSymptomCatalogV2(_StrictFrozenModel):
    catalog_id: Literal["continucare-core-symptom-catalog"]
    catalog_version: Literal["2.0.0"]
    status: Literal["draft-readiness-only"]
    shared_owner: Literal["continucare-shared-terminology"]
    pathway_owned: Literal[False] = False
    ui_runtime_enabled: Literal[False] = False
    source_catalog_id: Literal["continucare-glp1-patient-reported-symptoms"]
    source_catalog_version: Literal["1.0.0"]
    records: tuple[CoreSymptomRecord, ...] = Field(min_length=12, max_length=12)
    release_ready: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"

    @model_validator(mode="after")
    def validate_benchmark_set(self) -> "CoreSymptomCatalogV2":
        keys = tuple(item.benchmark_key for item in self.records)
        if keys != BENCHMARK_KEYS:
            raise ValueError("Core Symptom Catalog must contain the ordered 12-item benchmark")
        if len({item.benchmark_id for item in self.records}) != 12:
            raise ValueError("Core Symptom benchmark IDs must be unique")
        by_key = {item.benchmark_key: item for item in self.records}
        if (
            by_key["bloating"].concept_status != "alias_candidate"
            or by_key["bloating"].candidate_target_ref != "abdominal-distension"
        ):
            raise ValueError(
                "bloating may only target abdominal-distension as an alias candidate"
            )
        if (
            by_key["rash"].concept_status != "alias_candidate"
            or by_key["rash"].candidate_target_ref != "skin-eruption"
        ):
            raise ValueError("rash may only target skin-eruption as an alias candidate")
        if by_key["chest-pain"].concept_status != "internal_candidate":
            raise ValueError("chest-pain must remain an unmapped internal candidate")
        return self

    def symptom(self, benchmark_key: str) -> CoreSymptomRecord:
        record = next(
            (item for item in self.records if item.benchmark_key == benchmark_key),
            None,
        )
        if record is None:
            raise LookupError(f"unknown Core Symptom benchmark {benchmark_key!r}")
        return record


@lru_cache(maxsize=1)
def load_core_symptom_catalog_v2() -> CoreSymptomCatalogV2:
    raw = json.loads(CORE_CATALOG_V2_FILE.read_text(encoding="utf-8"))
    _assert_no_clinical_fields(raw)
    catalog = CoreSymptomCatalogV2.model_validate(raw)
    _validate_v1_references(catalog)
    _validate_fixed_alias_boundaries(catalog)
    return catalog


def _assert_no_clinical_fields(value: object, *, path: str = "catalog") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in _FORBIDDEN_CLINICAL_FIELDS:
                raise ValueError(f"prohibited clinical field at {path}.{key}")
            _assert_no_clinical_fields(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_clinical_fields(item, path=f"{path}[{index}]")


def _validate_v1_references(catalog: CoreSymptomCatalogV2) -> None:
    v1 = load_glp1_symptom_catalog()
    if v1.catalog_id != catalog.source_catalog_id or v1.version != catalog.source_catalog_version:
        raise ValueError("Core Symptom Catalog source catalog identity differs")
    known = {item.concept_id for item in v1.concepts}
    for record in catalog.records:
        for reference in (record.existing_concept_ref, record.candidate_target_ref):
            if reference is not None and reference not in known:
                raise ValueError(f"unknown v1 concept reference {reference!r}")


def _validate_fixed_alias_boundaries(catalog: CoreSymptomCatalogV2) -> None:
    bloating = catalog.symptom("bloating")
    rash = catalog.symptom("rash")
    chest_pain = catalog.symptom("chest-pain")
    if bloating.concept_status != "alias_candidate" or bloating.candidate_target_ref != "abdominal-distension":
        raise ValueError("bloating may only target abdominal-distension as an alias candidate")
    if rash.concept_status != "alias_candidate" or rash.candidate_target_ref != "skin-eruption":
        raise ValueError("rash may only target skin-eruption as an alias candidate")
    if chest_pain.concept_status != "internal_candidate":
        raise ValueError("chest-pain must remain an unmapped internal candidate")

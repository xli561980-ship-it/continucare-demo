"""Repository-backed terminology retrieval with an external-server seam.

The local catalog is the governed demo source.  A hospital deployment can
replace ``RepositoryTerminologyBackend`` with a FHIR terminology backend while
keeping the resolver and confirmation workflow unchanged.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from continucare.agents.contracts import (
    CodingContract,
    QuestionnaireItemContract,
    TerminologyMatchContract,
)


DATA_FILE = Path(__file__).parent / "data" / "glp1_symptom_catalog_v1.json"
DYNAMIC_LINK_PREFIX = "patient-reported-symptom::"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceDocument(_StrictModel):
    source_id: str
    title: str
    url: str
    retrieved_on: str


class CatalogConcept(_StrictModel):
    concept_id: str
    preferred_zh: str
    coding: CodingContract
    aliases_zh: list[str] = Field(min_length=1)
    category: str
    monitoring_class: str
    source_ids: list[str] = Field(min_length=1)
    approval_status: str

    @model_validator(mode="after")
    def preferred_name_is_searchable(self) -> "CatalogConcept":
        if self.preferred_zh not in self.aliases_zh:
            raise ValueError("preferred_zh must also appear in aliases_zh")
        return self

    @property
    def dynamic_link_id(self) -> str:
        return f"{DYNAMIC_LINK_PREFIX}{self.concept_id}"


class QuestionnaireBinding(_StrictModel):
    link_id: str
    coding: CodingContract
    symptom_concept_id: str | None = None


class TerminologyCatalog(_StrictModel):
    catalog_id: str
    version: str
    status: str
    scope_statement: str
    completeness_statement: str
    code_system_release: str
    validation_service: str
    validation_date: str
    target_hospital_validation_required: bool = True
    source_documents: list[SourceDocument]
    questionnaire_bindings: list[QuestionnaireBinding]
    concepts: list[CatalogConcept]

    @model_validator(mode="after")
    def validate_unique_keys_and_bindings(self) -> "TerminologyCatalog":
        concept_ids = [item.concept_id for item in self.concepts]
        if len(concept_ids) != len(set(concept_ids)):
            raise ValueError("catalog concept_id values must be unique")
        link_ids = [item.link_id for item in self.questionnaire_bindings]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("questionnaire binding link_id values must be unique")
        known = set(concept_ids)
        unknown = {
            item.symptom_concept_id
            for item in self.questionnaire_bindings
            if item.symptom_concept_id and item.symptom_concept_id not in known
        }
        if unknown:
            raise ValueError(f"unknown symptom concept bindings: {sorted(unknown)}")
        return self

    def concept(self, concept_id: str) -> CatalogConcept:
        item = next(
            (item for item in self.concepts if item.concept_id == concept_id), None
        )
        if item is None:
            raise LookupError(f"unknown terminology concept {concept_id!r}")
        return item

    def binding(self, link_id: str) -> QuestionnaireBinding | None:
        return next(
            (item for item in self.questionnaire_bindings if item.link_id == link_id),
            None,
        )

    def questionnaire_contracts(self) -> list[QuestionnaireItemContract]:
        return [
            QuestionnaireItemContract(
                link_id=item.dynamic_link_id,
                item_type="boolean",
                text=f"患者自述：{item.preferred_zh}",
                codes=[item.coding],
            )
            for item in self.concepts
        ]

    def as_fhir_value_set(self) -> dict:
        """Render the governed symptom subset as a FHIR R4 ValueSet."""

        grouped: dict[tuple[str, str | None], list[dict[str, str]]] = {}
        for item in self.concepts:
            grouped.setdefault(
                (item.coding.system, item.coding.version), []
            ).append(
                {"code": item.coding.code, "display": item.coding.display or item.preferred_zh}
            )
        includes = []
        for (system, version), concepts in grouped.items():
            include = {
                "system": system,
                "concept": sorted(concepts, key=lambda item: item["code"]),
            }
            if version:
                include["version"] = version
            includes.append(include)
        return {
            "resourceType": "ValueSet",
            "id": "continucare-glp1-patient-reported-symptoms-v1",
            "url": "urn:continucare:ValueSet:glp1-patient-reported-symptoms",
            "version": self.version,
            "name": "ContinuCareGLP1PatientReportedSymptoms",
            "title": "ContinuCare GLP-1 Patient-reported Symptoms",
            "status": "draft",
            "description": self.completeness_statement,
            "compose": {"include": includes},
        }

    def match_for_questionnaire(
        self,
        *,
        link_id: str,
        coding: CodingContract | None,
        matched_text: str,
    ) -> TerminologyMatchContract:
        binding = self.binding(link_id)
        if binding is None:
            raise ValueError(f"问卷字段 {link_id!r} 未登记到术语目录")
        if coding is None or (
            coding.system,
            coding.code,
            coding.version,
        ) != (
            binding.coding.system,
            binding.coding.code,
            binding.coding.version,
        ):
            raise ValueError(f"问卷字段 {link_id!r} 的代码与术语目录不一致")
        concept = (
            self.concept(binding.symptom_concept_id)
            if binding.symptom_concept_id
            else None
        )
        return TerminologyMatchContract(
            catalog_id=self.catalog_id,
            catalog_version=self.version,
            concept_id=concept.concept_id if concept else f"questionnaire::{link_id}",
            preferred_zh=concept.preferred_zh if concept else matched_text,
            coding=(concept.coding if concept else binding.coding),
            target_coding=binding.coding,
            matched_text=matched_text,
            matched_alias=matched_text,
            match_method="repository_questionnaire_binding",
            validation_status="repository_release_validated",
            approval_status=(
                concept.approval_status if concept else "prototype_governed"
            ),
            target_hospital_validation_required=(
                self.target_hospital_validation_required
            ),
        )

    def match_for_concept(
        self,
        concept: CatalogConcept,
        *,
        matched_text: str,
        matched_alias: str,
        match_method: str,
    ) -> TerminologyMatchContract:
        return TerminologyMatchContract(
            catalog_id=self.catalog_id,
            catalog_version=self.version,
            concept_id=concept.concept_id,
            preferred_zh=concept.preferred_zh,
            coding=concept.coding,
            target_coding=concept.coding,
            matched_text=matched_text,
            matched_alias=matched_alias,
            match_method=match_method,
            validation_status="repository_release_validated",
            approval_status=concept.approval_status,
            target_hospital_validation_required=(
                self.target_hospital_validation_required
            ),
        )


class TerminologySearchBackend(Protocol):
    """Seam for a local catalog, hospital FHIR server, or future RAG retriever."""

    catalog: TerminologyCatalog

    def search(self, phrase: str) -> list[TerminologyMatchContract]: ...

    def scan(self, text: str) -> list[tuple[int, int, list[TerminologyMatchContract]]]: ...

    def validate_code(self, coding: CodingContract) -> bool: ...


class RepositoryTerminologyBackend:
    """Deterministic exact-alias search over a versioned repository artifact."""

    def __init__(self, catalog: TerminologyCatalog | None = None):
        self.catalog = catalog or load_glp1_symptom_catalog()
        self._aliases: dict[str, list[tuple[str, CatalogConcept]]] = {}
        for concept in self.catalog.concepts:
            for alias in concept.aliases_zh:
                self._aliases.setdefault(_normalize(alias), []).append((alias, concept))

    def validate_code(self, coding: CodingContract) -> bool:
        known = [item.coding for item in self.catalog.concepts]
        known.extend(item.coding for item in self.catalog.questionnaire_bindings)
        return any(
            (item.system, item.code, item.version)
            == (coding.system, coding.code, coding.version)
            for item in known
        )

    def search(self, phrase: str) -> list[TerminologyMatchContract]:
        normalized = _normalize(phrase)
        exact = self._aliases.get(normalized, [])
        matches = exact
        method = "exact_repository_alias"
        if not matches:
            contained: list[tuple[str, CatalogConcept]] = []
            for normalized_alias, entries in self._aliases.items():
                if len(normalized_alias) >= 2 and normalized_alias in normalized:
                    contained.extend(entries)
            if contained:
                longest = max(len(_normalize(alias)) for alias, _ in contained)
                matches = [
                    item for item in contained if len(_normalize(item[0])) == longest
                ]
                method = "contained_repository_alias"
        unique: dict[str, TerminologyMatchContract] = {}
        for alias, concept in matches:
            unique[concept.concept_id] = self.catalog.match_for_concept(
                concept,
                matched_text=phrase,
                matched_alias=alias,
                match_method=method,
            )
        return list(unique.values())

    def scan(
        self, text: str
    ) -> list[tuple[int, int, list[TerminologyMatchContract]]]:
        raw_hits: dict[tuple[int, int], list[TerminologyMatchContract]] = {}
        for normalized_alias, entries in self._aliases.items():
            if len(normalized_alias) < 2:
                continue
            # Chinese aliases are stored without whitespace/punctuation.  Runtime
            # scanning deliberately remains exact and auditable rather than fuzzy.
            for match in re.finditer(re.escape(normalized_alias), _normalize(text)):
                span = _normalized_span_to_original(text, match.start(), match.end())
                if span is None:
                    continue
                start, end = span
                phrase = text[start:end]
                for alias, concept in entries:
                    raw_hits.setdefault((start, end), []).append(
                        self.catalog.match_for_concept(
                            concept,
                            matched_text=phrase,
                            matched_alias=alias,
                            match_method="repository_alias_scan",
                        )
                    )
        # Prefer longer overlapping phrases, while preserving multiple concepts
        # for the exact same span so the UI can ask a disambiguation question.
        selected: list[tuple[int, int, list[TerminologyMatchContract]]] = []
        for (start, end), matches in sorted(
            raw_hits.items(), key=lambda item: (-(item[0][1] - item[0][0]), item[0][0])
        ):
            if any(start < old_end and end > old_start for old_start, old_end, _ in selected):
                continue
            unique = {item.concept_id: item for item in matches}
            selected.append((start, end, list(unique.values())))
        return sorted(selected, key=lambda item: item[0])


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def _normalized_span_to_original(
    original: str, normalized_start: int, normalized_end: int
) -> tuple[int, int] | None:
    positions = [index for index, char in enumerate(original) if char.isalnum()]
    if normalized_start >= len(positions) or normalized_end <= normalized_start:
        return None
    final = normalized_end - 1
    if final >= len(positions):
        return None
    return positions[normalized_start], positions[final] + 1


@lru_cache(maxsize=1)
def load_glp1_symptom_catalog() -> TerminologyCatalog:
    return TerminologyCatalog.model_validate(
        json.loads(DATA_FILE.read_text(encoding="utf-8"))
    )

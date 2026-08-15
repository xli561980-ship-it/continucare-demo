"""Read-only resolver seams for bundle files, artifacts, identities and blobs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from continucare.fhir.questionnaires import flatten_questionnaire_items
from continucare.knowledge.models import (
    ArtifactRef,
    CatalogTermRef,
    ObservationMappingItemRef,
    PathwayRef,
    PlanDefinitionRef,
    QuestionnaireItemRef,
    QuestionnaireTerminologyBindingRef,
    TerminologyConceptRef,
    safe_relative_parts,
)
from continucare.pathways.fhir_artifacts import load_fhir_artifact
from continucare.pathways.mappings import load_observation_mapping
from continucare.pathways.registry import PathwayRegistry, load_builtin_pathways
from continucare.terminology.catalog import (
    CatalogConcept,
    TerminologyCatalog,
    load_glp1_symptom_catalog,
)

class BundleSource(Protocol):
    def read_bytes(self, relative_posix_path: str) -> bytes: ...


class SourceArtifactResolver(Protocol):
    def read_bytes(self, relative_posix_path: str) -> bytes: ...


@dataclass(frozen=True)
class FilesystemBundleSource:
    root: Path

    def __init__(self, root: str | Path):
        object.__setattr__(self, "root", Path(root).resolve())

    def read_bytes(self, relative_posix_path: str) -> bytes:
        parts = safe_relative_parts(relative_posix_path)
        candidate = self.root.joinpath(*parts)
        cursor = self.root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("resource path cannot traverse a symlink")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("resource path resolves outside its root")
        return resolved.read_bytes()


@dataclass(frozen=True)
class TraversableBundleSource:
    root: Traversable

    def read_bytes(self, relative_posix_path: str) -> bytes:
        if isinstance(self.root, Path):
            return FilesystemBundleSource(self.root).read_bytes(relative_posix_path)
        resource = self.root
        for part in safe_relative_parts(relative_posix_path):
            resource = resource.joinpath(part)
        return resource.read_bytes()


class FilesystemSourceArtifactResolver(FilesystemBundleSource):
    """Safe third-party artifact resolver rooted at an explicit directory."""


@dataclass(frozen=True)
class ResolvedAuthority:
    actor_reference: str
    roles: frozenset[str]


class AuthorityResolver(Protocol):
    def resolve(self, actor_reference: str) -> ResolvedAuthority | None: ...


class NullAuthorityResolver:
    def resolve(self, actor_reference: str) -> ResolvedAuthority | None:
        del actor_reference
        return None


@dataclass(frozen=True)
class MappingAuthorityResolver:
    """Deterministic resolver for tests or a pre-verified local authority map."""

    authorities: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authorities",
            MappingProxyType(
                {key: frozenset(value) for key, value in self.authorities.items()}
            ),
        )

    def resolve(self, actor_reference: str) -> ResolvedAuthority | None:
        roles = self.authorities.get(actor_reference)
        if roles is None:
            return None
        return ResolvedAuthority(actor_reference=actor_reference, roles=roles)


@dataclass(frozen=True)
class ArtifactResolution:
    resolved: bool
    detail: str


@dataclass(frozen=True)
class CatalogTermResolution:
    resolved: bool
    detail: str
    catalog: TerminologyCatalog | None = None
    concept: CatalogConcept | None = None


class ArtifactResolver(Protocol):
    def resolve(self, pathway: PathwayRef, artifact: ArtifactRef) -> ArtifactResolution: ...

    def resolve_catalog_term(self, term: CatalogTermRef) -> CatalogTermResolution: ...


class RepositoryArtifactResolver:
    """Resolve the five artifact kinds supported by the first implementation slice."""

    def __init__(
        self,
        *,
        pathways: PathwayRegistry | None = None,
        terminology_catalogs: tuple[TerminologyCatalog, ...] | None = None,
    ):
        self.pathways = pathways or load_builtin_pathways()
        catalogs = terminology_catalogs or (load_glp1_symptom_catalog(),)
        self.terminology_catalogs = MappingProxyType(
            {(item.catalog_id, item.version): item for item in catalogs}
        )
        if len(self.terminology_catalogs) != len(catalogs):
            raise ValueError("terminology catalog id/version values must be unique")

    def resolve(self, pathway: PathwayRef, artifact: ArtifactRef) -> ArtifactResolution:
        try:
            if isinstance(artifact, QuestionnaireItemRef):
                return self._questionnaire_item(pathway, artifact)
            if isinstance(artifact, ObservationMappingItemRef):
                return self._mapping_item(pathway, artifact)
            if isinstance(artifact, QuestionnaireTerminologyBindingRef):
                return self._questionnaire_binding(artifact)
            if isinstance(artifact, TerminologyConceptRef):
                return self._terminology_concept(artifact)
            if isinstance(artifact, PlanDefinitionRef):
                return self._plan_definition(pathway, artifact)
            return ArtifactResolution(False, f"no resolver for {artifact.artifact_kind}")
        except (LookupError, ValueError, KeyError) as exc:
            return ArtifactResolution(False, str(exc))

    def resolve_catalog_term(self, term: CatalogTermRef) -> CatalogTermResolution:
        catalog = self._catalog(term.catalog_id, term.catalog_version)
        if catalog is None:
            return CatalogTermResolution(
                False, "terminology catalog id/version is unavailable"
            )
        try:
            concept = catalog.concept(term.concept_id)
        except LookupError as exc:
            return CatalogTermResolution(False, str(exc), catalog=catalog)
        return CatalogTermResolution(
            True,
            "exact terminology catalog term resolved",
            catalog=catalog,
            concept=concept,
        )

    def _pathway(self, ref: PathwayRef):
        return self.pathways.get(ref.pathway_code, ref.pathway_version)

    def _published_artifact(self, pathway: PathwayRef, resource_type: str):
        definition = self._pathway(pathway)
        matches = [
            item for item in definition.artifacts if item.resource_type == resource_type
        ]
        if len(matches) != 1:
            raise LookupError(
                f"{pathway.pathway_code} {pathway.pathway_version} does not publish "
                f"exactly one {resource_type}"
            )
        return definition, matches[0]

    def _questionnaire_item(
        self, pathway: PathwayRef, artifact: QuestionnaireItemRef
    ) -> ArtifactResolution:
        _, reference = self._published_artifact(pathway, "Questionnaire")
        if (
            reference.canonical != artifact.questionnaire_canonical
            or reference.version != artifact.questionnaire_version
        ):
            return ArtifactResolution(False, "Questionnaire canonical/version is not owned by pathway")
        resource = load_fhir_artifact(reference.file_name, "Questionnaire")
        if (
            resource.get("url") != reference.canonical
            or resource.get("version") != reference.version
        ):
            return ArtifactResolution(False, "Questionnaire bytes disagree with pathway manifest")
        matches = [
            item
            for item in flatten_questionnaire_items(resource.get("item", []))
            if item.get("linkId") == artifact.link_id
        ]
        if len(matches) != 1:
            return ArtifactResolution(False, "Questionnaire linkId is missing or ambiguous")
        return ArtifactResolution(True, "Questionnaire item resolved")

    def _plan_definition(
        self, pathway: PathwayRef, artifact: PlanDefinitionRef
    ) -> ArtifactResolution:
        _, reference = self._published_artifact(pathway, "PlanDefinition")
        if reference.canonical != artifact.canonical or reference.version != artifact.version:
            return ArtifactResolution(False, "PlanDefinition canonical/version is not owned by pathway")
        resource = load_fhir_artifact(reference.file_name, "PlanDefinition")
        if resource.get("url") != artifact.canonical or resource.get("version") != artifact.version:
            return ArtifactResolution(False, "PlanDefinition bytes disagree with pathway manifest")
        return ArtifactResolution(True, "PlanDefinition resolved")

    def _mapping_item(
        self, pathway: PathwayRef, artifact: ObservationMappingItemRef
    ) -> ArtifactResolution:
        definition = self._pathway(pathway)
        if (artifact.pathway_code, artifact.pathway_version) != pathway.key():
            return ArtifactResolution(False, "mapping ref pathway disagrees with binding pathway")
        if artifact.mapping_file != definition.observation_mapping_file:
            return ArtifactResolution(False, "mapping file is not owned by pathway")
        policy = load_observation_mapping(definition.observation_mapping_file)
        if (policy.pathway_code, policy.pathway_version) != pathway.key():
            return ArtifactResolution(False, "mapping policy code/version disagrees with pathway")
        _, questionnaire_ref = self._published_artifact(pathway, "Questionnaire")
        if policy.questionnaire != f"{questionnaire_ref.canonical}|{questionnaire_ref.version}":
            return ArtifactResolution(False, "mapping policy targets a different Questionnaire")
        matches = [item for item in policy.mappings if item.link_id == artifact.link_id]
        if len(matches) != 1:
            return ArtifactResolution(False, "mapping linkId is missing or ambiguous")
        return ArtifactResolution(True, "Observation mapping item resolved")

    def _catalog(
        self, catalog_id: str, catalog_version: str
    ) -> TerminologyCatalog | None:
        return self.terminology_catalogs.get((catalog_id, catalog_version))

    def _questionnaire_binding(
        self, artifact: QuestionnaireTerminologyBindingRef
    ) -> ArtifactResolution:
        catalog = self._catalog(artifact.catalog_id, artifact.catalog_version)
        if catalog is None:
            return ArtifactResolution(False, "terminology catalog id/version is unavailable")
        if catalog.binding(artifact.link_id) is None:
            return ArtifactResolution(False, "questionnaire terminology binding was not found")
        return ArtifactResolution(True, "Questionnaire terminology binding resolved")

    def _terminology_concept(
        self, artifact: TerminologyConceptRef
    ) -> ArtifactResolution:
        catalog = self._catalog(artifact.catalog_id, artifact.catalog_version)
        if catalog is None:
            return ArtifactResolution(False, "terminology catalog id/version is unavailable")
        catalog.concept(artifact.concept_id)
        return ArtifactResolution(True, "Terminology concept resolved")

"""Atomic loading and read-only lookup for Knowledge Evidence bundles."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Callable, Iterable, TypeVar

from pydantic import TypeAdapter, ValidationError

from continucare.knowledge.models import (
    BindingLifecycle,
    BindingManifestFile,
    BindingRecord,
    BindingRef,
    CitationRef,
    ClaimLifecycle,
    ClaimRef,
    ClaimRegistryFile,
    ClaimType,
    CoverageReport,
    CoverageGapRecord,
    CoverageGapRef,
    DataQualityRule,
    EquivalenceDecisionEvent,
    EvidenceClaim,
    GovernanceRegistryFile,
    KnowledgeArtifactUnresolved,
    KnowledgeAuthorityError,
    KnowledgeBundleError,
    KnowledgeBundleIndex,
    KnowledgeClaim,
    KnowledgeCurrentSelectionError,
    KnowledgePinnedFileError,
    KnowledgeReferenceError,
    KnowledgeSchemaError,
    KnowledgeSourceArtifactError,
    KnowledgeRelease,
    LicenseDecisionEvent,
    LinkOnlyAccess,
    LocalArtifactAccess,
    MetricDefinition,
    PatientContent,
    PathwayWhitelistScope,
    PathwayRef,
    PayloadEnvelope,
    ProductRecord,
    ReleaseManifest,
    ReleaseSourceRecord,
    ReviewAggregate,
    ReviewEvent,
    ReviewSummary,
    SourceRecord,
    SourceRef,
    SourceRegistryFile,
    SourceRegistryStatus,
    SourcedClinicalClaim,
    SymptomIndexFile,
    SymptomIndexLifecycle,
    SymptomIndexRecord,
    SymptomIndexRef,
    TerminologyEntry,
    UniversalNonclinicalStandardScope,
    WorkflowDesignDecision,
    artifact_key,
)
from continucare.knowledge.resolvers import (
    ArtifactResolution,
    ArtifactResolver,
    AuthorityResolver,
    BundleSource,
    CatalogTermResolution,
    NullAuthorityResolver,
    RepositoryArtifactResolver,
    ResolvedAuthority,
    SourceArtifactResolver,
    TraversableBundleSource,
)


_ENVELOPE_ADAPTER = TypeAdapter(PayloadEnvelope)


class LoadMode(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"


@dataclass(frozen=True)
class PathwayKnowledgeView:
    pathway: PathwayRef
    mode: LoadMode
    bindings: tuple[BindingRecord, ...]
    gaps: tuple[CoverageGapRecord, ...]
    claims: Mapping[tuple[str, int], KnowledgeClaim]
    sources: Mapping[tuple[str, int], SourceRecord]
    review_summaries: Mapping[tuple[str, int], ReviewSummary]
    source_review_status: Mapping[tuple[str, int], str]
    artifact_resolutions: Mapping[tuple[str, str, int], ArtifactResolution]
    source_content_status: Mapping[tuple[str, int], str]
    current_source_keys: frozenset[tuple[str, int]]
    current_claim_keys: frozenset[tuple[str, int]]
    current_binding_keys: frozenset[tuple[str, int]]
    current_gap_keys: frozenset[tuple[str, int]]

    def __post_init__(self) -> None:
        for name in (
            "claims",
            "sources",
            "review_summaries",
            "source_review_status",
            "artifact_resolutions",
            "source_content_status",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def unique_artifact_count(self) -> int:
        """Count distinct exact artifact targets in this Pathway view."""

        return len(
            {artifact_key(item.artifact) for item in (*self.bindings, *self.gaps)}
        )

    @property
    def registered_relationship_count(self) -> int:
        return len(self.bindings)

    @property
    def explicit_gap_count(self) -> int:
        return len(self.gaps)

    @property
    def verified_citation_relationship_count(self) -> int:
        return sum(
            self.review_summaries[item.claim.key()].axes.get(
                "citation_verification"
            )
            == "approved"
            for item in self.bindings
        )

    @property
    def claim_review_approved_relationship_count(self) -> int:
        """Count bindings whose knowledge claim review is approved.

        This does not assert that the referenced artifact or binding has passed
        its separately listed governance requirements.
        """

        return sum(
            self.review_summaries[item.claim.key()].aggregate == "approved"
            for item in self.bindings
        )


@dataclass(frozen=True)
class SymptomKnowledgeView:
    record: SymptomIndexRecord
    mode: LoadMode
    catalog_resolution: CatalogTermResolution
    claims: tuple[KnowledgeClaim, ...]
    bindings: tuple[BindingRecord, ...]
    gaps: tuple[CoverageGapRecord, ...]
    sources: tuple[SourceRecord, ...]
    review_summaries: Mapping[tuple[str, int], ReviewSummary]
    source_review_status: Mapping[tuple[str, int], str]
    source_content_status: Mapping[tuple[str, int], str]

    def __post_init__(self) -> None:
        for name in (
            "review_summaries",
            "source_review_status",
            "source_content_status",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True)
class KnowledgeRegistry:
    mode: LoadMode
    sources: tuple[SourceRecord, ...]
    claims: tuple[KnowledgeClaim, ...]
    bindings: tuple[BindingRecord, ...]
    gaps: tuple[CoverageGapRecord, ...]
    symptom_indexes: tuple[SymptomIndexRecord, ...]
    governance: GovernanceRegistryFile
    current_source_refs: tuple[SourceRef, ...]
    current_claim_refs: tuple[ClaimRef, ...]
    current_binding_refs: tuple[BindingRef, ...]
    current_gap_refs: tuple[CoverageGapRef, ...]
    current_symptom_index_refs: tuple[SymptomIndexRef, ...]
    artifact_resolutions: Mapping[tuple[str, str, int], ArtifactResolution]
    source_content_status: Mapping[tuple[str, int], str]
    review_summaries: Mapping[tuple[str, int], ReviewSummary]
    source_review_status: Mapping[tuple[str, int], str]
    event_verification: Mapping[str, str]
    catalog_term_resolutions: Mapping[tuple[str, int], CatalogTermResolution]

    def __post_init__(self) -> None:
        for name in (
            "artifact_resolutions",
            "source_content_status",
            "review_summaries",
            "source_review_status",
            "event_verification",
            "catalog_term_resolutions",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def for_pathway(self, code: str, version: str) -> PathwayKnowledgeView:
        pathway = PathwayRef(pathway_code=code, pathway_version=version)
        binding_refs = {item.key() for item in self.current_binding_refs}
        gap_refs = {item.key() for item in self.current_gap_refs}
        if self.mode == LoadMode.CURRENT:
            bindings = tuple(
                item
                for item in self.bindings
                if item.pathway == pathway and item.ref.key() in binding_refs
            )
            gaps = tuple(
                item
                for item in self.gaps
                if item.pathway == pathway and item.ref.key() in gap_refs
            )
        else:
            bindings = tuple(item for item in self.bindings if item.pathway == pathway)
            gaps = tuple(item for item in self.gaps if item.pathway == pathway)
        if not bindings and not gaps:
            raise KnowledgeReferenceError(
                f"knowledge pathway {code} version {version} was not found"
            )
        claims = {(item.claim_id, item.claim_version): item for item in self.claims}
        sources = {(item.source_id, item.record_version): item for item in self.sources}
        return PathwayKnowledgeView(
            pathway=pathway,
            mode=self.mode,
            bindings=bindings,
            gaps=gaps,
            claims=claims,
            sources=sources,
            review_summaries=self.review_summaries,
            source_review_status=self.source_review_status,
            artifact_resolutions=self.artifact_resolutions,
            source_content_status=self.source_content_status,
            current_source_keys=frozenset(item.key() for item in self.current_source_refs),
            current_claim_keys=frozenset(item.key() for item in self.current_claim_refs),
            current_binding_keys=frozenset(item.key() for item in self.current_binding_refs),
            current_gap_keys=frozenset(item.key() for item in self.current_gap_refs),
        )

    def symptom_views(self) -> tuple[SymptomKnowledgeView, ...]:
        selected = {item.key() for item in self.current_symptom_index_refs}
        records = (
            tuple(item for item in self.symptom_indexes if item.ref.key() in selected)
            if self.mode == LoadMode.CURRENT
            else self.symptom_indexes
        )
        return tuple(self._symptom_view(item) for item in records)

    def for_symptom(self, symptom_index_id: str, record_version: int) -> SymptomKnowledgeView:
        record = next(
            (
                item
                for item in self.symptom_indexes
                if item.ref.key() == (symptom_index_id, record_version)
            ),
            None,
        )
        if record is None:
            raise KnowledgeReferenceError(
                f"symptom index {symptom_index_id} version {record_version} was not found"
            )
        if self.mode == LoadMode.CURRENT and record.ref.key() not in {
            item.key() for item in self.current_symptom_index_refs
        }:
            raise KnowledgeCurrentSelectionError(
                f"symptom index {record.ref} is not selected as CURRENT"
            )
        return self._symptom_view(record)

    def _symptom_view(self, record: SymptomIndexRecord) -> SymptomKnowledgeView:
        claim_map = {item.ref.key(): item for item in self.claims}
        binding_map = {item.ref.key(): item for item in self.bindings}
        gap_map = {item.ref.key(): item for item in self.gaps}
        claims = tuple(claim_map[item.key()] for item in record.claim_refs)
        sources_by_key: dict[tuple[str, int], SourceRecord] = {}
        source_map = {item.ref.key(): item for item in self.sources}
        for claim in claims:
            if isinstance(claim, SourcedClinicalClaim):
                for citation in claim.citations:
                    sources_by_key[citation.source.key()] = source_map[citation.source.key()]
        return SymptomKnowledgeView(
            record=record,
            mode=self.mode,
            catalog_resolution=self.catalog_term_resolutions[record.ref.key()],
            claims=claims,
            bindings=tuple(binding_map[item.key()] for item in record.binding_refs),
            gaps=tuple(gap_map[item.key()] for item in record.coverage_gap_refs),
            sources=tuple(sources_by_key.values()),
            review_summaries=self.review_summaries,
            source_review_status=self.source_review_status,
            source_content_status=self.source_content_status,
        )


def load_builtin_bundle(
    *,
    mode: LoadMode | str = LoadMode.CURRENT,
    artifact_resolver: ArtifactResolver | None = None,
    authority_resolver: AuthorityResolver | None = None,
    source_artifact_resolver: SourceArtifactResolver | None = None,
) -> KnowledgeRegistry:
    try:
        source = TraversableBundleSource(files("continucare.knowledge.manifests"))
    except Exception as exc:
        raise KnowledgePinnedFileError(
            f"cannot locate the built-in knowledge manifest package: {exc}"
        ) from exc
    if artifact_resolver is None:
        try:
            artifact_resolver = RepositoryArtifactResolver()
        except KnowledgeBundleError:
            raise
        except Exception as exc:
            raise KnowledgeArtifactUnresolved(
                f"cannot initialize the repository artifact resolver: {exc}"
            ) from exc
    return load_bundle(
        source,
        "bundle_index_v1.json",
        mode=mode,
        artifact_resolver=artifact_resolver,
        authority_resolver=authority_resolver,
        source_artifact_resolver=source_artifact_resolver,
    )


def inspect_bundle(
    source: BundleSource,
    index_name: str,
    *,
    artifact_resolver: ArtifactResolver,
    authority_resolver: AuthorityResolver | None = None,
    source_artifact_resolver: SourceArtifactResolver | None = None,
) -> KnowledgeRegistry:
    return load_bundle(
        source,
        index_name,
        mode=LoadMode.HISTORICAL,
        artifact_resolver=artifact_resolver,
        authority_resolver=authority_resolver,
        source_artifact_resolver=source_artifact_resolver,
    )


def load_bundle(
    source: BundleSource,
    index_name: str,
    *,
    artifact_resolver: ArtifactResolver,
    authority_resolver: AuthorityResolver | None = None,
    source_artifact_resolver: SourceArtifactResolver | None = None,
    mode: LoadMode | str = LoadMode.CURRENT,
) -> KnowledgeRegistry:
    """Load a pinned bundle atomically; no state is returned on any failure."""

    try:
        selected_mode = LoadMode(mode)
    except ValueError as exc:
        raise KnowledgeSchemaError(f"unknown load mode {mode!r}") from exc
    index_bytes = _read_pinned_source(source, index_name, "bundle index")
    try:
        index = KnowledgeBundleIndex.model_validate_json(index_bytes)
    except ValidationError as exc:
        raise KnowledgeSchemaError(f"invalid knowledge bundle index: {exc}") from exc

    file_keys = [item.ref.key() for item in index.files]
    paths = [item.relative_path for item in index.files]
    if len(file_keys) != len(set(file_keys)) or len(paths) != len(set(paths)):
        raise KnowledgeSchemaError("bundle index contains duplicate pinned files or paths")

    envelopes: list[PayloadEnvelope] = []
    for pinned in index.files:
        payload = _read_pinned_source(source, pinned.relative_path, str(pinned.ref))
        if pinned.size is not None and len(payload) != pinned.size:
            raise KnowledgePinnedFileError(
                f"{pinned.relative_path} size does not match pinned size"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != pinned.manifest_sha256:
            raise KnowledgePinnedFileError(
                f"{pinned.relative_path} raw-byte SHA-256 does not match bundle index"
            )
        try:
            envelope = _ENVELOPE_ADAPTER.validate_json(payload)
        except ValidationError as exc:
            raise KnowledgeSchemaError(
                f"invalid pinned payload {pinned.relative_path}: {exc}"
            ) from exc
        if envelope.ref != pinned.ref:
            raise KnowledgeReferenceError(
                f"payload {pinned.relative_path} declares {envelope.ref}, expected {pinned.ref}"
            )
        envelopes.append(envelope)

    source_files = [item for item in envelopes if isinstance(item, SourceRegistryFile)]
    claim_files = [item for item in envelopes if isinstance(item, ClaimRegistryFile)]
    binding_files = [item for item in envelopes if isinstance(item, BindingManifestFile)]
    governance_files = [
        item for item in envelopes if isinstance(item, GovernanceRegistryFile)
    ]
    symptom_index_files = [
        item for item in envelopes if isinstance(item, SymptomIndexFile)
    ]
    if len(source_files) != 1 or len(claim_files) != 1 or len(governance_files) != 1:
        raise KnowledgeSchemaError(
            "bundle must pin exactly one source, claim and governance registry"
        )
    if not binding_files:
        raise KnowledgeSchemaError("bundle must pin at least one binding manifest")
    if len(symptom_index_files) > 1:
        raise KnowledgeSchemaError("bundle may pin at most one symptom index")

    sources = tuple(source_files[0].records)
    claims = tuple(claim_files[0].records)
    bindings = tuple(item for file in binding_files for item in file.records)
    governance = governance_files[0]
    gaps = tuple(governance.coverage_gaps)
    symptom_indexes = (
        tuple(symptom_index_files[0].records) if symptom_index_files else ()
    )

    try:
        _validate_bundle_structure(
            sources=sources,
            claims=claims,
            bindings=bindings,
            binding_files=binding_files,
            governance=governance,
            gaps=gaps,
            symptom_indexes=symptom_indexes,
        )
    except KnowledgeReferenceError:
        raise
    except ValueError as exc:
        raise KnowledgeSchemaError(str(exc)) from exc

    source_map = {item.ref.key(): item for item in sources}
    claim_map = {item.ref.key(): item for item in claims}
    binding_map = {item.ref.key(): item for item in bindings}
    gap_map = {item.ref.key(): item for item in gaps}
    symptom_index_map = {item.ref.key(): item for item in symptom_indexes}

    current_source_refs = tuple(index.current_source_refs)
    current_claim_refs = tuple(index.current_claim_refs)
    current_binding_refs = tuple(index.current_binding_refs)
    current_gap_refs = tuple(index.current_gap_refs)
    current_symptom_index_refs = tuple(index.current_symptom_index_refs)
    _validate_selection_refs(
        current_source_refs,
        current_claim_refs,
        current_binding_refs,
        current_gap_refs,
        source_map,
        claim_map,
        binding_map,
        gap_map,
        current_symptom_index_refs,
        symptom_index_map,
    )
    if selected_mode == LoadMode.CURRENT:
        _validate_current_eligibility(
            current_source_refs,
            current_claim_refs,
            current_binding_refs,
            current_gap_refs,
            source_map,
            claim_map,
            binding_map,
            gap_map,
            current_symptom_index_refs,
            symptom_index_map,
        )

    catalog_term_resolutions: dict[tuple[str, int], CatalogTermResolution] = {}
    selected_symptom_keys = {item.key() for item in current_symptom_index_refs}
    for record in symptom_indexes:
        if selected_mode == LoadMode.CURRENT and record.ref.key() not in selected_symptom_keys:
            continue
        try:
            resolution = artifact_resolver.resolve_catalog_term(record.catalog_term)
            if not isinstance(resolution, CatalogTermResolution):
                raise TypeError(
                    "ArtifactResolver.resolve_catalog_term() must return CatalogTermResolution"
                )
        except KnowledgeBundleError:
            raise
        except Exception as exc:
            if selected_mode == LoadMode.CURRENT:
                raise KnowledgeArtifactUnresolved(
                    f"{record.ref} catalog resolver failed: {exc}"
                ) from exc
            resolution = CatalogTermResolution(
                False, f"catalog resolver failed during historical inspection: {exc}"
            )
        catalog_term_resolutions[record.ref.key()] = resolution
        if selected_mode == LoadMode.CURRENT and not resolution.resolved:
            raise KnowledgeArtifactUnresolved(
                f"{record.ref} exact catalog term {record.catalog_term.key()} is unresolved: "
                f"{resolution.detail}"
            )

    ownership = {
        (item.catalog_id, item.catalog_version): item.owner
        for item in governance.artifact_ownership
    }
    resolution_map: dict[tuple[str, str, int], ArtifactResolution] = {}
    selected_binding_keys = {item.key() for item in current_binding_refs}
    selected_gap_keys = {item.key() for item in current_gap_refs}
    for record_kind, record, selected in (
        *(
            ("binding", item, item.ref.key() in selected_binding_keys)
            for item in bindings
        ),
        *(("gap", item, item.ref.key() in selected_gap_keys) for item in gaps),
    ):
        _validate_catalog_ownership(record.pathway, record.artifact, ownership)
        if selected_mode == LoadMode.CURRENT and not selected:
            continue
        try:
            resolution = artifact_resolver.resolve(record.pathway, record.artifact)
            if not isinstance(resolution, ArtifactResolution):
                raise TypeError("ArtifactResolver must return ArtifactResolution")
        except KnowledgeArtifactUnresolved as exc:
            if selected_mode == LoadMode.CURRENT:
                raise
            resolution = ArtifactResolution(False, str(exc))
        except KnowledgeBundleError:
            raise
        except Exception as exc:
            if selected_mode == LoadMode.CURRENT:
                raise KnowledgeArtifactUnresolved(
                    f"{record.ref} artifact resolver failed: {exc}"
                ) from exc
            resolution = ArtifactResolution(
                False, f"artifact resolver failed during historical inspection: {exc}"
            )
        key = (record_kind, record.ref.key()[0], record.ref.key()[1])
        resolution_map[key] = resolution
        if selected_mode == LoadMode.CURRENT and selected and not resolution.resolved:
            raise KnowledgeArtifactUnresolved(
                f"{record.ref} target {artifact_key(record.artifact)} is unresolved: "
                f"{resolution.detail}"
            )

    heads = _review_heads(governance.review_events)
    trusted_event_ids, event_verification = _verify_review_events(
        events=governance.review_events,
        heads=heads,
        authority_resolver=authority_resolver or NullAuthorityResolver(),
        mode=selected_mode,
        current_sources={item.key() for item in current_source_refs},
        current_claims={item.key() for item in current_claim_refs},
        claim_map=claim_map,
    )
    if selected_mode == LoadMode.CURRENT:
        _reject_current_rejections(
            heads,
            trusted_event_ids,
            {item.key() for item in current_source_refs},
            {item.key() for item in current_claim_refs},
            claim_map,
        )
        _validate_current_quotes(
            claims=claims,
            current_claim_keys={item.key() for item in current_claim_refs},
            heads=heads,
            trusted_event_ids=trusted_event_ids,
        )

    content_status = _validate_source_artifacts(
        sources=sources,
        current_source_keys={item.key() for item in current_source_refs},
        mode=selected_mode,
        source_artifact_resolver=source_artifact_resolver,
        heads=heads,
        trusted_event_ids=trusted_event_ids,
    )
    _validate_manifestations(
        sources=sources,
        current_source_keys={item.key() for item in current_source_refs},
        mode=selected_mode,
        heads=heads,
        trusted_event_ids=trusted_event_ids,
    )
    review_summaries = {
        item.ref.key(): _claim_review_summary(item, heads, trusted_event_ids)
        for item in claims
    }
    source_review_status = {
        item.ref.key(): _trusted_decision(
            _head_for(heads, "internal_consistency", item.ref), trusted_event_ids
        )
        for item in sources
    }

    return KnowledgeRegistry(
        mode=selected_mode,
        sources=sources,
        claims=claims,
        bindings=bindings,
        gaps=gaps,
        symptom_indexes=symptom_indexes,
        governance=governance,
        current_source_refs=current_source_refs,
        current_claim_refs=current_claim_refs,
        current_binding_refs=current_binding_refs,
        current_gap_refs=current_gap_refs,
        current_symptom_index_refs=current_symptom_index_refs,
        artifact_resolutions=resolution_map,
        source_content_status=content_status,
        review_summaries=review_summaries,
        source_review_status=source_review_status,
        event_verification=event_verification,
        catalog_term_resolutions=catalog_term_resolutions,
    )


def _read_pinned_source(source: BundleSource, path: str, label: str) -> bytes:
    try:
        payload = source.read_bytes(path)
        if not isinstance(payload, bytes):
            raise TypeError("BundleSource.read_bytes() must return bytes")
        return payload
    except KnowledgePinnedFileError:
        raise
    except Exception as exc:
        raise KnowledgePinnedFileError(f"cannot read {label} at {path!r}: {exc}") from exc


T = TypeVar("T")


def _unique_map(items: Iterable[T], key: Callable[[T], Any], label: str) -> dict[Any, T]:
    result: dict[Any, T] = {}
    for item in items:
        item_key = key(item)
        if item_key in result:
            raise ValueError(f"duplicate {label} {item_key!r}")
        result[item_key] = item
    return result


def _validate_linear_versions(
    items: Iterable[T],
    *,
    logical_id: Callable[[T], str],
    version: Callable[[T], int],
    predecessor: Callable[[T], tuple[str, int] | None],
    label: str,
) -> None:
    groups: dict[str, dict[int, T]] = {}
    for item in items:
        groups.setdefault(logical_id(item), {})[version(item)] = item
    for identifier, versions in groups.items():
        expected = set(range(1, max(versions) + 1))
        if set(versions) != expected:
            raise ValueError(f"{label} {identifier!r} versions must be continuous from 1")
        for number, item in versions.items():
            actual = predecessor(item)
            wanted = None if number == 1 else (identifier, number - 1)
            if actual != wanted:
                raise ValueError(
                    f"{label} {identifier!r} version {number} must supersede {wanted!r}"
                )


def _validate_bundle_structure(
    *,
    sources: tuple[SourceRecord, ...],
    claims: tuple[KnowledgeClaim, ...],
    bindings: tuple[BindingRecord, ...],
    binding_files: list[BindingManifestFile],
    governance: GovernanceRegistryFile,
    gaps: tuple[CoverageGapRecord, ...],
    symptom_indexes: tuple[SymptomIndexRecord, ...],
) -> None:
    source_map = _unique_map(sources, lambda item: item.ref.key(), "source ref")
    claim_map = _unique_map(claims, lambda item: item.ref.key(), "claim ref")
    _unique_map(bindings, lambda item: item.ref.key(), "binding ref")
    gap_map = _unique_map(gaps, lambda item: item.ref.key(), "coverage gap ref")
    _unique_map(
        symptom_indexes, lambda item: item.ref.key(), "symptom index ref"
    )
    _validate_linear_versions(
        sources,
        logical_id=lambda item: item.source_id,
        version=lambda item: item.record_version,
        predecessor=lambda item: item.supersedes.key() if item.supersedes else None,
        label="source",
    )
    _validate_linear_versions(
        claims,
        logical_id=lambda item: item.claim_id,
        version=lambda item: item.claim_version,
        predecessor=lambda item: item.supersedes.key() if item.supersedes else None,
        label="claim",
    )
    _validate_linear_versions(
        bindings,
        logical_id=lambda item: item.binding_id,
        version=lambda item: item.binding_version,
        predecessor=lambda item: item.supersedes.key() if item.supersedes else None,
        label="binding",
    )
    _validate_linear_versions(
        gaps,
        logical_id=lambda item: item.gap_id,
        version=lambda item: item.gap_version,
        predecessor=lambda item: item.supersedes.key() if item.supersedes else None,
        label="coverage gap",
    )
    _validate_linear_versions(
        symptom_indexes,
        logical_id=lambda item: item.symptom_index_id,
        version=lambda item: item.record_version,
        predecessor=lambda item: item.supersedes.key() if item.supersedes else None,
        label="symptom index",
    )
    for claim in claims:
        if isinstance(claim, SourcedClinicalClaim):
            for citation in claim.citations:
                source = source_map.get(citation.source.key())
                if source is None:
                    raise KnowledgeReferenceError(
                        f"claim {claim.ref} cites unknown source {citation.source}"
                    )
                if isinstance(source.access, LinkOnlyAccess) and citation.quote is not None:
                    raise ValueError("link-only source citations cannot embed a quote")
            if isinstance(claim.applicable_scope, UniversalNonclinicalStandardScope):
                allowed = {
                    "terminology_standard",
                    "unit_standard",
                    "interoperability_standard",
                }
                if any(
                    str(source_map[item.source.key()].source_type) not in allowed
                    for item in claim.citations
                ):
                    raise ValueError(
                        "universal nonclinical claim may only cite terminology, unit or interoperability standards"
                    )
    for binding_file in binding_files:
        for binding in binding_file.records:
            if binding.pathway != binding_file.pathway:
                raise ValueError("binding pathway must equal its manifest pathway")
    for binding in bindings:
        claim = claim_map.get(binding.claim.key())
        if claim is None:
            raise KnowledgeReferenceError(
                f"binding {binding.ref} references unknown claim {binding.claim}"
            )
        if isinstance(claim, WorkflowDesignDecision):
            if binding.binding_purpose != "documents_design_decision":
                raise ValueError("workflow design decision has an invalid binding purpose")
        elif binding.binding_purpose == "documents_design_decision":
            raise ValueError("documents_design_decision requires a workflow design decision")
        if isinstance(claim.applicable_scope, PathwayWhitelistScope):
            allowed_pathways = {item.key() for item in claim.applicable_scope.pathways}
            if binding.pathway.key() not in allowed_pathways:
                raise KnowledgeReferenceError(
                    f"binding {binding.ref} pathway is outside claim {claim.ref} scope"
                )
    binding_map = {item.ref.key(): item for item in bindings}
    for symptom_index in symptom_indexes:
        missing_claims = {item.key() for item in symptom_index.claim_refs} - set(
            claim_map
        )
        missing_bindings = {
            item.key() for item in symptom_index.binding_refs
        } - set(binding_map)
        missing_gaps = {
            item.key() for item in symptom_index.coverage_gap_refs
        } - set(gap_map)
        if missing_claims or missing_bindings or missing_gaps:
            raise KnowledgeReferenceError(
                f"symptom index {symptom_index.ref} has unknown exact refs: "
                f"claims={sorted(missing_claims)}, bindings={sorted(missing_bindings)}, "
                f"gaps={sorted(missing_gaps)}"
            )
        declared_claims = {item.key() for item in symptom_index.claim_refs}
        binding_claims = {
            binding_map[item.key()].claim.key()
            for item in symptom_index.binding_refs
        }
        if not binding_claims.issubset(declared_claims):
            raise KnowledgeReferenceError(
                f"symptom index {symptom_index.ref} binding claims must be explicitly listed"
            )
    alias_keys = [
        (item.namespace, item.legacy_id) for item in governance.legacy_source_aliases
    ]
    alias_targets = [item.target.key() for item in governance.legacy_source_aliases]
    if len(alias_keys) != len(set(alias_keys)):
        raise ValueError("legacy source aliases must be unique within a namespace")
    if len(alias_targets) != len(set(alias_targets)):
        raise ValueError("slice-1 legacy source aliases must map one-to-one")
    if any(key not in source_map for key in alias_targets):
        raise KnowledgeReferenceError("legacy source alias targets an unknown SourceRef")
    ownership_keys = [
        (item.catalog_id, item.catalog_version) for item in governance.artifact_ownership
    ]
    if len(ownership_keys) != len(set(ownership_keys)):
        raise ValueError("artifact ownership entries must be unique")
    _validate_event_references(governance.review_events, source_map, claim_map)
    _review_heads(governance.review_events)


def _head_keys(records: Iterable[Any]) -> set[tuple[str, int]]:
    return {
        max(group, key=lambda item: item[1])
        for group in _group_keys(item.ref.key() for item in records).values()
    }


def _group_keys(keys: Iterable[tuple[str, int]]) -> dict[str, list[tuple[str, int]]]:
    groups: dict[str, list[tuple[str, int]]] = {}
    for key in keys:
        groups.setdefault(key[0], []).append(key)
    return groups


def _validate_selection_refs(
    source_refs: tuple[SourceRef, ...],
    claim_refs: tuple[ClaimRef, ...],
    binding_refs: tuple[BindingRef, ...],
    gap_refs: tuple[CoverageGapRef, ...],
    source_map: dict[tuple[str, int], SourceRecord],
    claim_map: dict[tuple[str, int], KnowledgeClaim],
    binding_map: dict[tuple[str, int], BindingRecord],
    gap_map: dict[tuple[str, int], CoverageGapRecord],
    symptom_index_refs: tuple[SymptomIndexRef, ...],
    symptom_index_map: dict[tuple[str, int], SymptomIndexRecord],
) -> None:
    selections = (
        (source_refs, source_map, "source"),
        (claim_refs, claim_map, "claim"),
        (binding_refs, binding_map, "binding"),
        (gap_refs, gap_map, "coverage gap"),
        (symptom_index_refs, symptom_index_map, "symptom index"),
    )
    for refs, records, label in selections:
        keys = [item.key() for item in refs]
        if len(keys) != len(set(keys)):
            raise KnowledgeCurrentSelectionError(f"duplicate current {label} ref")
        unknown = set(keys) - set(records)
        if unknown:
            raise KnowledgeCurrentSelectionError(
                f"current {label} selection references unknown records: {sorted(unknown)}"
            )
        non_heads = set(keys) - _head_keys(records.values())
        if non_heads:
            raise KnowledgeCurrentSelectionError(
                f"current {label} selection contains non-head refs: {sorted(non_heads)}"
            )


def _validate_current_eligibility(
    source_refs: tuple[SourceRef, ...],
    claim_refs: tuple[ClaimRef, ...],
    binding_refs: tuple[BindingRef, ...],
    gap_refs: tuple[CoverageGapRef, ...],
    source_map: dict[tuple[str, int], SourceRecord],
    claim_map: dict[tuple[str, int], KnowledgeClaim],
    binding_map: dict[tuple[str, int], BindingRecord],
    gap_map: dict[tuple[str, int], CoverageGapRecord],
    symptom_index_refs: tuple[SymptomIndexRef, ...],
    symptom_index_map: dict[tuple[str, int], SymptomIndexRecord],
) -> None:
    for ref in source_refs:
        if source_map[ref.key()].registry_status != SourceRegistryStatus.ACTIVE:
            raise KnowledgeCurrentSelectionError(f"current source {ref} is not active")
    for ref in claim_refs:
        if claim_map[ref.key()].lifecycle in {
            ClaimLifecycle.SUPERSEDED,
            ClaimLifecycle.RETRACTED,
        }:
            raise KnowledgeCurrentSelectionError(f"current claim {ref} is not eligible")
    selected_claims = {item.key() for item in claim_refs}
    selected_sources = {item.key() for item in source_refs}
    for ref in binding_refs:
        binding = binding_map[ref.key()]
        if binding.lifecycle != BindingLifecycle.ACTIVE:
            raise KnowledgeCurrentSelectionError(f"current binding {ref} is not active")
        if binding.claim.key() not in selected_claims:
            raise KnowledgeCurrentSelectionError(
                f"current binding {ref} does not reference a selected current claim"
            )
        claim = claim_map[binding.claim.key()]
        if isinstance(claim, SourcedClinicalClaim):
            missing = {item.source.key() for item in claim.citations} - selected_sources
            if missing:
                raise KnowledgeCurrentSelectionError(
                    f"current binding {ref} depends on unselected sources {sorted(missing)}"
                )
    for ref in gap_refs:
        if gap_map[ref.key()].lifecycle != "open":
            raise KnowledgeCurrentSelectionError(f"current coverage gap {ref} is not open")
    selected_claims = {item.key() for item in claim_refs}
    selected_bindings = {item.key() for item in binding_refs}
    selected_gaps = {item.key() for item in gap_refs}
    for ref in symptom_index_refs:
        record = symptom_index_map[ref.key()]
        if record.lifecycle != SymptomIndexLifecycle.ACTIVE:
            raise KnowledgeCurrentSelectionError(
                f"current symptom index {ref} is not active"
            )
        if not {item.key() for item in record.claim_refs}.issubset(selected_claims):
            raise KnowledgeCurrentSelectionError(
                f"current symptom index {ref} references non-current claims"
            )
        if not {item.key() for item in record.binding_refs}.issubset(selected_bindings):
            raise KnowledgeCurrentSelectionError(
                f"current symptom index {ref} references non-current bindings"
            )
        if not {item.key() for item in record.coverage_gap_refs}.issubset(selected_gaps):
            raise KnowledgeCurrentSelectionError(
                f"current symptom index {ref} references non-current gaps"
            )


def _validate_catalog_ownership(
    pathway: PathwayRef,
    artifact: Any,
    ownership: dict[tuple[str, str], PathwayRef],
) -> None:
    catalog_id = getattr(artifact, "catalog_id", None)
    catalog_version = getattr(artifact, "catalog_version", None)
    if catalog_id is None:
        return
    owner = ownership.get((catalog_id, catalog_version))
    if owner != pathway:
        raise KnowledgeReferenceError(
            f"catalog {catalog_id}|{catalog_version} is not explicitly owned by {pathway.key()}"
        )


def _subject_key(event: ReviewEvent) -> tuple[Any, ...]:
    if isinstance(event, EquivalenceDecisionEvent):
        subject = event.subject
        normalized = (
            "equivalence_pair",
            *subject.manifestation.key(),
            *subject.canonical.key(),
        )
    else:
        normalized = _subject_identity(event.subject)
    return (event.event_type, *normalized)


def _subject_identity(subject: SourceRef | ClaimRef | CitationRef) -> tuple[Any, ...]:
    if isinstance(subject, CitationRef):
        return ("citation", *subject.key())
    if isinstance(subject, SourceRef):
        return ("source", *subject.key())
    return ("claim", *subject.key())


def _validate_event_references(
    events: list[ReviewEvent],
    source_map: dict[tuple[str, int], SourceRecord],
    claim_map: dict[tuple[str, int], KnowledgeClaim],
) -> None:
    citation_keys = {
        (claim.claim_id, claim.claim_version, citation.citation_id)
        for claim in claim_map.values()
        if isinstance(claim, SourcedClinicalClaim)
        for citation in claim.citations
    }
    for event in events:
        if isinstance(event, EquivalenceDecisionEvent):
            manifestation = source_map.get(event.subject.manifestation.key())
            if manifestation is None or event.subject.canonical.key() not in source_map:
                raise KnowledgeReferenceError("equivalence event references an unknown source")
            if manifestation.manifestation_of != event.subject.canonical:
                raise ValueError(
                    "equivalence event pair must equal the manifestation source record edge"
                )
        elif isinstance(event.subject, SourceRef):
            if event.subject.key() not in source_map:
                raise KnowledgeReferenceError("review event references an unknown source")
        elif isinstance(event.subject, ClaimRef):
            if event.subject.key() not in claim_map:
                raise KnowledgeReferenceError("review event references an unknown claim")
        elif isinstance(event.subject, CitationRef):
            if event.subject.key() not in citation_keys:
                raise KnowledgeReferenceError("review event references an unknown citation")


def _review_heads(events: list[ReviewEvent]) -> dict[tuple[Any, ...], ReviewEvent]:
    by_id = _unique_map(events, lambda item: item.event_id, "review event_id")
    groups: dict[tuple[Any, ...], list[ReviewEvent]] = {}
    for event in events:
        groups.setdefault(_subject_key(event), []).append(event)
    heads: dict[tuple[Any, ...], ReviewEvent] = {}
    for key, group in groups.items():
        group_ids = {item.event_id for item in group}
        roots = [item for item in group if item.supersedes_event_id is None]
        if len(roots) != 1:
            raise ValueError(f"review chain {key!r} must have exactly one root")
        successors: dict[str, str] = {}
        for event in group:
            predecessor = event.supersedes_event_id
            if predecessor is None:
                continue
            if predecessor not in by_id:
                raise ValueError(f"review event {event.event_id} supersedes an unknown event")
            if predecessor not in group_ids:
                raise ValueError("review event predecessor must have the same domain and subject")
            if predecessor in successors:
                raise ValueError("review event chain cannot branch")
            successors[predecessor] = event.event_id
        visited: set[str] = set()
        current = roots[0].event_id
        while current not in visited:
            visited.add(current)
            successor = successors.get(current)
            if successor is None:
                break
            current = successor
        if len(visited) != len(group):
            raise ValueError("review event chain must be connected and acyclic")
        heads[key] = by_id[current]
    return heads


def _event_is_current_related(
    event: ReviewEvent,
    current_sources: set[tuple[str, int]],
    current_claims: set[tuple[str, int]],
    claim_map: dict[tuple[str, int], KnowledgeClaim],
) -> bool:
    if isinstance(event, EquivalenceDecisionEvent):
        return event.subject.manifestation.key() in current_sources
    if isinstance(event.subject, SourceRef):
        return event.subject.key() in current_sources
    if isinstance(event.subject, ClaimRef):
        return event.subject.key() in current_claims
    claim = claim_map.get(event.subject.claim.key())
    return claim is not None and claim.ref.key() in current_claims


def _verify_review_events(
    *,
    events: list[ReviewEvent],
    heads: dict[tuple[Any, ...], ReviewEvent],
    authority_resolver: AuthorityResolver,
    mode: LoadMode,
    current_sources: set[tuple[str, int]],
    current_claims: set[tuple[str, int]],
    claim_map: dict[tuple[str, int], KnowledgeClaim],
) -> tuple[set[str], dict[str, str]]:
    trusted: set[str] = set()
    verification: dict[str, str] = {}
    head_ids = {item.event_id for item in heads.values()}
    for event in events:
        resolver_error: Exception | None = None
        try:
            identity = authority_resolver.resolve(event.actor_reference)
            if identity is not None and not isinstance(identity, ResolvedAuthority):
                raise TypeError("AuthorityResolver must return ResolvedAuthority or None")
        except Exception as exc:
            identity = None
            resolver_error = exc
        resolved = (
            identity is not None
            and identity.actor_reference == event.actor_reference
            and event.claimed_role in identity.roles
        )
        if resolved:
            trusted.add(event.event_id)
            verification[event.event_id] = "trusted"
        else:
            verification[event.event_id] = "unverified_assertion"
            if (
                mode == LoadMode.CURRENT
                and event.event_id in head_ids
                and _event_is_current_related(
                    event, current_sources, current_claims, claim_map
                )
            ):
                detail = (
                    f": resolver failed: {resolver_error}"
                    if resolver_error is not None
                    else ""
                )
                raise KnowledgeAuthorityError(
                    f"review event {event.event_id} actor/role could not be resolved"
                    f"{detail}"
                ) from resolver_error
    return trusted, verification


def _reject_current_rejections(
    heads: dict[tuple[Any, ...], ReviewEvent],
    trusted: set[str],
    current_sources: set[tuple[str, int]],
    current_claims: set[tuple[str, int]],
    claim_map: dict[tuple[str, int], KnowledgeClaim],
) -> None:
    for event in heads.values():
        if (
            event.event_id in trusted
            and event.decision == "rejected"
            and _event_is_current_related(event, current_sources, current_claims, claim_map)
        ):
            raise KnowledgeCurrentSelectionError(
                f"trusted rejected review event {event.event_id} blocks CURRENT selection"
            )


def _validate_source_artifacts(
    *,
    sources: tuple[SourceRecord, ...],
    current_source_keys: set[tuple[str, int]],
    mode: LoadMode,
    source_artifact_resolver: SourceArtifactResolver | None,
    heads: dict[tuple[Any, ...], ReviewEvent],
    trusted_event_ids: set[str],
) -> dict[tuple[str, int], str]:
    status: dict[tuple[str, int], str] = {}
    license_heads = {
        event.subject.key(): event
        for event in heads.values()
        if isinstance(event, LicenseDecisionEvent)
    }
    for source in sources:
        if isinstance(source.access, LinkOnlyAccess):
            status[source.ref.key()] = "not_content_fixed"
            continue
        assert isinstance(source.access, LocalArtifactAccess)
        if mode == LoadMode.HISTORICAL:
            status[source.ref.key()] = "not_read_in_historical_mode"
            continue
        selected = source.ref.key() in current_source_keys
        if not selected:
            status[source.ref.key()] = "not_read_not_selected"
            continue
        license_event = license_heads.get(source.ref.key())
        cleared = (
            license_event is not None
            and license_event.event_id in trusted_event_ids
            and license_event.decision == "approved"
            and license_event.payload is not None
            and "local_copy" in license_event.payload.allowed_uses
        )
        if not cleared:
            raise KnowledgeSourceArtifactError(
                f"local source artifact {source.ref} lacks a trusted approved local_copy license"
            )
        try:
            if source_artifact_resolver is None:
                raise ValueError("no SourceArtifactResolver was supplied")
            payload = source_artifact_resolver.read_bytes(
                source.access.artifact.relative_path
            )
            expected_size = source.access.artifact.size
            if expected_size is not None and len(payload) != expected_size:
                raise ValueError("third-party artifact size mismatch")
            if (
                hashlib.sha256(payload).hexdigest()
                != source.access.artifact.third_party_content_sha256
            ):
                raise ValueError("third-party artifact hash mismatch")
        except KnowledgeSourceArtifactError:
            raise
        except Exception as exc:
            raise KnowledgeSourceArtifactError(
                f"local source artifact {source.ref} is unavailable: {exc}"
            ) from exc
        status[source.ref.key()] = "content_fixed"
    return status


def _validate_current_quotes(
    *,
    claims: tuple[KnowledgeClaim, ...],
    current_claim_keys: set[tuple[str, int]],
    heads: dict[tuple[Any, ...], ReviewEvent],
    trusted_event_ids: set[str],
) -> None:
    license_heads = {
        event.subject.key(): event
        for event in heads.values()
        if isinstance(event, LicenseDecisionEvent)
    }
    for claim in claims:
        if not isinstance(claim, SourcedClinicalClaim):
            continue
        if claim.ref.key() not in current_claim_keys:
            continue
        for citation in claim.citations:
            if citation.quote is None:
                continue
            event = license_heads.get(citation.source.key())
            allowed = (
                event is not None
                and event.event_id in trusted_event_ids
                and event.decision == "approved"
                and event.payload is not None
                and "short_quote" in event.payload.allowed_uses
            )
            if not allowed:
                raise KnowledgeSourceArtifactError(
                    f"citation {claim.ref}/{citation.citation_id} embeds a quote "
                    "without a trusted approved short_quote license"
                )


def _validate_manifestations(
    *,
    sources: tuple[SourceRecord, ...],
    current_source_keys: set[tuple[str, int]],
    mode: LoadMode,
    heads: dict[tuple[Any, ...], ReviewEvent],
    trusted_event_ids: set[str],
) -> None:
    source_map = {item.ref.key(): item for item in sources}
    edges = {
        item.ref.key(): item.manifestation_of.key()
        for item in sources
        if item.manifestation_of is not None
    }
    visiting: set[tuple[str, int]] = set()
    visited: set[tuple[str, int]] = set()

    def visit(node: tuple[str, int]) -> None:
        if node in visiting:
            raise KnowledgeReferenceError("source manifestation graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        successor = edges.get(node)
        if successor is not None:
            visit(successor)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)

    equivalence_heads = {
        (event.subject.manifestation.key(), event.subject.canonical.key()): event
        for event in heads.values()
        if isinstance(event, EquivalenceDecisionEvent)
    }
    for source in sources:
        if source.manifestation_of is None:
            continue
        canonical = source_map.get(source.manifestation_of.key())
        if canonical is None:
            raise KnowledgeReferenceError(
                f"source {source.ref} manifestation_of target is unknown"
            )
        source_ids = {(item.scheme, item.value) for item in source.document_identifiers}
        canonical_ids = {(item.scheme, item.value) for item in canonical.document_identifiers}
        if not source_ids.intersection(canonical_ids):
            raise KnowledgeReferenceError(
                f"source {source.ref} manifestation edge lacks a shared document identifier"
            )
        if mode == LoadMode.CURRENT and source.ref.key() in current_source_keys:
            event = equivalence_heads.get((source.ref.key(), canonical.ref.key()))
            if (
                event is None
                or event.event_id not in trusted_event_ids
                or event.decision != "approved"
            ):
                raise KnowledgeCurrentSelectionError(
                    f"source {source.ref} manifestation edge lacks trusted equivalence approval"
                )


def _head_for(
    heads: dict[tuple[Any, ...], ReviewEvent], event_type: str, subject: Any
) -> ReviewEvent | None:
    return heads.get((event_type, *_subject_identity(subject)))


def _trusted_decision(event: ReviewEvent | None, trusted: set[str]) -> str:
    if event is None or event.event_id not in trusted:
        return "not_assessed"
    return str(event.decision)


def _citation_axis(
    claim: SourcedClinicalClaim,
    heads: dict[tuple[Any, ...], ReviewEvent],
    trusted: set[str],
) -> str:
    decisions = [
        _trusted_decision(
            _head_for(
                heads,
                "citation_verification",
                CitationRef(claim=claim.ref, citation_id=citation.citation_id),
            ),
            trusted,
        )
        for citation in claim.citations
    ]
    for state in ("rejected", "changes_requested"):
        if state in decisions:
            return state
    if decisions and all(item == "approved" for item in decisions):
        return "approved"
    if "in_review" in decisions:
        return "in_review"
    return "not_assessed"


def _claim_review_summary(
    claim: KnowledgeClaim,
    heads: dict[tuple[Any, ...], ReviewEvent],
    trusted: set[str],
) -> ReviewSummary:
    if isinstance(claim, WorkflowDesignDecision):
        return ReviewSummary(aggregate=ReviewAggregate.DESIGN_DOCUMENTED, axes={})
    assert isinstance(claim, SourcedClinicalClaim)
    axes = {
        "clinical": _trusted_decision(_head_for(heads, "clinical", claim.ref), trusted),
        "terminology": _trusted_decision(
            _head_for(heads, "terminology", claim.ref), trusted
        ),
        "internal_consistency": _trusted_decision(
            _head_for(heads, "internal_consistency", claim.ref), trusted
        ),
        "citation_verification": _citation_axis(claim, heads, trusted),
    }
    pharmacy = _trusted_decision(_head_for(heads, "pharmacy", claim.ref), trusted)
    non_pharmacy = set(axes.values())
    if "rejected" in non_pharmacy:
        aggregate = ReviewAggregate.REJECTED
    elif "changes_requested" in non_pharmacy:
        aggregate = ReviewAggregate.CHANGES_REQUESTED
    else:
        required = {"clinical", "citation_verification", "internal_consistency"}
        if claim.claim_type == ClaimType.TERMINOLOGY_SUPPORT:
            required.add("terminology")
        if all(axes[item] == "approved" for item in required):
            aggregate = ReviewAggregate.APPROVED
        elif axes["clinical"] == "approved":
            aggregate = ReviewAggregate.CLINICIAN_REVIEWED
        elif (
            axes["internal_consistency"] == "approved"
            or axes["citation_verification"] == "approved"
        ):
            aggregate = ReviewAggregate.INTERNALLY_CHECKED
        elif "in_review" in non_pharmacy:
            aggregate = ReviewAggregate.IN_REVIEW
        else:
            aggregate = ReviewAggregate.NOT_ASSESSED
    return ReviewSummary(aggregate=aggregate, axes=axes, pharmacy=pharmacy)


@dataclass(frozen=True)
class KnowledgeReleaseRegistry:
    release: KnowledgeRelease

    def source(self, source_id: str) -> ReleaseSourceRecord:
        return self._find(self.release.sources, "source_id", source_id)

    def claim(self, claim_id: str) -> EvidenceClaim:
        return self._find(self.release.evidence_claims, "claim_id", claim_id)

    def metric(self, metric_id: str) -> MetricDefinition:
        return self._find(self.release.metrics, "metric_id", metric_id)

    @staticmethod
    def _find(items: list, attribute: str, value: str):
        item = next((item for item in items if getattr(item, attribute) == value), None)
        if item is None:
            raise LookupError(f"unknown {attribute} {value!r}")
        return item


def _read_json(data_dir, name: str, model):
    text = data_dir.joinpath(name).read_text("utf-8")
    if model is ReleaseManifest:
        return model.model_validate_json(text)
    return [model.model_validate(item) for item in __import__("json").loads(text)]


def load_cn_glp1_release() -> KnowledgeReleaseRegistry:
    data_dir = files("continucare.knowledge.data.cn_glp1.v1")
    release = KnowledgeRelease(
        manifest=_read_json(data_dir, "release_manifest.json", ReleaseManifest),
        sources=_read_json(data_dir, "source_registry.json", ReleaseSourceRecord),
        products=_read_json(data_dir, "product_registry.json", ProductRecord),
        evidence_claims=_read_json(data_dir, "evidence_claims.json", EvidenceClaim),
        metrics=_read_json(data_dir, "metric_definitions.json", MetricDefinition),
        terminology=_read_json(data_dir, "terminology_manifest.json", TerminologyEntry),
        patient_content=_read_json(data_dir, "patient_content.zh-CN.json", PatientContent),
        data_quality_rules=_read_json(data_dir, "data_quality_rules.json", DataQualityRule),
        clinical_rules=__import__("json").loads(
            data_dir.joinpath("clinical_rules.json").read_text("utf-8")
        ),
        coverage=CoverageReport.model_validate_json(
            data_dir.joinpath("coverage_report.json").read_text("utf-8")
        ),
    )
    return KnowledgeReleaseRegistry(release=release)

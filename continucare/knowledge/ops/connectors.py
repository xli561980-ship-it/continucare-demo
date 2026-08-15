"""Offline-first connectors for staged knowledge acquisition."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from continucare.knowledge.ops.models import (
    Jurisdiction,
    KnowledgeOpsIntegrityError,
    KnowledgeOpsPolicyError,
    LanguageCode,
    NonBlank,
    PolicyDecision,
    SafeId,
    Sha256,
    SourceOperation,
    SourcePolicy,
    StrictModel,
    safe_relative_parts,
)
from continucare.knowledge.ops.security import (
    DigestTrustProfile,
    assert_no_sensitive_data,
    validate_transport_route,
    validate_url_against_policy,
)


class NetworkAccessDisabled(KnowledgeOpsPolicyError):
    pass


class DiscoveryRequest(Protocol):
    validation_profile_id: str
    policy_ids: tuple[str, ...]


class FixtureResource(StrictModel):
    stable_id: SafeId
    validation_profile_id: SafeId
    policy_id: SafeId
    canonical_url: AnyHttpUrl
    title: NonBlank
    issuing_authority: NonBlank
    source_type: NonBlank
    jurisdictions: tuple[Jurisdiction, ...] = Field(min_length=1)
    languages: tuple[LanguageCode, ...] = Field(min_length=1)
    document_version: NonBlank
    content_path: NonBlank
    content_sha256: Sha256
    content_type: NonBlank
    metadata: dict[str, object] = Field(default_factory=dict)
    synthetic: Literal[True] = True

    @field_validator("content_path")
    @classmethod
    def validate_content_path(cls, value: str) -> str:
        safe_relative_parts(value)
        return value


class FixtureCatalog(StrictModel):
    fixture_set_id: SafeId
    fixture_set_version: int = Field(ge=1)
    synthetic: Literal[True] = True
    resources: tuple[FixtureResource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_resources(self) -> "FixtureCatalog":
        stable_ids = [item.stable_id for item in self.resources]
        if len(stable_ids) != len(set(stable_ids)):
            raise ValueError("offline fixture stable_id values must be unique")
        return self


class DiscoveredResource(StrictModel):
    connector_id: SafeId
    stable_id: SafeId
    validation_profile_id: SafeId
    policy_id: SafeId
    canonical_url: AnyHttpUrl
    title: NonBlank
    issuing_authority: NonBlank
    source_type: NonBlank
    jurisdictions: tuple[Jurisdiction, ...] = Field(min_length=1)
    languages: tuple[LanguageCode, ...] = Field(min_length=1)
    document_version: NonBlank
    metadata: dict[str, object] = Field(default_factory=dict)
    synthetic: bool


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    connector_id: str
    stable_id: str
    canonical_url: str
    content_type: str
    body: bytes
    content_sha256: str
    synthetic: bool
    redirect_urls: tuple[str, ...] = ()
    peer_ips: tuple[str, ...] = ()


class AcquisitionConnector(Protocol):
    connector_id: str

    def discover(
        self, request: DiscoveryRequest, policy: SourcePolicy
    ) -> tuple[DiscoveredResource, ...]: ...

    def fetch(
        self, resource: DiscoveredResource, policy: SourcePolicy
    ) -> FetchedDocument: ...


class OfflineFixtureConnector:
    connector_id = "offline-fixture"

    def __init__(
        self,
        root: Path,
        *,
        catalog_sha256: str,
        catalog_path: str = "catalog.json",
    ) -> None:
        self._root = Path(root).resolve(strict=True)
        if Path(root).is_symlink():
            raise KnowledgeOpsIntegrityError("offline fixture root cannot be a symlink")
        self._catalog_path = catalog_path
        catalog_bytes = self._read_fixture_file(catalog_path)
        actual = hashlib.sha256(catalog_bytes).hexdigest()
        if actual != catalog_sha256:
            raise KnowledgeOpsIntegrityError("offline fixture catalog SHA-256 mismatch")
        try:
            self._catalog = FixtureCatalog.model_validate_json(catalog_bytes)
        except Exception as exc:
            raise KnowledgeOpsIntegrityError(
                f"invalid offline fixture catalog: {exc}"
            ) from exc
        self._by_id = {item.stable_id: item for item in self._catalog.resources}
        for item in self._catalog.resources:
            assert_no_sensitive_data(
                item.model_dump(mode="json", exclude={"content_sha256"})
            )
            payload = self._read_fixture_file(item.content_path)
            if hashlib.sha256(payload).hexdigest() != item.content_sha256:
                raise KnowledgeOpsIntegrityError(
                    f"offline fixture {item.stable_id} content SHA-256 mismatch"
                )
            assert_no_sensitive_data(
                item.model_dump(mode="json"),
                digest_trust_profile=DigestTrustProfile.OFFLINE_FIXTURE_RESOURCE,
            )

    @property
    def fixture_set_id(self) -> str:
        return self._catalog.fixture_set_id

    def discover(
        self, request: DiscoveryRequest, policy: SourcePolicy
    ) -> tuple[DiscoveredResource, ...]:
        if policy.policy_id not in request.policy_ids:
            raise KnowledgeOpsPolicyError("request does not authorize this SourcePolicy")
        if policy.decision_for(SourceOperation.DISCOVER_METADATA) not in {
            PolicyDecision.ALLOW.value,
            PolicyDecision.OFFLINE_FIXTURE_ONLY.value,
        }:
            raise KnowledgeOpsPolicyError("SourcePolicy denies fixture metadata discovery")
        discovered: list[DiscoveredResource] = []
        for item in self._catalog.resources:
            if (
                item.validation_profile_id != request.validation_profile_id
                or item.policy_id != policy.policy_id
            ):
                continue
            if item.source_type not in policy.source_types:
                raise KnowledgeOpsPolicyError(
                    f"fixture source_type is outside SourcePolicy {policy.policy_id}"
                )
            canonical_url = validate_url_against_policy(str(item.canonical_url), policy)
            discovered.append(
                DiscoveredResource(
                    connector_id=self.connector_id,
                    stable_id=item.stable_id,
                    validation_profile_id=item.validation_profile_id,
                    policy_id=item.policy_id,
                    canonical_url=canonical_url,
                    title=item.title,
                    issuing_authority=item.issuing_authority,
                    source_type=item.source_type,
                    jurisdictions=item.jurisdictions,
                    languages=item.languages,
                    document_version=item.document_version,
                    metadata=item.metadata,
                    synthetic=True,
                )
            )
        return tuple(discovered)

    def fetch(
        self, resource: DiscoveredResource, policy: SourcePolicy
    ) -> FetchedDocument:
        item = self._by_id.get(resource.stable_id)
        if item is None:
            raise KnowledgeOpsIntegrityError("offline fixture resource is unknown")
        if item.policy_id != policy.policy_id or item.policy_id != resource.policy_id:
            raise KnowledgeOpsPolicyError("offline fixture SourcePolicy mismatch")
        decision = policy.decision_for(SourceOperation.FETCH_FOR_CHANGE_DETECTION)
        if decision != PolicyDecision.OFFLINE_FIXTURE_ONLY.value:
            raise KnowledgeOpsPolicyError(
                "offline fixture fetch requires offline_fixture_only policy"
            )
        expected_url = validate_url_against_policy(str(item.canonical_url), policy)
        actual_url = validate_url_against_policy(str(resource.canonical_url), policy)
        if actual_url != expected_url:
            raise KnowledgeOpsIntegrityError("offline fixture URL identity mismatch")
        body = self._read_fixture_file(item.content_path)
        digest = hashlib.sha256(body).hexdigest()
        if digest != item.content_sha256:
            raise KnowledgeOpsIntegrityError("offline fixture content SHA-256 mismatch")
        if len(body) > policy.maximum_response_bytes:
            raise KnowledgeOpsPolicyError("offline fixture exceeds SourcePolicy byte limit")
        if item.content_type not in policy.allowed_content_types:
            raise KnowledgeOpsPolicyError("offline fixture content type is not allowlisted")
        return FetchedDocument(
            connector_id=self.connector_id,
            stable_id=item.stable_id,
            canonical_url=expected_url,
            content_type=item.content_type,
            body=body,
            content_sha256=digest,
            synthetic=True,
        )

    def _read_fixture_file(self, relative_path: str) -> bytes:
        parts = safe_relative_parts(relative_path)
        current = self._root
        for part in parts:
            candidate = current / part
            if candidate.is_symlink():
                raise KnowledgeOpsIntegrityError("offline fixture path cannot use symlinks")
            current = candidate
        try:
            resolved = current.resolve(strict=True)
        except OSError as exc:
            raise KnowledgeOpsIntegrityError(
                f"offline fixture file is unavailable: {relative_path}"
            ) from exc
        if self._root not in resolved.parents or not resolved.is_file():
            raise KnowledgeOpsIntegrityError("offline fixture path escaped its root")
        return resolved.read_bytes()


@dataclass(frozen=True, slots=True)
class TransportResponse:
    final_url: str
    redirect_urls: tuple[str, ...]
    peer_ips: tuple[str, ...]
    content_type: str
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, maximum_bytes: int) -> TransportResponse: ...


class GuardedHttpConnector:
    """Future transport boundary; inert under the entire v2.0 contract."""

    connector_id = "guarded-http"

    def __init__(
        self,
        *,
        network_enabled: bool = False,
        transport: HttpTransport | None = None,
    ) -> None:
        self._network_enabled = network_enabled
        self._transport = transport

    def discover(
        self, request: DiscoveryRequest, policy: SourcePolicy
    ) -> tuple[DiscoveredResource, ...]:
        raise NetworkAccessDisabled(
            "live discovery is not implemented or enabled in Knowledge Ops v2.0"
        )

    def fetch(
        self, resource: DiscoveredResource, policy: SourcePolicy
    ) -> FetchedDocument:
        if not self._network_enabled or not policy.live_network_enabled:
            raise NetworkAccessDisabled("live network connector is disabled by default")
        if self._transport is None:
            raise NetworkAccessDisabled("no reviewed HTTP transport was supplied")
        requested = validate_url_against_policy(str(resource.canonical_url), policy)
        response = self._transport.get(
            requested, maximum_bytes=policy.maximum_response_bytes
        )
        route = validate_transport_route(
            requested_url=requested,
            redirect_urls=response.redirect_urls,
            peer_ips=response.peer_ips,
            policy=policy,
        )
        final_url = validate_url_against_policy(response.final_url, policy)
        if final_url != route[-1]:
            raise KnowledgeOpsIntegrityError("transport final URL attestation mismatch")
        if len(response.body) > policy.maximum_response_bytes:
            raise KnowledgeOpsPolicyError("transport response exceeds SourcePolicy byte limit")
        if response.content_type not in policy.allowed_content_types:
            raise KnowledgeOpsPolicyError("transport content type is not allowlisted")
        digest = hashlib.sha256(response.body).hexdigest()
        return FetchedDocument(
            connector_id=self.connector_id,
            stable_id=resource.stable_id,
            canonical_url=final_url,
            content_type=response.content_type,
            body=response.body,
            content_sha256=digest,
            synthetic=False,
            redirect_urls=response.redirect_urls,
            peer_ips=response.peer_ips,
        )

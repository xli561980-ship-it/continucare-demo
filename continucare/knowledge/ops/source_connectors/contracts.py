"""Typed request and response contracts shared by official-source connectors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, field_validator, model_validator

from continucare.knowledge.ops.models import NonBlank, SafeId, StrictModel
from continucare.knowledge.ops.source_connectors.errors import (
    ConnectorErrorCode,
    ConnectorFailure,
)


class EndpointPolicy(StrictModel):
    endpoint_id: SafeId
    source_id: SafeId
    source_policy_id: SafeId
    source_policy_version: int = Field(ge=1)
    official_documentation_url: NonBlank
    hostname: NonBlank
    path_pattern: NonBlank
    path_template: NonBlank
    allowed_query_keys: tuple[SafeId, ...] = ()
    query_value_patterns: dict[SafeId, NonBlank] = Field(default_factory=dict)
    allowed_media_types: tuple[
        Literal["application/json", "application/xml", "text/xml"], ...
    ] = Field(min_length=1)
    maximum_response_bytes: int = Field(gt=0, le=50_000_000)
    minimum_interval_seconds: float = Field(default=0, ge=0, le=60)
    rate_limit_key: SafeId | None = None
    rights_status: Literal["metadata_only", "rights_unresolved"]

    @field_validator("hostname")
    @classmethod
    def canonical_hostname(cls, value: str) -> str:
        if (
            value != value.lower()
            or value.endswith(".")
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value)
        ):
            raise ValueError("endpoint hostname must be canonical lower-case DNS text")
        return value

    @field_validator("path_pattern")
    @classmethod
    def anchored_path_pattern(cls, value: str) -> str:
        if not value.startswith("^") or not value.endswith("$"):
            raise ValueError("endpoint path_pattern must be fully anchored")
        try:
            re.compile(value, flags=re.ASCII)
        except re.error as exc:
            raise ValueError("invalid endpoint path_pattern") from exc
        return value

    @model_validator(mode="after")
    def unique_constraints(self) -> "EndpointPolicy":
        if len(self.allowed_query_keys) != len(set(self.allowed_query_keys)):
            raise ValueError("endpoint query keys must be unique")
        if set(self.query_value_patterns) != set(self.allowed_query_keys):
            raise ValueError("every endpoint query key requires one value pattern")
        for pattern in self.query_value_patterns.values():
            if not pattern.startswith("^") or not pattern.endswith("$"):
                raise ValueError("query value patterns must be fully anchored")
            try:
                re.compile(pattern, flags=re.ASCII)
            except re.error as exc:
                raise ValueError("invalid query value pattern") from exc
        if len(self.allowed_media_types) != len(set(self.allowed_media_types)):
            raise ValueError("endpoint media types must be unique")
        if not self.path_template.startswith("/"):
            raise ValueError("endpoint path template must be absolute")
        return self


class ControlledRequest(StrictModel):
    endpoint_id: SafeId
    method: Literal["GET"] = "GET"
    url: NonBlank
    query_identity: SafeId
    accept: Literal["application/json", "application/xml", "text/xml"]


@dataclass(frozen=True, slots=True)
class MetadataResponse:
    endpoint_id: str
    status: int
    media_type: str
    charset: str | None
    headers: Mapping[str, str]
    body: bytes
    peer_ip: str | None = None


class MetadataTransport(Protocol):
    def execute(
        self,
        request: ControlledRequest,
        endpoint: EndpointPolicy,
    ) -> MetadataResponse: ...


class FakeMetadataTransport:
    """Offline transport double that still enforces the endpoint contract."""

    def __init__(self, responses: Mapping[str, MetadataResponse]) -> None:
        self._responses = dict(responses)
        self.captured_requests: list[ControlledRequest] = []

    def execute(
        self,
        request: ControlledRequest,
        endpoint: EndpointPolicy,
    ) -> MetadataResponse:
        validate_controlled_request(request, endpoint)
        self.captured_requests.append(request)
        try:
            response = self._responses[endpoint.endpoint_id]
        except KeyError as exc:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="offline response fixture is missing",
            ) from exc
        if response.endpoint_id != endpoint.endpoint_id:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="offline response endpoint identity does not match",
            )
        return response


def validate_controlled_request(
    request: ControlledRequest,
    endpoint: EndpointPolicy,
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """Fail before DNS/capture unless URL is an exact typed endpoint product."""

    if request.endpoint_id != endpoint.endpoint_id:
        raise ConnectorFailure(
            ConnectorErrorCode.ENDPOINT_NOT_ALLOWED,
            detail="request endpoint identity is not allowlisted",
        )
    if any(ord(character) < 33 or ord(character) > 126 for character in request.url):
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="request URL must be canonical printable ASCII",
        )
    try:
        parsed = urlsplit(request.url)
        port = parsed.port
    except ValueError as exc:
        raise ConnectorFailure(
            ConnectorErrorCode.ENDPOINT_NOT_ALLOWED,
            detail="request URL is malformed",
        ) from exc
    if (
        parsed.scheme != "https"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.hostname or "") != endpoint.hostname
    ):
        raise ConnectorFailure(
            ConnectorErrorCode.ENDPOINT_NOT_ALLOWED,
            detail="scheme, port, userinfo, fragment, or origin is not allowed",
        )
    if not re.fullmatch(endpoint.path_pattern, parsed.path, flags=re.ASCII):
        raise ConnectorFailure(
            ConnectorErrorCode.PATH_NOT_ALLOWED,
            detail="request path is outside the exact endpoint rule",
        )
    if "%" in parsed.path or "%" in parsed.query:
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="typed endpoint requests never use percent encoding",
        )
    try:
        pairs = tuple(parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True))
    except ValueError as exc:
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="query string is malformed",
        ) from exc
    keys = tuple(key for key, _ in pairs)
    if len(keys) != len(set(keys)) or set(keys) != set(endpoint.allowed_query_keys):
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="query keys do not exactly match the endpoint contract",
        )
    for key, value in pairs:
        if not re.fullmatch(endpoint.query_value_patterns[key], value, flags=re.ASCII):
            raise ConnectorFailure(
                ConnectorErrorCode.UNSAFE_QUERY,
                detail=f"query value for {key!r} is outside the typed contract",
            )
    if request.accept not in endpoint.allowed_media_types:
        raise ConnectorFailure(
            ConnectorErrorCode.UNSUPPORTED_MIME,
            detail="request Accept is outside the endpoint contract",
        )
    return endpoint.hostname, parsed.path, pairs

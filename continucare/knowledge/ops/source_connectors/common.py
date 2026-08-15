"""Shared metadata-only connector helpers."""

from __future__ import annotations

import hashlib
from typing import TypeVar

from continucare.knowledge.ops.source_connectors.contracts import (
    EndpointPolicy,
    MetadataResponse,
)
from continucare.knowledge.ops.source_connectors.errors import (
    ConnectorErrorCode,
    ConnectorFailure,
)


T = TypeVar("T")


def assert_response_contract(
    response: MetadataResponse,
    endpoint: EndpointPolicy,
) -> None:
    if response.endpoint_id != endpoint.endpoint_id or response.status != 200:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="metadata parser received an unexpected endpoint or status",
            http_status=response.status,
        )
    if response.media_type not in endpoint.allowed_media_types:
        raise ConnectorFailure(
            ConnectorErrorCode.UNSUPPORTED_MIME,
            detail="metadata parser received a media type outside its endpoint policy",
        )
    if response.charset not in {None, "utf-8"}:
        raise ConnectorFailure(
            ConnectorErrorCode.UNSUPPORTED_CHARSET,
            detail="metadata parser only accepts strict UTF-8",
        )
    if len(response.body) > endpoint.maximum_response_bytes:
        raise ConnectorFailure(
            ConnectorErrorCode.RESPONSE_TOO_LARGE,
            detail="metadata body exceeds its endpoint cap",
        )


def response_digest(response: MetadataResponse) -> str:
    return hashlib.sha256(response.body).hexdigest()


def response_validator(headers: object, key: str) -> str | None:
    if not isinstance(headers, dict):
        try:
            value = headers.get(key)  # type: ignore[union-attr]
        except AttributeError:
            return None
    else:
        value = headers.get(key)
    return value if isinstance(value, str) and value else None


def required_text(
    value: object,
    *,
    field: str,
    maximum_characters: int = 2048,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail=f"required metadata field {field!r} is absent or non-canonical",
        )
    if len(value) > maximum_characters:
        raise ConnectorFailure(
            ConnectorErrorCode.PARSER_LIMIT_EXCEEDED,
            detail=f"metadata field {field!r} exceeds its character cap",
        )
    return value


def optional_text(
    value: object,
    *,
    field: str,
    maximum_characters: int = 2048,
) -> str | None:
    if value is None or value == "":
        return None
    return required_text(value, field=field, maximum_characters=maximum_characters)

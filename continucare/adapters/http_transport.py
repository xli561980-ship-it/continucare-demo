"""Small fail-closed HTTP transport used by optional external adapters."""

from __future__ import annotations

import json
import re
import socket
import ssl
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

import certifi


OFFICIAL_FEISHU_HOST = "open.feishu.cn"
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
_PERMIT_MARKER = object()
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "app_secret",
        "tenant_access_token",
        "access_token",
        "refresh_token",
        "encrypt_key",
        "verification_token",
        "receive_id",
    }
)


class TransportError(RuntimeError):
    """Stable error without response bodies, URLs or credentials."""


class TransportPolicyError(TransportError):
    pass


class TransportTimeoutError(TransportError):
    pass


class TransportNetworkError(TransportError):
    pass


class TransportResponseTooLarge(TransportError):
    pass


class TransportInvalidJson(TransportError):
    pass


@dataclass(frozen=True, repr=False)
class EgressPermit:
    """Capability object issued only by the validated adapter factory."""

    _marker: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._marker is not _PERMIT_MARKER:
            raise TransportPolicyError("external egress permit was not issued")

    def __repr__(self) -> str:
        return "EgressPermit(validated=True)"


def issue_egress_permit(*, globally_enabled: bool, capability_enabled: bool) -> EgressPermit:
    if not globally_enabled or not capability_enabled:
        raise TransportPolicyError("external egress is disabled")
    return EgressPermit(_PERMIT_MARKER)


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout_seconds: float = 8.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json_object(self) -> dict[str, Any]:
        try:
            decoded = self.body.decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TransportInvalidJson("external response was not valid JSON") from None
        if not isinstance(value, dict):
            raise TransportInvalidJson("external response JSON must be an object")
        return value


class HttpTransport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportPolicyError("external redirects are not allowed")


class StdlibHttpTransport:
    """urllib transport with no proxy inheritance and a fixed official host."""

    def __init__(self, *, permit: EgressPermit):
        if not isinstance(permit, EgressPermit) or permit._marker is not _PERMIT_MARKER:
            raise TransportPolicyError("external egress permit is required")
        context = ssl.create_default_context(cafile=certifi.where())
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._opener = build_opener(
            ProxyHandler({}),
            _NoRedirect(),
            HTTPSHandler(context=context),
        )

    def send(self, request: HttpRequest) -> HttpResponse:
        _validate_official_url(request.url)
        if request.method.upper() not in {"GET", "POST"}:
            raise TransportPolicyError("external HTTP method is not allowed")
        if not 0 < request.timeout_seconds <= 30:
            raise TransportPolicyError("external timeout is outside the allowed range")
        if not 0 < request.max_response_bytes <= DEFAULT_MAX_RESPONSE_BYTES:
            raise TransportPolicyError("external response limit is outside the allowed range")
        raw = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method.upper(),
        )
        try:
            with self._opener.open(raw, timeout=request.timeout_seconds) as response:
                body = response.read(request.max_response_bytes + 1)
                if len(body) > request.max_response_bytes:
                    raise TransportResponseTooLarge("external response exceeded the size limit")
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=body,
                )
        except TransportError:
            raise
        except HTTPError as exc:
            body = exc.read(request.max_response_bytes + 1)
            if len(body) > request.max_response_bytes:
                raise TransportResponseTooLarge("external response exceeded the size limit") from None
            return HttpResponse(status=int(exc.code), headers={}, body=body)
        except (TimeoutError, socket.timeout):
            raise TransportTimeoutError("external request outcome is unknown after timeout") from None
        except (URLError, OSError):
            raise TransportNetworkError("external request failed with an unknown remote outcome") from None


class SecretRedactor:
    """Shared sink redactor; secret values are never represented."""

    def __init__(self, *secret_values: str):
        self._secret_values = tuple(
            value for value in secret_values if isinstance(value, str) and value
        )

    def __repr__(self) -> str:
        return f"SecretRedactor(secret_count={len(self._secret_values)})"

    def redact_text(self, value: str) -> str:
        redacted = value
        for secret in self._secret_values:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted

    def redact_request(self, request: HttpRequest) -> HttpRequest:
        headers = {
            key: "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else self.redact_text(value)
            for key, value in request.headers.items()
        }
        body = _redact_body(request.body, self)
        return HttpRequest(
            method=request.method,
            url=_redact_url(self.redact_text(request.url)),
            headers=headers,
            body=body,
            timeout_seconds=request.timeout_seconds,
            max_response_bytes=request.max_response_bytes,
        )


class FakeTransport:
    """Deterministic transport; captured requests are redacted by default."""

    def __init__(
        self,
        responses: list[HttpResponse | Exception] | None = None,
        *,
        redactor: SecretRedactor | None = None,
    ):
        self._responses = deque(responses or [])
        self._redactor = redactor or SecretRedactor()
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(self._redactor.redact_request(request))
        if not self._responses:
            raise AssertionError("FakeTransport has no queued response")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        if len(response.body) > request.max_response_bytes:
            raise TransportResponseTooLarge("external response exceeded the size limit")
        return response


def json_response(payload: dict[str, Any], *, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
    )


def _redact_body(body: bytes | None, redactor: SecretRedactor) -> bytes | None:
    if body is None:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return redactor.redact_text(body.decode("utf-8", errors="replace")).encode()

    def visit(item):
        if isinstance(item, dict):
            return {
                key: "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else visit(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, str):
            return redactor.redact_text(item)
        return item

    return json.dumps(visit(value), ensure_ascii=False, separators=(",", ":")).encode()


def _validate_official_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != OFFICIAL_FEISHU_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/open-apis/")
        or parsed.fragment
    ):
        raise TransportPolicyError("external URL is not an allowed official endpoint")


def _redact_url(url: str) -> str:
    """Redact write-target identifiers even when no secret list was supplied."""

    url = re.sub(
        r"(/open-apis/bitable/v1/apps/)[^/?]+(/tables/)[^/?]+",
        r"\1[REDACTED]\2[REDACTED]",
        url,
    )
    return re.sub(r"([?&]client_token=)[^&]+", r"\1[REDACTED]", url)

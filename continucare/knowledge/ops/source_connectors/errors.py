"""Stable failure taxonomy for source connector and live-contract validation."""

from __future__ import annotations

from enum import StrEnum


class ConnectorErrorCode(StrEnum):
    FEATURE_DISABLED = "feature_disabled"
    ENDPOINT_NOT_ALLOWED = "endpoint_not_allowed"
    PATH_NOT_ALLOWED = "path_not_allowed"
    UNSAFE_QUERY = "unsafe_query"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    NON_PUBLIC_DNS_ANSWER = "non_public_dns_answer"
    PEER_IDENTITY_MISMATCH = "peer_identity_mismatch"
    TLS_VERIFICATION_FAILED = "tls_verification_failed"
    REDIRECT_NOT_FOLLOWED = "redirect_not_followed"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    REMOTE_5XX = "remote_5xx"
    TIMEOUT = "timeout"
    NETWORK_FAILED = "network_failed"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    UNSUPPORTED_MIME = "unsupported_mime"
    UNSUPPORTED_CHARSET = "unsupported_charset"
    MALFORMED_JSON = "malformed_json"
    MALFORMED_XML = "malformed_xml"
    PARSER_LIMIT_EXCEEDED = "parser_limit_exceeded"
    CONTRACT_CHANGED = "contract_changed"
    RIGHTS_UNRESOLVED = "rights_unresolved"


class ErrorDisposition(StrEnum):
    RETRY = "retry"
    GAP = "gap"
    ABORT = "abort"
    NOT_ATTEMPTED = "not_attempted"


_DISPOSITIONS: dict[ConnectorErrorCode, ErrorDisposition] = {
    ConnectorErrorCode.FEATURE_DISABLED: ErrorDisposition.NOT_ATTEMPTED,
    ConnectorErrorCode.ENDPOINT_NOT_ALLOWED: ErrorDisposition.ABORT,
    ConnectorErrorCode.PATH_NOT_ALLOWED: ErrorDisposition.ABORT,
    ConnectorErrorCode.UNSAFE_QUERY: ErrorDisposition.ABORT,
    ConnectorErrorCode.DNS_RESOLUTION_FAILED: ErrorDisposition.RETRY,
    ConnectorErrorCode.NON_PUBLIC_DNS_ANSWER: ErrorDisposition.ABORT,
    ConnectorErrorCode.PEER_IDENTITY_MISMATCH: ErrorDisposition.ABORT,
    ConnectorErrorCode.TLS_VERIFICATION_FAILED: ErrorDisposition.ABORT,
    ConnectorErrorCode.REDIRECT_NOT_FOLLOWED: ErrorDisposition.ABORT,
    ConnectorErrorCode.UNAUTHORIZED: ErrorDisposition.GAP,
    ConnectorErrorCode.FORBIDDEN: ErrorDisposition.GAP,
    ConnectorErrorCode.NOT_FOUND: ErrorDisposition.GAP,
    ConnectorErrorCode.RATE_LIMITED: ErrorDisposition.RETRY,
    ConnectorErrorCode.REMOTE_5XX: ErrorDisposition.RETRY,
    ConnectorErrorCode.TIMEOUT: ErrorDisposition.RETRY,
    ConnectorErrorCode.NETWORK_FAILED: ErrorDisposition.RETRY,
    ConnectorErrorCode.RESPONSE_TOO_LARGE: ErrorDisposition.GAP,
    ConnectorErrorCode.UNSUPPORTED_ENCODING: ErrorDisposition.ABORT,
    ConnectorErrorCode.UNSUPPORTED_MIME: ErrorDisposition.GAP,
    ConnectorErrorCode.UNSUPPORTED_CHARSET: ErrorDisposition.ABORT,
    ConnectorErrorCode.MALFORMED_JSON: ErrorDisposition.GAP,
    ConnectorErrorCode.MALFORMED_XML: ErrorDisposition.GAP,
    ConnectorErrorCode.PARSER_LIMIT_EXCEEDED: ErrorDisposition.GAP,
    ConnectorErrorCode.CONTRACT_CHANGED: ErrorDisposition.GAP,
    ConnectorErrorCode.RIGHTS_UNRESOLVED: ErrorDisposition.NOT_ATTEMPTED,
}


def disposition_for(code: ConnectorErrorCode | str) -> ErrorDisposition:
    return _DISPOSITIONS[ConnectorErrorCode(code)]


class ConnectorFailure(RuntimeError):
    """A source failure carrying only stable, non-body diagnostic metadata."""

    def __init__(
        self,
        code: ConnectorErrorCode | str,
        *,
        detail: str,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.code = ConnectorErrorCode(code)
        self.detail = detail
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{self.code.value}: {detail}")

    @property
    def disposition(self) -> ErrorDisposition:
        return disposition_for(self.code)

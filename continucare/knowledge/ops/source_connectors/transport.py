"""DNS-bound TLS transport used only by the explicit live validator.

The transport never hands a hostname back to a general HTTP client.  It
resolves and validates every DNS answer itself, connects a validated address,
performs certificate verification with the original hostname, checks the
actual socket peer, and only then emits a minimal HTTP/1.1 request.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from email.message import Message
from threading import Lock
from typing import Any

from continucare.knowledge.ops.source_connectors.contracts import (
    ControlledRequest,
    EndpointPolicy,
    MetadataResponse,
    validate_controlled_request,
)
from continucare.knowledge.ops.source_connectors.errors import (
    ConnectorErrorCode,
    ConnectorFailure,
)
from continucare.knowledge.ops.source_connectors.flags import (
    KnowledgeEgressPermit,
    assert_valid_egress_permit,
)


Resolver = Callable[[str, int], Sequence[tuple[Any, ...]]]
TcpConnector = Callable[[str, int, float], Any]
ContextFactory = Callable[[], ssl.SSLContext]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]
Jitter = Callable[[], float]

_TRANSIENT_STATUS = frozenset({500, 502, 503, 504})
_MAX_RETRIES = 2
_MAX_RETRY_AFTER_SECONDS = 30.0
_READ_CHUNK_BYTES = 64 * 1024


class ProcessSharedRateLimiter:
    """Thread-safe request budget shared by every injected transport instance."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._last_request_at: dict[str, float] = {}

    def acquire(
        self,
        key: str,
        minimum_interval_seconds: float,
        *,
        clock: Clock,
        sleeper: Sleeper,
    ) -> None:
        if minimum_interval_seconds <= 0:
            return
        with self._lock:
            now = float(clock())
            previous = self._last_request_at.get(key)
            if previous is not None:
                earliest = previous + minimum_interval_seconds
                remaining = earliest - now
                if remaining > 0:
                    sleeper(remaining)
                    now = max(float(clock()), earliest)
            self._last_request_at[key] = now

    def reset_for_testing(self) -> None:
        with self._lock:
            self._last_request_at.clear()


_PROCESS_SHARED_RATE_LIMITER = ProcessSharedRateLimiter()


class SecureMetadataTransport:
    """Small, no-proxy, no-redirect, identity-bound metadata GET transport."""

    identity_binding_proven = True

    def __init__(
        self,
        *,
        permit: KnowledgeEgressPermit,
        resolver: Resolver | None = None,
        tcp_connector: TcpConnector | None = None,
        context_factory: ContextFactory | None = None,
        sleeper: Sleeper = time.sleep,
        clock: Clock = time.monotonic,
        jitter: Jitter = lambda: 0.0,
        timeout_seconds: float = 10.0,
        maximum_retries: int = _MAX_RETRIES,
        rate_limiter: ProcessSharedRateLimiter | None = None,
    ) -> None:
        assert_valid_egress_permit(permit)
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("transport timeout must be within (0, 60] seconds")
        if (
            isinstance(maximum_retries, bool)
            or not isinstance(maximum_retries, int)
            or not 0 <= maximum_retries <= _MAX_RETRIES
        ):
            raise ValueError("transport retries must be an integer within [0, 2]")
        self._resolver = resolver or _resolve_addresses
        self._tcp_connector = tcp_connector or _connect_tcp
        self._context_factory = context_factory or ssl.create_default_context
        self._sleeper = sleeper
        self._clock = clock
        self._jitter = jitter
        self._timeout_seconds = timeout_seconds
        self._maximum_retries = maximum_retries
        self._rate_limiter = rate_limiter or _PROCESS_SHARED_RATE_LIMITER

    def execute(
        self,
        request: ControlledRequest,
        endpoint: EndpointPolicy,
    ) -> MetadataResponse:
        hostname, path, query_pairs = validate_controlled_request(request, endpoint)
        target = path
        if query_pairs:
            target = request.url.split("?", maxsplit=1)[1]
            target = f"{path}?{target}"

        retry_index = 0
        while True:
            self._respect_rate_limit(endpoint)
            try:
                response = self._execute_once(
                    request=request,
                    endpoint=endpoint,
                    hostname=hostname,
                    target=target,
                )
            except ConnectorFailure as exc:
                if retry_index >= self._maximum_retries or not _is_retryable(exc):
                    raise
                self._sleep_before_retry(exc, retry_index)
                retry_index += 1
                continue
            return response

    def _respect_rate_limit(self, endpoint: EndpointPolicy) -> None:
        self._rate_limiter.acquire(
            endpoint.rate_limit_key or endpoint.source_id,
            endpoint.minimum_interval_seconds,
            clock=self._clock,
            sleeper=self._sleeper,
        )

    def _sleep_before_retry(
        self,
        failure: ConnectorFailure,
        retry_index: int,
    ) -> None:
        bounded_jitter = max(0.0, min(float(self._jitter()), 0.25))
        delay = min(0.25 * (2**retry_index) + bounded_jitter, 2.25)
        if failure.retry_after_seconds is not None:
            delay = max(delay, failure.retry_after_seconds)
        self._sleeper(delay)

    def _execute_once(
        self,
        *,
        request: ControlledRequest,
        endpoint: EndpointPolicy,
        hostname: str,
        target: str,
    ) -> MetadataResponse:
        validated_addresses = self._resolve_public_addresses(hostname)
        raw_socket: Any | None = None
        tls_socket: Any | None = None
        response: http.client.HTTPResponse | None = None
        try:
            raw_socket = self._open_validated_socket(validated_addresses)
            context = self._verified_context()
            try:
                tls_socket = context.wrap_socket(raw_socket, server_hostname=hostname)
            except (ssl.SSLError, ssl.CertificateError, OSError) as exc:
                raise ConnectorFailure(
                    ConnectorErrorCode.TLS_VERIFICATION_FAILED,
                    detail="TLS certificate or hostname verification failed",
                ) from exc
            peer_ip = _peer_ip(tls_socket)
            if peer_ip not in validated_addresses:
                raise ConnectorFailure(
                    ConnectorErrorCode.PEER_IDENTITY_MISMATCH,
                    detail="TLS socket peer is outside the validated DNS answer set",
                )
            request_bytes = (
                f"GET {target} HTTP/1.1\r\n"
                f"Host: {hostname}\r\n"
                f"Accept: {request.accept}\r\n"
                "Accept-Encoding: identity\r\n"
                "Connection: close\r\n"
                "User-Agent: ContinuCare-Knowledge-Contract-Validator/1\r\n\r\n"
            ).encode("ascii")
            tls_socket.sendall(request_bytes)
            response = http.client.HTTPResponse(tls_socket, method="GET")
            try:
                response.begin()
            except (http.client.HTTPException, OSError) as exc:
                raise ConnectorFailure(
                    ConnectorErrorCode.CONTRACT_CHANGED,
                    detail="remote HTTP response headers are malformed",
                ) from exc
            headers = _normalized_headers(response.headers)
            _raise_for_status(response.status, headers)
            media_type, charset = _validate_content_contract(headers, endpoint)
            body = _read_bounded_body(
                response,
                maximum_bytes=endpoint.maximum_response_bytes,
                content_length=headers.get("content-length"),
            )
            return MetadataResponse(
                endpoint_id=endpoint.endpoint_id,
                status=response.status,
                media_type=media_type,
                charset=charset,
                headers=headers,
                body=body,
                peer_ip=peer_ip,
            )
        except (TimeoutError, socket.timeout) as exc:
            raise ConnectorFailure(
                ConnectorErrorCode.TIMEOUT,
                detail="network operation timed out",
            ) from exc
        finally:
            if response is not None:
                response.close()
            if tls_socket is not None:
                tls_socket.close()
            elif raw_socket is not None:
                raw_socket.close()

    def _resolve_public_addresses(self, hostname: str) -> tuple[str, ...]:
        try:
            answers = self._resolver(hostname, 443)
        except (OSError, socket.gaierror) as exc:
            raise ConnectorFailure(
                ConnectorErrorCode.DNS_RESOLUTION_FAILED,
                detail="official endpoint DNS resolution failed",
            ) from exc
        addresses: list[str] = []
        for answer in answers:
            try:
                sockaddr = answer[4]
                candidate = str(sockaddr[0])
                address = ipaddress.ip_address(candidate)
            except (IndexError, TypeError, ValueError) as exc:
                raise ConnectorFailure(
                    ConnectorErrorCode.DNS_RESOLUTION_FAILED,
                    detail="DNS resolver returned an invalid address record",
                ) from exc
            if not _is_public_address(address):
                raise ConnectorFailure(
                    ConnectorErrorCode.NON_PUBLIC_DNS_ANSWER,
                    detail="at least one DNS answer is not a globally routable IP",
                )
            compressed = address.compressed
            if compressed not in addresses:
                addresses.append(compressed)
        if not addresses:
            raise ConnectorFailure(
                ConnectorErrorCode.DNS_RESOLUTION_FAILED,
                detail="official endpoint DNS returned no addresses",
            )
        return tuple(addresses)

    def _open_validated_socket(self, addresses: tuple[str, ...]) -> Any:
        last_error: OSError | None = None
        for address in addresses:
            try:
                return self._tcp_connector(address, 443, self._timeout_seconds)
            except (TimeoutError, socket.timeout):
                raise
            except OSError as exc:
                last_error = exc
        raise ConnectorFailure(
            ConnectorErrorCode.NETWORK_FAILED,
            detail="TCP connection to validated official endpoint addresses failed",
        ) from last_error

    def _verified_context(self) -> ssl.SSLContext:
        try:
            context = self._context_factory()
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception as exc:
            raise ConnectorFailure(
                ConnectorErrorCode.TLS_VERIFICATION_FAILED,
                detail="a verified default TLS context could not be configured",
            ) from exc
        return context


def _resolve_addresses(hostname: str, port: int) -> Sequence[tuple[Any, ...]]:
    return socket.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )


def _connect_tcp(address: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((address, port), timeout=timeout)


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global and not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _peer_ip(tls_socket: Any) -> str:
    try:
        peer = tls_socket.getpeername()
        address = ipaddress.ip_address(str(peer[0]))
    except (IndexError, OSError, TypeError, ValueError) as exc:
        raise ConnectorFailure(
            ConnectorErrorCode.PEER_IDENTITY_MISMATCH,
            detail="TLS peer address could not be verified",
        ) from exc
    return address.compressed


def _normalized_headers(headers: Message) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key in headers.keys():
        lowered = key.lower()
        values = headers.get_all(key, failobj=[])
        if lowered in normalized or len(values) != 1:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail=f"duplicate response header is not accepted: {lowered}",
            )
        normalized[lowered] = values[0].strip()
    return normalized


def _raise_for_status(status: int, headers: dict[str, str]) -> None:
    if 200 <= status < 300:
        return
    if 300 <= status < 400:
        raise ConnectorFailure(
            ConnectorErrorCode.REDIRECT_NOT_FOLLOWED,
            detail="redirect responses are never followed",
            http_status=status,
        )
    if status == 401:
        code = ConnectorErrorCode.UNAUTHORIZED
    elif status == 403:
        code = ConnectorErrorCode.FORBIDDEN
    elif status in {404, 410}:
        code = ConnectorErrorCode.CONTRACT_CHANGED
    elif status == 429:
        code = ConnectorErrorCode.RATE_LIMITED
    elif 500 <= status < 600:
        code = ConnectorErrorCode.REMOTE_5XX
    else:
        code = ConnectorErrorCode.CONTRACT_CHANGED
    retry_after = _retry_after_seconds(headers.get("retry-after")) if status == 429 else None
    raise ConnectorFailure(
        code,
        detail=f"official endpoint returned HTTP {status}",
        http_status=status,
        retry_after_seconds=retry_after,
    )


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return _MAX_RETRY_AFTER_SECONDS + 1
    if seconds < 0:
        return _MAX_RETRY_AFTER_SECONDS + 1
    return seconds


def _is_retryable(failure: ConnectorFailure) -> bool:
    if failure.code == ConnectorErrorCode.TIMEOUT:
        return True
    if failure.code == ConnectorErrorCode.REMOTE_5XX:
        return failure.http_status in _TRANSIENT_STATUS
    if failure.code == ConnectorErrorCode.RATE_LIMITED:
        return (
            failure.retry_after_seconds is None
            or failure.retry_after_seconds <= _MAX_RETRY_AFTER_SECONDS
        )
    return False


def _validate_content_contract(
    headers: dict[str, str],
    endpoint: EndpointPolicy,
) -> tuple[str, str | None]:
    encoding = headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise ConnectorFailure(
            ConnectorErrorCode.UNSUPPORTED_ENCODING,
            detail="compressed or transformed responses are not accepted",
        )
    transfer_encoding = headers.get("transfer-encoding")
    if transfer_encoding is not None and transfer_encoding.lower() != "chunked":
        raise ConnectorFailure(
            ConnectorErrorCode.UNSUPPORTED_ENCODING,
            detail="only identity bodies or standard chunked framing are accepted",
        )
    raw_content_type = headers.get("content-type")
    if raw_content_type is None:
        raise ConnectorFailure(
            ConnectorErrorCode.UNSUPPORTED_MIME,
            detail="response Content-Type is required",
        )
    media_type, parameters = _parse_content_type(raw_content_type)
    if media_type not in endpoint.allowed_media_types:
        raise ConnectorFailure(
            ConnectorErrorCode.UNSUPPORTED_MIME,
            detail="response media type is outside the endpoint allowlist",
        )
    charset = parameters.get("charset")
    if charset is not None and charset.lower().replace("_", "-") != "utf-8":
        raise ConnectorFailure(
            ConnectorErrorCode.UNSUPPORTED_CHARSET,
            detail="only UTF-8 response text is accepted",
        )
    return media_type, None if charset is None else "utf-8"


def _parse_content_type(value: str) -> tuple[str, dict[str, str]]:
    parts = [item.strip() for item in value.split(";")]
    media_type = parts[0].lower()
    parameters: dict[str, str] = {}
    for item in parts[1:]:
        if "=" not in item:
            raise ConnectorFailure(
                ConnectorErrorCode.UNSUPPORTED_MIME,
                detail="Content-Type parameter is malformed",
            )
        key, raw = item.split("=", maxsplit=1)
        key = key.strip().lower()
        raw = raw.strip().strip('"')
        if not key or key in parameters:
            raise ConnectorFailure(
                ConnectorErrorCode.UNSUPPORTED_MIME,
                detail="Content-Type parameters must be unique",
            )
        parameters[key] = raw
    return media_type, parameters


def _read_bounded_body(
    response: http.client.HTTPResponse,
    *,
    maximum_bytes: int,
    content_length: str | None,
) -> bytes:
    advertised: int | None = None
    if content_length is not None:
        try:
            advertised = int(content_length)
        except ValueError as exc:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="Content-Length is malformed",
            ) from exc
        if advertised < 0:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="Content-Length cannot be negative",
            )
        if advertised > maximum_bytes:
            raise ConnectorFailure(
                ConnectorErrorCode.RESPONSE_TOO_LARGE,
                detail="advertised response size exceeds the endpoint cap",
            )
    if response.chunked and advertised is not None:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="response cannot combine chunked framing with Content-Length",
        )
    # HTTPResponse otherwise treats Content-Length as authoritative and would
    # hide trailing bytes.  The validator requested Connection: close, so for
    # non-chunked responses it reads to EOF under its own hard cap instead.
    if not response.chunked:
        response.length = None
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = response.read(min(_READ_CHUNK_BYTES, maximum_bytes + 1 - total))
        except (http.client.HTTPException, OSError) as exc:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="response body framing is malformed",
            ) from exc
        if not chunk:
            break
        total += len(chunk)
        if total > maximum_bytes:
            raise ConnectorFailure(
                ConnectorErrorCode.RESPONSE_TOO_LARGE,
                detail="streamed response exceeded the endpoint cap",
            )
        chunks.append(chunk)
    if advertised is not None and advertised != total:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="actual response size differs from Content-Length",
        )
    return b"".join(chunks)

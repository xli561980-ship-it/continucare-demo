from __future__ import annotations

import io
import socket
import ssl
from collections.abc import Mapping

import pytest

from continucare.knowledge.ops.source_connectors import (
    PMC_OA_ENDPOINT,
    PUBMED_ESUMMARY_ENDPOINT,
    ConnectorErrorCode,
    ConnectorFailure,
    ControlledRequest,
    EndpointPolicy,
    Pmcid,
    Pmid,
    ProcessSharedRateLimiter,
    PubMedPmcConnector,
    SecureMetadataTransport,
    issue_knowledge_egress_permit,
    knowledge_live_validation_enabled,
)
from continucare.knowledge.ops.source_connectors.transport import (
    _PROCESS_SHARED_RATE_LIMITER,
)


PUBLIC_IP = "93.184.216.34"


class FakeRawSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeTlsSocket:
    def __init__(self, response: bytes, *, peer_ip: str = PUBLIC_IP) -> None:
        self._response = response
        self._peer_ip = peer_ip
        self.sent = b""
        self.closed = False

    def getpeername(self) -> tuple[str, int]:
        return self._peer_ip, 443

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def makefile(self, _mode: str) -> io.BytesIO:
        return io.BytesIO(self._response)

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, sockets: list[FakeTlsSocket]) -> None:
        self.check_hostname = False
        self.verify_mode = ssl.CERT_NONE
        self.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        self.maximum_version = ssl.TLSVersion.MAXIMUM_SUPPORTED
        self.server_names: list[str] = []
        self._sockets = sockets

    def wrap_socket(self, _raw: FakeRawSocket, *, server_hostname: str) -> FakeTlsSocket:
        self.server_names.append(server_hostname)
        return self._sockets.pop(0)


class FakeClock:
    def __init__(self, initial: float = 100.0) -> None:
        self.value = initial
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _permit():
    return issue_knowledge_egress_permit(
        external_egress_enabled=True,
        identity_binding_proven=True,
        environ={"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "true"},
    )


def _endpoint(*, maximum_bytes: int = 64) -> EndpointPolicy:
    return EndpointPolicy(
        endpoint_id="synthetic-json-endpoint",
        source_id="synthetic-source",
        source_policy_id="synthetic-policy",
        source_policy_version=1,
        official_documentation_url="https://example.com/docs",
        hostname="example.com",
        path_pattern=r"^/metadata\.json$",
        path_template="/metadata.json",
        allowed_media_types=("application/json",),
        maximum_response_bytes=maximum_bytes,
        rights_status="rights_unresolved",
    )


def _request() -> ControlledRequest:
    return ControlledRequest(
        endpoint_id="synthetic-json-endpoint",
        url="https://example.com/metadata.json",
        query_identity="synthetic-record",
        accept="application/json",
    )


def _wire_response(
    *,
    status: int = 200,
    body: bytes = b"{}",
    headers: Mapping[str, str] | None = None,
) -> bytes:
    supplied = {"Content-Type": "application/json; charset=UTF-8", **dict(headers or {})}
    lines = [f"HTTP/1.1 {status} Test"]
    lines.extend(f"{key}: {value}" for key, value in supplied.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def _resolver(*addresses: str):
    return lambda _host, _port: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ]


def _transport(
    responses: list[bytes],
    *,
    peer_ips: list[str] | None = None,
    resolver=None,
    sleeps: list[float] | None = None,
    clock=None,
    sleeper=None,
    rate_limiter: ProcessSharedRateLimiter | None = None,
):
    tls_sockets = [
        FakeTlsSocket(response, peer_ip=(peer_ips or [PUBLIC_IP] * len(responses))[index])
        for index, response in enumerate(responses)
    ]
    all_sockets = list(tls_sockets)
    context = FakeContext(tls_sockets)
    raw_sockets: list[FakeRawSocket] = []

    def connect(address: str, port: int, timeout: float) -> FakeRawSocket:
        assert address == PUBLIC_IP
        assert port == 443
        assert timeout == 10.0
        raw = FakeRawSocket()
        raw_sockets.append(raw)
        return raw

    transport = SecureMetadataTransport(
        permit=_permit(),
        resolver=resolver or _resolver(PUBLIC_IP),
        tcp_connector=connect,
        context_factory=lambda: context,  # type: ignore[arg-type]
        sleeper=sleeper or (sleeps if sleeps is not None else []).append,
        clock=clock or (lambda: 100.0),
        jitter=lambda: 0.0,
        rate_limiter=rate_limiter,
    )
    return transport, context, all_sockets, raw_sockets


@pytest.fixture(autouse=True)
def _isolate_process_shared_rate_limiter():
    _PROCESS_SHARED_RATE_LIMITER.reset_for_testing()
    yield
    _PROCESS_SHARED_RATE_LIMITER.reset_for_testing()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, False),
        ({"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": ""}, False),
        ({"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "false"}, False),
        ({"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "TRUE"}, False),
        ({"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "1"}, False),
        ({"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "yes"}, False),
        ({"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "on"}, False),
        ({"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "random"}, False),
        ({"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "true"}, True),
    ],
)
def test_live_flag_is_exact_and_default_off(raw: dict[str, str], expected: bool) -> None:
    assert knowledge_live_validation_enabled(raw) is expected


def test_capability_requires_global_flag_and_identity_proof() -> None:
    for global_enabled, binding in [(False, True), (True, False)]:
        with pytest.raises(ConnectorFailure) as raised:
            issue_knowledge_egress_permit(
                external_egress_enabled=global_enabled,
                identity_binding_proven=binding,
                environ={"CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION": "true"},
            )
        assert raised.value.code == ConnectorErrorCode.FEATURE_DISABLED


def test_transport_binds_dns_socket_tls_peer_and_original_host() -> None:
    transport, context, sockets, _raw = _transport(
        [_wire_response(headers={"ETag": '"v1"', "Last-Modified": "today"})]
    )
    response = transport.execute(_request(), _endpoint())
    assert response.body == b"{}"
    assert response.peer_ip == PUBLIC_IP
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.maximum_version == ssl.TLSVersion.MAXIMUM_SUPPORTED
    assert context.server_names == ["example.com"]
    assert b"Host: example.com\r\n" in sockets[0].sent
    assert b"Accept-Encoding: identity\r\n" in sockets[0].sent
    assert b"Proxy" not in sockets[0].sent


def test_any_non_public_dns_answer_rejects_before_tcp() -> None:
    called: list[str] = []
    transport = SecureMetadataTransport(
        permit=_permit(),
        resolver=_resolver(PUBLIC_IP, "127.0.0.1"),
        tcp_connector=lambda address, _port, _timeout: called.append(address),
    )
    with pytest.raises(ConnectorFailure) as raised:
        transport.execute(_request(), _endpoint())
    assert raised.value.code == ConnectorErrorCode.NON_PUBLIC_DNS_ANSWER
    assert called == []


def test_tls_peer_mismatch_rejects_before_http_send() -> None:
    mismatched = "8.8.8.8"
    transport, _context, sockets, _raw = _transport(
        [_wire_response()], peer_ips=[mismatched]
    )
    with pytest.raises(ConnectorFailure) as raised:
        transport.execute(_request(), _endpoint())
    assert raised.value.code == ConnectorErrorCode.PEER_IDENTITY_MISMATCH
    assert sockets[0].sent == b""


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (301, ConnectorErrorCode.REDIRECT_NOT_FOLLOWED),
        (401, ConnectorErrorCode.UNAUTHORIZED),
        (403, ConnectorErrorCode.FORBIDDEN),
        (404, ConnectorErrorCode.CONTRACT_CHANGED),
        (410, ConnectorErrorCode.CONTRACT_CHANGED),
        (501, ConnectorErrorCode.REMOTE_5XX),
    ],
)
def test_non_success_statuses_are_stable_and_not_retried(status: int, expected) -> None:
    transport, _context, _sockets, raw = _transport([_wire_response(status=status)])
    with pytest.raises(ConnectorFailure) as raised:
        transport.execute(_request(), _endpoint())
    assert raised.value.code == expected
    assert len(raw) == 1


def test_pubmed_and_pmc_share_process_rate_budget_across_instances() -> None:
    fake_time = FakeClock()
    pubmed_body = (
        b'{"result":{"uids":["12345"],"12345":'
        b'{"title":"Synthetic bibliographic title"}}}'
    )
    pmc_body = (
        b'<OA><records><record id="PMC12345" license="CC BY" retracted="no">'
        b'<link format="tgz" href="https://example.invalid/synthetic.tar.gz"/>'
        b"</record></records></OA>"
    )
    pubmed_transport, _context, _sockets, _raw = _transport(
        [_wire_response(body=pubmed_body)],
        clock=fake_time,
        sleeper=fake_time.sleep,
    )
    pmc_transport, _context, _sockets, _raw = _transport(
        [
            _wire_response(
                body=pmc_body,
                headers={"Content-Type": "application/xml; charset=UTF-8"},
            )
        ],
        clock=fake_time,
        sleeper=fake_time.sleep,
    )

    pubmed = PubMedPmcConnector().discover_pubmed(
        Pmid(value="12345"), transport=pubmed_transport
    )
    pmc = PubMedPmcConnector().discover_pmc_license_locator(
        Pmcid(value="PMC12345"), transport=pmc_transport
    )

    assert pubmed.records[0].pmid == "12345"
    assert pmc.records[0].pmcid == "PMC12345"
    assert PUBMED_ESUMMARY_ENDPOINT.rate_limit_key == "ncbi"
    assert PMC_OA_ENDPOINT.rate_limit_key == "ncbi"
    assert fake_time.sleeps == [pytest.approx(0.4)]


def test_non_ncbi_sources_keep_separate_rate_buckets() -> None:
    fake_time = FakeClock()
    limiter = ProcessSharedRateLimiter()
    source_a = _endpoint().model_copy(
        update={"source_id": "source-a", "minimum_interval_seconds": 0.4}
    )
    source_b = _endpoint().model_copy(
        update={"source_id": "source-b", "minimum_interval_seconds": 0.4}
    )
    transport_a, _context, _sockets, _raw = _transport(
        [_wire_response()],
        clock=fake_time,
        sleeper=fake_time.sleep,
        rate_limiter=limiter,
    )
    transport_b, _context, _sockets, _raw = _transport(
        [_wire_response()],
        clock=fake_time,
        sleeper=fake_time.sleep,
        rate_limiter=limiter,
    )

    transport_a.execute(_request(), source_a)
    transport_b.execute(_request(), source_b)

    assert fake_time.sleeps == []


def test_selected_5xx_retries_are_bounded_and_injected() -> None:
    sleeps: list[float] = []
    transport, _context, _sockets, raw = _transport(
        [_wire_response(status=503), _wire_response(status=503), _wire_response()],
        sleeps=sleeps,
    )
    assert transport.execute(_request(), _endpoint()).status == 200
    assert len(raw) == 3
    assert sleeps == [0.25, 0.5]


def test_timeout_retries_are_bounded_and_can_be_disabled() -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def timeout_connect(address: str, _port: int, _timeout: float):
        calls.append(address)
        raise socket.timeout("synthetic timeout")

    transport = SecureMetadataTransport(
        permit=_permit(),
        resolver=_resolver(PUBLIC_IP),
        tcp_connector=timeout_connect,
        sleeper=sleeps.append,
        clock=lambda: 100.0,
        maximum_retries=0,
    )
    with pytest.raises(ConnectorFailure) as raised:
        transport.execute(_request(), _endpoint())
    assert raised.value.code == ConnectorErrorCode.TIMEOUT
    assert calls == [PUBLIC_IP]
    assert sleeps == []


def test_chunked_body_is_subject_to_the_same_streaming_hard_cap() -> None:
    chunked_body = b"4\r\n1234\r\n2\r\n56\r\n0\r\n\r\n"
    transport, _context, _sockets, _raw = _transport(
        [
            _wire_response(
                body=chunked_body,
                headers={"Transfer-Encoding": "chunked"},
            )
        ]
    )
    with pytest.raises(ConnectorFailure) as raised:
        transport.execute(_request(), _endpoint(maximum_bytes=5))
    assert raised.value.code == ConnectorErrorCode.RESPONSE_TOO_LARGE


def test_long_retry_after_does_not_wait_or_retry() -> None:
    sleeps: list[float] = []
    transport, _context, _sockets, raw = _transport(
        [_wire_response(status=429, headers={"Retry-After": "31"})], sleeps=sleeps
    )
    with pytest.raises(ConnectorFailure) as raised:
        transport.execute(_request(), _endpoint())
    assert raised.value.code == ConnectorErrorCode.RATE_LIMITED
    assert len(raw) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({"Content-Length": "6"}, b"{}", ConnectorErrorCode.RESPONSE_TOO_LARGE),
        ({}, b"123456", ConnectorErrorCode.RESPONSE_TOO_LARGE),
        ({"Content-Length": "1"}, b"123456", ConnectorErrorCode.RESPONSE_TOO_LARGE),
        ({"Content-Encoding": "gzip"}, b"{}", ConnectorErrorCode.UNSUPPORTED_ENCODING),
        ({"Transfer-Encoding": "gzip"}, b"{}", ConnectorErrorCode.UNSUPPORTED_ENCODING),
        ({"Content-Type": "text/html"}, b"{}", ConnectorErrorCode.UNSUPPORTED_MIME),
        ({"Content-Type": "application/json; charset=iso-8859-1"}, b"{}", ConnectorErrorCode.UNSUPPORTED_CHARSET),
    ],
)
def test_response_boundaries_ignore_advisory_lengths_and_reject_transforms(
    headers: dict[str, str], body: bytes, expected
) -> None:
    base = {} if "Content-Type" in headers else {"Content-Type": "application/json"}
    transport, _context, _sockets, _raw = _transport(
        [_wire_response(body=body, headers={**base, **headers})]
    )
    with pytest.raises(ConnectorFailure) as raised:
        transport.execute(_request(), _endpoint(maximum_bytes=5))
    assert raised.value.code == expected

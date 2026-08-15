from __future__ import annotations

import json

import pytest

from continucare.knowledge.ops.source_connectors import (
    ConnectorErrorCode,
    ConnectorFailure,
    ParserLimits,
    parse_bounded_json,
    parse_bounded_xml,
)


def test_json_rejects_malformed_non_utf8_and_nonstandard_numbers() -> None:
    for body in (b"{", b'{"x":"\xff"}', b'{"x":NaN}'):
        with pytest.raises(ConnectorFailure) as raised:
            parse_bounded_json(body, limits=ParserLimits(maximum_bytes=100))
        assert raised.value.code == ConnectorErrorCode.MALFORMED_JSON


def test_json_shape_limits_depth_containers_fields_and_scalar_length() -> None:
    cases = [
        (json.dumps({"a": {"b": {"c": 1}}}).encode(), ParserLimits(maximum_depth=2)),
        (json.dumps([[], []]).encode(), ParserLimits(maximum_containers=2)),
        (json.dumps({"a": 1, "b": 2}).encode(), ParserLimits(maximum_fields=1)),
        (json.dumps({"a": "12345"}).encode(), ParserLimits(maximum_scalar_characters=4)),
    ]
    for body, limits in cases:
        with pytest.raises(ConnectorFailure) as raised:
            parse_bounded_json(body, limits=limits)
        assert raised.value.code == ConnectorErrorCode.PARSER_LIMIT_EXCEEDED


def test_xml_rejects_malformed_doctype_xxe_parameter_entity_and_expansion() -> None:
    cases = [
        b"<root>",
        b'<!DOCTYPE root><root/>',
        b'<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><root>&xxe;</root>',
        b'<!DOCTYPE root [<!ENTITY % ext SYSTEM "https://example.com/x"> %ext;]><root/>',
        b'<!DOCTYPE root [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><root>&b;</root>',
    ]
    for body in cases:
        with pytest.raises(ConnectorFailure) as raised:
            parse_bounded_xml(body, limits=ParserLimits(maximum_bytes=1_000))
        assert raised.value.code == ConnectorErrorCode.MALFORMED_XML


def test_xml_limits_depth_elements_attributes_text_and_scalar_length() -> None:
    cases = [
        (b"<a><b><c/></b></a>", ParserLimits(maximum_depth=2)),
        (b"<a><b/><c/></a>", ParserLimits(maximum_elements=2)),
        (b'<a x="1" y="2"/>', ParserLimits(maximum_attributes=1)),
        (b"<a>12345</a>", ParserLimits(maximum_text_characters=4)),
        (b"<a>12345</a>", ParserLimits(maximum_scalar_characters=4)),
    ]
    for body, limits in cases:
        with pytest.raises(ConnectorFailure) as raised:
            parse_bounded_xml(body, limits=limits)
        assert raised.value.code == ConnectorErrorCode.PARSER_LIMIT_EXCEEDED


def test_parser_byte_cap_is_independent_of_transport_headers() -> None:
    for parser, body in [
        (parse_bounded_json, b'{"value":1}'),
        (parse_bounded_xml, b"<root/>")
    ]:
        with pytest.raises(ConnectorFailure) as raised:
            parser(body, limits=ParserLimits(maximum_bytes=3))
        assert raised.value.code == ConnectorErrorCode.RESPONSE_TOO_LARGE

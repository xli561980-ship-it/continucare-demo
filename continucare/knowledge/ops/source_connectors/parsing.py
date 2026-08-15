"""Bounded UTF-8 JSON and DTD-free XML parsing for metadata fixtures."""

from __future__ import annotations

import json
import re
import xml.parsers.expat
from dataclasses import dataclass, field
from pydantic import Field

from continucare.knowledge.ops.models import StrictModel
from continucare.knowledge.ops.source_connectors.errors import (
    ConnectorErrorCode,
    ConnectorFailure,
)


class ParserLimits(StrictModel):
    maximum_bytes: int = Field(default=1_000_000, gt=0, le=50_000_000)
    maximum_depth: int = Field(default=24, ge=1, le=128)
    maximum_containers: int = Field(default=10_000, ge=1, le=100_000)
    maximum_fields: int = Field(default=25_000, ge=1, le=250_000)
    maximum_elements: int = Field(default=10_000, ge=1, le=100_000)
    maximum_attributes: int = Field(default=25_000, ge=1, le=250_000)
    maximum_text_characters: int = Field(default=1_000_000, ge=1, le=10_000_000)
    maximum_scalar_characters: int = Field(default=16_384, ge=1, le=1_000_000)


@dataclass(frozen=True, slots=True)
class XmlNode:
    tag: str
    attributes: dict[str, str]
    text: str
    children: tuple["XmlNode", ...]

    def children_named(self, local_name: str) -> tuple["XmlNode", ...]:
        return tuple(item for item in self.children if xml_local_name(item.tag) == local_name)


@dataclass(slots=True)
class _MutableXmlNode:
    tag: str
    attributes: dict[str, str]
    text_parts: list[str] = field(default_factory=list)
    children: list["_MutableXmlNode"] = field(default_factory=list)


def parse_bounded_json(body: bytes, *, limits: ParserLimits) -> object:
    text = _strict_utf8(body, maximum_bytes=limits.maximum_bytes, kind="json")

    def parse_integer(value: str) -> int:
        if len(value) > 64:
            _parser_limit("JSON numeric scalar exceeds the character cap")
        return int(value)

    def parse_decimal(value: str) -> float:
        if len(value) > 64:
            _parser_limit("JSON numeric scalar exceeds the character cap")
        result = float(value)
        if result in {float("inf"), float("-inf")}:
            raise ValueError("non-finite JSON number")
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-standard JSON constant {value}")

    try:
        value = json.loads(
            text,
            parse_int=parse_integer,
            parse_float=parse_decimal,
            parse_constant=reject_constant,
        )
    except ConnectorFailure:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise ConnectorFailure(
            ConnectorErrorCode.MALFORMED_JSON,
            detail="metadata response is not strict JSON",
        ) from exc
    _validate_json_shape(value, limits=limits)
    return value


def _validate_json_shape(root: object, *, limits: ParserLimits) -> None:
    containers = 0
    fields = 0
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > limits.maximum_depth:
            _parser_limit("JSON nesting exceeds the configured depth")
        if isinstance(value, dict):
            containers += 1
            fields += len(value)
            if fields > limits.maximum_fields:
                _parser_limit("JSON field count exceeds the configured cap")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ConnectorFailure(
                        ConnectorErrorCode.MALFORMED_JSON,
                        detail="JSON object key is not text",
                    )
                if len(key) > limits.maximum_scalar_characters:
                    _parser_limit("JSON key exceeds the scalar character cap")
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            containers += 1
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str) and len(value) > limits.maximum_scalar_characters:
            _parser_limit("JSON string exceeds the scalar character cap")
        if containers > limits.maximum_containers:
            _parser_limit("JSON container count exceeds the configured cap")


def parse_bounded_xml(body: bytes, *, limits: ParserLimits) -> XmlNode:
    text = _strict_utf8(body, maximum_bytes=limits.maximum_bytes, kind="xml")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, flags=re.IGNORECASE):
        raise ConnectorFailure(
            ConnectorErrorCode.MALFORMED_XML,
            detail="DTD and entity declarations are forbidden",
        )
    declaration = re.match(r"\s*<\?xml\s+[^?]*encoding\s*=\s*['\"]([^'\"]+)", text, re.I)
    if declaration and declaration.group(1).lower().replace("_", "-") != "utf-8":
        raise ConnectorFailure(
            ConnectorErrorCode.MALFORMED_XML,
            detail="XML declaration must specify UTF-8 when an encoding is present",
        )

    parser = xml.parsers.expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: list[_MutableXmlNode] = []
    root: _MutableXmlNode | None = None
    elements = 0
    attributes = 0
    text_characters = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal root, elements, attributes
        elements += 1
        attributes += len(attrs)
        if elements > limits.maximum_elements:
            _parser_limit("XML element count exceeds the configured cap")
        if attributes > limits.maximum_attributes:
            _parser_limit("XML attribute count exceeds the configured cap")
        if len(stack) + 1 > limits.maximum_depth:
            _parser_limit("XML nesting exceeds the configured depth")
        for key, value in attrs.items():
            if (
                len(key) > limits.maximum_scalar_characters
                or len(value) > limits.maximum_scalar_characters
            ):
                _parser_limit("XML attribute exceeds the scalar character cap")
        node = _MutableXmlNode(tag=name, attributes=dict(attrs))
        if stack:
            stack[-1].children.append(node)
        elif root is None:
            root = node
        else:
            raise ConnectorFailure(
                ConnectorErrorCode.MALFORMED_XML,
                detail="XML has more than one root element",
            )
        stack.append(node)

    def end(_name: str) -> None:
        if not stack:
            raise ConnectorFailure(
                ConnectorErrorCode.MALFORMED_XML,
                detail="XML element stack is unbalanced",
            )
        stack.pop()

    def character_data(value: str) -> None:
        nonlocal text_characters
        if not value:
            return
        text_characters += len(value)
        if text_characters > limits.maximum_text_characters:
            _parser_limit("XML text exceeds the configured aggregate cap")
        if stack:
            stack[-1].text_parts.append(value)

    def reject_declaration(*_args: object) -> None:
        raise ConnectorFailure(
            ConnectorErrorCode.MALFORMED_XML,
            detail="DTD, entities, and external references are forbidden",
        )

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = character_data
    parser.StartDoctypeDeclHandler = reject_declaration
    parser.EntityDeclHandler = reject_declaration
    parser.UnparsedEntityDeclHandler = reject_declaration
    parser.ExternalEntityRefHandler = lambda *_args: reject_declaration()
    try:
        parser.Parse(text, True)
    except ConnectorFailure:
        raise
    except (xml.parsers.expat.ExpatError, RecursionError, UnicodeError) as exc:
        raise ConnectorFailure(
            ConnectorErrorCode.MALFORMED_XML,
            detail="metadata response is not safe, well-formed XML",
        ) from exc
    if root is None or stack:
        raise ConnectorFailure(
            ConnectorErrorCode.MALFORMED_XML,
            detail="XML document has no complete root element",
        )
    return _freeze_xml(root, limits=limits)


def _freeze_xml(node: _MutableXmlNode, *, limits: ParserLimits) -> XmlNode:
    text = "".join(node.text_parts).strip()
    if len(text) > limits.maximum_scalar_characters:
        _parser_limit("XML element text exceeds the scalar character cap")
    return XmlNode(
        tag=node.tag,
        attributes=node.attributes,
        text=text,
        children=tuple(_freeze_xml(item, limits=limits) for item in node.children),
    )


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1].rsplit(":", maxsplit=1)[-1]


def _strict_utf8(body: bytes, *, maximum_bytes: int, kind: str) -> str:
    if len(body) > maximum_bytes:
        raise ConnectorFailure(
            ConnectorErrorCode.RESPONSE_TOO_LARGE,
            detail=f"{kind.upper()} body exceeds the parser byte cap",
        )
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        code = (
            ConnectorErrorCode.MALFORMED_JSON
            if kind == "json"
            else ConnectorErrorCode.MALFORMED_XML
        )
        raise ConnectorFailure(code, detail=f"{kind.upper()} body is not strict UTF-8") from exc


def _parser_limit(detail: str) -> None:
    raise ConnectorFailure(ConnectorErrorCode.PARSER_LIMIT_EXCEEDED, detail=detail)

"""MedlinePlus health-topic feed metadata connector (no topic body capture)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from continucare.knowledge.ops.models import NonBlank, Sha256, StrictModel
from continucare.knowledge.ops.source_connectors.common import (
    assert_response_contract,
    required_text,
    response_digest,
    response_validator,
)
from continucare.knowledge.ops.source_connectors.contracts import (
    ControlledRequest,
    EndpointPolicy,
    MetadataResponse,
    MetadataTransport,
)
from continucare.knowledge.ops.source_connectors.errors import (
    ConnectorErrorCode,
    ConnectorFailure,
)
from continucare.knowledge.ops.source_connectors.parsing import (
    ParserLimits,
    XmlNode,
    parse_bounded_xml,
    xml_local_name,
)


class MedlinePlusFeed(StrictModel):
    feed_id: Literal["health-topics"] = "health-topics"
    publication_date: date


class MedlinePlusTopicMetadata(StrictModel):
    topic_id: NonBlank
    title: NonBlank
    language: NonBlank
    updated: NonBlank
    canonical_link: NonBlank
    content_origin: Literal["nlm_topic_metadata"] = "nlm_topic_metadata"
    includes_patient_facing_body: Literal[False] = False


class MedlinePlusMetadataBatch(StrictModel):
    generated_at: NonBlank | None = None
    records: tuple[MedlinePlusTopicMetadata, ...] = Field(min_length=1)
    etag: NonBlank | None = None
    last_modified: NonBlank | None = None
    whole_response_sha256: Sha256
    includes_third_party_content: Literal[False] = False


MEDLINEPLUS_TOPICS_ENDPOINT = EndpointPolicy(
    endpoint_id="medlineplus-health-topics-xml",
    source_id="medlineplus",
    source_policy_id="source-medlineplus",
    source_policy_version=1,
    official_documentation_url="https://medlineplus.gov/xml.html",
    hostname="medlineplus.gov",
    path_pattern=r"^/xml/mplus_topics_[0-9]{4}-[0-9]{2}-[0-9]{2}\.xml$",
    path_template="/xml/mplus_topics_{publication_date}.xml",
    allowed_media_types=("application/xml", "text/xml"),
    maximum_response_bytes=2_000_000,
    rights_status="rights_unresolved",
)


def build_medlineplus_topics_request(feed: MedlinePlusFeed) -> ControlledRequest:
    if not isinstance(feed, MedlinePlusFeed) or feed.feed_id != "health-topics":
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="MedlinePlus requests require a typed health-topic feed date",
        )
    iso_date = feed.publication_date.isoformat()
    path = MEDLINEPLUS_TOPICS_ENDPOINT.path_template.format(publication_date=iso_date)
    return ControlledRequest(
        endpoint_id=MEDLINEPLUS_TOPICS_ENDPOINT.endpoint_id,
        url=f"https://{MEDLINEPLUS_TOPICS_ENDPOINT.hostname}{path}",
        query_identity=f"topics-{iso_date}",
        accept="application/xml",
    )


def parse_medlineplus_topics(response: MetadataResponse) -> MedlinePlusMetadataBatch:
    endpoint = MEDLINEPLUS_TOPICS_ENDPOINT
    assert_response_contract(response, endpoint)
    root = parse_bounded_xml(
        response.body,
        limits=ParserLimits(
            maximum_bytes=endpoint.maximum_response_bytes,
            maximum_elements=20_000,
            maximum_attributes=50_000,
        ),
    )
    if xml_local_name(root.tag) not in {"health-topics", "medlineplus"}:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="MedlinePlus feed root is not recognized",
        )
    records: list[MedlinePlusTopicMetadata] = []
    seen: set[str] = set()
    for node in _walk(root):
        if xml_local_name(node.tag) != "health-topic":
            continue
        topic_id = required_text(
            node.attributes.get("id"), field="health-topic@id", maximum_characters=128
        )
        if topic_id in seen:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="MedlinePlus feed contains duplicate topic identifiers",
            )
        seen.add(topic_id)
        records.append(
            MedlinePlusTopicMetadata(
                topic_id=topic_id,
                title=required_text(node.attributes.get("title"), field="health-topic@title"),
                language=required_text(
                    node.attributes.get("language"),
                    field="health-topic@language",
                    maximum_characters=32,
                ),
                updated=required_text(
                    node.attributes.get("date-created")
                    or node.attributes.get("date-generated")
                    or node.attributes.get("last-modified"),
                    field="health-topic@updated",
                    maximum_characters=64,
                ),
                canonical_link=required_text(
                    node.attributes.get("url"), field="health-topic@url"
                ),
            )
        )
    if not records:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="MedlinePlus feed contains no topic metadata records",
        )
    generated = root.attributes.get("dategenerated") or root.attributes.get("date-generated")
    return MedlinePlusMetadataBatch(
        generated_at=generated,
        records=tuple(records),
        etag=response_validator(response.headers, "etag"),
        last_modified=response_validator(response.headers, "last-modified"),
        whole_response_sha256=response_digest(response),
    )


def _walk(root: XmlNode) -> tuple[XmlNode, ...]:
    result: list[XmlNode] = []
    stack = [root]
    while stack:
        node = stack.pop()
        result.append(node)
        stack.extend(reversed(node.children))
    return tuple(result)


class MedlinePlusConnector:
    connector_id = "medlineplus-metadata-v1"
    connector_version = "1.0.0"
    parser_id = "medlineplus-health-topics-xml"
    parser_version = "1.0.0"
    endpoint_policy = MEDLINEPLUS_TOPICS_ENDPOINT

    def discover_topics(
        self,
        feed: MedlinePlusFeed,
        *,
        transport: MetadataTransport,
    ) -> MedlinePlusMetadataBatch:
        return parse_medlineplus_topics(
            transport.execute(build_medlineplus_topics_request(feed), self.endpoint_policy)
        )

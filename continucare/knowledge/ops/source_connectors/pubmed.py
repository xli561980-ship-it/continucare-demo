"""Separate PubMed metadata and PMC Open Access locator contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from continucare.knowledge.ops.models import NonBlank, Sha256, StrictModel
from continucare.knowledge.ops.source_connectors.common import (
    assert_response_contract,
    optional_text,
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
    parse_bounded_json,
    parse_bounded_xml,
    xml_local_name,
)


PmidValue = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,8}$")]
PmcidValue = Annotated[str, StringConstraints(pattern=r"^PMC[1-9][0-9]{0,9}$")]


class Pmid(StrictModel):
    value: PmidValue


class Pmcid(StrictModel):
    value: PmcidValue


class PubMedMetadata(StrictModel):
    pmid: PmidValue
    title: NonBlank
    publication_date: NonBlank | None = None
    source_title: NonBlank | None = None
    record_locator: NonBlank
    abstract_included: Literal[False] = False
    clinical_conclusion: Literal[False] = False


class PubMedMetadataBatch(StrictModel):
    records: tuple[PubMedMetadata, ...] = Field(min_length=1)
    etag: NonBlank | None = None
    last_modified: NonBlank | None = None
    whole_response_sha256: Sha256
    rights_scope: Literal["bibliographic_metadata_only"] = (
        "bibliographic_metadata_only"
    )


class PmcOpenAccessLocator(StrictModel):
    pmcid: PmcidValue
    license_label: NonBlank | None = None
    retracted: bool
    locator: NonBlank
    full_text_included: Literal[False] = False
    license_requires_item_review: Literal[True] = True


class PmcOpenAccessBatch(StrictModel):
    records: tuple[PmcOpenAccessLocator, ...] = Field(min_length=1)
    etag: NonBlank | None = None
    last_modified: NonBlank | None = None
    whole_response_sha256: Sha256
    rights_scope: Literal["per_article_license_locator_only"] = (
        "per_article_license_locator_only"
    )


PUBMED_ESUMMARY_ENDPOINT = EndpointPolicy(
    endpoint_id="pubmed-esummary-json",
    source_id="pubmed",
    source_policy_id="nlm-pubmed-metadata",
    source_policy_version=2,
    official_documentation_url="https://www.ncbi.nlm.nih.gov/books/NBK25499/",
    hostname="eutils.ncbi.nlm.nih.gov",
    path_pattern=r"^/entrez/eutils/esummary\.fcgi$",
    path_template="/entrez/eutils/esummary.fcgi",
    allowed_query_keys=("db", "id", "retmode", "tool"),
    query_value_patterns={
        "db": r"^pubmed$",
        "id": r"^[1-9][0-9]{0,8}$",
        "retmode": r"^json$",
        "tool": r"^continucare_knowledge$",
    },
    allowed_media_types=("application/json",),
    maximum_response_bytes=512_000,
    minimum_interval_seconds=0.4,
    rate_limit_key="ncbi",
    rights_status="rights_unresolved",
)


PMC_OA_ENDPOINT = EndpointPolicy(
    endpoint_id="pmc-open-access-locator-xml",
    source_id="pmc",
    source_policy_id="source-pmc-open-access",
    source_policy_version=1,
    official_documentation_url="https://pmc.ncbi.nlm.nih.gov/tools/oa-service/",
    hostname="www.ncbi.nlm.nih.gov",
    path_pattern=r"^/pmc/utils/oa/oa\.fcgi$",
    path_template="/pmc/utils/oa/oa.fcgi",
    allowed_query_keys=("id",),
    query_value_patterns={"id": r"^PMC[1-9][0-9]{0,9}$"},
    allowed_media_types=("application/xml", "text/xml"),
    maximum_response_bytes=512_000,
    minimum_interval_seconds=0.4,
    rate_limit_key="ncbi",
    rights_status="rights_unresolved",
)


def build_pubmed_summary_request(pmid: Pmid) -> ControlledRequest:
    if not isinstance(pmid, Pmid):
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="PubMed requests require a typed PMID",
        )
    query = f"db=pubmed&id={pmid.value}&retmode=json&tool=continucare_knowledge"
    return ControlledRequest(
        endpoint_id=PUBMED_ESUMMARY_ENDPOINT.endpoint_id,
        url=f"https://{PUBMED_ESUMMARY_ENDPOINT.hostname}{PUBMED_ESUMMARY_ENDPOINT.path_template}?{query}",
        query_identity=f"pmid-{pmid.value}",
        accept="application/json",
    )


def build_pmc_oa_request(pmcid: Pmcid) -> ControlledRequest:
    if not isinstance(pmcid, Pmcid):
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="PMC OA requests require a typed PMCID",
        )
    return ControlledRequest(
        endpoint_id=PMC_OA_ENDPOINT.endpoint_id,
        url=(
            f"https://{PMC_OA_ENDPOINT.hostname}{PMC_OA_ENDPOINT.path_template}"
            f"?id={pmcid.value}"
        ),
        query_identity=f"pmcid-{pmcid.value}",
        accept="application/xml",
    )


def parse_pubmed_summary(response: MetadataResponse) -> PubMedMetadataBatch:
    endpoint = PUBMED_ESUMMARY_ENDPOINT
    assert_response_contract(response, endpoint)
    value = parse_bounded_json(
        response.body,
        limits=ParserLimits(maximum_bytes=endpoint.maximum_response_bytes),
    )
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="PubMed ESummary response lacks the documented result object",
        )
    result = value["result"]
    uids = result.get("uids")
    if not isinstance(uids, list) or not uids:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="PubMed ESummary response has no documented UID list",
        )
    records: list[PubMedMetadata] = []
    seen: set[str] = set()
    for raw_uid in uids:
        uid = Pmid(value=required_text(raw_uid, field="result.uids[]", maximum_characters=9)).value
        row = result.get(uid)
        if uid in seen or not isinstance(row, dict):
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="PubMed ESummary UID record is missing or duplicated",
            )
        seen.add(uid)
        records.append(
            PubMedMetadata(
                pmid=uid,
                title=required_text(row.get("title"), field=f"result.{uid}.title"),
                publication_date=optional_text(
                    row.get("pubdate"), field=f"result.{uid}.pubdate", maximum_characters=128
                ),
                source_title=optional_text(
                    row.get("fulljournalname") or row.get("source"),
                    field=f"result.{uid}.source",
                ),
                record_locator=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            )
        )
    return PubMedMetadataBatch(
        records=tuple(records),
        etag=response_validator(response.headers, "etag"),
        last_modified=response_validator(response.headers, "last-modified"),
        whole_response_sha256=response_digest(response),
    )


def parse_pmc_oa_locator(response: MetadataResponse) -> PmcOpenAccessBatch:
    endpoint = PMC_OA_ENDPOINT
    assert_response_contract(response, endpoint)
    root = parse_bounded_xml(
        response.body,
        limits=ParserLimits(maximum_bytes=endpoint.maximum_response_bytes),
    )
    records: list[PmcOpenAccessLocator] = []
    seen: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))
        if xml_local_name(node.tag) != "record":
            continue
        pmcid = Pmcid(
            value=required_text(node.attributes.get("id"), field="record@id", maximum_characters=16)
        ).value
        if pmcid in seen:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="PMC OA response contains a duplicate record",
            )
        seen.add(pmcid)
        locator: str | None = None
        for child in node.children:
            if xml_local_name(child.tag) == "link" and child.attributes.get("href"):
                locator = child.attributes["href"]
                break
        records.append(
            PmcOpenAccessLocator(
                pmcid=pmcid,
                license_label=optional_text(
                    node.attributes.get("license"), field="record@license", maximum_characters=256
                ),
                retracted=node.attributes.get("retracted", "no").lower() in {"yes", "true", "1"},
                locator=required_text(locator, field="record/link@href"),
            )
        )
    if not records:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="PMC OA response contains no license locator record",
        )
    return PmcOpenAccessBatch(
        records=tuple(records),
        etag=response_validator(response.headers, "etag"),
        last_modified=response_validator(response.headers, "last-modified"),
        whole_response_sha256=response_digest(response),
    )


class PubMedPmcConnector:
    connector_id = "pubmed-pmc-metadata-v1"
    connector_version = "1.0.0"
    pubmed_parser_id = "pubmed-esummary-json"
    pmc_parser_id = "pmc-oa-locator-xml"
    parser_version = "1.0.0"

    def discover_pubmed(
        self,
        pmid: Pmid,
        *,
        transport: MetadataTransport,
    ) -> PubMedMetadataBatch:
        return parse_pubmed_summary(
            transport.execute(build_pubmed_summary_request(pmid), PUBMED_ESUMMARY_ENDPOINT)
        )

    def discover_pmc_license_locator(
        self,
        pmcid: Pmcid,
        *,
        transport: MetadataTransport,
    ) -> PmcOpenAccessBatch:
        return parse_pmc_oa_locator(
            transport.execute(build_pmc_oa_request(pmcid), PMC_OA_ENDPOINT)
        )

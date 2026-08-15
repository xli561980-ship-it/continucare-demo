"""EMA documented website JSON dataset metadata connector."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

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
)


class EmaDataset(StrictModel):
    dataset_id: Literal["medicines"] = "medicines"


class EmaMedicineMetadata(StrictModel):
    ema_product_number: NonBlank
    name_of_medicine: NonBlank
    active_substance: NonBlank | None = None
    medicine_status: NonBlank | None = None
    revision_number: NonBlank | None = None
    medicine_url: NonBlank
    metadata_only: Literal[True] = True


class EmaMetadataBatch(StrictModel):
    records: tuple[EmaMedicineMetadata, ...] = Field(min_length=1)
    etag: NonBlank | None = None
    last_modified: NonBlank | None = None
    whole_response_sha256: Sha256
    contains_document_text: Literal[False] = False


EMA_MEDICINES_ENDPOINT = EndpointPolicy(
    endpoint_id="ema-website-medicines-json",
    source_id="ema",
    source_policy_id="source-ema-website-data",
    source_policy_version=1,
    official_documentation_url=(
        "https://www.ema.europa.eu/en/about-us/about-website/"
        "download-website-data-json-data-format"
    ),
    hostname="www.ema.europa.eu",
    path_pattern=(
        r"^/en/documents/report/medicines-output-medicines_json-report_en\.json$"
    ),
    path_template="/en/documents/report/medicines-output-medicines_json-report_en.json",
    allowed_media_types=("application/json",),
    maximum_response_bytes=2_000_000,
    rights_status="rights_unresolved",
)


def build_ema_dataset_request(dataset: EmaDataset) -> ControlledRequest:
    if not isinstance(dataset, EmaDataset) or dataset.dataset_id != "medicines":
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="EMA requests require the registered fixed medicines dataset",
        )
    return ControlledRequest(
        endpoint_id=EMA_MEDICINES_ENDPOINT.endpoint_id,
        url=f"https://{EMA_MEDICINES_ENDPOINT.hostname}{EMA_MEDICINES_ENDPOINT.path_template}",
        query_identity="dataset-medicines",
        accept="application/json",
    )


def parse_ema_medicines(response: MetadataResponse) -> EmaMetadataBatch:
    endpoint = EMA_MEDICINES_ENDPOINT
    assert_response_contract(response, endpoint)
    value = parse_bounded_json(
        response.body,
        limits=ParserLimits(maximum_bytes=endpoint.maximum_response_bytes),
    )
    if isinstance(value, dict):
        rows = value.get("data")
    else:
        rows = value
    if not isinstance(rows, list) or not rows:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="EMA medicines dataset is not the documented record list",
        )
    records: list[EmaMedicineMetadata] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="EMA medicines record is not an object",
            )
        product_number = required_text(
            row.get("ema_product_number"),
            field=f"data[{index}].ema_product_number",
            maximum_characters=128,
        )
        if product_number in seen:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="EMA dataset contains duplicate product identifiers",
            )
        seen.add(product_number)
        records.append(
            EmaMedicineMetadata(
                ema_product_number=product_number,
                name_of_medicine=required_text(
                    row.get("name_of_medicine"), field=f"data[{index}].name_of_medicine"
                ),
                active_substance=optional_text(
                    row.get("active_substance"), field=f"data[{index}].active_substance"
                ),
                medicine_status=optional_text(
                    row.get("medicine_status") or row.get("medicine_statuses"),
                    field=f"data[{index}].medicine_status",
                ),
                revision_number=optional_text(
                    row.get("revision_number"),
                    field=f"data[{index}].revision_number",
                    maximum_characters=128,
                ),
                medicine_url=required_text(
                    row.get("medicine_url"), field=f"data[{index}].medicine_url"
                ),
            )
        )
    return EmaMetadataBatch(
        records=tuple(records),
        etag=response_validator(response.headers, "etag"),
        last_modified=response_validator(response.headers, "last-modified"),
        whole_response_sha256=response_digest(response),
    )


class EmaConnector:
    connector_id = "ema-website-metadata-v1"
    connector_version = "1.0.0"
    parser_id = "ema-medicines-json"
    parser_version = "1.0.0"
    endpoint_policy = EMA_MEDICINES_ENDPOINT

    def discover_medicines(
        self,
        dataset: EmaDataset,
        *,
        transport: MetadataTransport,
    ) -> EmaMetadataBatch:
        return parse_ema_medicines(
            transport.execute(build_ema_dataset_request(dataset), self.endpoint_policy)
        )

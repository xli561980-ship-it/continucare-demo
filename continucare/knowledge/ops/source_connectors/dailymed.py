"""DailyMed SPL history metadata connector (no label text acquisition)."""

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
)


DailyMedSetIdValue = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    ),
]


class DailyMedSetId(StrictModel):
    value: DailyMedSetIdValue


class DailyMedHistoryRecord(StrictModel):
    set_id: DailyMedSetIdValue
    spl_version: NonBlank
    published_date: NonBlank
    title: NonBlank | None = None
    document_locator: NonBlank
    metadata_only: Literal[True] = True


class DailyMedMetadataBatch(StrictModel):
    records: tuple[DailyMedHistoryRecord, ...] = Field(min_length=1)
    etag: NonBlank | None = None
    last_modified: NonBlank | None = None
    whole_response_sha256: Sha256
    contains_label_text: Literal[False] = False


DAILYMED_HISTORY_ENDPOINT = EndpointPolicy(
    endpoint_id="dailymed-spl-history-json",
    source_id="dailymed",
    source_policy_id="source-dailymed",
    source_policy_version=1,
    official_documentation_url=(
        "https://dailymed.nlm.nih.gov/dailymed/webservices-help/"
        "v2/spls_setid_history_api.cfm"
    ),
    hostname="dailymed.nlm.nih.gov",
    path_pattern=(
        r"^/dailymed/services/v2/spls/"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/history\.json$"
    ),
    path_template="/dailymed/services/v2/spls/{set_id}/history.json",
    allowed_media_types=("application/json",),
    maximum_response_bytes=1_000_000,
    rights_status="rights_unresolved",
)


def build_dailymed_history_request(set_id: DailyMedSetId) -> ControlledRequest:
    if not isinstance(set_id, DailyMedSetId):
        raise ConnectorFailure(
            ConnectorErrorCode.UNSAFE_QUERY,
            detail="DailyMed live requests require a typed SetID",
        )
    path = DAILYMED_HISTORY_ENDPOINT.path_template.format(set_id=set_id.value)
    return ControlledRequest(
        endpoint_id=DAILYMED_HISTORY_ENDPOINT.endpoint_id,
        url=f"https://{DAILYMED_HISTORY_ENDPOINT.hostname}{path}",
        query_identity=f"setid-{set_id.value}",
        accept="application/json",
    )


def parse_dailymed_history(response: MetadataResponse) -> DailyMedMetadataBatch:
    endpoint = DAILYMED_HISTORY_ENDPOINT
    assert_response_contract(response, endpoint)
    value = parse_bounded_json(
        response.body,
        limits=ParserLimits(maximum_bytes=endpoint.maximum_response_bytes),
    )
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="DailyMed history response lacks the documented data object",
        )
    data = value["data"]
    spl = data.get("spl")
    history = data.get("history")
    if not isinstance(spl, dict) or not isinstance(history, list) or not history:
        raise ConnectorFailure(
            ConnectorErrorCode.CONTRACT_CHANGED,
            detail="DailyMed history response lacks documented SPL history metadata",
        )
    set_id = DailyMedSetId(
        value=required_text(spl.get("setid"), field="data.spl.setid", maximum_characters=36)
    ).value
    title = optional_text(spl.get("title"), field="data.spl.title")
    records: list[DailyMedHistoryRecord] = []
    seen_versions: set[str] = set()
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="DailyMed history item is not an object",
            )
        version = required_text(
            item.get("spl_version"), field=f"data.history[{index}].spl_version", maximum_characters=64
        )
        if version in seen_versions:
            raise ConnectorFailure(
                ConnectorErrorCode.CONTRACT_CHANGED,
                detail="DailyMed history contains duplicate SPL versions",
            )
        seen_versions.add(version)
        records.append(
            DailyMedHistoryRecord(
                set_id=set_id,
                spl_version=version,
                published_date=required_text(
                    item.get("published_date"),
                    field=f"data.history[{index}].published_date",
                    maximum_characters=64,
                ),
                title=title,
                document_locator=(
                    f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
                ),
            )
        )
    return DailyMedMetadataBatch(
        records=tuple(records),
        etag=response_validator(response.headers, "etag"),
        last_modified=response_validator(response.headers, "last-modified"),
        whole_response_sha256=response_digest(response),
    )


class DailyMedConnector:
    connector_id = "dailymed-metadata-v1"
    connector_version = "1.0.0"
    parser_id = "dailymed-spl-history-json"
    parser_version = "1.0.0"
    endpoint_policy = DAILYMED_HISTORY_ENDPOINT

    def discover_history(
        self,
        set_id: DailyMedSetId,
        *,
        transport: MetadataTransport,
    ) -> DailyMedMetadataBatch:
        return parse_dailymed_history(
            transport.execute(build_dailymed_history_request(set_id), self.endpoint_policy)
        )

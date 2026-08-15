from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from continucare.knowledge.ops.source_connectors import (
    DAILYMED_HISTORY_ENDPOINT,
    EMA_MEDICINES_ENDPOINT,
    MEDLINEPLUS_TOPICS_ENDPOINT,
    PMC_OA_ENDPOINT,
    PUBMED_ESUMMARY_ENDPOINT,
    ConnectorErrorCode,
    ConnectorFailure,
    DailyMedConnector,
    DailyMedSetId,
    EmaConnector,
    EmaDataset,
    FakeMetadataTransport,
    MetadataResponse,
    MedlinePlusConnector,
    MedlinePlusFeed,
    Pmcid,
    Pmid,
    PubMedPmcConnector,
    build_dailymed_history_request,
    build_ema_dataset_request,
    build_medlineplus_topics_request,
    build_pmc_oa_request,
    build_pubmed_summary_request,
)
from continucare.knowledge.ops.source_connectors.contracts import (
    ControlledRequest,
    validate_controlled_request,
)


SET_ID = "00000000-0000-0000-0000-000000000001"


def _response(endpoint_id: str, media_type: str, body: bytes) -> MetadataResponse:
    return MetadataResponse(
        endpoint_id=endpoint_id,
        status=200,
        media_type=media_type,
        charset="utf-8",
        headers={"etag": '"synthetic-v1"', "last-modified": "Thu, 14 Aug 2026 00:00:00 GMT"},
        body=body,
    )


def test_source_specific_builders_emit_only_exact_official_contracts() -> None:
    daily = build_dailymed_history_request(DailyMedSetId(value=SET_ID))
    ema = build_ema_dataset_request(EmaDataset())
    medline = build_medlineplus_topics_request(
        MedlinePlusFeed(publication_date=date(2026, 8, 14))
    )
    pubmed = build_pubmed_summary_request(Pmid(value="12345678"))
    pmc = build_pmc_oa_request(Pmcid(value="PMC1234567"))

    assert validate_controlled_request(daily, DAILYMED_HISTORY_ENDPOINT)[0] == "dailymed.nlm.nih.gov"
    assert validate_controlled_request(ema, EMA_MEDICINES_ENDPOINT)[1].endswith(".json")
    assert validate_controlled_request(medline, MEDLINEPLUS_TOPICS_ENDPOINT)[1].endswith(".xml")
    assert dict(validate_controlled_request(pubmed, PUBMED_ESUMMARY_ENDPOINT)[2])["id"] == "12345678"
    assert dict(validate_controlled_request(pmc, PMC_OA_ENDPOINT)[2]) == {"id": "PMC1234567"}


@pytest.mark.parametrize(
    ("model", "value"),
    [
        (DailyMedSetId, "00000000-0000-0000-0000-00000000000A"),
        (DailyMedSetId, "patient has nausea"),
        (Pmid, "PMID123"),
        (Pmid, "123%252F456"),
        (Pmid, "１２３４"),
        (Pmcid, "pmc123"),
        (Pmcid, "PMC0"),
        (Pmcid, "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/"),
    ],
)
def test_typed_official_identifiers_reject_free_text_encoding_and_confusables(
    model: type, value: str
) -> None:
    with pytest.raises(ValidationError):
        model(value=value)


@pytest.mark.parametrize(
    "url",
    [
        f"http://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{SET_ID}/history.json",
        f"https://user@dailymed.nlm.nih.gov/dailymed/services/v2/spls/{SET_ID}/history.json",
        f"https://dailymed.nlm.nih.gov:444/dailymed/services/v2/spls/{SET_ID}/history.json",
        f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{SET_ID}/history.json#x",
        f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{SET_ID}/label.json",
    ],
)
def test_endpoint_origin_and_path_are_fail_closed(url: str) -> None:
    request = ControlledRequest(
        endpoint_id=DAILYMED_HISTORY_ENDPOINT.endpoint_id,
        url=url,
        query_identity=f"setid-{SET_ID}",
        accept="application/json",
    )
    with pytest.raises(ConnectorFailure) as raised:
        validate_controlled_request(request, DAILYMED_HISTORY_ENDPOINT)
    assert raised.value.code in {
        ConnectorErrorCode.ENDPOINT_NOT_ALLOWED,
        ConnectorErrorCode.PATH_NOT_ALLOWED,
    }


def test_query_values_are_revalidated_at_transport_boundary() -> None:
    request = ControlledRequest(
        endpoint_id=PUBMED_ESUMMARY_ENDPOINT.endpoint_id,
        url=(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            "?db=pubmed&id=patient%20has%20pain&retmode=json&tool=continucare_knowledge"
        ),
        query_identity="pmid-123",
        accept="application/json",
    )
    with pytest.raises(ConnectorFailure) as raised:
        validate_controlled_request(request, PUBMED_ESUMMARY_ENDPOINT)
    assert raised.value.code == ConnectorErrorCode.UNSAFE_QUERY


@pytest.mark.parametrize("encoded_id", ["%31%32%33", "%2531%2532%2533"])
def test_percent_encoded_official_ids_are_rejected_before_capture(encoded_id: str) -> None:
    request = ControlledRequest(
        endpoint_id=PUBMED_ESUMMARY_ENDPOINT.endpoint_id,
        url=(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            f"?db=pubmed&id={encoded_id}&retmode=json&tool=continucare_knowledge"
        ),
        query_identity="pmid-123",
        accept="application/json",
    )
    with pytest.raises(ConnectorFailure) as raised:
        validate_controlled_request(request, PUBMED_ESUMMARY_ENDPOINT)
    assert raised.value.code == ConnectorErrorCode.UNSAFE_QUERY


def test_daily_med_connector_parses_metadata_without_label_text() -> None:
    body = json.dumps(
        {
            "data": {
                "spl": {"setid": SET_ID, "title": "Synthetic medicine label metadata"},
                "history": [
                    {"spl_version": "2", "published_date": "2026-08-01"},
                    {"spl_version": "1", "published_date": "2026-07-01"},
                ],
            }
        }
    ).encode()
    transport = FakeMetadataTransport(
        {DAILYMED_HISTORY_ENDPOINT.endpoint_id: _response(DAILYMED_HISTORY_ENDPOINT.endpoint_id, "application/json", body)}
    )
    result = DailyMedConnector().discover_history(
        DailyMedSetId(value=SET_ID), transport=transport
    )
    assert [item.spl_version for item in result.records] == ["2", "1"]
    assert result.contains_label_text is False
    assert result.etag == '"synthetic-v1"'
    assert len(transport.captured_requests) == 1


def test_ema_connector_parses_documented_website_dataset_shape() -> None:
    body = json.dumps(
        [
            {
                "ema_product_number": "EMEA-H-C-000001",
                "name_of_medicine": "Synthetic medicine",
                "active_substance": "Synthetic substance",
                "medicine_status": "authorised",
                "revision_number": "7",
                "medicine_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/synthetic",
                "ignored_document_body": "not selected by metadata parser",
            }
        ]
    ).encode()
    transport = FakeMetadataTransport(
        {EMA_MEDICINES_ENDPOINT.endpoint_id: _response(EMA_MEDICINES_ENDPOINT.endpoint_id, "application/json", body)}
    )
    result = EmaConnector().discover_medicines(EmaDataset(), transport=transport)
    assert result.records[0].ema_product_number == "EMEA-H-C-000001"
    assert result.contains_document_text is False


def test_medlineplus_parser_selects_only_topic_attributes() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <health-topics dategenerated="2026-08-14">
      <health-topic id="100" title="Synthetic topic" language="English"
        date-created="2026-08-13" url="https://medlineplus.gov/synthetic.html">
        <full-summary>This body must not enter the parsed record.</full-summary>
      </health-topic>
    </health-topics>"""
    transport = FakeMetadataTransport(
        {MEDLINEPLUS_TOPICS_ENDPOINT.endpoint_id: _response(MEDLINEPLUS_TOPICS_ENDPOINT.endpoint_id, "application/xml", body)}
    )
    result = MedlinePlusConnector().discover_topics(
        MedlinePlusFeed(publication_date=date(2026, 8, 14)), transport=transport
    )
    assert result.records[0].topic_id == "100"
    assert result.records[0].includes_patient_facing_body is False
    assert "full-summary" not in result.model_dump_json()


def test_pubmed_and_pmc_are_separate_metadata_and_rights_models() -> None:
    pubmed_body = json.dumps(
        {
            "result": {
                "uids": ["12345"],
                "12345": {
                    "title": "Synthetic bibliographic title",
                    "pubdate": "2026",
                    "fulljournalname": "Synthetic Journal",
                    "abstract": "must not be selected",
                },
            }
        }
    ).encode()
    pmc_body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <OA><records><record id="PMC12345" license="CC BY" retracted="no">
      <link format="tgz" href="https://ftp.ncbi.nlm.nih.gov/synthetic.tar.gz" />
    </record></records></OA>"""
    connector = PubMedPmcConnector()
    pubmed = connector.discover_pubmed(
        Pmid(value="12345"),
        transport=FakeMetadataTransport(
            {PUBMED_ESUMMARY_ENDPOINT.endpoint_id: _response(PUBMED_ESUMMARY_ENDPOINT.endpoint_id, "application/json", pubmed_body)}
        ),
    )
    pmc = connector.discover_pmc_license_locator(
        Pmcid(value="PMC12345"),
        transport=FakeMetadataTransport(
            {PMC_OA_ENDPOINT.endpoint_id: _response(PMC_OA_ENDPOINT.endpoint_id, "application/xml", pmc_body)}
        ),
    )
    assert pubmed.records[0].abstract_included is False
    assert pubmed.records[0].clinical_conclusion is False
    assert pmc.records[0].full_text_included is False
    assert pmc.records[0].license_requires_item_review is True
    assert pubmed.rights_scope != pmc.rights_scope


def test_fake_transport_captures_nothing_when_builder_input_is_untyped() -> None:
    transport = FakeMetadataTransport({})
    with pytest.raises(ConnectorFailure) as raised:
        DailyMedConnector().discover_history("patient has nausea", transport=transport)  # type: ignore[arg-type]
    assert raised.value.code == ConnectorErrorCode.UNSAFE_QUERY
    assert transport.captured_requests == []

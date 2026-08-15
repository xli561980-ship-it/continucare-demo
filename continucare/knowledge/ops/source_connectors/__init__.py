"""Offline-testable official metadata connector contracts.

Nothing in this package grants operational acquisition network access.  The
only socket implementation requires an opaque, explicitly issued live-
validation capability and is kept separate from ``AcquisitionService``.
"""

from continucare.knowledge.ops.source_connectors.contracts import (
    ControlledRequest,
    EndpointPolicy,
    FakeMetadataTransport,
    MetadataResponse,
    MetadataTransport,
)
from continucare.knowledge.ops.source_connectors.dailymed import (
    DAILYMED_HISTORY_ENDPOINT,
    DailyMedConnector,
    DailyMedHistoryRecord,
    DailyMedMetadataBatch,
    DailyMedSetId,
    build_dailymed_history_request,
    parse_dailymed_history,
)
from continucare.knowledge.ops.source_connectors.ema import (
    EMA_MEDICINES_ENDPOINT,
    EmaConnector,
    EmaDataset,
    EmaMedicineMetadata,
    EmaMetadataBatch,
    build_ema_dataset_request,
    parse_ema_medicines,
)
from continucare.knowledge.ops.source_connectors.errors import (
    ConnectorErrorCode,
    ConnectorFailure,
    ErrorDisposition,
    disposition_for,
)
from continucare.knowledge.ops.source_connectors.flags import (
    KNOWLEDGE_LIVE_VALIDATION_ENV,
    KnowledgeEgressPermit,
    issue_knowledge_egress_permit,
    knowledge_live_validation_enabled,
)
from continucare.knowledge.ops.source_connectors.medlineplus import (
    MEDLINEPLUS_TOPICS_ENDPOINT,
    MedlinePlusConnector,
    MedlinePlusFeed,
    MedlinePlusMetadataBatch,
    MedlinePlusTopicMetadata,
    build_medlineplus_topics_request,
    parse_medlineplus_topics,
)
from continucare.knowledge.ops.source_connectors.parsing import (
    ParserLimits,
    XmlNode,
    parse_bounded_json,
    parse_bounded_xml,
)
from continucare.knowledge.ops.source_connectors.pubmed import (
    PMC_OA_ENDPOINT,
    PUBMED_ESUMMARY_ENDPOINT,
    PmcOpenAccessBatch,
    PmcOpenAccessLocator,
    Pmcid,
    Pmid,
    PubMedMetadata,
    PubMedMetadataBatch,
    PubMedPmcConnector,
    build_pmc_oa_request,
    build_pubmed_summary_request,
    parse_pmc_oa_locator,
    parse_pubmed_summary,
)
from continucare.knowledge.ops.source_connectors.transport import (
    ProcessSharedRateLimiter,
    SecureMetadataTransport,
)

OFFICIAL_ENDPOINT_POLICIES = (
    DAILYMED_HISTORY_ENDPOINT,
    EMA_MEDICINES_ENDPOINT,
    MEDLINEPLUS_TOPICS_ENDPOINT,
    PUBMED_ESUMMARY_ENDPOINT,
    PMC_OA_ENDPOINT,
)

__all__ = [
    "ControlledRequest",
    "ConnectorErrorCode",
    "ConnectorFailure",
    "DAILYMED_HISTORY_ENDPOINT",
    "DailyMedConnector",
    "DailyMedHistoryRecord",
    "DailyMedMetadataBatch",
    "DailyMedSetId",
    "EMA_MEDICINES_ENDPOINT",
    "EmaConnector",
    "EmaDataset",
    "EmaMedicineMetadata",
    "EmaMetadataBatch",
    "EndpointPolicy",
    "ErrorDisposition",
    "FakeMetadataTransport",
    "KNOWLEDGE_LIVE_VALIDATION_ENV",
    "KnowledgeEgressPermit",
    "MEDLINEPLUS_TOPICS_ENDPOINT",
    "MetadataResponse",
    "MetadataTransport",
    "MedlinePlusConnector",
    "MedlinePlusFeed",
    "MedlinePlusMetadataBatch",
    "MedlinePlusTopicMetadata",
    "OFFICIAL_ENDPOINT_POLICIES",
    "PMC_OA_ENDPOINT",
    "PUBMED_ESUMMARY_ENDPOINT",
    "ParserLimits",
    "PmcOpenAccessBatch",
    "PmcOpenAccessLocator",
    "Pmcid",
    "Pmid",
    "PubMedMetadata",
    "PubMedMetadataBatch",
    "PubMedPmcConnector",
    "ProcessSharedRateLimiter",
    "SecureMetadataTransport",
    "XmlNode",
    "build_dailymed_history_request",
    "build_ema_dataset_request",
    "build_medlineplus_topics_request",
    "build_pmc_oa_request",
    "build_pubmed_summary_request",
    "disposition_for",
    "issue_knowledge_egress_permit",
    "knowledge_live_validation_enabled",
    "parse_bounded_json",
    "parse_bounded_xml",
    "parse_dailymed_history",
    "parse_ema_medicines",
    "parse_medlineplus_topics",
    "parse_pmc_oa_locator",
    "parse_pubmed_summary",
]

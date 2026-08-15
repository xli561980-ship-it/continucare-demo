"""Explicit, non-operational smoke validator for official metadata contracts.

This module has no ledger, Claim, manifest, runtime, or database write path.
It accepts no URL or free-text query arguments.  Response bodies exist only in
an automatically removed temporary directory and are never included in the
JSON report.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from continucare.config import get_settings
from continucare.knowledge.ops.manifests import load_builtin_ops_bundle
from continucare.knowledge.ops.models import NonBlank, SafeId, Sha256, StrictModel
from continucare.knowledge.ops.source_connectors.contracts import (
    ControlledRequest,
    EndpointPolicy,
    MetadataResponse,
)
from continucare.knowledge.ops.source_connectors.dailymed import (
    DAILYMED_HISTORY_ENDPOINT,
    DailyMedSetId,
    build_dailymed_history_request,
    parse_dailymed_history,
)
from continucare.knowledge.ops.source_connectors.ema import (
    EMA_MEDICINES_ENDPOINT,
    EmaDataset,
    build_ema_dataset_request,
    parse_ema_medicines,
)
from continucare.knowledge.ops.source_connectors.errors import (
    ConnectorErrorCode,
    ConnectorFailure,
)
from continucare.knowledge.ops.source_connectors.flags import (
    issue_knowledge_egress_permit,
)
from continucare.knowledge.ops.source_connectors.medlineplus import (
    MEDLINEPLUS_TOPICS_ENDPOINT,
    MedlinePlusFeed,
    build_medlineplus_topics_request,
    parse_medlineplus_topics,
)
from continucare.knowledge.ops.source_connectors.pubmed import (
    PMC_OA_ENDPOINT,
    PUBMED_ESUMMARY_ENDPOINT,
    Pmcid,
    Pmid,
    build_pmc_oa_request,
    build_pubmed_summary_request,
    parse_pmc_oa_locator,
    parse_pubmed_summary,
)
from continucare.knowledge.ops.source_connectors.transport import (
    SecureMetadataTransport,
)


class LiveValidationStatus(StrEnum):
    VALIDATED = "validated"
    ACCESS_BLOCKED = "access_blocked"
    RATE_LIMITED = "rate_limited"
    NETWORK_FAILED = "network_failed"
    CONTRACT_CHANGED = "contract_changed"
    RIGHTS_UNRESOLVED = "rights_unresolved"
    NOT_ATTEMPTED = "not_attempted"


class LiveValidationRecord(StrictModel):
    source: SafeId
    status: LiveValidationStatus
    official_documentation_url: NonBlank
    endpoint_origin: NonBlank
    endpoint_path_template: NonBlank
    timestamp: datetime
    http_status: int | None = Field(default=None, ge=100, le=599)
    normalized_mime: NonBlank | None = None
    byte_count: int | None = Field(default=None, ge=0)
    whole_response_sha256: Sha256 | None = None
    parsed_metadata_record_count: int | None = Field(default=None, ge=0)
    stable_error: ConnectorErrorCode | None = None
    limitations: tuple[NonBlank, ...] = Field(min_length=1)
    response_body_recorded: Literal[False] = False
    ledger_write_performed: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"


class LiveValidationReport(StrictModel):
    validator_id: Literal["continucare-knowledge-live-contract-validator-v1"]
    generated_at: datetime
    request_count: int = Field(ge=0, le=8)
    records: tuple[LiveValidationRecord, ...] = Field(min_length=5, max_length=5)
    contains_response_body: Literal[False] = False
    contains_patient_data: Literal[False] = False
    wrote_knowledge_state: Literal[False] = False
    release_ready: Literal[False] = False
    knowledge_effect: Literal["informational_only"] = "informational_only"
    runtime_authority: Literal["none"] = "none"


class _ValidationTarget:
    def __init__(
        self,
        *,
        source: str,
        endpoint: EndpointPolicy,
        request: ControlledRequest,
        parser: Callable[[MetadataResponse], object],
        limitations: tuple[str, ...],
    ) -> None:
        self.source = source
        self.endpoint = endpoint
        self.request = request
        self.parser = parser
        self.limitations = limitations


def run_live_validation(
    *,
    external_egress_enabled: bool,
    environ: Mapping[str, str] | None = None,
) -> LiveValidationReport:
    generated_at = datetime.now(timezone.utc)
    targets = _fixed_targets(generated_at.date())
    try:
        permit = issue_knowledge_egress_permit(
            external_egress_enabled=external_egress_enabled,
            identity_binding_proven=SecureMetadataTransport.identity_binding_proven,
            environ=environ,
        )
    except ConnectorFailure as exc:
        return LiveValidationReport(
            validator_id="continucare-knowledge-live-contract-validator-v1",
            generated_at=generated_at,
            request_count=0,
            records=tuple(
                _failure_record(
                    target,
                    failure=exc,
                    timestamp=generated_at,
                    status=LiveValidationStatus.NOT_ATTEMPTED,
                    extra_limitation="Live validation feature gates were not satisfied.",
                )
                for target in targets
            ),
        )

    bundle = load_builtin_ops_bundle()
    # The generic transport supports bounded retries for operational contract
    # tests, but the live smoke budget counts physical requests.  Disabling
    # retries here fixes the validator at one request per modeled endpoint:
    # five total, below both the per-source and global P1b limits.
    transport = SecureMetadataTransport(permit=permit, maximum_retries=0)
    records: list[LiveValidationRecord] = []
    request_count = 0
    with tempfile.TemporaryDirectory(prefix="continucare-knowledge-live-") as directory:
        temp_root = Path(directory).resolve(strict=True)
        for target in targets:
            timestamp = datetime.now(timezone.utc)
            try:
                _assert_policy_alignment(bundle, target.endpoint)
                request_count += 1
                if request_count > 8:
                    raise ConnectorFailure(
                        ConnectorErrorCode.FEATURE_DISABLED,
                        detail="global live validation request cap reached",
                    )
                response = transport.execute(target.request, target.endpoint)
                response_path = temp_root / f"{target.source}.response"
                response_path.write_bytes(response.body)
                parsed = target.parser(
                    MetadataResponse(
                        endpoint_id=response.endpoint_id,
                        status=response.status,
                        media_type=response.media_type,
                        charset=response.charset,
                        headers=response.headers,
                        body=response_path.read_bytes(),
                        peer_ip=response.peer_ip,
                    )
                )
                parsed_records = getattr(parsed, "records", ())
                records.append(
                    LiveValidationRecord(
                        source=target.source,
                        status=LiveValidationStatus.VALIDATED,
                        official_documentation_url=target.endpoint.official_documentation_url,
                        endpoint_origin=f"https://{target.endpoint.hostname}",
                        endpoint_path_template=target.endpoint.path_template,
                        timestamp=timestamp,
                        http_status=response.status,
                        normalized_mime=response.media_type,
                        byte_count=len(response.body),
                        whole_response_sha256=hashlib.sha256(response.body).hexdigest(),
                        parsed_metadata_record_count=len(parsed_records),
                        limitations=target.limitations,
                    )
                )
            except ConnectorFailure as exc:
                records.append(
                    _failure_record(
                        target,
                        failure=exc,
                        timestamp=timestamp,
                        status=_status_for_failure(exc.code),
                    )
                )
    return LiveValidationReport(
        validator_id="continucare-knowledge-live-contract-validator-v1",
        generated_at=generated_at,
        request_count=request_count,
        records=tuple(records),
    )


def _fixed_targets(today: date) -> tuple[_ValidationTarget, ...]:
    return (
        _ValidationTarget(
            source="dailymed",
            endpoint=DAILYMED_HISTORY_ENDPOINT,
            request=build_dailymed_history_request(
                DailyMedSetId(value="ee06186f-2aa3-4990-a760-757579d8f77b")
            ),
            parser=parse_dailymed_history,
            limitations=(
                "Metadata contract only; no label text is retained.",
                "Formal reuse rights remain unresolved.",
            ),
        ),
        _ValidationTarget(
            source="ema",
            endpoint=EMA_MEDICINES_ENDPOINT,
            request=build_ema_dataset_request(EmaDataset()),
            parser=parse_ema_medicines,
            limitations=(
                "Only the documented website JSON dataset is allowed.",
                "The dataset may exceed the hard cap; PMS and undocumented APIs are excluded.",
            ),
        ),
        _ValidationTarget(
            source="medlineplus",
            endpoint=MEDLINEPLUS_TOPICS_ENDPOINT,
            request=build_medlineplus_topics_request(
                MedlinePlusFeed(publication_date=today)
            ),
            parser=parse_medlineplus_topics,
            limitations=(
                "Topic attributes only; patient-facing and third-party body text is excluded.",
                "Formal reuse rights remain unresolved.",
            ),
        ),
        _ValidationTarget(
            source="pubmed",
            endpoint=PUBMED_ESUMMARY_ENDPOINT,
            request=build_pubmed_summary_request(Pmid(value="31452104")),
            parser=parse_pubmed_summary,
            limitations=(
                "Bibliographic metadata is not a clinical conclusion.",
                "No API key, personal email, abstract or full text is used.",
            ),
        ),
        _ValidationTarget(
            source="pmc",
            endpoint=PMC_OA_ENDPOINT,
            request=build_pmc_oa_request(Pmcid(value="PMC13901")),
            parser=parse_pmc_oa_locator,
            limitations=(
                "Per-article licence locator only; no full text is fetched or retained.",
                "Legacy OA service retirement is scheduled on or after 2026-08-24.",
            ),
        ),
    )


def _assert_policy_alignment(bundle: object, endpoint: EndpointPolicy) -> None:
    try:
        policy = bundle.source_policy(  # type: ignore[attr-defined]
            endpoint.source_policy_id, endpoint.source_policy_version
        )
    except (AttributeError, KeyError) as exc:
        raise ConnectorFailure(
            ConnectorErrorCode.ENDPOINT_NOT_ALLOWED,
            detail="endpoint has no exact current SourcePolicy",
        ) from exc
    allowed_hosts = {
        urlsplit(str(origin)).hostname for origin in policy.allowed_origins
    }
    if (
        allowed_hosts != {endpoint.hostname}
        or endpoint.path_template not in policy.allowed_path_templates
        or set(endpoint.allowed_query_keys) != set(policy.allowed_query_parameters)
        or set(endpoint.allowed_media_types) != set(policy.allowed_content_types)
        or endpoint.maximum_response_bytes != policy.maximum_response_bytes
        or policy.live_network_enabled is not False
    ):
        raise ConnectorFailure(
            ConnectorErrorCode.ENDPOINT_NOT_ALLOWED,
            detail="endpoint contract differs from its exact SourcePolicy",
        )


def _failure_record(
    target: _ValidationTarget,
    *,
    failure: ConnectorFailure,
    timestamp: datetime,
    status: LiveValidationStatus,
    extra_limitation: str | None = None,
) -> LiveValidationRecord:
    limitations = target.limitations
    if extra_limitation is not None:
        limitations = (*limitations, extra_limitation)
    return LiveValidationRecord(
        source=target.source,
        status=status,
        official_documentation_url=target.endpoint.official_documentation_url,
        endpoint_origin=f"https://{target.endpoint.hostname}",
        endpoint_path_template=target.endpoint.path_template,
        timestamp=timestamp,
        http_status=failure.http_status,
        stable_error=failure.code,
        limitations=limitations,
    )


def _status_for_failure(code: ConnectorErrorCode) -> LiveValidationStatus:
    if code in {ConnectorErrorCode.UNAUTHORIZED, ConnectorErrorCode.FORBIDDEN}:
        return LiveValidationStatus.ACCESS_BLOCKED
    if code == ConnectorErrorCode.RATE_LIMITED:
        return LiveValidationStatus.RATE_LIMITED
    if code in {
        ConnectorErrorCode.DNS_RESOLUTION_FAILED,
        ConnectorErrorCode.NON_PUBLIC_DNS_ANSWER,
        ConnectorErrorCode.PEER_IDENTITY_MISMATCH,
        ConnectorErrorCode.TLS_VERIFICATION_FAILED,
        ConnectorErrorCode.TIMEOUT,
        ConnectorErrorCode.NETWORK_FAILED,
    }:
        return LiveValidationStatus.NETWORK_FAILED
    if code == ConnectorErrorCode.RIGHTS_UNRESOLVED:
        return LiveValidationStatus.RIGHTS_UNRESOLVED
    if code == ConnectorErrorCode.FEATURE_DISABLED:
        return LiveValidationStatus.NOT_ATTEMPTED
    return LiveValidationStatus.CONTRACT_CHANGED


def main() -> int:
    settings = get_settings()
    report = run_live_validation(
        external_egress_enabled=settings.integrations.external_egress_enabled,
        environ=os.environ,
    )
    print(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

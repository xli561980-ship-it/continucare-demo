"""Write-only synthetic Bitable projection contract; SQLite remains authoritative."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import StrEnum
from urllib.parse import urlencode
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from continucare.adapters.feishu.token_provider import TenantTokenProvider, TokenError
from continucare.adapters.http_transport import (
    HttpRequest,
    HttpTransport,
    TransportNetworkError,
    TransportTimeoutError,
)


_APP_TOKEN = re.compile(r"[A-Za-z0-9_-]{4,80}\Z")
_TABLE_ID = re.compile(r"[A-Za-z0-9_-]{4,80}\Z")
_REFERENCE = re.compile(r"(?:task|summary)\.synthetic-[A-Za-z0-9._:-]{1,60}\Z")
BITABLE_BASE = "https://open.feishu.cn/open-apis/bitable/v1/apps"


class ProjectionState(StrEnum):
    ACCEPTED_BY_REMOTE = "accepted_by_remote"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ProjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ProjectionState
    attempted: bool = True
    accepted_by_remote: bool = False
    external_record_id: str | None = None
    failure_reason: str | None = None


class BitableProjectionWriter:
    """Never reads clinical state and never participates in a local transaction."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        token_provider: TenantTokenProvider,
        app_token: str,
        table_id: str,
        idempotency_salt: str,
        timeout_seconds: float = 8.0,
    ):
        if not _APP_TOKEN.fullmatch(app_token):
            raise ValueError("Bitable app token has an invalid format")
        if not _TABLE_ID.fullmatch(table_id):
            raise ValueError("Bitable table id has an invalid format")
        if len(idempotency_salt) < 16:
            raise ValueError("Bitable idempotency salt is too short")
        self.transport = transport
        self.token_provider = token_provider
        self._app_token = app_token
        self._table_id = table_id
        self._idempotency_salt = idempotency_salt.encode()
        self.timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return "BitableProjectionWriter(configuration=[REDACTED])"

    def idempotency_key(self, *, projection_kind: str, synthetic_reference: str) -> str:
        if projection_kind not in {"manual_review", "doctor_brief"}:
            raise ValueError("unsupported Bitable projection kind")
        if not _REFERENCE.fullmatch(synthetic_reference):
            raise ValueError("synthetic reference has an invalid format")
        digest = bytearray(
            hmac.new(
                self._idempotency_salt,
                f"{projection_kind}|{synthetic_reference}".encode(),
                hashlib.sha256,
            ).digest()[:16]
        )
        digest[6] = (digest[6] & 0x0F) | 0x40
        digest[8] = (digest[8] & 0x3F) | 0x80
        return str(UUID(bytes=bytes(digest)))

    def write_synthetic_projection(
        self,
        *,
        projection_kind: str,
        synthetic_reference: str,
        workflow_state: str,
    ) -> ProjectionResult:
        if workflow_state not in {"pending", "ready", "completed"}:
            raise ValueError("unsupported synthetic workflow state")
        client_token = self.idempotency_key(
            projection_kind=projection_kind,
            synthetic_reference=synthetic_reference,
        )
        try:
            token = self.token_provider.get_token()
        except (TokenError, TransportTimeoutError, TransportNetworkError):
            return ProjectionResult(
                state=ProjectionState.FAILED,
                attempted=False,
                failure_reason="authentication_unavailable_no_write_attempt",
            )
        url = (
            f"{BITABLE_BASE}/{self._app_token}/tables/{self._table_id}/records?"
            + urlencode({"client_token": client_token})
        )
        try:
            response = self.transport.send(
                HttpRequest(
                    method="POST",
                    url=url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    body=json.dumps(
                        {
                            "fields": {
                                "Synthetic": True,
                                "ProjectionKind": projection_kind,
                                "OpaqueReference": synthetic_reference,
                                "WorkflowState": workflow_state,
                            }
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode(),
                    timeout_seconds=self.timeout_seconds,
                    max_response_bytes=262_144,
                )
            )
        except (TransportTimeoutError, TransportNetworkError):
            return ProjectionResult(
                state=ProjectionState.OUTCOME_UNKNOWN,
                failure_reason="remote_outcome_unknown_no_retry",
            )
        if response.status != 200:
            return ProjectionResult(
                state=ProjectionState.FAILED,
                failure_reason="remote_http_rejected",
            )
        try:
            payload = response.json_object()
            record = payload.get("data", {}).get("record")
            record_id = record.get("record_id") if isinstance(record, dict) else None
            accepted = payload.get("code") == 0 and isinstance(record_id, str) and bool(record_id)
        except ValueError:
            accepted = False
            record_id = None
        return ProjectionResult(
            state=(
                ProjectionState.ACCEPTED_BY_REMOTE if accepted else ProjectionState.FAILED
            ),
            accepted_by_remote=accepted,
            external_record_id=record_id if accepted else None,
            failure_reason=None if accepted else "remote_platform_rejected",
        )


# The old placeholder name stays importable but cannot become a second store.
BitableStore = BitableProjectionWriter

"""Feishu Bot card contract with explicit, non-retryable send outcomes."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict

from continucare.adapters.http_transport import (
    HttpRequest,
    HttpTransport,
    TransportNetworkError,
    TransportTimeoutError,
)
from continucare.adapters.feishu.token_provider import TenantTokenProvider, TokenError
from continucare.services.audit import record_audit_event


MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_REFERENCE = re.compile(r"(?:task|summary)\.synthetic-[A-Za-z0-9._:-]{1,60}\Z")
_RECEIVE_IDS = {
    "open_id": re.compile(r"ou_[A-Za-z0-9]{4,64}\Z"),
    "chat_id": re.compile(r"oc_[A-Za-z0-9]{4,64}\Z"),
    "user_id": re.compile(r"[A-Za-z0-9._-]{1,64}\Z"),
    "union_id": re.compile(r"on_[A-Za-z0-9]{4,64}\Z"),
    "email": re.compile(r"[^@\s]{1,64}@[^@\s]{1,128}\Z"),
}


class NotificationState(StrEnum):
    RENDERED = "rendered"
    EXTERNAL_REQUEST_PREPARED = "external_request_prepared"
    EXTERNAL_ATTEMPTED = "external_attempted"
    ACCEPTED_BY_REMOTE = "accepted_by_remote"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class RenderedCard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    card_kind: str
    synthetic_reference: str
    content: dict


class NotificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: NotificationState
    rendered: bool
    send_attempted: bool
    accepted_by_remote: bool
    remote_request_id: str | None = None
    failure_reason: str | None = None

    @property
    def delivery_confirmed(self) -> bool:
        return False


class AttemptRecorder(Protocol):
    def record(self, result: NotificationResult) -> None: ...


class AuditAttemptRecorder:
    """Append-only, recipient-free audit sink for a future explicit send action."""

    def __init__(self, store, *, patient_id: str, entity_id: str):
        self.store = store
        self.patient_id = patient_id
        self.entity_id = entity_id

    def record(self, result: NotificationResult) -> None:
        record_audit_event(
            self.store,
            patient_id=self.patient_id,
            entity_type="ExternalNotificationAttempt",
            entity_id=self.entity_id,
            event_type=f"external_notification_{result.state.value}",
            actor_type="feishu_bot_adapter",
            details={
                "state": result.state.value,
                "rendered": result.rendered,
                "send_attempted": result.send_attempted,
                "accepted_by_remote": result.accepted_by_remote,
                "delivery_confirmed": False,
                "remote_request_id": result.remote_request_id,
                "failure_reason": result.failure_reason,
            },
        )


def render_synthetic_card(*, card_kind: str, synthetic_reference: str) -> RenderedCard:
    if card_kind not in {"nurse_manual_review", "doctor_brief_ready"}:
        raise ValueError("unsupported synthetic notification card kind")
    if not _REFERENCE.fullmatch(synthetic_reference):
        raise ValueError("synthetic reference has an invalid format")
    title = (
        "ContinuCare 合成人工复核提醒"
        if card_kind == "nurse_manual_review"
        else "ContinuCare 合成复诊简报提醒"
    )
    return RenderedCard(
        card_kind=card_kind,
        synthetic_reference=synthetic_reference,
        content={
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "**仅限合成演示**\n"
                        "这是一条非诊断性工作流提醒，不包含患者身份、风险分级或治疗建议。"
                    ),
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"本地不透明引用：{synthetic_reference}",
                        }
                    ],
                },
            ],
        },
    )


class FeishuBotNotifier:
    """Standalone test-tenant adapter; not wired to manual Communication."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        token_provider: TenantTokenProvider,
        receive_id_type: str,
        receive_id: str,
        attempt_recorder: AttemptRecorder,
        timeout_seconds: float = 8.0,
    ):
        pattern = _RECEIVE_IDS.get(receive_id_type)
        if pattern is None or not pattern.fullmatch(receive_id):
            raise ValueError("receive_id does not match the selected receive_id_type")
        self.transport = transport
        self.token_provider = token_provider
        self.receive_id_type = receive_id_type
        self._receive_id = receive_id
        self.attempt_recorder = attempt_recorder
        self.timeout_seconds = timeout_seconds
        self._consumed_action_tokens: set[str] = set()

    def __repr__(self) -> str:
        return (
            "FeishuBotNotifier(receive_id=[REDACTED], "
            f"receive_id_type={self.receive_id_type!r})"
        )

    def send_card(self, card: RenderedCard, *, action_token: str) -> NotificationResult:
        token_digest = hashlib.sha256(action_token.encode()).hexdigest()
        if not action_token or token_digest in self._consumed_action_tokens:
            raise ValueError("send action token is missing or already consumed")
        self._consumed_action_tokens.add(token_digest)
        self.attempt_recorder.record(
            NotificationResult(
                state=NotificationState.EXTERNAL_REQUEST_PREPARED,
                rendered=True,
                send_attempted=False,
                accepted_by_remote=False,
            )
        )
        try:
            token = self.token_provider.get_token()
        except (TokenError, TransportTimeoutError, TransportNetworkError):
            result = NotificationResult(
                state=NotificationState.FAILED,
                rendered=True,
                send_attempted=False,
                accepted_by_remote=False,
                failure_reason="authentication_unavailable_no_send_attempt",
            )
            self.attempt_recorder.record(result)
            return result
        self.attempt_recorder.record(
            NotificationResult(
                state=NotificationState.EXTERNAL_ATTEMPTED,
                rendered=True,
                send_attempted=True,
                accepted_by_remote=False,
            )
        )
        try:
            response = self.transport.send(
                HttpRequest(
                    method="POST",
                    url=f"{MESSAGE_URL}?{urlencode({'receive_id_type': self.receive_id_type})}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    body=json.dumps(
                        {
                            "receive_id": self._receive_id,
                            "msg_type": "interactive",
                            "content": json.dumps(
                                card.content,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode(),
                    timeout_seconds=self.timeout_seconds,
                    max_response_bytes=262_144,
                )
            )
        except (TransportTimeoutError, TransportNetworkError):
            result = NotificationResult(
                state=NotificationState.OUTCOME_UNKNOWN,
                rendered=True,
                send_attempted=True,
                accepted_by_remote=False,
                failure_reason="remote_outcome_unknown_no_retry",
            )
            self.attempt_recorder.record(result)
            return result
        if response.status != 200:
            result = NotificationResult(
                state=NotificationState.FAILED,
                rendered=True,
                send_attempted=True,
                accepted_by_remote=False,
                failure_reason="remote_http_rejected",
            )
            self.attempt_recorder.record(result)
            return result
        try:
            payload = response.json_object()
            accepted = (
                payload.get("code") == 0
                and isinstance(payload.get("data"), dict)
                and isinstance(payload["data"].get("message_id"), str)
                and bool(payload["data"]["message_id"])
            )
        except ValueError:
            accepted = False
        result = NotificationResult(
            state=(
                NotificationState.ACCEPTED_BY_REMOTE
                if accepted
                else NotificationState.FAILED
            ),
            rendered=True,
            send_attempted=True,
            accepted_by_remote=accepted,
            remote_request_id=(
                response.headers.get("X-Tt-Logid")
                or response.headers.get("X-Request-Id")
            ),
            failure_reason=None if accepted else "remote_platform_rejected",
        )
        self.attempt_recorder.record(result)
        return result

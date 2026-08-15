"""Aily OpenAPI protocol adapter that still feeds the local Layer-3 gates."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from continucare.adapters.feishu.token_provider import TenantTokenProvider
from continucare.adapters.http_transport import HttpRequest, HttpTransport
from continucare.agents.contracts import (
    CandidateSource,
    ReportedSymptomMention,
    SemanticCandidate,
    SemanticResult,
    SemanticStatus,
    SemanticTask,
    SubjectType,
    Temporality,
)
from continucare.care_agent.safety import instruction_like_text
from continucare.care_agent.model_api import SemanticModelConfig
from continucare.fhir.terminology import UCUM
from continucare.db import utc_now_iso
from continucare.demo_data import DEMO_PATIENT_ID


AILY_BASE = "https://open.feishu.cn/open-apis/aily/v1/sessions"
_SESSION_ID = re.compile(r"session_[0-9a-hjkmnp-z]{1,24}\Z")
_MESSAGE_ID = re.compile(r"message_[0-9a-hjkmnp-z]{1,24}\Z")
_RUN_ID = re.compile(r"run_[0-9a-hjkmnp-z]{1,28}\Z")
_APP_ID = re.compile(r"[A-Za-z0-9_:-]{1,80}\Z")
_SKILL_ID = re.compile(r"[A-Za-z0-9_:-]{1,80}\Z")


class AilyAdapterError(RuntimeError):
    """Stable fallback-triggering error without third-party response text."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _RawItem(_StrictModel):
    link_id: str = Field(min_length=1, max_length=120)
    answer: Any
    evidence_text: str = Field(min_length=1, max_length=1000)
    subject: SubjectType
    temporality: Temporality
    negated: bool


class _RawMention(_StrictModel):
    symptom_text: str = Field(min_length=1, max_length=200)
    evidence_text: str = Field(min_length=1, max_length=1000)
    subject: SubjectType
    temporality: Temporality
    negated: bool


class _RawOutput(_StrictModel):
    blocked: bool = False
    items: list[_RawItem] = Field(default_factory=list, max_length=20)
    symptom_mentions: list[_RawMention] = Field(default_factory=list, max_length=20)


class AilySemanticAdapter:
    """Official session/message/run protocol with unverified live output shape."""

    VERSION = "feishu-aily-openapi-v1-contract-only"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        token_provider: TenantTokenProvider,
        app_id: str,
        skill_id: str | None = None,
        timeout_seconds: float = 8.0,
        max_polls: int = 4,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if not _APP_ID.fullmatch(app_id):
            raise ValueError("Aily app_id has an invalid format")
        if skill_id is not None and not _SKILL_ID.fullmatch(skill_id):
            raise ValueError("Aily skill_id has an invalid format")
        if not 1 <= max_polls <= 10:
            raise ValueError("Aily max_polls is outside the allowed range")
        self.transport = transport
        self.token_provider = token_provider
        self.app_id = app_id
        self.skill_id = skill_id
        self.timeout_seconds = timeout_seconds
        self.max_polls = max_polls
        self.sleeper = sleeper
        self.config = SemanticModelConfig(
            provider="feishu_aily",
            model_name="aily_app_unverified",
            prompt_version="aily-semantic-candidates-v1",
            timeout_seconds=timeout_seconds,
        )

    @property
    def configured(self) -> bool:
        return True

    def extract(self, task: SemanticTask) -> SemanticResult:
        if task.patient_id != DEMO_PATIENT_ID:
            raise AilyAdapterError("Aily test-tenant adapter accepts synthetic demo data only")
        session_id = self._create_session()
        self._create_message(session_id, task)
        run_id = self._create_run(session_id, task)
        self._wait_for_run(session_id, run_id)
        raw = self._read_completed_output(session_id, run_id)
        return self._to_semantic_result(task, raw, run_id)

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self.token_provider.get_token()
        response = self.transport.send(
            HttpRequest(
                method=method,
                url=url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                body=(
                    json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                    if body is not None
                    else None
                ),
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=524_288,
            )
        )
        if response.status != 200:
            raise AilyAdapterError("Aily request was rejected")
        payload = response.json_object()
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise AilyAdapterError("Aily returned a platform error")
        return payload["data"]

    def _create_session(self) -> str:
        data = self._request(
            "POST",
            AILY_BASE,
            body={
                "channel_context": json.dumps(
                    {"channel": "continucare_synthetic_contract_test"},
                    separators=(",", ":"),
                ),
                "metadata": json.dumps(
                    {"data_class": "synthetic_only"}, separators=(",", ":")
                ),
            },
        )
        session = data.get("session")
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise AilyAdapterError("Aily session response failed validation")
        return session_id

    def _create_message(self, session_id: str, task: SemanticTask) -> None:
        content = json.dumps(_minimal_task_payload(task), ensure_ascii=False, separators=(",", ":"))
        message_uuid = str(uuid5(NAMESPACE_URL, f"aily-message|{task.task_id}"))
        data = self._request(
            "POST",
            f"{AILY_BASE}/{session_id}/messages",
            body={
                "idempotent_id": message_uuid,
                "content_type": "MDX",
                "content": content,
            },
        )
        message = data.get("message")
        message_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
            raise AilyAdapterError("Aily message response failed validation")

    def _create_run(self, session_id: str, task: SemanticTask) -> str:
        body = {
            "app_id": self.app_id,
            "skill_input": json.dumps(
                {
                    "contract": "continucare_semantic_candidates_v1",
                    "rules": (
                        "Return one JSON object only. Do not return diagnosis, risk, "
                        "treatment, approval, priority, or code fields."
                    ),
                },
                separators=(",", ":"),
            ),
            "metadata": json.dumps(
                {"request_id": str(uuid5(NAMESPACE_URL, task.task_id))},
                separators=(",", ":"),
            ),
        }
        if self.skill_id:
            body["skill_id"] = self.skill_id
        data = self._request("POST", f"{AILY_BASE}/{session_id}/runs", body=body)
        run = data.get("run")
        run_id = run.get("id") if isinstance(run, dict) else None
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise AilyAdapterError("Aily run response failed validation")
        return run_id

    def _wait_for_run(self, session_id: str, run_id: str) -> None:
        for index in range(self.max_polls):
            data = self._request("GET", f"{AILY_BASE}/{session_id}/runs/{run_id}")
            run = data.get("run")
            status = run.get("status") if isinstance(run, dict) else None
            if status == "COMPLETED":
                return
            if status in {"FAILED", "CANCELLED", "EXPIRED"} or not isinstance(status, str):
                raise AilyAdapterError("Aily run did not complete successfully")
            if status not in {"PENDING", "QUEUED", "IN_PROGRESS"}:
                raise AilyAdapterError("Aily run returned an unknown status")
            if index + 1 < self.max_polls:
                self.sleeper(0.05)
        raise AilyAdapterError("Aily run polling limit was reached")

    def _read_completed_output(self, session_id: str, run_id: str) -> _RawOutput:
        query = urlencode(
            {"run_id": run_id, "with_partial_message": "false", "page_size": "20"}
        )
        data = self._request("GET", f"{AILY_BASE}/{session_id}/messages?{query}")
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise AilyAdapterError("Aily message list failed validation")
        completed = []
        for message in messages:
            if not isinstance(message, dict) or message.get("run_id") != run_id:
                continue
            sender = message.get("sender")
            if (
                message.get("status") == "COMPLETED"
                and isinstance(sender, dict)
                and sender.get("sender_type") in {"AILY", "ASSISTANT"}
            ):
                completed.append(message)
        if len(completed) != 1:
            raise AilyAdapterError("Aily completed assistant message was ambiguous")
        message = completed[0]
        content = message.get("plain_text") or message.get("content")
        if not isinstance(content, str) or not 1 <= len(content) <= 65_536:
            raise AilyAdapterError("Aily output content failed validation")
        if instruction_like_text(content):
            raise AilyAdapterError("Aily output was blocked by local safety preflight")
        try:
            decoded = json.loads(content)
            return _RawOutput.model_validate(decoded)
        except (json.JSONDecodeError, ValidationError, TypeError):
            raise AilyAdapterError("Aily structured output failed validation") from None

    def _to_semantic_result(
        self, task: SemanticTask, raw: _RawOutput, provider_run_id: str
    ) -> SemanticResult:
        run_id = f"run-{uuid5(NAMESPACE_URL, task.task_id + '|' + self.VERSION).hex}"
        if raw.blocked:
            return SemanticResult(
                run_id=run_id,
                task_id=task.task_id,
                status=SemanticStatus.BLOCKED,
                mode="model_api:feishu_aily_not_live_verified",
                care_agent_version=self.VERSION,
                safety_agent_version="pending",
                language_policy_version="local-fixed-template-v1",
                ignored_reasons=["aily_output_blocked_local_fallback_required"],
                provider_request_id=provider_run_id,
                completed_at=utc_now_iso(),
            )
        allowed = {item.link_id: item for item in task.allowed_items}
        candidates: list[SemanticCandidate] = []
        for index, item in enumerate(raw.items):
            question = allowed.get(item.link_id)
            if question is None:
                raise AilyAdapterError("Aily returned an unknown questionnaire field")
            answer = _locally_validate_answer(question, item.answer)
            start = task.message_text.find(item.evidence_text)
            if start < 0:
                raise AilyAdapterError("Aily evidence was not verbatim patient text")
            candidates.append(
                SemanticCandidate(
                    candidate_id=(
                        "candidate-"
                        + uuid5(
                            NAMESPACE_URL,
                            f"{task.task_id}|aily|{index}|{item.link_id}",
                        ).hex
                    ),
                    link_id=item.link_id,
                    answer=answer,
                    questionnaire_code=question.codes[0] if question.codes else None,
                    evidence_text=item.evidence_text,
                    evidence_start=start,
                    evidence_end=start + len(item.evidence_text),
                    subject=item.subject,
                    temporality=item.temporality,
                    negated=item.negated,
                    requires_patient_confirmation=True,
                    patient_message=(
                        f"系统只整理到原话“{item.evidence_text}”；请确认这项记录是否正确。"
                    ),
                    template_id="confirm_aily_candidate_local_template",
                    source_mode=CandidateSource.AILY,
                )
            )
        mentions: list[ReportedSymptomMention] = []
        for index, mention in enumerate(raw.symptom_mentions):
            start = task.message_text.find(mention.evidence_text)
            if start < 0:
                raise AilyAdapterError("Aily symptom evidence was not verbatim patient text")
            mentions.append(
                ReportedSymptomMention(
                    mention_id=(
                        "symptom-mention-"
                        + uuid5(
                            NAMESPACE_URL,
                            f"{task.task_id}|aily-mention|{index}",
                        ).hex
                    ),
                    symptom_text=mention.symptom_text,
                    evidence_text=mention.evidence_text,
                    evidence_start=start,
                    evidence_end=start + len(mention.evidence_text),
                    subject=mention.subject,
                    temporality=mention.temporality,
                    negated=mention.negated,
                    source_mode=CandidateSource.AILY,
                )
            )
        status = (
            SemanticStatus.NEEDS_CONFIRMATION
            if candidates or mentions
            else SemanticStatus.NO_MATCH
        )
        return SemanticResult(
            run_id=run_id,
            task_id=task.task_id,
            status=status,
            mode="model_api:feishu_aily_not_live_verified",
            care_agent_version=self.VERSION,
            safety_agent_version="pending",
            language_policy_version="local-fixed-template-v1",
            candidates=candidates,
            reported_symptom_mentions=mentions,
            provider_request_id=provider_run_id,
            completed_at=utc_now_iso(),
        )


def _minimal_task_payload(task: SemanticTask) -> dict[str, Any]:
    return {
        "contract": "continucare_semantic_candidates_v1",
        "message_text": task.message_text,
        "allowed_items": [
            {
                "link_id": item.link_id,
                "item_type": item.item_type,
                "answer_options": [option.code for option in item.answer_options],
            }
            for item in task.allowed_items
        ],
        "output_schema": {
            "blocked": "boolean",
            "items": [
                {
                    "link_id": "allowed link_id only",
                    "answer": "value only; never code",
                    "evidence_text": "verbatim substring",
                    "subject": "patient|other_person|unknown",
                    "temporality": "current|explicit_24h|unspecified|historical",
                    "negated": "boolean",
                }
            ],
            "symptom_mentions": [],
        },
    }


def _locally_validate_answer(question, value: Any) -> Any:
    """Whitelist Aily values and reconstruct any coded quantity locally."""

    if question.item_type == "choice":
        allowed = {option.code for option in question.answer_options}
        if not isinstance(value, str) or value not in allowed:
            raise AilyAdapterError("Aily answer was outside the local value set")
        return value
    if question.item_type == "boolean":
        if type(value) is not bool:
            raise AilyAdapterError("Aily boolean answer failed local validation")
        return value
    if question.item_type == "integer":
        if type(value) is not int or value < 0:
            raise AilyAdapterError("Aily integer answer failed local validation")
        return value
    if question.item_type == "quantity":
        if type(value) not in {int, float} or value < 0:
            raise AilyAdapterError("Aily quantity answer failed local validation")
        return {"value": value, "unit": "mL", "system": UCUM, "code": "mL"}
    raise AilyAdapterError("Aily returned an unsupported questionnaire item type")


# Backward-compatible name for the old placeholder import.
AilyExtractor = AilySemanticAdapter

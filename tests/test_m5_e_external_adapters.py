from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

import pytest

from continucare.adapters.factory import (
    AdapterConfigurationError,
    AdapterFactory,
    EnvironmentSecretProvider,
)
from continucare.adapters.feishu.aily_extractor import (
    AilyAdapterError,
    AilySemanticAdapter,
)
from continucare.adapters.feishu.bitable_store import (
    BitableProjectionWriter,
    ProjectionState,
)
from continucare.adapters.feishu.bot_notifier import (
    FeishuBotNotifier,
    NotificationState,
    render_synthetic_card,
)
from continucare.adapters.feishu.token_provider import TenantTokenProvider, TokenError
from continucare.adapters.http_transport import (
    FakeTransport,
    HttpRequest,
    HttpResponse,
    SecretRedactor,
    StdlibHttpTransport,
    TransportInvalidJson,
    TransportNetworkError,
    TransportPolicyError,
    TransportResponseTooLarge,
    TransportTimeoutError,
    issue_egress_permit,
    json_response,
)
from continucare.adapters.sqlite_store import SQLiteStore
from continucare.agents.contracts import CandidateSource
from continucare.care_agent.agent import CareSemanticAgent
from continucare.care_agent.model_api import SemanticModelConfig, UnconfiguredModelAdapter
from continucare.care_agent.service import CareAgentService
from continucare.care_engine import CareEngine
from continucare.config import AdapterMode, IntegrationSettings
from continucare.demo_data import DEMO_PATIENT_ID


FULL_ENV = {
    "CONTINUCARE_FEISHU_APP_ID": "cli_fake_app",
    "CONTINUCARE_FEISHU_APP_SECRET": "fake-secret-never-live",
    "CONTINUCARE_FEISHU_RECEIVE_ID_TYPE": "open_id",
    "CONTINUCARE_FEISHU_RECEIVE_ID": "ou_fake1234",
    "CONTINUCARE_AILY_APP_ID": "aily_fake_app",
    "CONTINUCARE_AILY_SKILL_ID": "skill_fake",
    "CONTINUCARE_BITABLE_APP_TOKEN": "app_fake_token",
    "CONTINUCARE_BITABLE_TABLE_ID": "tbl_fake_table",
    "CONTINUCARE_BITABLE_IDEMPOTENCY_SALT": "synthetic-salt-at-least-16",
}


@dataclass
class StaticTokenProvider:
    token: str = "tenant-fake-token"

    def get_token(self) -> str:
        return self.token


class FailingTokenProvider:
    def get_token(self) -> str:
        raise TransportTimeoutError("synthetic token timeout")


class Recorder:
    def __init__(self):
        self.results = []

    def record(self, result):
        self.results.append(result)


def _settings(**updates) -> IntegrationSettings:
    values = IntegrationSettings().__dict__ | updates
    return IntegrationSettings(**values)


def _semantic_task(tmp_path, text="过去24小时呕吐了2次。"):
    store = SQLiteStore(tmp_path / "task.db")
    engine = CareEngine(store)
    session = engine.start_or_resume(DEMO_PATIENT_ID)
    service = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
    )
    return service._build_task(
        session,
        engine.questionnaire_for_session(session),
        text,
        task_id="semantic-m5e-fake",
        received_at="2026-08-13T08:00:00+00:00",
    )


def _aily_responses(output):
    return [
        json_response(
            {"code": 0, "tenant_access_token": "tenant-fake-token", "expire": 7200}
        ),
        json_response({"code": 0, "data": {"session": {"id": "session_abcd"}}}),
        json_response({"code": 0, "data": {"message": {"id": "message_abcd"}}}),
        json_response({"code": 0, "data": {"run": {"id": "run_abcd"}}}),
        json_response({"code": 0, "data": {"run": {"status": "COMPLETED"}}}),
        json_response(
            {
                "code": 0,
                "data": {
                    "messages": [
                        {
                            "run_id": "run_abcd",
                            "status": "COMPLETED",
                            "sender": {"sender_type": "AILY"},
                            "plain_text": json.dumps(output, ensure_ascii=False),
                        }
                    ]
                },
            }
        ),
    ]


def _aily_adapter(responses):
    fake = FakeTransport(
        responses,
        redactor=SecretRedactor("fake-secret-never-live", "tenant-fake-token"),
    )
    token_provider = TenantTokenProvider(
        fake,
        secrets=EnvironmentSecretProvider(FULL_ENV),
        now=lambda: 0,
    )
    return (
        AilySemanticAdapter(
            fake,
            token_provider=token_provider,
            app_id="aily_fake_app",
            skill_id="skill_fake",
            sleeper=lambda _: None,
        ),
        fake,
    )


def test_default_and_disabled_modes_are_zero_token_offline_fallbacks():
    statuses = AdapterFactory(
        _settings(),
        environment={
            "CONTINUCARE_FEISHU_APP_SECRET": "an-accidental-secret-must-not-enable"
        },
    ).statuses()

    assert statuses["feishu"].selected_mode == "mock"
    assert statuses["feishu"].external_calls_allowed is False
    assert statuses["feishu"].fallback_reason == "default_offline_mock"
    assert statuses["aily"].implementation == "deterministic_semantic_mock"
    assert statuses["bitable"].selected_mode == "disabled"
    assert all(item.live_tenant_verified is False for item in statuses.values())
    assert all(item.production_ready is False for item in statuses.values())


def test_empty_secret_is_missing_and_test_tenant_fails_closed():
    settings = _settings(
        feishu_mode=AdapterMode.TEST_TENANT.value,
        external_egress_enabled=True,
        feishu_test_tenant_enabled=True,
    )
    environment = FULL_ENV | {"CONTINUCARE_FEISHU_APP_SECRET": "   "}
    factory = AdapterFactory(settings, environment=environment)

    status = factory.statuses()["feishu"]
    assert status.external_calls_allowed is False
    assert status.missing_config_keys == ("CONTINUCARE_FEISHU_APP_SECRET",)
    with pytest.raises(AdapterConfigurationError, match="CONTINUCARE_FEISHU_APP_SECRET"):
        factory.build_feishu_bot(attempt_recorder=Recorder(), transport=FakeTransport())


def test_only_explicit_complete_test_tenant_builds_injected_clients():
    settings = _settings(
        feishu_mode="test_tenant",
        aily_mode="test_tenant",
        bitable_mode="test_tenant",
        external_egress_enabled=True,
        feishu_test_tenant_enabled=True,
        aily_test_tenant_enabled=True,
        bitable_test_tenant_enabled=True,
    )
    factory = AdapterFactory(settings, environment=FULL_ENV)
    transport = FakeTransport()

    assert factory.statuses()["feishu"].external_calls_allowed is True
    assert factory.build_feishu_bot(
        attempt_recorder=Recorder(), transport=transport
    ).transport is transport
    assert factory.build_aily(transport=transport).transport is transport
    assert factory.build_bitable(transport=transport).transport is transport


def test_secret_values_are_absent_from_status_repr_capture_and_errors():
    secret = "fake-secret-never-live"
    factory = AdapterFactory(
        _settings(feishu_mode="test_tenant"), environment=FULL_ENV
    )
    rendered = repr(factory.secrets) + repr(factory.statuses())
    assert secret not in rendered
    fake = FakeTransport(
        [json_response({"code": 999, "app_secret": secret})],
        redactor=SecretRedactor(secret),
    )
    provider = TenantTokenProvider(fake, secrets=factory.secrets)
    with pytest.raises(TokenError) as caught:
        provider.get_token()
    assert secret not in str(caught.value)
    assert secret not in fake.requests[0].body.decode()


def test_tenant_token_is_cached_and_refreshed_early_in_memory_only():
    clock = [0.0]
    fake = FakeTransport(
        [
            json_response({"code": 0, "tenant_access_token": "token-one", "expire": 100}),
            json_response({"code": 0, "tenant_access_token": "token-two", "expire": 100}),
        ],
        redactor=SecretRedactor("fake-secret-never-live", "token-one", "token-two"),
    )
    provider = TenantTokenProvider(
        fake,
        secrets=EnvironmentSecretProvider(FULL_ENV),
        now=lambda: clock[0],
    )

    assert provider.get_token() == "token-one"
    clock[0] = 49
    assert provider.get_token() == "token-one"
    assert len(fake.requests) == 1
    clock[0] = 51
    assert provider.get_token() == "token-two"
    assert len(fake.requests) == 2
    assert "token-one" not in repr(provider)


def test_transport_rejects_non_official_urls_without_opening_network():
    transport = StdlibHttpTransport(
        permit=issue_egress_permit(globally_enabled=True, capability_enabled=True)
    )
    for url in (
        "http://open.feishu.cn/open-apis/test",
        "https://evil.example/open-apis/test",
        "https://open.feishu.cn.evil.example/open-apis/test",
        "https://open.feishu.cn/not-open-apis/test",
    ):
        with pytest.raises(TransportPolicyError):
            transport.send(HttpRequest(method="GET", url=url, timeout_seconds=0.1))


def test_transport_json_and_size_contracts_are_strict():
    with pytest.raises(TransportInvalidJson):
        HttpResponse(status=200, headers={}, body=b"not-json").json_object()
    fake = FakeTransport([HttpResponse(status=200, headers={}, body=b"x" * 20)])
    with pytest.raises(TransportResponseTooLarge):
        fake.send(
            HttpRequest(
                method="GET",
                url="https://open.feishu.cn/open-apis/test",
                max_response_bytes=10,
            )
        )


def test_feishu_card_is_minimal_and_remote_acceptance_is_not_delivery():
    recorder = Recorder()
    fake = FakeTransport(
        [json_response({"code": 0, "data": {"message_id": "om_fake"}})],
        redactor=SecretRedactor("tenant-fake-token", "ou_fake1234"),
    )
    notifier = FeishuBotNotifier(
        fake,
        token_provider=StaticTokenProvider(),
        receive_id_type="open_id",
        receive_id="ou_fake1234",
        attempt_recorder=recorder,
    )
    card = render_synthetic_card(
        card_kind="nurse_manual_review", synthetic_reference="task.synthetic-1"
    )

    result = notifier.send_card(card, action_token="one-time-action")

    assert result.state == NotificationState.ACCEPTED_BY_REMOTE
    assert result.accepted_by_remote is True
    assert result.delivery_confirmed is False
    assert [item.state for item in recorder.results] == [
        NotificationState.EXTERNAL_REQUEST_PREPARED,
        NotificationState.EXTERNAL_ATTEMPTED,
        NotificationState.ACCEPTED_BY_REMOTE,
    ]
    assert recorder.results[-1] == result
    request_json = json.loads(fake.requests[0].body)
    assert request_json["receive_id"] == "[REDACTED]"
    assert "真实患者" not in request_json["content"]
    assert "治疗建议：" not in request_json["content"]
    assert "ou_fake1234" not in request_json["content"]


@pytest.mark.parametrize(
    "failure",
    [
        TransportTimeoutError("synthetic timeout"),
        TransportNetworkError("synthetic network failure"),
    ],
)
def test_feishu_unknown_outcome_is_not_retried_or_mocked(failure):
    recorder = Recorder()
    fake = FakeTransport([failure])
    notifier = FeishuBotNotifier(
        fake,
        token_provider=StaticTokenProvider(),
        receive_id_type="open_id",
        receive_id="ou_fake1234",
        attempt_recorder=recorder,
    )

    result = notifier.send_card(
        render_synthetic_card(
            card_kind="doctor_brief_ready", synthetic_reference="summary.synthetic-1"
        ),
        action_token="one-time-action",
    )

    assert result.state == NotificationState.OUTCOME_UNKNOWN
    assert result.accepted_by_remote is False
    assert result.failure_reason == "remote_outcome_unknown_no_retry"
    assert len(fake.requests) == 1
    assert [item.state for item in recorder.results] == [
        NotificationState.EXTERNAL_REQUEST_PREPARED,
        NotificationState.EXTERNAL_ATTEMPTED,
        NotificationState.OUTCOME_UNKNOWN,
    ]
    with pytest.raises(ValueError, match="already consumed"):
        notifier.send_card(
            render_synthetic_card(
                card_kind="doctor_brief_ready", synthetic_reference="summary.synthetic-1"
            ),
            action_token="one-time-action",
        )


def test_feishu_token_failure_is_not_misreported_as_send_attempt():
    recorder = Recorder()
    notifier = FeishuBotNotifier(
        FakeTransport(),
        token_provider=FailingTokenProvider(),
        receive_id_type="open_id",
        receive_id="ou_fake1234",
        attempt_recorder=recorder,
    )

    result = notifier.send_card(
        render_synthetic_card(
            card_kind="doctor_brief_ready", synthetic_reference="summary.synthetic-1"
        ),
        action_token="one-time-action",
    )

    assert result.state == NotificationState.FAILED
    assert result.send_attempted is False
    assert [item.state for item in recorder.results] == [
        NotificationState.EXTERNAL_REQUEST_PREPARED,
        NotificationState.FAILED,
    ]


def test_aily_structured_candidate_uses_local_coding_and_source_provenance(tmp_path):
    output = {
        "blocked": False,
        "items": [
            {
                "link_id": "vomiting-count-24h",
                "answer": 2,
                "evidence_text": "过去24小时呕吐了2次",
                "subject": "patient",
                "temporality": "explicit_24h",
                "negated": False,
            }
        ],
        "symptom_mentions": [],
    }
    adapter, fake = _aily_adapter(_aily_responses(output))

    task = _semantic_task(tmp_path)
    result = adapter.extract(task)

    assert result.mode == "model_api:feishu_aily_not_live_verified"
    assert result.candidates[0].source_mode == CandidateSource.AILY
    local_question = next(
        item for item in task.allowed_items if item.link_id == "vomiting-count-24h"
    )
    assert result.candidates[0].questionnaire_code == local_question.codes[0]
    assert len(fake.requests) == 6
    all_requests = b"\n".join(item.body or b"" for item in fake.requests).decode()
    assert DEMO_PATIENT_ID not in all_requests
    assert "fake-secret-never-live" not in all_requests
    assert "tenant-fake-token" not in repr(fake.requests)


@pytest.mark.parametrize(
    "forged",
    [
        {"code": "fake-diagnosis", "value": 2},
        {"value": 2, "unit": "tablet", "system": "fake", "code": "fake"},
    ],
)
def test_aily_cannot_supply_coding_inside_answer(tmp_path, forged):
    output = {
        "blocked": False,
        "items": [
            {
                "link_id": "vomiting-count-24h",
                "answer": forged,
                "evidence_text": "过去24小时呕吐了2次",
                "subject": "patient",
                "temporality": "explicit_24h",
                "negated": False,
            }
        ],
        "symptom_mentions": [],
    }
    adapter, _ = _aily_adapter(_aily_responses(output))

    with pytest.raises(AilyAdapterError, match="local validation"):
        adapter.extract(_semantic_task(tmp_path))


@pytest.mark.parametrize("forbidden_key", ["diagnosis", "risk", "treatment", "code"])
def test_aily_rejects_forbidden_or_unknown_output_fields(tmp_path, forbidden_key):
    output = {
        "blocked": False,
        "items": [],
        "symptom_mentions": [],
        forbidden_key: "must never pass",
    }
    adapter, _ = _aily_adapter(_aily_responses(output))

    with pytest.raises(AilyAdapterError, match="structured output"):
        adapter.extract(_semantic_task(tmp_path))


def test_aily_failure_falls_back_to_explicit_deterministic_mock(tmp_path):
    adapter, _ = _aily_adapter(
        _aily_responses(
            {
                "blocked": False,
                "items": [],
                "symptom_mentions": [],
                "diagnosis": "forbidden",
            }
        )
    )

    result = CareSemanticAgent(model_adapter=adapter).analyze(_semantic_task(tmp_path))

    assert result.mode == "local_semantic_mock"
    assert any(reason.startswith("model_adapter_error_fallback:") for reason in result.ignored_reasons)


def test_bitable_projection_has_stable_uuid4_idempotency_and_no_patient_fields():
    fake = FakeTransport(
        [json_response({"code": 0, "data": {"record": {"record_id": "rec_fake"}}})],
        redactor=SecretRedactor("tenant-fake-token"),
    )
    writer = BitableProjectionWriter(
        fake,
        token_provider=StaticTokenProvider(),
        app_token="app_fake_token",
        table_id="tbl_fake_table",
        idempotency_salt="synthetic-salt-at-least-16",
    )
    first = writer.idempotency_key(
        projection_kind="manual_review", synthetic_reference="task.synthetic-1"
    )
    second = writer.idempotency_key(
        projection_kind="manual_review", synthetic_reference="task.synthetic-1"
    )
    assert first == second
    assert UUID(first).version == 4

    result = writer.write_synthetic_projection(
        projection_kind="manual_review",
        synthetic_reference="task.synthetic-1",
        workflow_state="completed",
    )
    assert result.state == ProjectionState.ACCEPTED_BY_REMOTE
    request = fake.requests[0]
    assert "client_token=[REDACTED]" in request.url
    assert "app_fake_token" not in request.url
    assert "tbl_fake_table" not in request.url
    body = json.loads(request.body)
    assert body["fields"]["Synthetic"] is True
    assert set(body["fields"]) == {
        "Synthetic",
        "ProjectionKind",
        "OpaqueReference",
        "WorkflowState",
    }
    assert "patient" not in request.body.decode().lower()


def test_bitable_unknown_outcome_is_terminal_without_retry():
    fake = FakeTransport([TransportTimeoutError("synthetic timeout")])
    writer = BitableProjectionWriter(
        fake,
        token_provider=StaticTokenProvider(),
        app_token="app_fake_token",
        table_id="tbl_fake_table",
        idempotency_salt="synthetic-salt-at-least-16",
    )

    result = writer.write_synthetic_projection(
        projection_kind="doctor_brief",
        synthetic_reference="summary.synthetic-1",
        workflow_state="ready",
    )

    assert result.state == ProjectionState.OUTCOME_UNKNOWN
    assert len(fake.requests) == 1


def test_bitable_token_failure_is_not_misreported_as_write_attempt():
    writer = BitableProjectionWriter(
        FakeTransport(),
        token_provider=FailingTokenProvider(),
        app_token="app_fake_token",
        table_id="tbl_fake_table",
        idempotency_salt="synthetic-salt-at-least-16",
    )

    result = writer.write_synthetic_projection(
        projection_kind="doctor_brief",
        synthetic_reference="summary.synthetic-1",
        workflow_state="ready",
    )

    assert result.state == ProjectionState.FAILED
    assert result.attempted is False

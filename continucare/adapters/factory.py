"""Single configuration and construction boundary for optional integrations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from continucare.adapters.http_transport import (
    HttpTransport,
    StdlibHttpTransport,
    issue_egress_permit,
)
from continucare.config import AdapterMode, IntegrationSettings, get_settings


_VALID_MODES = {item.value for item in AdapterMode}


@dataclass(frozen=True)
class AdapterStatus:
    capability: str
    selected_mode: str
    implementation: str
    configured: bool
    external_calls_allowed: bool
    fallback_reason: str | None
    last_known_health: str
    missing_config_keys: tuple[str, ...]
    protocol_implemented: bool = True
    mock_fallback_verified: bool = True
    contract_tested_with_fake_transport: bool = True
    structured_output_status: str | None = None

    @property
    def live_tenant_verified(self) -> bool:
        return False

    @property
    def production_ready(self) -> bool:
        return False


@dataclass(frozen=True, repr=False)
class EnvironmentSecretProvider:
    """Resolve only named environment secrets; never represent their values."""

    environment: Mapping[str, str]

    def get(self, key: str) -> str:
        value = self.environment.get(key, "").strip()
        if not value:
            raise AdapterConfigurationError(f"missing required configuration key: {key}")
        return value

    def __repr__(self) -> str:
        return "EnvironmentSecretProvider(values=[REDACTED])"


class AdapterConfigurationError(RuntimeError):
    """Stable configuration error containing key names but never values."""


_REQUIRED = {
    "feishu": (
        "CONTINUCARE_FEISHU_APP_ID",
        "CONTINUCARE_FEISHU_APP_SECRET",
        "CONTINUCARE_FEISHU_RECEIVE_ID_TYPE",
        "CONTINUCARE_FEISHU_RECEIVE_ID",
    ),
    "aily": (
        "CONTINUCARE_FEISHU_APP_ID",
        "CONTINUCARE_FEISHU_APP_SECRET",
        "CONTINUCARE_AILY_APP_ID",
    ),
    "bitable": (
        "CONTINUCARE_FEISHU_APP_ID",
        "CONTINUCARE_FEISHU_APP_SECRET",
        "CONTINUCARE_BITABLE_APP_TOKEN",
        "CONTINUCARE_BITABLE_TABLE_ID",
        "CONTINUCARE_BITABLE_IDEMPOTENCY_SALT",
    ),
}


class AdapterFactory:
    def __init__(
        self,
        settings: IntegrationSettings | None = None,
        *,
        environment: Mapping[str, str] | None = None,
    ):
        self.settings = settings or get_settings().integrations
        self.environment = environment if environment is not None else os.environ
        self.secrets = EnvironmentSecretProvider(self.environment)

    def statuses(self) -> dict[str, AdapterStatus]:
        return {
            "feishu": self._status(
                "feishu",
                self.settings.feishu_mode,
                self.settings.feishu_test_tenant_enabled,
                "FeishuBotNotifier",
            ),
            "aily": self._status(
                "aily",
                self.settings.aily_mode,
                self.settings.aily_test_tenant_enabled,
                "AilySemanticAdapter",
                structured_output_status="not_verified",
            ),
            "bitable": self._status(
                "bitable",
                self.settings.bitable_mode,
                self.settings.bitable_test_tenant_enabled,
                "BitableProjectionWriter",
            ),
        }

    def _status(
        self,
        capability: str,
        mode: str,
        capability_enabled: bool,
        implementation: str,
        *,
        structured_output_status: str | None = None,
    ) -> AdapterStatus:
        if mode not in _VALID_MODES:
            return AdapterStatus(
                capability=capability,
                selected_mode=mode or "invalid",
                implementation=implementation,
                configured=False,
                external_calls_allowed=False,
                fallback_reason="invalid_mode",
                last_known_health="not_checked",
                missing_config_keys=(),
                structured_output_status=structured_output_status,
            )
        if mode == AdapterMode.DISABLED.value:
            return AdapterStatus(
                capability=capability,
                selected_mode=mode,
                implementation="disabled",
                configured=True,
                external_calls_allowed=False,
                fallback_reason="capability_disabled",
                last_known_health="not_checked",
                missing_config_keys=(),
                structured_output_status=structured_output_status,
            )
        if mode == AdapterMode.MOCK.value:
            return AdapterStatus(
                capability=capability,
                selected_mode=mode,
                implementation=(
                    "deterministic_semantic_mock"
                    if capability == "aily"
                    else "local_mock"
                ),
                configured=True,
                external_calls_allowed=False,
                fallback_reason="default_offline_mock",
                last_known_health="not_checked",
                missing_config_keys=(),
                structured_output_status=structured_output_status,
            )
        missing = tuple(
            key for key in _REQUIRED[capability] if not self.environment.get(key, "").strip()
        )
        allowed = bool(
            not missing
            and capability_enabled
            and self.settings.external_egress_enabled
        )
        reason = None
        if missing:
            reason = "missing_configuration"
        elif not capability_enabled:
            reason = "capability_enable_flag_required"
        elif not self.settings.external_egress_enabled:
            reason = "global_egress_flag_required"
        return AdapterStatus(
            capability=capability,
            selected_mode=mode,
            implementation=implementation,
            configured=not missing,
            external_calls_allowed=allowed,
            fallback_reason=reason,
            last_known_health="not_checked",
            missing_config_keys=missing,
            structured_output_status=structured_output_status,
        )

    def _transport(self, capability: str, supplied: HttpTransport | None) -> HttpTransport:
        status = self.statuses()[capability]
        if status.selected_mode != AdapterMode.TEST_TENANT.value or not status.external_calls_allowed:
            missing = ", ".join(status.missing_config_keys)
            suffix = f"; missing keys: {missing}" if missing else ""
            raise AdapterConfigurationError(
                f"{capability} test_tenant client is fail-closed{suffix}"
            )
        if supplied is not None:
            return supplied
        permit = issue_egress_permit(
            globally_enabled=self.settings.external_egress_enabled,
            capability_enabled={
                "feishu": self.settings.feishu_test_tenant_enabled,
                "aily": self.settings.aily_test_tenant_enabled,
                "bitable": self.settings.bitable_test_tenant_enabled,
            }[capability],
        )
        return StdlibHttpTransport(permit=permit)

    def build_feishu_bot(
        self, *, attempt_recorder, transport: HttpTransport | None = None
    ):
        from continucare.adapters.feishu.bot_notifier import FeishuBotNotifier
        from continucare.adapters.feishu.token_provider import TenantTokenProvider

        selected = self._transport("feishu", transport)
        token_provider = TenantTokenProvider(
            selected,
            secrets=self.secrets,
            timeout_seconds=self.settings.timeout_seconds,
        )
        return FeishuBotNotifier(
            selected,
            token_provider=token_provider,
            receive_id_type=self.secrets.get("CONTINUCARE_FEISHU_RECEIVE_ID_TYPE"),
            receive_id=self.secrets.get("CONTINUCARE_FEISHU_RECEIVE_ID"),
            attempt_recorder=attempt_recorder,
            timeout_seconds=self.settings.timeout_seconds,
        )

    def build_aily(self, *, transport: HttpTransport | None = None):
        from continucare.adapters.feishu.aily_extractor import AilySemanticAdapter
        from continucare.adapters.feishu.token_provider import TenantTokenProvider

        selected = self._transport("aily", transport)
        token_provider = TenantTokenProvider(
            selected,
            secrets=self.secrets,
            timeout_seconds=self.settings.timeout_seconds,
        )
        return AilySemanticAdapter(
            selected,
            token_provider=token_provider,
            app_id=self.secrets.get("CONTINUCARE_AILY_APP_ID"),
            skill_id=self.environment.get("CONTINUCARE_AILY_SKILL_ID", "").strip() or None,
            timeout_seconds=self.settings.timeout_seconds,
        )

    def build_bitable(self, *, transport: HttpTransport | None = None):
        from continucare.adapters.feishu.bitable_store import BitableProjectionWriter
        from continucare.adapters.feishu.token_provider import TenantTokenProvider

        selected = self._transport("bitable", transport)
        token_provider = TenantTokenProvider(
            selected,
            secrets=self.secrets,
            timeout_seconds=self.settings.timeout_seconds,
        )
        return BitableProjectionWriter(
            selected,
            token_provider=token_provider,
            app_token=self.secrets.get("CONTINUCARE_BITABLE_APP_TOKEN"),
            table_id=self.secrets.get("CONTINUCARE_BITABLE_TABLE_ID"),
            idempotency_salt=self.secrets.get(
                "CONTINUCARE_BITABLE_IDEMPOTENCY_SALT"
            ),
            timeout_seconds=self.settings.timeout_seconds,
        )


def read_adapter_statuses() -> dict[str, AdapterStatus]:
    """Pure configuration projection: no client, token, health check or network."""

    return AdapterFactory().statuses()

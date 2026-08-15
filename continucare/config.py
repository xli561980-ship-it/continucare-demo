"""Runtime configuration with safe, key-free defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AdapterMode(StrEnum):
    MOCK = "mock"
    TEST_TENANT = "test_tenant"
    DISABLED = "disabled"


@dataclass(frozen=True)
class IntegrationSettings:
    feishu_mode: str = AdapterMode.MOCK.value
    aily_mode: str = AdapterMode.MOCK.value
    bitable_mode: str = AdapterMode.DISABLED.value
    external_egress_enabled: bool = False
    feishu_test_tenant_enabled: bool = False
    aily_test_tenant_enabled: bool = False
    bitable_test_tenant_enabled: bool = False
    timeout_seconds: float = 8.0


@dataclass(frozen=True)
class Settings:
    db_path: Path
    mode: str
    patient_timezone: str
    integrations: IntegrationSettings


def load_local_environment(path: Path | str = ".env") -> None:
    """Load an ignored local env file without overwriting deployed secrets."""

    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def get_settings() -> Settings:
    load_local_environment()
    return Settings(
        db_path=Path(os.getenv("CONTINUCARE_DB_PATH", "data/continucare.db")),
        mode=os.getenv("CONTINUCARE_MODE", "local_stable_demo"),
        patient_timezone=os.getenv("CONTINUCARE_PATIENT_TIMEZONE", "Asia/Shanghai"),
        integrations=IntegrationSettings(
            feishu_mode=os.getenv("CONTINUCARE_FEISHU_MODE", "mock").strip(),
            aily_mode=os.getenv("CONTINUCARE_AILY_MODE", "mock").strip(),
            bitable_mode=os.getenv("CONTINUCARE_BITABLE_MODE", "disabled").strip(),
            external_egress_enabled=_env_bool("CONTINUCARE_EXTERNAL_EGRESS_ENABLED"),
            feishu_test_tenant_enabled=_env_bool(
                "CONTINUCARE_FEISHU_TEST_TENANT_ENABLED"
            ),
            aily_test_tenant_enabled=_env_bool(
                "CONTINUCARE_AILY_TEST_TENANT_ENABLED"
            ),
            bitable_test_tenant_enabled=_env_bool(
                "CONTINUCARE_BITABLE_TEST_TENANT_ENABLED"
            ),
            timeout_seconds=_safe_timeout(
                os.getenv("CONTINUCARE_FEISHU_TIMEOUT_SECONDS", "8")
            ),
        ),
    )


def _env_bool(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _safe_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        return 8.0
    return value if 0 < value <= 30 else 8.0

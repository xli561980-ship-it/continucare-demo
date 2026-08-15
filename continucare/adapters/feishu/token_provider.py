"""In-memory tenant token cache for an explicitly enabled test tenant."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from continucare.adapters.http_transport import HttpRequest, HttpTransport


TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"


class SecretProvider(Protocol):
    def get(self, key: str) -> str: ...


class TokenError(RuntimeError):
    """Stable auth error that never includes response content."""


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: int
    tenant_access_token: str = Field(min_length=1)
    expire: int = Field(gt=0)


@dataclass(repr=False)
class TenantTokenProvider:
    transport: HttpTransport
    secrets: SecretProvider
    timeout_seconds: float = 8.0
    now: Callable[[], float] = time.monotonic
    _token: str | None = field(default=None, init=False, repr=False)
    _refresh_at: float = field(default=0.0, init=False, repr=False)

    def __repr__(self) -> str:
        return "TenantTokenProvider(cache=in_memory, token=[REDACTED])"

    def get_token(self) -> str:
        current = self.now()
        if self._token and current < self._refresh_at:
            return self._token
        response = self.transport.send(
            HttpRequest(
                method="POST",
                url=TOKEN_URL,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=json.dumps(
                    {
                        "app_id": self.secrets.get("CONTINUCARE_FEISHU_APP_ID"),
                        "app_secret": self.secrets.get(
                            "CONTINUCARE_FEISHU_APP_SECRET"
                        ),
                    },
                    separators=(",", ":"),
                ).encode(),
                timeout_seconds=self.timeout_seconds,
            )
        )
        if response.status != 200:
            raise TokenError("tenant token request was rejected")
        try:
            payload = _TokenResponse.model_validate(response.json_object())
        except (ValidationError, ValueError):
            raise TokenError("tenant token response failed validation") from None
        if payload.code != 0:
            raise TokenError("tenant token request returned a platform error")
        refresh_skew = min(180.0, payload.expire / 2)
        self._token = payload.tenant_access_token
        self._refresh_at = current + payload.expire - refresh_skew
        return payload.tenant_access_token

"""Default-off capability gate for the isolated live-contract validator."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from continucare.knowledge.ops.source_connectors.errors import (
    ConnectorErrorCode,
    ConnectorFailure,
)


KNOWLEDGE_LIVE_VALIDATION_ENV = "CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION"
_PERMIT_SEAL = object()


def knowledge_live_validation_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return true only for the exact, case-sensitive value ``"true"``."""

    try:
        source = os.environ if environ is None else environ
        return source.get(KNOWLEDGE_LIVE_VALIDATION_ENV) == "true"
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class KnowledgeEgressPermit:
    """Opaque capability; construction outside this module is not accepted."""

    _seal: object


def issue_knowledge_egress_permit(
    *,
    external_egress_enabled: bool,
    identity_binding_proven: bool,
    environ: Mapping[str, str] | None = None,
) -> KnowledgeEgressPermit:
    if not external_egress_enabled or not knowledge_live_validation_enabled(environ):
        raise ConnectorFailure(
            ConnectorErrorCode.FEATURE_DISABLED,
            detail="both global egress and the exact Knowledge live flag are required",
        )
    if not identity_binding_proven:
        raise ConnectorFailure(
            ConnectorErrorCode.FEATURE_DISABLED,
            detail="DNS-to-socket-to-TLS identity binding is not proven",
        )
    return KnowledgeEgressPermit(_PERMIT_SEAL)


def assert_valid_egress_permit(permit: KnowledgeEgressPermit) -> None:
    if not isinstance(permit, KnowledgeEgressPermit) or permit._seal is not _PERMIT_SEAL:
        raise ConnectorFailure(
            ConnectorErrorCode.FEATURE_DISABLED,
            detail="a valid Knowledge egress capability is required",
        )

"""Technology-neutral read ports for Follow-up Planning."""

from __future__ import annotations

from typing import Protocol

from continucare.followup_planning.models import ClinicalContext, EvidenceSnapshot


class ClinicalContextSource(Protocol):
    """Returns pre-structured synthetic facts without parsing clinical prose."""

    def get_context(self, case_id: str) -> ClinicalContext | None: ...


class EvidenceSnapshotReader(Protocol):
    """Exact-ID-only, read-only evidence lookup boundary."""

    def lookup_exact(self, topic_id: str) -> EvidenceSnapshot | None: ...

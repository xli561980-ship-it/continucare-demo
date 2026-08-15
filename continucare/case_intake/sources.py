"""Read-only source protocol for normalized Case Intake drafts."""

from __future__ import annotations

from typing import Iterable, Protocol

from continucare.case_intake.models import CaseDraft, CaseSourceType


class CaseSource(Protocol):
    """A future hospital connector implements this without owning persistence."""

    source_type: CaseSourceType
    source_system: str

    def iter_cases(self) -> Iterable[CaseDraft]: ...

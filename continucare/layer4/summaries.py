"""Deterministic evidence summaries and immutable doctor review workflow."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Iterable, cast

from continucare.errors import ConcurrentWriteConflict
from continucare.layer4.contracts import (
    DoctorReview,
    DoctorReviewDecision,
    DoctorReviewOutcome,
    EvidenceReference,
    Layer4SummaryDraft,
    MemoryEventKind,
    ResourceReference,
    SummaryDraftStatus,
    SummaryEvidenceItem,
    TimelineEvent,
)
from continucare.layer4.fhir import build_provenance
from continucare.layer4.memory import ClinicalMemoryService
from continucare.layer4.repository import Layer4Repository
from continucare.services.audit import build_audit_event


SUMMARY_AGENT_REFERENCE = "Device/continucare-deterministic-summary"


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("summary times must include a timezone offset")
    return parsed


def _versioned_reference(reference: ResourceReference) -> str:
    if reference.reference.startswith("urn:"):
        return reference.reference
    if reference.version_id:
        return f"{reference.reference}/_history/{reference.version_id}"
    return reference.reference


def _summary_reference(summary_id: str, version: str) -> str:
    return f"urn:continucare:summary:{summary_id}:version:{version}"


def _review_reference(review_id: str, version: str) -> str:
    return f"urn:continucare:doctor-review:{review_id}:version:{version}"


def _section_for(event: TimelineEvent) -> str:
    return {
        MemoryEventKind.QUESTIONNAIRE_RESPONSE: "overview",
        MemoryEventKind.OBSERVATION: "key_changes",
        MemoryEventKind.COMMUNICATION: "overview",
        MemoryEventKind.TASK: "tasks_and_actions",
        MemoryEventKind.REVIEW: "doctor_to_confirm",
        MemoryEventKind.CONFLICT: "conflicts",
        MemoryEventKind.MISSING_DATA: "missing_data",
    }[event.kind]


def _canonical_evidence(evidence: EvidenceReference) -> str:
    return json.dumps(
        evidence.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _modified_items_digest(items: list[SummaryEvidenceItem] | None) -> str:
    payload = json.dumps(
        [item.model_dump(mode="json") for item in items] if items is not None else [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvidenceSummaryService:
    """Project current Timeline events into evidence-bound deterministic bullets."""

    GENERATOR_VERSION = "deterministic-summary-v1"

    def __init__(
        self,
        memory: ClinicalMemoryService,
        repository: Layer4Repository,
    ):
        self.memory = memory
        self.repository = repository

    def generate(
        self,
        *,
        patient_id: str,
        period_start: str,
        period_end: str,
        generated_at: str,
    ) -> Layer4SummaryDraft:
        start = _instant(period_start)
        end = _instant(period_end)
        generated = _instant(generated_at)
        if end < start:
            raise ValueError("summary period_end cannot precede period_start")
        if generated < end:
            raise ValueError("summary generated_at cannot precede period_end")
        timeline = [
            item
            for item in self.memory.list_timeline(
                patient_id, include_audit=False, include_history=False
            )
            if _instant(item.effective_start) <= end
            and _instant(item.effective_end) >= start
            and _instant(item.recorded_at) <= generated
            and item.kind != MemoryEventKind.AUDIT
        ]
        items = [self._item_from_timeline(item) for item in timeline]
        source_ids = [item.timeline_event_id for item in timeline]
        summary_id = _stable_id(
            "summary-v2",
            patient_id,
            self.memory.pathway_code,
            self.memory.pathway_version,
            "timeline_evidence",
            period_start,
            period_end,
        )
        current = self.repository.get_contract("summary_draft", summary_id)
        if current is not None:
            persisted = cast(Layer4SummaryDraft, current)
            if (
                persisted.patient_id == patient_id
                and persisted.pathway_code == self.memory.pathway_code
                and persisted.pathway_version == self.memory.pathway_version
                and persisted.period_start == period_start
                and persisted.period_end == period_end
                and persisted.source_timeline_event_ids == source_ids
                and persisted.generator_version == self.GENERATOR_VERSION
            ):
                return persisted
            try:
                version = str(int(persisted.version) + 1)
            except ValueError as exc:
                raise ValueError("summary versions must be numeric") from exc
        else:
            version = "1"

        provenance_id = _stable_id("provenance", summary_id, version)
        provenance = build_provenance(
            target_references=[_summary_reference(summary_id, version)],
            recorded_at=generated_at,
            agent_reference=SUMMARY_AGENT_REFERENCE,
            agent_role_code="assembler",
            agent_role_display="Assembler",
            provenance_id=provenance_id,
            activity_code="TRANSFORM",
            activity_display="deterministic evidence summary",
            entity_source_references=self._source_references(timeline),
        )
        summary = Layer4SummaryDraft(
            summary_id=summary_id,
            version=version,
            patient_id=patient_id,
            pathway_code=self.memory.pathway_code,
            pathway_version=self.memory.pathway_version,
            period_start=period_start,
            period_end=period_end,
            status=SummaryDraftStatus.SAFETY_REVIEWED,
            items=items,
            source_timeline_event_ids=source_ids,
            provenance_refs=[
                ResourceReference(
                    reference=f"Provenance/{provenance_id}", version_id="1"
                )
            ],
            generation_mode="deterministic",
            generator_version=self.GENERATOR_VERSION,
            created_at=generated_at,
        )
        self.repository.persist_summary_bundle(
            expected_current=cast(Layer4SummaryDraft | None, current),
            summary=summary,
            provenance=provenance,
        )
        return summary

    @staticmethod
    def _item_from_timeline(event: TimelineEvent) -> SummaryEvidenceItem:
        section = _section_for(event)
        text = f"{event.effective_start}｜{event.title}：{event.summary}"
        return SummaryEvidenceItem(
            item_id=_stable_id(
                "summary-item", event.timeline_event_id, section, text
            ),
            section=section,
            text=text,
            evidence_refs=event.evidence_refs,
            requires_doctor_confirmation=(
                event.kind in {MemoryEventKind.CONFLICT, MemoryEventKind.REVIEW}
            ),
        )

    @staticmethod
    def _source_references(timeline: list[TimelineEvent]) -> list[str]:
        references = {
            f"urn:continucare:timeline-event:{item.timeline_event_id}"
            for item in timeline
        }
        references.update(
            _versioned_reference(evidence.resource)
            for item in timeline
            for evidence in item.evidence_refs
        )
        return sorted(references)


class DoctorReviewService:
    """Accept, modify or reject one immutable evidence summary version."""

    def __init__(self, repository: Layer4Repository):
        self.repository = repository

    def review(
        self,
        *,
        summary_id: str,
        summary_version: str,
        reviewer_reference: str,
        decision: DoctorReviewDecision,
        reviewed_at: str,
        note: str | None = None,
        modified_items: Iterable[SummaryEvidenceItem] | None = None,
    ) -> DoctorReviewOutcome:
        reviewer = reviewer_reference.strip()
        if not reviewer:
            raise ValueError("doctor review requires reviewer_reference")
        _instant(reviewed_at)
        note_text = (note.strip() or None) if note is not None else None
        requested_items = (
            [SummaryEvidenceItem.model_validate(item) for item in modified_items]
            if modified_items is not None
            else None
        )
        items_digest = _modified_items_digest(requested_items)
        review_id = _stable_id(
            "doctor-review",
            summary_id,
            summary_version,
            reviewer,
            decision.value,
            note_text or "",
            items_digest,
        )
        existing = self.repository.get_contract("doctor_review", review_id)
        if existing is not None:
            return self._idempotent_retry(
                review=cast(DoctorReview, existing),
                summary_id=summary_id,
                summary_version=summary_version,
                reviewer_reference=reviewer,
                decision=decision,
                note=note_text,
                modified_items=requested_items,
            )

        source_record = self.repository.get_contract(
            "summary_draft", summary_id, version=summary_version
        )
        if source_record is None:
            raise ValueError("summary version does not exist")
        source = cast(Layer4SummaryDraft, source_record)
        if (
            source.summary_kind == "timeline_evidence"
            and not source.summary_id.startswith("summary-v2-")
        ):
            raise ValueError("legacy timeline Summary is read-only and cannot be reviewed")
        current = self.repository.get_contract("summary_draft", summary_id)
        if current is None or cast(Layer4SummaryDraft, current).version != summary_version:
            raise ConcurrentWriteConflict(
                "doctor review cannot act on a stale summary version"
            )
        if source.status != SummaryDraftStatus.SAFETY_REVIEWED:
            raise ValueError("doctor review requires a safety-reviewed summary")
        if _instant(reviewed_at) <= _instant(source.created_at):
            raise ValueError("doctor review time must follow summary creation")

        if decision == DoctorReviewDecision.MODIFY:
            if not requested_items:
                raise ValueError("modify review requires modified evidence items")
            self._validate_modified_items(source, requested_items)
            if requested_items == source.items:
                raise ValueError("modify review must change at least one summary item")
            result_items = requested_items
            result_status = SummaryDraftStatus.DOCTOR_REVIEWED
        elif decision == DoctorReviewDecision.ACCEPT:
            if requested_items is not None:
                raise ValueError("accept review cannot include modified items")
            result_items = source.items
            result_status = SummaryDraftStatus.DOCTOR_REVIEWED
        else:
            if requested_items is not None:
                raise ValueError("reject review cannot include modified items")
            result_items = source.items
            result_status = SummaryDraftStatus.REJECTED

        try:
            result_version = str(int(source.version) + 1)
        except ValueError as exc:
            raise ValueError("summary versions must be numeric") from exc
        provenance_id = _stable_id("provenance", review_id)
        provenance_reference = ResourceReference(
            reference=f"Provenance/{provenance_id}", version_id="1"
        )
        result = Layer4SummaryDraft.model_validate(
            {
                **source.model_dump(mode="json"),
                "version": result_version,
                "status": result_status,
                "items": result_items,
                "provenance_refs": [
                    *source.model_dump(mode="json")["provenance_refs"],
                    provenance_reference.model_dump(mode="json"),
                ],
                "created_at": reviewed_at,
            }
        )
        review = DoctorReview(
            review_id=review_id,
            summary_id=summary_id,
            summary_version=summary_version,
            patient_id=source.patient_id,
            reviewer_reference=reviewer,
            decision=decision,
            note=note_text,
            amended_summary_id=(
                summary_id if decision == DoctorReviewDecision.MODIFY else None
            ),
            amended_summary_version=(
                result_version if decision == DoctorReviewDecision.MODIFY else None
            ),
            result_summary_id=summary_id,
            result_summary_version=result_version,
            provenance_reference=f"Provenance/{provenance_id}",
            reviewed_at=reviewed_at,
        )
        provenance = build_provenance(
            target_references=[
                _summary_reference(summary_id, result_version),
                _review_reference(review_id, review.version),
            ],
            recorded_at=reviewed_at,
            agent_reference=reviewer,
            agent_role_code="verifier",
            agent_role_display="Verifier",
            provenance_id=provenance_id,
            activity_code="UPDATE",
            activity_display=f"doctor review: {decision.value}",
            entity_source_references=[
                _summary_reference(summary_id, summary_version),
                *self._evidence_references(result_items),
            ],
        )
        audit_event = build_audit_event(
            event_id=_stable_id("audit", review_id, "doctor_reviewed_summary"),
            patient_id=source.patient_id,
            entity_type="Layer4SummaryDraft",
            entity_id=summary_id,
            event_type="doctor_reviewed_summary",
            actor_type="doctor",
            created_at=reviewed_at,
            details={
                "source_summary_version": summary_version,
                "result_summary_version": result_version,
                "review_id": review_id,
                "reviewer_reference": reviewer,
                "decision": decision.value,
                "modified_items_digest": items_digest,
                "clinical_assessment": "not_assessed",
            },
        )
        created = self.repository.persist_doctor_review_bundle(
            expected_source=source,
            provenance=provenance,
            result_summary=result,
            review=review,
            audit_event=audit_event,
        )
        return DoctorReviewOutcome(
            review=review,
            summary=result,
            idempotent_replay=not created,
        )

    def _idempotent_retry(
        self,
        *,
        review: DoctorReview,
        summary_id: str,
        summary_version: str,
        reviewer_reference: str,
        decision: DoctorReviewDecision,
        note: str | None,
        modified_items: list[SummaryEvidenceItem] | None,
    ) -> DoctorReviewOutcome:
        if (
            review.summary_id != summary_id
            or review.summary_version != summary_version
            or review.reviewer_reference != reviewer_reference
            or review.decision != decision
        ):
            raise ConcurrentWriteConflict(
                "doctor review identity conflicts with a different request"
            )
        if review.note != note:
            raise ConcurrentWriteConflict(
                "doctor review identity conflicts with a different note"
            )
        if not review.result_summary_id or not review.result_summary_version:
            raise ValueError("stored doctor review has no result summary reference")
        result_record = self.repository.get_contract(
            "summary_draft",
            review.result_summary_id,
            version=review.result_summary_version,
        )
        if result_record is None:
            raise ValueError("doctor review result summary is missing")
        result = cast(Layer4SummaryDraft, result_record)
        if review.decision == DoctorReviewDecision.MODIFY:
            if modified_items != result.items:
                raise ConcurrentWriteConflict(
                    "doctor review identity conflicts with different modified items"
                )
        elif modified_items is not None:
            raise ValueError("stored accept/reject review has no modified items")
        source_record = self.repository.get_contract(
            "summary_draft", summary_id, version=summary_version
        )
        if source_record is None:
            raise ConcurrentWriteConflict("doctor review source summary is missing")
        if not review.provenance_reference:
            raise ConcurrentWriteConflict("doctor review Provenance is missing")
        provenance = self.repository.get_fhir_resource(
            "Provenance",
            review.provenance_reference.removeprefix("Provenance/"),
            version_id="1",
        )
        if provenance is None:
            raise ConcurrentWriteConflict("doctor review Provenance is missing")
        items_digest = _modified_items_digest(modified_items)
        audit_event = build_audit_event(
            event_id=_stable_id(
                "audit", review.review_id, "doctor_reviewed_summary"
            ),
            patient_id=review.patient_id,
            entity_type="Layer4SummaryDraft",
            entity_id=summary_id,
            event_type="doctor_reviewed_summary",
            actor_type="doctor",
            created_at=review.reviewed_at,
            details={
                "source_summary_version": summary_version,
                "result_summary_version": result.version,
                "review_id": review.review_id,
                "reviewer_reference": review.reviewer_reference,
                "decision": review.decision.value,
                "modified_items_digest": items_digest,
                "clinical_assessment": "not_assessed",
            },
        )
        created = self.repository.persist_doctor_review_bundle(
            expected_source=cast(Layer4SummaryDraft, source_record),
            provenance=provenance,
            result_summary=result,
            review=review,
            audit_event=audit_event,
        )
        if created:
            raise ConcurrentWriteConflict(
                "doctor review replay unexpectedly created new persistence facts"
            )
        return DoctorReviewOutcome(
            review=review,
            summary=result,
            idempotent_replay=True,
        )

    @staticmethod
    def _validate_modified_items(
        source: Layer4SummaryDraft,
        modified_items: list[SummaryEvidenceItem],
    ) -> None:
        allowed = {
            _canonical_evidence(evidence)
            for item in source.items
            for evidence in item.evidence_refs
        }
        for item in modified_items:
            for evidence in item.evidence_refs:
                if _canonical_evidence(evidence) not in allowed:
                    raise ValueError(
                        "modified summary item cites evidence absent from source summary"
                    )

    @staticmethod
    def _evidence_references(items: list[SummaryEvidenceItem]) -> list[str]:
        return sorted(
            {
                _versioned_reference(evidence.resource)
                for item in items
                for evidence in item.evidence_refs
            }
        )

"""Role-aware orchestration for the synthetic Follow-up Planning core."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from continucare.followup_planning.models import (
    ActorRole,
    CandidateConfirmationStatus,
    ClinicalContext,
    ClinicalContextCandidate,
    CompletedAnswerBinding,
    EvidenceLookupResult,
    EvidenceLookupStatus,
    EvidenceReviewStatus,
    EvidenceUse,
    FieldOrigin,
    FieldProvenance,
    FollowupItemStatus,
    MonitoringDefinitionOrigin,
    MonitoringItemDefinition,
    NursePlanView,
    PatientBurden,
    PatientFollowupItem,
    PatientFollowupPlanVersion,
    PatientItemView,
    PatientPlanView,
    PlanAggregate,
    PlanningActor,
    PlanningAuditEvent,
    PlanningMode,
    PlanStatus,
    PublishConfirmation,
    ResolvedSchedule,
    ResponseOption,
    ResponseType,
    SAFE_MONITORING_USES,
    ScheduleApprovalStatus,
    ScheduleBasis,
    ScheduleFieldProvenance,
    SchedulePreview,
    ScheduleTemplate,
    ScheduleValues,
    SnapshotReference,
    VersionedReference,
    require_aware,
)
from continucare.followup_planning.ports import (
    ClinicalContextSource,
    EvidenceSnapshotReader,
)
from continucare.followup_planning.repository import (
    SQLiteFollowupPlanningRepository,
)
from continucare.followup_planning.scheduling import (
    calculate_patient_burden,
    preview_schedule,
)


class FollowupPlanningError(RuntimeError):
    pass


class FollowupPlanningDisabledError(FollowupPlanningError):
    pass


class FollowupPlanningAuthorizationError(FollowupPlanningError):
    pass


class FollowupPlanningStateError(FollowupPlanningError):
    pass


class EvidenceUseNotPermittedError(FollowupPlanningError):
    pass


class SemanticChangeDeclarationError(FollowupPlanningError):
    pass


class FollowupPlanningService:
    """The only role-aware writer API for Follow-up Planning.

    Construction and disabled-mode factories are side-effect free. This service
    creates no FHIR resources, communications, alerts, tasks, or send queues.
    """

    def __init__(
        self,
        repository: SQLiteFollowupPlanningRepository,
        *,
        mode: PlanningMode = PlanningMode.DISABLED,
        evidence_reader: EvidenceSnapshotReader | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ):
        self.repository = repository
        self.mode = mode
        self.evidence_reader = evidence_reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda prefix: f"{prefix}_{uuid4().hex}")

    @classmethod
    def from_storage(
        cls,
        storage: Path | str | sqlite3.Connection,
        *,
        mode: PlanningMode | str = PlanningMode.DISABLED,
        evidence_reader: EvidenceSnapshotReader | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> "FollowupPlanningService":
        selected = PlanningMode(mode)
        repository = SQLiteFollowupPlanningRepository(storage)
        if selected == PlanningMode.ENABLED:
            repository.initialize()
        return cls(
            repository,
            mode=selected,
            evidence_reader=evidence_reader,
            clock=clock,
            id_factory=id_factory,
        )

    def ingest_context(
        self,
        actor: PlanningActor,
        source: ClinicalContextSource,
        *,
        case_id: str,
    ) -> tuple[ClinicalContextCandidate, ...]:
        self._require_enabled()
        self._require_clinician(actor)
        supplied = source.get_context(case_id)
        if supplied is None:
            raise LookupError(f"synthetic context for case {case_id!r} was not found")
        # Revalidate so model_construct/model_copy cannot bypass synthetic guards.
        context = ClinicalContext.model_validate(supplied.model_dump(mode="python"))
        if context.case_id != case_id:
            raise ValueError("clinical context source returned a different case_id")
        candidates: list[ClinicalContextCandidate] = []
        for entry in context.entries:
            now = self._now()
            candidate = ClinicalContextCandidate(
                candidate_id=self._id("candidate"),
                version=1,
                case_id=context.case_id,
                patient_id=context.patient_id,
                source_reference=entry.source_reference,
                source_excerpt_snapshot=entry.source_excerpt_snapshot,
                source_kind=entry.source_kind,
                extracted_text=entry.extracted_text,
                coding=entry.coding,
                confirmation_status=CandidateConfirmationStatus.EXTRACTED,
                confirmed_by=None,
                confirmed_at=None,
                created_at=now,
                synthetic=True,
            )
            self.repository.append_candidate(
                candidate,
                expected_version=0,
                audit_event=self._audit(
                    actor,
                    entity_type="clinical_context_candidate",
                    entity_id=candidate.candidate_id,
                    event_type="candidate_extracted",
                    occurred_at=now,
                    details={
                        "case_id": context.case_id,
                        "source_reference": entry.source_reference,
                    },
                ),
            )
            candidates.append(candidate)
        return tuple(candidates)

    def confirm_candidate(
        self,
        actor: PlanningActor,
        candidate_id: str,
        *,
        expected_version: int,
    ) -> ClinicalContextCandidate:
        return self._decide_candidate(
            actor,
            candidate_id,
            expected_version=expected_version,
            status=CandidateConfirmationStatus.DOCTOR_CONFIRMED,
        )

    def reject_candidate(
        self,
        actor: PlanningActor,
        candidate_id: str,
        *,
        expected_version: int,
    ) -> ClinicalContextCandidate:
        return self._decide_candidate(
            actor,
            candidate_id,
            expected_version=expected_version,
            status=CandidateConfirmationStatus.REJECTED,
        )

    def _decide_candidate(
        self,
        actor: PlanningActor,
        candidate_id: str,
        *,
        expected_version: int,
        status: CandidateConfirmationStatus,
    ) -> ClinicalContextCandidate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._candidate(candidate_id)
        if current.version != expected_version:
            # The repository repeats this check atomically; this early error is clearer.
            raise FollowupPlanningStateError(
                f"expected candidate version {expected_version}, found {current.version}"
            )
        if current.confirmation_status != CandidateConfirmationStatus.EXTRACTED:
            raise FollowupPlanningStateError("candidate has already received a decision")
        now = self._now()
        values = current.model_dump(mode="python")
        values.update(
            version=current.version + 1,
            confirmation_status=status,
            confirmed_by=(actor.actor_id if status == CandidateConfirmationStatus.DOCTOR_CONFIRMED else None),
            confirmed_at=(now if status == CandidateConfirmationStatus.DOCTOR_CONFIRMED else None),
            created_at=now,
        )
        decided = ClinicalContextCandidate.model_validate(values)
        self.repository.append_candidate(
            decided,
            expected_version=expected_version,
            audit_event=self._audit(
                actor,
                entity_type="clinical_context_candidate",
                entity_id=candidate_id,
                event_type=(
                    "candidate_confirmed"
                    if status == CandidateConfirmationStatus.DOCTOR_CONFIRMED
                    else "candidate_rejected"
                ),
                occurred_at=now,
                details={"candidate_version": decided.version},
            ),
        )
        return decided

    def lookup_evidence(
        self,
        actor: PlanningActor,
        candidate_id: str,
        *,
        exact_topic_id: str,
        requested_use: EvidenceUse,
    ) -> EvidenceLookupResult:
        self._require_enabled()
        self._require_clinician(actor)
        self._confirmed_candidate(candidate_id)
        snapshot = (
            self.evidence_reader.lookup_exact(exact_topic_id)
            if self.evidence_reader is not None
            else None
        )
        if snapshot is None:
            return EvidenceLookupResult(
                status=EvidenceLookupStatus.NO_MATCH,
                exact_topic_id=exact_topic_id,
                requested_use=requested_use,
                snapshot=None,
            )
        if (
            requested_use not in SAFE_MONITORING_USES
            or requested_use not in snapshot.permitted_use
        ):
            return EvidenceLookupResult(
                status=EvidenceLookupStatus.UNSUPPORTED_USE,
                exact_topic_id=exact_topic_id,
                requested_use=requested_use,
                snapshot=snapshot,
            )
        status = (
            EvidenceLookupStatus.NOT_ASSESSED
            if snapshot.review_status == EvidenceReviewStatus.NOT_ASSESSED
            else EvidenceLookupStatus.MATCHED
        )
        return EvidenceLookupResult(
            status=status,
            exact_topic_id=exact_topic_id,
            requested_use=requested_use,
            snapshot=snapshot,
        )

    def create_evidence_definition(
        self,
        actor: PlanningActor,
        candidate_id: str,
        lookup: EvidenceLookupResult,
        *,
        label: str,
        patient_prompt: str,
        response_type: ResponseType,
        unit: str | None = None,
        options: Sequence[ResponseOption] = (),
        definition_id: str | None = None,
    ) -> MonitoringItemDefinition:
        self._require_enabled()
        self._require_clinician(actor)
        self._confirmed_candidate(candidate_id)
        if lookup.status not in {
            EvidenceLookupStatus.MATCHED,
            EvidenceLookupStatus.NOT_ASSESSED,
        } or lookup.snapshot is None:
            raise EvidenceUseNotPermittedError(
                "only a matched or explicitly not_assessed snapshot can be referenced"
            )
        if lookup.requested_use not in SAFE_MONITORING_USES:
            raise EvidenceUseNotPermittedError("requested evidence use is not executable")
        snapshot = lookup.snapshot
        if lookup.requested_use not in snapshot.permitted_use:
            raise EvidenceUseNotPermittedError(
                "evidence snapshot does not permit the requested monitoring use"
            )
        now = self._now()
        definition = MonitoringItemDefinition(
            definition_id=definition_id or self._id("definition"),
            version=1,
            label=label,
            patient_prompt=patient_prompt,
            response_type=response_type,
            unit=unit,
            options=tuple(options),
            evidence_refs=(
                SnapshotReference(
                    snapshot_id=snapshot.snapshot_id,
                    version=snapshot.version,
                    snapshot_hash=snapshot.snapshot_hash,
                ),
            ),
            permitted_use=lookup.requested_use,
            review_status=snapshot.review_status,
            origin=MonitoringDefinitionOrigin.EVIDENCE_REFERENCED,
            authored_by=actor.actor_id,
            created_at=now,
            synthetic=True,
        )
        self.repository.append_definition(
            definition,
            expected_version=0,
            evidence_snapshots=(snapshot,),
            audit_event=self._audit(
                actor,
                entity_type="monitoring_definition",
                entity_id=definition.definition_id,
                event_type="evidence_definition_created",
                occurred_at=now,
                details={
                    "candidate_id": candidate_id,
                    "review_status": snapshot.review_status.value,
                    "knowledge_effect": "informational_only",
                    "runtime_authority": "none",
                },
            ),
        )
        return definition

    def create_manual_definition(
        self,
        actor: PlanningActor,
        *,
        label: str,
        patient_prompt: str,
        response_type: ResponseType,
        permitted_use: EvidenceUse,
        unit: str | None = None,
        options: Sequence[ResponseOption] = (),
        definition_id: str | None = None,
    ) -> MonitoringItemDefinition:
        self._require_enabled()
        self._require_clinician(actor)
        if permitted_use not in SAFE_MONITORING_USES:
            raise EvidenceUseNotPermittedError(
                "manual monitoring item use is outside the safe collection boundary"
            )
        now = self._now()
        definition = MonitoringItemDefinition(
            definition_id=definition_id or self._id("definition"),
            version=1,
            label=label,
            patient_prompt=patient_prompt,
            response_type=response_type,
            unit=unit,
            options=tuple(options),
            evidence_refs=(),
            permitted_use=permitted_use,
            review_status=EvidenceReviewStatus.NOT_ASSESSED,
            origin=MonitoringDefinitionOrigin.DOCTOR_AUTHORED,
            authored_by=actor.actor_id,
            created_at=now,
            synthetic=True,
        )
        self.repository.append_definition(
            definition,
            expected_version=0,
            evidence_snapshots=(),
            audit_event=self._audit(
                actor,
                entity_type="monitoring_definition",
                entity_id=definition.definition_id,
                event_type="manual_definition_created",
                occurred_at=now,
                details={"review_status": EvidenceReviewStatus.NOT_ASSESSED.value},
            ),
        )
        return definition

    def register_synthetic_schedule_template(
        self,
        actor: PlanningActor,
        template: ScheduleTemplate,
        *,
        expected_version: int = 0,
    ) -> ScheduleTemplate:
        """Register a demo fixture; this is intentionally not an approval workflow."""

        self._require_enabled()
        self._require_clinician(actor)
        checked = ScheduleTemplate.model_validate(template.model_dump(mode="python"))
        now = self._now()
        self.repository.append_schedule_template(
            checked,
            expected_version=expected_version,
            audit_event=self._audit(
                actor,
                entity_type="synthetic_schedule_template",
                entity_id=checked.schedule_id,
                event_type="synthetic_demo_template_registered",
                occurred_at=now,
                details={
                    "approval_scope": "demo_only",
                    "approval_status": checked.approval_status.value,
                },
            ),
        )
        return checked

    def create_draft_plan(
        self,
        actor: PlanningActor,
        *,
        confirmed_candidate_ids: Sequence[str],
        effective_from: datetime,
        change_summary: str,
        pathway_reference: str | None = None,
        plan_id: str | None = None,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        require_aware(effective_from, "effective_from")
        candidates = self._same_patient_confirmed_candidates(confirmed_candidate_ids)
        now = self._now()
        stable_plan_id = plan_id or self._id("plan")
        plan = PatientFollowupPlanVersion(
            plan_id=stable_plan_id,
            plan_version_id=f"{stable_plan_id}:v1",
            patient_id=candidates[0].patient_id,
            pathway_reference=pathway_reference,
            version=1,
            revision=1,
            status=PlanStatus.DRAFT,
            effective_from=effective_from,
            created_by=actor.actor_id,
            created_at=now,
            change_summary=change_summary,
            supersedes_version=None,
            published_by=None,
            published_at=None,
            synthetic=True,
        )
        aggregate = PlanAggregate(plan=plan, items=())
        self.repository.append_plan_revision(
            aggregate,
            expected_revision=0,
            audit_event=self._audit(
                actor,
                entity_type="followup_plan",
                entity_id=stable_plan_id,
                event_type="draft_plan_created",
                occurred_at=now,
                details={
                    "plan_version": 1,
                    "candidate_ids": list(confirmed_candidate_ids),
                },
            ),
        )
        return aggregate

    def add_item_from_definition(
        self,
        actor: PlanningActor,
        plan_id: str,
        *,
        expected_version: int,
        definition_id: str,
        definition_version: int,
        source_candidate_ids: Sequence[str],
        change_reason: str,
        effective_start: date | None = None,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._editable_plan(plan_id, expected_version)
        definition = self.repository.get_definition(definition_id, definition_version)
        if definition is None:
            raise LookupError("monitoring definition was not found")
        if definition.permitted_use not in SAFE_MONITORING_USES:
            raise EvidenceUseNotPermittedError("definition is not executable")
        candidates = self._same_patient_confirmed_candidates(source_candidate_ids)
        if any(candidate.patient_id != current.plan.patient_id for candidate in candidates):
            raise ValueError("candidate patient does not match plan patient")
        now = self._now()
        item_origin = (
            FieldOrigin.KNOWLEDGE_REFERENCE
            if definition.origin == MonitoringDefinitionOrigin.EVIDENCE_REFERENCED
            else FieldOrigin.DOCTOR_CURRENT_SETTING
        )
        definition_ref = f"{definition.definition_id}@{definition.version}"
        item = PatientFollowupItem(
            item_id=self._id("item"),
            item_version=1,
            plan_version_id=current.plan.plan_version_id,
            source_template_item_id=None,
            source_candidate_ids=tuple(source_candidate_ids),
            monitoring_definition_ref=VersionedReference(
                reference_id=definition.definition_id,
                version=definition.version,
            ),
            evidence_snapshot_refs=definition.evidence_refs,
            prompt=definition.patient_prompt,
            response_type=definition.response_type,
            unit=definition.unit,
            options=definition.options,
            schedule=None,
            display_order=len(current.items),
            status=FollowupItemStatus.ACTIVE,
            series_id=self._id("series"),
            effective_start=effective_start or current.plan.effective_from.date(),
            effective_end=None,
            change_reason=change_reason,
            field_provenance=self._item_provenance(
                origin=item_origin,
                source_reference=definition_ref,
                actor_reference=actor.actor_id,
                schedule=None,
            ),
            created_by=actor.actor_id,
            created_at=now,
            synthetic=True,
        )
        items = (*current.items, item)
        return self._append_draft_revision(
            actor,
            current,
            items=items,
            expected_version=expected_version,
            event_type="item_added",
            change_summary=change_reason,
            details={"item_id": item.item_id, "definition_id": definition_id},
        )

    def apply_schedule_template(
        self,
        actor: PlanningActor,
        plan_id: str,
        item_id: str,
        *,
        expected_version: int,
        schedule_id: str,
        schedule_version: int,
        change_reason: str,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._editable_plan(plan_id, expected_version)
        template = self.repository.get_schedule_template(schedule_id, schedule_version)
        if template is None:
            raise LookupError("synthetic schedule template was not found")
        if (
            template.approval_status != ScheduleApprovalStatus.APPROVED
            or template.values is None
        ):
            raise FollowupPlanningStateError(
                "only an approved synthetic demo template can prefill a schedule"
            )
        source = f"{template.schedule_id}@{template.version}"
        provenance = self._schedule_provenance(
            FieldOrigin.INSTITUTION_TEMPLATE, source_reference=source
        )
        resolved = ResolvedSchedule(
            values=template.values,
            basis=ScheduleBasis.INSTITUTION_TEMPLATE,
            source_template_ref=VersionedReference(
                reference_id=template.schedule_id, version=template.version
            ),
            source_template_values=template.values,
            field_provenance=provenance,
        )
        return self._replace_item_schedule(
            actor,
            current,
            item_id=item_id,
            expected_version=expected_version,
            schedule=resolved,
            change_reason=change_reason,
            event_type="approved_demo_template_applied",
        )

    def set_manual_schedule(
        self,
        actor: PlanningActor,
        plan_id: str,
        item_id: str,
        *,
        expected_version: int,
        values: ScheduleValues,
        change_reason: str,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._editable_plan(plan_id, expected_version)
        item = self._item(current, item_id)
        prior = item.schedule
        has_template = prior is not None and prior.source_template_ref is not None
        origin = (
            FieldOrigin.DOCTOR_PATIENT_OVERRIDE
            if has_template
            else FieldOrigin.DOCTOR_CURRENT_SETTING
        )
        resolved = ResolvedSchedule(
            values=values,
            basis=ScheduleBasis.DOCTOR_MANUAL,
            source_template_ref=(prior.source_template_ref if has_template else None),
            source_template_values=(
                prior.source_template_values if has_template else None
            ),
            field_provenance=self._schedule_provenance(
                origin, source_reference=actor.actor_id
            ),
        )
        return self._replace_item_schedule(
            actor,
            current,
            item_id=item_id,
            expected_version=expected_version,
            schedule=resolved,
            change_reason=change_reason,
            event_type="manual_schedule_set",
        )

    def edit_item(
        self,
        actor: PlanningActor,
        plan_id: str,
        item_id: str,
        *,
        expected_version: int,
        prompt: str,
        response_type: ResponseType,
        unit: str | None,
        options: Sequence[ResponseOption],
        semantic_change: bool,
        change_reason: str,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._editable_plan(plan_id, expected_version)
        item = self._item(current, item_id)
        if item.status == FollowupItemStatus.STOPPED:
            raise FollowupPlanningStateError("stopped item cannot be edited")
        response_structure_changed = (
            response_type != item.response_type
            or unit != item.unit
            or tuple(options) != item.options
        )
        if response_structure_changed and not semantic_change:
            raise SemanticChangeDeclarationError(
                "response structure changes must be declared semantic"
            )
        changed_paths = set()
        if prompt != item.prompt:
            changed_paths.add("prompt")
        if response_type != item.response_type:
            changed_paths.add("response_type")
        if unit != item.unit:
            changed_paths.add("unit")
        if tuple(options) != item.options:
            changed_paths.add("options")
        if not changed_paths:
            raise ValueError("item edit did not change any field")
        now = self._now()
        provenance = self._replace_item_field_provenance(
            item.field_provenance,
            {
                path: FieldProvenance(
                    field_path=path,
                    origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
                    source_reference=actor.actor_id,
                )
                for path in changed_paths
            }
            | {
                "change_reason": FieldProvenance(
                    field_path="change_reason",
                    origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
                    source_reference=actor.actor_id,
                )
            },
        )
        if semantic_change:
            provenance = self._replace_item_field_provenance(
                provenance,
                {
                    "series_id": FieldProvenance(
                        field_path="series_id",
                        origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
                        source_reference=actor.actor_id,
                    )
                },
            )
        updated = self._updated_item(
            item,
            item_version=item.item_version + 1,
            prompt=prompt,
            response_type=response_type,
            unit=unit,
            options=tuple(options),
            series_id=(self._id("series") if semantic_change else item.series_id),
            change_reason=change_reason,
            field_provenance=provenance,
            created_by=actor.actor_id,
            created_at=now,
        )
        return self._replace_item_in_revision(
            actor,
            current,
            updated,
            expected_version=expected_version,
            event_type="item_edited",
            change_summary=change_reason,
            details={
                "item_id": item_id,
                "semantic_change": semantic_change,
                "series_id": updated.series_id,
            },
        )

    def copy_item(
        self,
        actor: PlanningActor,
        plan_id: str,
        item_id: str,
        *,
        expected_version: int,
        change_reason: str,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._editable_plan(plan_id, expected_version)
        source = self._item(current, item_id)
        if source.status == FollowupItemStatus.STOPPED:
            raise FollowupPlanningStateError("stopped item cannot be copied")
        now = self._now()
        copied_values = source.model_dump(mode="python")
        copied_provenance = self._replace_item_field_provenance(
            source.field_provenance,
            {
                "source_template_item_id": FieldProvenance(
                    field_path="source_template_item_id",
                    origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                    source_reference=actor.actor_id,
                ),
                "series_id": FieldProvenance(
                    field_path="series_id",
                    origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                    source_reference=actor.actor_id,
                ),
                "display_order": FieldProvenance(
                    field_path="display_order",
                    origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                    source_reference=actor.actor_id,
                ),
                "change_reason": FieldProvenance(
                    field_path="change_reason",
                    origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                    source_reference=actor.actor_id,
                ),
            },
        )
        copied_values.update(
            item_id=self._id("item"),
            item_version=1,
            source_template_item_id=source.item_id,
            display_order=len(current.items),
            series_id=self._id("series"),
            change_reason=change_reason,
            field_provenance=copied_provenance,
            created_by=actor.actor_id,
            created_at=now,
        )
        copied = PatientFollowupItem.model_validate(copied_values)
        return self._append_draft_revision(
            actor,
            current,
            items=(*current.items, copied),
            expected_version=expected_version,
            event_type="item_copied",
            change_summary=change_reason,
            details={"source_item_id": item_id, "item_id": copied.item_id},
        )

    def reorder_items(
        self,
        actor: PlanningActor,
        plan_id: str,
        *,
        expected_version: int,
        ordered_item_ids: Sequence[str],
        change_reason: str,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._editable_plan(plan_id, expected_version)
        existing_ids = [item.item_id for item in current.items]
        if len(ordered_item_ids) != len(set(ordered_item_ids)) or set(
            ordered_item_ids
        ) != set(existing_ids):
            raise ValueError("reorder must contain every current item exactly once")
        if list(ordered_item_ids) == existing_ids:
            raise ValueError("reorder did not change item order")
        by_id = {item.item_id: item for item in current.items}
        now = self._now()
        reordered: list[PatientFollowupItem] = []
        for display_order, item_id in enumerate(ordered_item_ids):
            item = by_id[item_id]
            if item.display_order == display_order:
                reordered.append(item)
                continue
            provenance = self._replace_item_field_provenance(
                item.field_provenance,
                {
                    "display_order": FieldProvenance(
                        field_path="display_order",
                        origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
                        source_reference=actor.actor_id,
                    ),
                    "change_reason": FieldProvenance(
                        field_path="change_reason",
                        origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
                        source_reference=actor.actor_id,
                    ),
                },
            )
            reordered.append(
                self._updated_item(
                    item,
                    item_version=item.item_version + 1,
                    display_order=display_order,
                    change_reason=change_reason,
                    field_provenance=provenance,
                    created_by=actor.actor_id,
                    created_at=now,
                )
            )
        return self._append_draft_revision(
            actor,
            current,
            items=tuple(reordered),
            expected_version=expected_version,
            event_type="items_reordered",
            change_summary=change_reason,
            details={"ordered_item_ids": list(ordered_item_ids)},
        )

    def stop_item(
        self,
        actor: PlanningActor,
        plan_id: str,
        item_id: str,
        *,
        expected_version: int,
        effective_end: date,
        change_reason: str,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._editable_plan(plan_id, expected_version)
        item = self._item(current, item_id)
        if item.status == FollowupItemStatus.STOPPED:
            raise FollowupPlanningStateError("item is already stopped")
        now = self._now()
        provenance = self._replace_item_field_provenance(
            item.field_provenance,
            {
                "status": FieldProvenance(
                    field_path="status",
                    origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
                    source_reference=actor.actor_id,
                ),
                "effective_end": FieldProvenance(
                    field_path="effective_end",
                    origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
                    source_reference=actor.actor_id,
                ),
                "change_reason": FieldProvenance(
                    field_path="change_reason",
                    origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
                    source_reference=actor.actor_id,
                ),
            },
        )
        stopped = self._updated_item(
            item,
            item_version=item.item_version + 1,
            status=FollowupItemStatus.STOPPED,
            effective_end=effective_end,
            change_reason=change_reason,
            field_provenance=provenance,
            created_by=actor.actor_id,
            created_at=now,
        )
        return self._replace_item_in_revision(
            actor,
            current,
            stopped,
            expected_version=expected_version,
            event_type="item_stopped",
            change_summary=change_reason,
            details={"item_id": item_id, "effective_end": effective_end.isoformat()},
        )

    def publish_plan(
        self,
        actor: PlanningActor,
        plan_id: str,
        *,
        expected_version: int,
        confirmation: PublishConfirmation,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        current = self._editable_plan(plan_id, expected_version)
        if (
            confirmation.plan_id != plan_id
            or confirmation.expected_revision != expected_version
            or confirmation.confirmed_by != actor.actor_id
        ):
            raise FollowupPlanningStateError(
                "publication confirmation must bind the actor and exact draft revision"
            )
        active = [
            item for item in current.items if item.status == FollowupItemStatus.ACTIVE
        ]
        if not active:
            raise FollowupPlanningStateError("a plan requires at least one active item")
        if any(item.schedule is None for item in active):
            raise FollowupPlanningStateError(
                "every active item needs an explicit schedule before publication"
            )
        now = self._now()
        plan_values = current.plan.model_dump(mode="python")
        plan_values.update(
            revision=current.plan.revision + 1,
            status=PlanStatus.PUBLISHED,
            created_by=actor.actor_id,
            created_at=now,
            published_by=actor.actor_id,
            published_at=confirmation.confirmed_at,
        )
        published_plan = PatientFollowupPlanVersion.model_validate(plan_values)
        published = PlanAggregate(plan=published_plan, items=current.items)
        self.repository.append_plan_revision(
            published,
            expected_revision=expected_version,
            publish_version=published_plan.version,
            supersede_version=(
                published_plan.version - 1 if published_plan.version > 1 else None
            ),
            audit_event=self._audit(
                actor,
                entity_type="followup_plan",
                entity_id=plan_id,
                event_type="plan_published",
                occurred_at=now,
                details={
                    "plan_version": published_plan.version,
                    "confirmed_revision": confirmation.expected_revision,
                },
            ),
        )
        return published

    def create_next_version(
        self,
        actor: PlanningActor,
        plan_id: str,
        *,
        expected_version: int,
        effective_from: datetime,
        change_summary: str,
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        require_aware(effective_from, "effective_from")
        current = self._plan(plan_id)
        if current.plan.revision != expected_version:
            raise FollowupPlanningStateError(
                f"expected plan revision {expected_version}, found {current.plan.revision}"
            )
        if current.plan.status != PlanStatus.PUBLISHED:
            raise FollowupPlanningStateError(
                "next version can only be created from the latest published plan"
            )
        now = self._now()
        next_version = current.plan.version + 1
        plan_values = current.plan.model_dump(mode="python")
        plan_values.update(
            plan_version_id=f"{plan_id}:v{next_version}",
            version=next_version,
            revision=current.plan.revision + 1,
            status=PlanStatus.DRAFT,
            effective_from=effective_from,
            created_by=actor.actor_id,
            created_at=now,
            change_summary=change_summary,
            supersedes_version=current.plan.version,
            published_by=None,
            published_at=None,
        )
        draft_plan = PatientFollowupPlanVersion.model_validate(plan_values)
        copied_items = []
        for display_order, item in enumerate(
            value
            for value in current.items
            if value.status == FollowupItemStatus.ACTIVE
        ):
            item_values = item.model_dump(mode="python")
            next_provenance = self._replace_item_field_provenance(
                item.field_provenance,
                {
                    "effective_start": FieldProvenance(
                        field_path="effective_start",
                        origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                        source_reference=actor.actor_id,
                    ),
                    "display_order": FieldProvenance(
                        field_path="display_order",
                        origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                        source_reference=actor.actor_id,
                    ),
                    "change_reason": FieldProvenance(
                        field_path="change_reason",
                        origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                        source_reference=actor.actor_id,
                    ),
                },
            )
            item_values.update(
                item_version=item.item_version + 1,
                plan_version_id=draft_plan.plan_version_id,
                display_order=display_order,
                effective_start=effective_from.date(),
                change_reason=change_summary,
                field_provenance=next_provenance,
                created_by=actor.actor_id,
                created_at=now,
            )
            copied_items.append(PatientFollowupItem.model_validate(item_values))
        aggregate = PlanAggregate(plan=draft_plan, items=tuple(copied_items))
        self.repository.append_plan_revision(
            aggregate,
            expected_revision=expected_version,
            audit_event=self._audit(
                actor,
                entity_type="followup_plan",
                entity_id=plan_id,
                event_type="next_plan_version_created",
                occurred_at=now,
                details={
                    "plan_version": next_version,
                    "supersedes_version": current.plan.version,
                },
            ),
        )
        return aggregate

    def preview_plan(
        self,
        actor: PlanningActor,
        plan_id: str,
        *,
        window_start: date,
    ) -> SchedulePreview:
        self._require_enabled()
        self._require_clinician(actor)
        return preview_schedule(self._plan(plan_id).items, window_start=window_start)

    def calculate_plan_burden(
        self, actor: PlanningActor, plan_id: str
    ) -> PatientBurden:
        self._require_enabled()
        self._require_clinician(actor)
        return calculate_patient_burden(self._plan(plan_id).items)

    def get_doctor_plan(
        self, actor: PlanningActor, plan_id: str
    ) -> PlanAggregate:
        self._require_enabled()
        self._require_clinician(actor)
        return self._plan(plan_id)

    def get_nurse_current_plan(
        self, actor: PlanningActor, patient_id: str
    ) -> NursePlanView:
        self._require_enabled()
        if actor.role != ActorRole.NURSE:
            raise FollowupPlanningAuthorizationError("nurse role is required")
        aggregate = self._published_plan(patient_id)
        return NursePlanView(
            plan_id=aggregate.plan.plan_id,
            patient_id=aggregate.plan.patient_id,
            version=aggregate.plan.version,
            effective_from=aggregate.plan.effective_from,
            change_summary=aggregate.plan.change_summary,
            items=tuple(
                item
                for item in aggregate.items
                if item.status == FollowupItemStatus.ACTIVE
            ),
        )

    def get_patient_current_plan(
        self, actor: PlanningActor, patient_id: str
    ) -> PatientPlanView:
        self._require_enabled()
        if actor.role != ActorRole.PATIENT or actor.actor_id != patient_id:
            raise FollowupPlanningAuthorizationError(
                "patients can read only their own published plan"
            )
        aggregate = self._published_plan(patient_id)
        items = []
        for item in aggregate.items:
            if item.status != FollowupItemStatus.ACTIVE:
                continue
            if item.schedule is None:  # Published validation should make this unreachable.
                raise FollowupPlanningStateError("published item has no schedule")
            items.append(
                PatientItemView(
                    item_id=item.item_id,
                    item_version=item.item_version,
                    prompt=item.prompt,
                    response_type=item.response_type,
                    unit=item.unit,
                    options=item.options,
                    schedule=item.schedule,
                    display_order=item.display_order,
                )
            )
        return PatientPlanView(
            plan_id=aggregate.plan.plan_id,
            patient_id=aggregate.plan.patient_id,
            version=aggregate.plan.version,
            effective_from=aggregate.plan.effective_from,
            items=tuple(items),
        )

    def record_completed_answer_binding(
        self,
        actor: PlanningActor,
        *,
        patient_id: str,
        item_id: str,
        response_snapshot: object,
        answer_id: str | None = None,
    ) -> CompletedAnswerBinding:
        self._require_enabled()
        if actor.role != ActorRole.PATIENT or actor.actor_id != patient_id:
            raise FollowupPlanningAuthorizationError(
                "patients can record only their own synthetic answers"
            )
        try:
            json.dumps(response_snapshot, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("response_snapshot must be JSON serializable") from exc
        aggregate = self._published_plan(patient_id)
        item = self._item(aggregate, item_id)
        if item.status != FollowupItemStatus.ACTIVE:
            raise FollowupPlanningStateError("cannot answer a stopped item")
        now = self._now()
        binding = CompletedAnswerBinding(
            answer_id=answer_id or self._id("answer"),
            patient_id=patient_id,
            plan_id=aggregate.plan.plan_id,
            plan_version=aggregate.plan.version,
            item_id=item.item_id,
            item_version=item.item_version,
            series_id=item.series_id,
            prompt_snapshot=item.prompt,
            response_type_snapshot=item.response_type,
            response_snapshot=response_snapshot,
            answered_at=now,
            synthetic=True,
        )
        self.repository.append_answer_binding(
            binding,
            audit_event=self._audit(
                actor,
                entity_type="completed_answer_binding",
                entity_id=binding.answer_id,
                event_type="answer_snapshot_bound",
                occurred_at=now,
                details={
                    "plan_version": binding.plan_version,
                    "item_id": binding.item_id,
                    "item_version": binding.item_version,
                },
            ),
        )
        return binding

    def _replace_item_schedule(
        self,
        actor: PlanningActor,
        current: PlanAggregate,
        *,
        item_id: str,
        expected_version: int,
        schedule: ResolvedSchedule,
        change_reason: str,
        event_type: str,
    ) -> PlanAggregate:
        item = self._item(current, item_id)
        if item.status == FollowupItemStatus.STOPPED:
            raise FollowupPlanningStateError("stopped item cannot be rescheduled")
        now = self._now()
        schedule_provenance = {
            provenance.field_path: provenance
            for provenance in (
                schedule.field_provenance.interval_days,
                schedule.field_provenance.times_of_day,
                schedule.field_provenance.duration_days,
                schedule.field_provenance.timezone,
            )
        }
        schedule_provenance["change_reason"] = FieldProvenance(
            field_path="change_reason",
            origin=FieldOrigin.DOCTOR_PATIENT_OVERRIDE,
            source_reference=actor.actor_id,
        )
        updated = self._updated_item(
            item,
            item_version=item.item_version + 1,
            schedule=schedule,
            change_reason=change_reason,
            field_provenance=self._replace_item_field_provenance(
                item.field_provenance, schedule_provenance
            ),
            created_by=actor.actor_id,
            created_at=now,
        )
        return self._replace_item_in_revision(
            actor,
            current,
            updated,
            expected_version=expected_version,
            event_type=event_type,
            change_summary=change_reason,
            details={"item_id": item_id},
        )

    def _replace_item_in_revision(
        self,
        actor: PlanningActor,
        current: PlanAggregate,
        updated: PatientFollowupItem,
        *,
        expected_version: int,
        event_type: str,
        change_summary: str,
        details: dict[str, object],
    ) -> PlanAggregate:
        items = tuple(
            updated if item.item_id == updated.item_id else item
            for item in current.items
        )
        return self._append_draft_revision(
            actor,
            current,
            items=items,
            expected_version=expected_version,
            event_type=event_type,
            change_summary=change_summary,
            details=details,
        )

    def _append_draft_revision(
        self,
        actor: PlanningActor,
        current: PlanAggregate,
        *,
        items: Sequence[PatientFollowupItem],
        expected_version: int,
        event_type: str,
        change_summary: str,
        details: dict[str, object],
    ) -> PlanAggregate:
        now = self._now()
        plan_values = current.plan.model_dump(mode="python")
        plan_values.update(
            revision=current.plan.revision + 1,
            created_by=actor.actor_id,
            created_at=now,
            change_summary=change_summary,
        )
        plan = PatientFollowupPlanVersion.model_validate(plan_values)
        aggregate = PlanAggregate(
            plan=plan,
            items=tuple(sorted(items, key=lambda item: item.display_order)),
        )
        self.repository.append_plan_revision(
            aggregate,
            expected_revision=expected_version,
            audit_event=self._audit(
                actor,
                entity_type="followup_plan",
                entity_id=plan.plan_id,
                event_type=event_type,
                occurred_at=now,
                details={"plan_revision": plan.revision, **details},
            ),
        )
        return aggregate

    @staticmethod
    def _updated_item(
        item: PatientFollowupItem, **updates: object
    ) -> PatientFollowupItem:
        values = item.model_dump(mode="python")
        values.update(updates)
        return PatientFollowupItem.model_validate(values)

    @staticmethod
    def _replace_item_field_provenance(
        existing: Sequence[FieldProvenance],
        replacements: dict[str, FieldProvenance],
    ) -> tuple[FieldProvenance, ...]:
        values = {item.field_path: item for item in existing}
        values.update(replacements)
        return tuple(values[key] for key in sorted(values))

    @staticmethod
    def _schedule_provenance(
        origin: FieldOrigin, *, source_reference: str
    ) -> ScheduleFieldProvenance:
        return ScheduleFieldProvenance(
            **{
                name: FieldProvenance(
                    field_path=f"schedule.{name}",
                    origin=origin,
                    source_reference=source_reference,
                )
                for name in (
                    "interval_days",
                    "times_of_day",
                    "duration_days",
                    "timezone",
                )
            }
        )

    @classmethod
    def _item_provenance(
        cls,
        *,
        origin: FieldOrigin,
        source_reference: str,
        actor_reference: str,
        schedule: ResolvedSchedule | None,
    ) -> tuple[FieldProvenance, ...]:
        definition_fields = (
            "monitoring_definition_ref",
            "evidence_snapshot_refs",
            "prompt",
            "response_type",
            "unit",
            "options",
        )
        doctor_fields = (
            "source_candidate_ids",
            "display_order",
            "review_role",
            "include_in_summary",
            "status",
            "series_id",
            "effective_start",
            "effective_end",
            "change_reason",
        )
        values = {
            field_name: FieldProvenance(
                field_path=field_name,
                origin=origin,
                source_reference=source_reference,
            )
            for field_name in definition_fields
        }
        values.update(
            {
                field_name: FieldProvenance(
                    field_path=field_name,
                    origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                    source_reference=actor_reference,
                )
                for field_name in doctor_fields
            }
        )
        if schedule is not None:
            for provenance in (
                schedule.field_provenance.interval_days,
                schedule.field_provenance.times_of_day,
                schedule.field_provenance.duration_days,
                schedule.field_provenance.timezone,
            ):
                values[provenance.field_path] = provenance
        return tuple(values[key] for key in sorted(values))

    def _editable_plan(self, plan_id: str, expected_version: int) -> PlanAggregate:
        current = self._plan(plan_id)
        if current.plan.revision != expected_version:
            raise FollowupPlanningStateError(
                f"expected plan revision {expected_version}, found {current.plan.revision}"
            )
        if current.plan.status != PlanStatus.DRAFT:
            raise FollowupPlanningStateError(
                "published plan is immutable; create the next version before editing"
            )
        return current

    def _plan(self, plan_id: str) -> PlanAggregate:
        aggregate = self.repository.get_latest_plan(plan_id)
        if aggregate is None:
            raise LookupError(f"plan {plan_id!r} was not found")
        return aggregate

    def _published_plan(self, patient_id: str) -> PlanAggregate:
        aggregate = self.repository.get_current_published(patient_id)
        if aggregate is None:
            raise LookupError("patient has no published follow-up plan")
        return aggregate

    @staticmethod
    def _item(aggregate: PlanAggregate, item_id: str) -> PatientFollowupItem:
        for item in aggregate.items:
            if item.item_id == item_id:
                return item
        raise LookupError(f"item {item_id!r} was not found in the plan")

    def _candidate(self, candidate_id: str) -> ClinicalContextCandidate:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError(f"candidate {candidate_id!r} was not found")
        return candidate

    def _confirmed_candidate(self, candidate_id: str) -> ClinicalContextCandidate:
        candidate = self._candidate(candidate_id)
        if candidate.confirmation_status != CandidateConfirmationStatus.DOCTOR_CONFIRMED:
            raise FollowupPlanningStateError(
                "candidate must be doctor_confirmed before planning"
            )
        return candidate

    def _same_patient_confirmed_candidates(
        self, candidate_ids: Sequence[str]
    ) -> tuple[ClinicalContextCandidate, ...]:
        if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("at least one unique confirmed candidate is required")
        candidates = tuple(self._confirmed_candidate(value) for value in candidate_ids)
        if len({candidate.patient_id for candidate in candidates}) != 1:
            raise ValueError("all candidates must belong to the same patient")
        if len({candidate.case_id for candidate in candidates}) != 1:
            raise ValueError("all candidates must belong to the same case")
        return candidates

    def _require_enabled(self) -> None:
        if self.mode == PlanningMode.DISABLED:
            raise FollowupPlanningDisabledError("Follow-up Planning is disabled")

    @staticmethod
    def _require_clinician(actor: PlanningActor) -> None:
        if actor.role not in {ActorRole.DOCTOR, ActorRole.CLINICIAN}:
            raise FollowupPlanningAuthorizationError(
                "doctor or clinician role is required"
            )

    def _id(self, prefix: str) -> str:
        return self._id_factory(prefix)

    def _now(self) -> datetime:
        value = self._clock()
        return require_aware(value, "Follow-up Planning clock")

    def _audit(
        self,
        actor: PlanningActor,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        occurred_at: datetime,
        details: dict[str, object],
    ) -> PlanningAuditEvent:
        return PlanningAuditEvent(
            event_id=self._id("audit"),
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor_id=actor.actor_id,
            actor_role=actor.role,
            occurred_at=occurred_at,
            details=details,
        )

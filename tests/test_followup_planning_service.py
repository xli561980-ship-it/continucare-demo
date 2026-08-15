from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from continucare.followup_planning import (
    ActorRole,
    CandidateConfirmationStatus,
    ClinicalContext,
    EvidenceLookupStatus,
    EvidenceUse,
    EvidenceUseNotPermittedError,
    FieldOrigin,
    FollowupItemStatus,
    FollowupPlanningAuthorizationError,
    FollowupPlanningService,
    FollowupPlanningStateError,
    JsonEvidenceSnapshotReader,
    PlanningActor,
    PlanStatus,
    PublishConfirmation,
    ResponseOption,
    ResponseType,
    ScheduleApprovalStatus,
    ScheduleBasis,
    ScheduleTemplate,
    ScheduleValues,
    SemanticChangeDeclarationError,
)


NOW = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "followup_planning"
EVIDENCE_DIGEST = "5092e39c3a8fb8a18357c53c138974dc4d781ee680a081c765bab91b1b2fdcd2"
DOCTOR = PlanningActor(
    actor_id="Practitioner/synthetic-followup-doctor", role=ActorRole.DOCTOR
)
CLINICIAN = PlanningActor(
    actor_id="Practitioner/synthetic-followup-clinician", role=ActorRole.CLINICIAN
)
NURSE = PlanningActor(
    actor_id="Practitioner/synthetic-followup-nurse", role=ActorRole.NURSE
)
PATIENT_ID = "Patient/synthetic-followup-001"
PATIENT = PlanningActor(actor_id=PATIENT_ID, role=ActorRole.PATIENT)


class FixtureContextSource:
    def __init__(self, context: ClinicalContext | None = None):
        self.context = context or ClinicalContext.model_validate_json(
            (FIXTURES / "clinical_context.json").read_text(encoding="utf-8")
        )

    def get_context(self, case_id: str) -> ClinicalContext | None:
        return self.context if self.context.case_id == case_id else None


class DeterministicIds:
    def __init__(self):
        self.counts = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self.counts[prefix] += 1
        return f"{prefix}-synthetic-{self.counts[prefix]}"


def _service(tmp_path):
    return FollowupPlanningService.from_storage(
        tmp_path / "followup.db",
        mode="enabled",
        evidence_reader=JsonEvidenceSnapshotReader(
            FIXTURES / "evidence_snapshots.json",
            expected_payload_sha256=EVIDENCE_DIGEST,
        ),
        clock=lambda: NOW,
        id_factory=DeterministicIds(),
    )


def _ingest_and_confirm(service, *, index=0, actor=DOCTOR):
    candidates = service.ingest_context(
        actor,
        FixtureContextSource(),
        case_id="case-synthetic-followup-001",
    )
    return service.confirm_candidate(
        actor, candidates[index].candidate_id, expected_version=1
    )


def _manual_values(*, duration_days=5, interval_days=2):
    return ScheduleValues(
        interval_days=interval_days,
        times_of_day=(time(9), time(18)),
        duration_days=duration_days,
        timezone="Europe/Berlin",
    )


def _draft_with_manual_item(service):
    candidate = _ingest_and_confirm(service)
    definition = service.create_manual_definition(
        DOCTOR,
        label="合成患者自述",
        patient_prompt="今天是否感觉不适？",
        response_type=ResponseType.BOOLEAN,
        permitted_use=EvidenceUse.PATIENT_REPORTED_RECORD,
    )
    aggregate = service.create_draft_plan(
        DOCTOR,
        confirmed_candidate_ids=(candidate.candidate_id,),
        effective_from=datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
        change_summary="创建合成随访草稿",
    )
    aggregate = service.add_item_from_definition(
        DOCTOR,
        aggregate.plan.plan_id,
        expected_version=aggregate.plan.revision,
        definition_id=definition.definition_id,
        definition_version=definition.version,
        source_candidate_ids=(candidate.candidate_id,),
        change_reason="添加合成随访项目",
    )
    return candidate, definition, aggregate


def _draft_with_scheduled_item(service):
    candidate, definition, aggregate = _draft_with_manual_item(service)
    item_id = aggregate.items[0].item_id
    aggregate = service.set_manual_schedule(
        DOCTOR,
        aggregate.plan.plan_id,
        item_id,
        expected_version=aggregate.plan.revision,
        values=_manual_values(),
        change_reason="医生为合成患者明确设置时间",
    )
    return candidate, definition, aggregate


def _publish(service, aggregate, actor=DOCTOR):
    return service.publish_plan(
        actor,
        aggregate.plan.plan_id,
        expected_version=aggregate.plan.revision,
        confirmation=PublishConfirmation(
            plan_id=aggregate.plan.plan_id,
            expected_revision=aggregate.plan.revision,
            confirmed_by=actor.actor_id,
            confirmed=True,
            confirmed_at=NOW,
        ),
    )


def _schedule_templates():
    raw = json.loads(
        (FIXTURES / "schedule_templates.json").read_text(encoding="utf-8")
    )
    return {
        value["approval_status"]: ScheduleTemplate.model_validate(value)
        for value in raw["templates"]
    }


def test_context_is_structured_synthetic_only_and_extracted_candidate_is_gated(tmp_path):
    service = _service(tmp_path)
    candidates = service.ingest_context(
        DOCTOR,
        FixtureContextSource(),
        case_id="case-synthetic-followup-001",
    )

    assert all(
        candidate.confirmation_status == CandidateConfirmationStatus.EXTRACTED
        for candidate in candidates
    )
    assert all(candidate.source_excerpt_snapshot for candidate in candidates)
    with pytest.raises(FollowupPlanningStateError, match="doctor_confirmed"):
        service.create_draft_plan(
            DOCTOR,
            confirmed_candidate_ids=(candidates[0].candidate_id,),
            effective_from=NOW,
            change_summary="不应创建",
        )
    assert service.repository.get_latest_plan("plan-synthetic-1") is None


def test_model_construct_cannot_bypass_synthetic_context_guard(tmp_path):
    service = _service(tmp_path)
    valid = FixtureContextSource().context
    bypass_attempt = valid.model_copy(update={"synthetic": False})

    with pytest.raises(ValidationError, match="Input should be True"):
        service.ingest_context(
            DOCTOR,
            FixtureContextSource(bypass_attempt),
            case_id=valid.case_id,
        )


def test_only_clinician_roles_can_confirm_modify_or_publish(tmp_path):
    service = _service(tmp_path)
    candidates = service.ingest_context(
        DOCTOR,
        FixtureContextSource(),
        case_id="case-synthetic-followup-001",
    )
    with pytest.raises(FollowupPlanningAuthorizationError):
        service.confirm_candidate(
            NURSE, candidates[0].candidate_id, expected_version=1
        )

    _, _, aggregate = _draft_with_scheduled_item(_service(tmp_path / "second"))
    other_service = _service(tmp_path / "third")
    _, _, other_aggregate = _draft_with_scheduled_item(other_service)
    with pytest.raises(FollowupPlanningAuthorizationError):
        _publish(other_service, other_aggregate, actor=NURSE)


def test_exact_evidence_lookup_returns_all_four_explicit_outcomes(tmp_path):
    service = _service(tmp_path)
    candidate = _ingest_and_confirm(service)

    not_assessed = service.lookup_evidence(
        DOCTOR,
        candidate.candidate_id,
        exact_topic_id="synthetic-topic-nausea",
        requested_use=EvidenceUse.FOLLOWUP_QUESTION,
    )
    matched = service.lookup_evidence(
        DOCTOR,
        candidate.candidate_id,
        exact_topic_id="synthetic-topic-weight-self-report",
        requested_use=EvidenceUse.PATIENT_SELF_MEASUREMENT,
    )
    no_match = service.lookup_evidence(
        DOCTOR,
        candidate.candidate_id,
        exact_topic_id="synthetic-topic-nausea-near-match",
        requested_use=EvidenceUse.FOLLOWUP_QUESTION,
    )
    unsupported = service.lookup_evidence(
        DOCTOR,
        candidate.candidate_id,
        exact_topic_id="synthetic-topic-lab-order-only",
        requested_use=EvidenceUse.LAB_ORDER,
    )

    assert not_assessed.status == EvidenceLookupStatus.NOT_ASSESSED
    assert not_assessed.snapshot.review_status == "not_assessed"
    assert matched.status == EvidenceLookupStatus.MATCHED
    assert no_match.status == EvidenceLookupStatus.NO_MATCH
    assert no_match.snapshot is None
    assert unsupported.status == EvidenceLookupStatus.UNSUPPORTED_USE


def test_no_match_still_allows_manual_item_but_unsupported_use_is_hard_rejected(
    tmp_path,
):
    service = _service(tmp_path)
    candidate = _ingest_and_confirm(service)
    no_match = service.lookup_evidence(
        DOCTOR,
        candidate.candidate_id,
        exact_topic_id="no-such-topic",
        requested_use=EvidenceUse.FOLLOWUP_QUESTION,
    )

    with pytest.raises(EvidenceUseNotPermittedError):
        service.create_evidence_definition(
            DOCTOR,
            candidate.candidate_id,
            no_match,
            label="不应创建",
            patient_prompt="不应创建",
            response_type=ResponseType.TEXT,
        )
    manual = service.create_manual_definition(
        DOCTOR,
        label="医生手动项目",
        patient_prompt="请诚实记录合成体验。",
        response_type=ResponseType.TEXT,
        permitted_use=EvidenceUse.PATIENT_REPORTED_RECORD,
    )
    assert manual.evidence_refs == ()
    assert manual.review_status == "not_assessed"

    with pytest.raises(EvidenceUseNotPermittedError):
        service.create_manual_definition(
            DOCTOR,
            label="不允许的合成检验",
            patient_prompt="不应执行。",
            response_type=ResponseType.TEXT,
            permitted_use=EvidenceUse.LAB_ORDER,
        )


def test_not_assessed_evidence_can_be_referenced_without_gaining_authority(tmp_path):
    service = _service(tmp_path)
    candidate = _ingest_and_confirm(service)
    lookup = service.lookup_evidence(
        DOCTOR,
        candidate.candidate_id,
        exact_topic_id="synthetic-topic-nausea",
        requested_use=EvidenceUse.FOLLOWUP_QUESTION,
    )
    definition = service.create_evidence_definition(
        DOCTOR,
        candidate.candidate_id,
        lookup,
        label="合成恶心自述",
        patient_prompt="今天是否感觉恶心？",
        response_type=ResponseType.BOOLEAN,
    )

    assert definition.review_status == "not_assessed"
    assert definition.origin == "evidence_referenced"
    events = service.repository.list_audit_events(
        entity_type="monitoring_definition", entity_id=definition.definition_id
    )
    assert events[0].details["runtime_authority"] == "none"


def test_no_template_means_no_frequency_or_duration_and_publish_is_blocked(tmp_path):
    service = _service(tmp_path)
    _, _, aggregate = _draft_with_manual_item(service)

    assert aggregate.items[0].schedule is None
    with pytest.raises(FollowupPlanningStateError, match="explicit schedule"):
        _publish(service, aggregate)


def test_only_approved_synthetic_demo_template_prefills_and_override_retains_both_sources(
    tmp_path,
):
    service = _service(tmp_path)
    templates = _schedule_templates()
    for template in templates.values():
        service.register_synthetic_schedule_template(DOCTOR, template)
    _, _, aggregate = _draft_with_manual_item(service)
    item_id = aggregate.items[0].item_id

    draft = templates[ScheduleApprovalStatus.DRAFT.value]
    with pytest.raises(FollowupPlanningStateError, match="approved synthetic demo"):
        service.apply_schedule_template(
            DOCTOR,
            aggregate.plan.plan_id,
            item_id,
            expected_version=aggregate.plan.revision,
            schedule_id=draft.schedule_id,
            schedule_version=draft.version,
            change_reason="draft 不可使用",
        )

    approved = templates[ScheduleApprovalStatus.APPROVED.value]
    aggregate = service.apply_schedule_template(
        DOCTOR,
        aggregate.plan.plan_id,
        item_id,
        expected_version=aggregate.plan.revision,
        schedule_id=approved.schedule_id,
        schedule_version=approved.version,
        change_reason="应用纯合成 demo 模板",
    )
    template_schedule = aggregate.items[0].schedule
    assert template_schedule.basis == ScheduleBasis.INSTITUTION_TEMPLATE
    assert template_schedule.source_template_values == approved.values

    manual = _manual_values(duration_days=7, interval_days=1)
    aggregate = service.set_manual_schedule(
        DOCTOR,
        aggregate.plan.plan_id,
        item_id,
        expected_version=aggregate.plan.revision,
        values=manual,
        change_reason="医生为当前合成患者覆盖",
    )
    overridden = aggregate.items[0].schedule
    assert overridden.basis == ScheduleBasis.DOCTOR_MANUAL
    assert overridden.values == manual
    assert overridden.source_template_values == approved.values
    assert overridden.source_template_ref.reference_id == approved.schedule_id
    assert {
        overridden.field_provenance.interval_days.origin,
        overridden.field_provenance.times_of_day.origin,
        overridden.field_provenance.duration_days.origin,
        overridden.field_provenance.timezone.origin,
    } == {FieldOrigin.DOCTOR_PATIENT_OVERRIDE}


def test_semantic_change_is_explicit_and_controls_series_id(tmp_path):
    service = _service(tmp_path)
    _, _, aggregate = _draft_with_scheduled_item(service)
    item = aggregate.items[0]

    with pytest.raises(SemanticChangeDeclarationError):
        service.edit_item(
            DOCTOR,
            aggregate.plan.plan_id,
            item.item_id,
            expected_version=aggregate.plan.revision,
            prompt=item.prompt,
            response_type=ResponseType.SINGLE_CHOICE,
            unit=None,
            options=(ResponseOption(value="yes", label="是"),),
            semantic_change=False,
            change_reason="错误声明",
        )

    semantic = service.edit_item(
        DOCTOR,
        aggregate.plan.plan_id,
        item.item_id,
        expected_version=aggregate.plan.revision,
        prompt="今天是否感觉明显不适？",
        response_type=ResponseType.BOOLEAN,
        unit=None,
        options=(),
        semantic_change=True,
        change_reason="医生明确声明语义变化",
    )
    assert semantic.items[0].series_id != item.series_id

    stable = service.edit_item(
        DOCTOR,
        semantic.plan.plan_id,
        item.item_id,
        expected_version=semantic.plan.revision,
        prompt="今天是否感觉明显不适？（合成演示）",
        response_type=ResponseType.BOOLEAN,
        unit=None,
        options=(),
        semantic_change=False,
        change_reason="仅展示文字调整",
    )
    assert stable.items[0].series_id == semantic.items[0].series_id


def test_copy_reorder_and_stop_are_append_only(tmp_path):
    service = _service(tmp_path)
    _, _, aggregate = _draft_with_scheduled_item(service)
    original = aggregate.items[0]

    copied = service.copy_item(
        DOCTOR,
        aggregate.plan.plan_id,
        original.item_id,
        expected_version=aggregate.plan.revision,
        change_reason="复制合成项目",
    )
    copy = copied.items[1]
    assert copy.item_id != original.item_id
    assert copy.series_id != original.series_id
    assert copy.source_template_item_id == original.item_id

    reordered = service.reorder_items(
        DOCTOR,
        copied.plan.plan_id,
        expected_version=copied.plan.revision,
        ordered_item_ids=(copy.item_id, original.item_id),
        change_reason="调整合成项目顺序",
    )
    assert [item.item_id for item in reordered.items] == [copy.item_id, original.item_id]

    stopped = service.stop_item(
        DOCTOR,
        reordered.plan.plan_id,
        original.item_id,
        expected_version=reordered.plan.revision,
        effective_end=date(2026, 8, 20),
        change_reason="停止合成项目",
    )
    stopped_item = next(item for item in stopped.items if item.item_id == original.item_id)
    assert stopped_item.status == FollowupItemStatus.STOPPED
    before_stop = service.repository.get_plan_revision(
        stopped.plan.plan_id, reordered.plan.revision
    )
    assert next(
        item for item in before_stop.items if item.item_id == original.item_id
    ).status == FollowupItemStatus.ACTIVE


def test_patient_and_nurse_read_only_published_views(tmp_path):
    service = _service(tmp_path)
    _, _, draft = _draft_with_scheduled_item(service)

    with pytest.raises(LookupError, match="no published"):
        service.get_patient_current_plan(PATIENT, PATIENT_ID)
    with pytest.raises(FollowupPlanningAuthorizationError):
        service.get_doctor_plan(NURSE, draft.plan.plan_id)

    published = _publish(service, draft)
    patient_view = service.get_patient_current_plan(PATIENT, PATIENT_ID)
    nurse_view = service.get_nurse_current_plan(NURSE, PATIENT_ID)

    assert published.plan.status == PlanStatus.PUBLISHED
    assert patient_view.version == 1
    assert patient_view.items[0].prompt == published.items[0].prompt
    assert nurse_view.change_summary == published.plan.change_summary
    with pytest.raises(ValidationError):
        nurse_view.change_summary = "护士不能编辑"
    with pytest.raises(FollowupPlanningAuthorizationError):
        service.get_patient_current_plan(
            PlanningActor(actor_id="Patient/other-synthetic", role=ActorRole.PATIENT),
            PATIENT_ID,
        )


def test_expected_version_prevents_silent_plan_overwrite(tmp_path):
    service = _service(tmp_path)
    _, _, aggregate = _draft_with_scheduled_item(service)
    stale = aggregate.plan.revision - 1

    with pytest.raises(FollowupPlanningStateError, match="expected plan revision"):
        service.set_manual_schedule(
            DOCTOR,
            aggregate.plan.plan_id,
            aggregate.items[0].item_id,
            expected_version=stale,
            values=_manual_values(duration_days=9),
            change_reason="stale write",
        )


def test_v2_does_not_modify_v1_and_completed_answer_keeps_old_item_snapshot(tmp_path):
    service = _service(tmp_path)
    _, _, draft_v1 = _draft_with_scheduled_item(service)
    published_v1 = _publish(service, draft_v1)
    old_item = published_v1.items[0]
    answer = service.record_completed_answer_binding(
        PATIENT,
        patient_id=PATIENT_ID,
        item_id=old_item.item_id,
        response_snapshot=True,
        answer_id="answer-synthetic-v1",
    )

    draft_v2 = service.create_next_version(
        DOCTOR,
        published_v1.plan.plan_id,
        expected_version=published_v1.plan.revision,
        effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        change_summary="创建合成 v2",
    )
    edited_v2 = service.edit_item(
        DOCTOR,
        draft_v2.plan.plan_id,
        draft_v2.items[0].item_id,
        expected_version=draft_v2.plan.revision,
        prompt="v2 的全新合成问题含义？",
        response_type=ResponseType.BOOLEAN,
        unit=None,
        options=(),
        semantic_change=True,
        change_reason="v2 明确语义变化",
    )
    published_v2 = _publish(service, edited_v2)

    historical_v1 = service.repository.get_published_version(
        published_v1.plan.plan_id, 1
    )
    persisted_answer = service.repository.get_answer_binding(answer.answer_id)
    patient_v2 = service.get_patient_current_plan(PATIENT, PATIENT_ID)

    assert historical_v1.plan.status == PlanStatus.SUPERSEDED
    assert historical_v1.items[0].prompt == old_item.prompt
    assert historical_v1.items[0].item_version == old_item.item_version
    assert persisted_answer.item_version == old_item.item_version
    assert persisted_answer.prompt_snapshot == old_item.prompt
    assert persisted_answer.series_id == old_item.series_id
    assert published_v2.plan.version == 2
    assert patient_v2.version == 2
    assert patient_v2.items[0].prompt != persisted_answer.prompt_snapshot


def test_preview_and_burden_expose_separate_deterministic_read_models(tmp_path):
    service = _service(tmp_path)
    _, _, aggregate = _draft_with_scheduled_item(service)

    preview = service.preview_plan(
        DOCTOR, aggregate.plan.plan_id, window_start=date(2026, 8, 17)
    )
    burden = service.calculate_plan_burden(DOCTOR, aggregate.plan.plan_id)

    assert len(preview.sessions) == burden.scheduled_sessions
    assert burden.total_items == 6
    assert burden.busiest_day_items == 2

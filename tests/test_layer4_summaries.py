from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from pydantic import ValidationError

from continucare.fhir.observations import (
    build_patient_reported_observation,
    per_day_quantity,
)
from continucare.fhir.questionnaires import build_free_text_questionnaire_response
from continucare.fhir.r4 import validate_official_json_schema
from continucare.fhir.terminology import BODY_WEIGHT, VOMITING_COUNT_24H
from continucare.db import connect
from continucare.errors import ConcurrentWriteConflict
from continucare.layer4 import (
    ClinicalMemoryService,
    DoctorReviewService,
    EvidenceSummaryService,
    Layer4InputSnapshot,
    Layer4SummaryDraft,
    Layer4SQLiteStore,
    SummaryEvidenceItem,
    TaskWorkflowService,
    build_communication,
    build_workflow_task,
)
from continucare.layer4.contracts import (
    DoctorReviewDecision,
    EvidenceReference,
    EvidenceRole,
    MemoryEventKind,
    MissingDataExpectation,
    ResourceReference,
    SummaryDraftStatus,
)


PATIENT_ID = "P-DEMO-001"
PATHWAY_CODE = "GLP1-14D"
PATHWAY_VERSION = "1.0.0"
PERIOD_START = "2026-08-01T00:00:00+00:00"
PERIOD_END = "2026-08-02T12:00:00+00:00"
GENERATED_AT = "2026-08-02T12:01:00+00:00"


class MutableInputReader:
    def __init__(self, snapshot: Layer4InputSnapshot):
        self.snapshot = snapshot

    def read(
        self, patient_id: str, *, pathway_code: str, pathway_version: str
    ) -> Layer4InputSnapshot:
        assert patient_id == self.snapshot.patient_id
        assert pathway_code == self.snapshot.pathway_code
        assert pathway_version == self.snapshot.pathway_version
        return self.snapshot


def _response(response_id: str = "response-summary") -> dict:
    return build_free_text_questionnaire_response(
        response_id=response_id,
        patient_id=PATIENT_ID,
        authored="2026-08-02T10:00:00+00:00",
        text="合成摘要测试内容。",
    )


def _vomiting(
    value: int,
    *,
    observation_id: str,
    response_id: str = "response-summary",
    issued_time: str | None = None,
) -> dict:
    return build_patient_reported_observation(
        observation_id=observation_id,
        patient_id=PATIENT_ID,
        questionnaire_response_id=response_id,
        effective_time="2026-08-02T10:00:00+00:00",
        issued_time=issued_time,
        code=VOMITING_COUNT_24H,
        value_element="valueQuantity",
        value=per_day_quantity(value, unit="vomiting episodes/24 hours"),
    )


def _snapshot(
    *,
    responses: list[dict] | None = None,
    observations: list[dict] | None = None,
) -> Layer4InputSnapshot:
    return Layer4InputSnapshot(
        patient_id=PATIENT_ID,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
        questionnaire_responses=responses or [],
        observations=observations or [],
        assembled_at="2026-08-02T12:00:00+00:00",
    )


def _services(tmp_path, snapshot: Layer4InputSnapshot):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = Layer4SQLiteStore(tmp_path / "layer4-summary.db")
    reader = MutableInputReader(snapshot)
    memory = ClinicalMemoryService(
        reader,
        repository,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
    )
    summaries = EvidenceSummaryService(memory, repository)
    reviews = DoctorReviewService(repository)
    return repository, reader, memory, summaries, reviews


def _generate(service: EvidenceSummaryService, *, generated_at: str = GENERATED_AT):
    return service.generate(
        patient_id=PATIENT_ID,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        generated_at=generated_at,
    )


def test_deterministic_summary_covers_current_timeline_sections_with_evidence(tmp_path):
    response = _response()
    first = _vomiting(1, observation_id="observation-summary-conflict-1")
    second = _vomiting(2, observation_id="observation-summary-conflict-2")
    repository, _, memory, summaries, _ = _services(
        tmp_path,
        _snapshot(responses=[response], observations=[first, second]),
    )
    task = build_workflow_task(
        patient_id=PATIENT_ID,
        rule_id="synthetic-summary-task-source",
        rule_version="1.0.0",
        task_code_system="urn:continucare:task-code",
        task_code="synthetic-review",
        task_code_display="合成人工复核",
        description="仅作为摘要中的合成任务证据。",
        requester_reference="Organization/continucare",
        owner_reference="PractitionerRole/nurse",
        authored_on="2026-08-02T10:30:00+00:00",
        trigger_reference="Observation/observation-summary-conflict-1",
        due_at="2026-08-02T14:30:00+00:00",
        task_id="task-summary-source",
        based_on_references=[
            f"urn:continucare:pathway:{PATHWAY_CODE}|{PATHWAY_VERSION}"
        ],
        evidence_references=["Observation/observation-summary-conflict-1/_history/1"],
    )
    repository.save_fhir_resource(task, patient_id=PATIENT_ID)
    TaskWorkflowService(repository).transition(
        patient_id=PATIENT_ID,
        task_id=task["id"],
        to_status="received",
        actor_reference="PractitionerRole/nurse",
        note="已收到合成复核任务。",
        transitioned_at="2026-08-02T10:35:00+00:00",
    )
    expectation = MissingDataExpectation(
        expectation_id="expect-summary-weight",
        patient_id=PATIENT_ID,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
        code_system=BODY_WEIGHT.system,
        code=BODY_WEIGHT.code,
        display="体重",
        period_start=PERIOD_START,
        period_end="2026-08-02T11:00:00+00:00",
        pathway_reference=ResourceReference(
            reference="PlanDefinition/glp1-followup-plan-v1", version_id="1.0.0"
        ),
    )
    memory.rebuild(PATIENT_ID, missing_expectations=[expectation])

    summary = _generate(summaries)

    assert summary.status == SummaryDraftStatus.SAFETY_REVIEWED
    assert summary.generation_mode == "deterministic"
    assert summary.model_name is None
    assert summary.prompt_version is None
    assert {item.section for item in summary.items} >= {
        "overview",
        "key_changes",
        "tasks_and_actions",
        "missing_data",
        "conflicts",
    }
    assert all(item.evidence_refs for item in summary.items)
    conflict = next(item for item in summary.items if item.section == "conflicts")
    assert conflict.requires_doctor_confirmation is True
    task_item = next(
        item for item in summary.items if item.section == "tasks_and_actions"
    )
    assert "任务状态：received" in task_item.text
    assert "已收到合成复核任务" in task_item.text
    assert len(summary.source_timeline_event_ids) == len(summary.items)
    provenance_id = summary.provenance_refs[0].reference.removeprefix("Provenance/")
    provenance = repository.get_fhir_resource("Provenance", provenance_id)
    assert provenance is not None
    assert any(
        item["what"]["reference"].startswith("urn:continucare:timeline-event:")
        for item in provenance["entity"]
    )


def test_summary_generation_is_idempotent_and_new_timeline_creates_new_version(
    tmp_path,
):
    response = _response()
    observation = _vomiting(1, observation_id="observation-summary-version")
    repository, _, memory, summaries, _ = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    task = build_workflow_task(
        patient_id=PATIENT_ID,
        rule_id="synthetic-summary-version-rule",
        rule_version="1.0.0",
        task_code_system="urn:continucare:task-code",
        task_code="synthetic-review",
        task_code_display="合成人工复核",
        description="仅用于 Communication Pathway 传递测试。",
        requester_reference="Organization/continucare",
        owner_reference="PractitionerRole/nurse",
        authored_on="2026-08-02T10:30:00+00:00",
        trigger_reference="Observation/observation-summary-version/_history/1",
        due_at="2026-08-02T14:30:00+00:00",
        task_id="task-summary-version",
        based_on_references=[
            f"urn:continucare:pathway:{PATHWAY_CODE}|{PATHWAY_VERSION}"
        ],
        evidence_references=["Observation/observation-summary-version/_history/1"],
    )
    repository.save_fhir_resource(task, patient_id=PATIENT_ID)
    memory.rebuild(PATIENT_ID)

    first = _generate(summaries)
    repeated = _generate(summaries, generated_at="2026-08-02T12:02:00+00:00")
    communication = build_communication(
        patient_id=PATIENT_ID,
        content_text="新增一条合成沟通记录。",
        sender_reference=f"Patient/{PATIENT_ID}",
        recipient_references=["PractitionerRole/nurse"],
        sent_at="2026-08-02T11:30:00+00:00",
        communication_id="communication-summary-version",
        based_on_references=["Task/task-summary-version/_history/1"],
    )
    repository.save_fhir_resource(communication, patient_id=PATIENT_ID)
    memory.rebuild(PATIENT_ID)
    changed = _generate(summaries, generated_at="2026-08-02T12:03:00+00:00")

    assert repeated == first
    assert first.summary_id.startswith("summary-v2-")
    assert first.version == "1"
    assert changed.version == "2"
    assert len(changed.source_timeline_event_ids) == (
        len(first.source_timeline_event_ids) + 1
    )
    assert repository.get_contract(
        "summary_draft", first.summary_id, version="1"
    ) == first
    assert repository.get_contract("summary_draft", first.summary_id) == changed


def test_legacy_timeline_summary_remains_byte_identical_and_read_only(tmp_path):
    response = _response()
    observation = _vomiting(1, observation_id="observation-summary-legacy")
    repository, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    legacy = Layer4SummaryDraft(
        summary_id="summary-legacy-timeline",
        patient_id=PATIENT_ID,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        status=SummaryDraftStatus.SAFETY_REVIEWED,
        created_at=GENERATED_AT,
    )
    repository.save_contract(legacy)
    with connect(repository.db_path) as connection:
        before = connection.execute(
            "SELECT record_json FROM layer4_contract_records "
            "WHERE record_type='summary_draft' AND record_id=?",
            (legacy.summary_id,),
        ).fetchone()["record_json"]

    memory.rebuild(PATIENT_ID)
    generated = _generate(summaries, generated_at="2026-08-02T12:02:00+00:00")

    with connect(repository.db_path) as connection:
        after = connection.execute(
            "SELECT record_json FROM layer4_contract_records "
            "WHERE record_type='summary_draft' AND record_id=?",
            (legacy.summary_id,),
        ).fetchone()["record_json"]
    assert before == after
    assert generated.summary_id.startswith("summary-v2-")
    assert generated.summary_id != legacy.summary_id
    assert generated.version == "1"
    with pytest.raises(ValueError, match="legacy timeline Summary is read-only"):
        reviews.review(
            summary_id=legacy.summary_id,
            summary_version="1",
            reviewer_reference="Practitioner/doctor",
            decision=DoctorReviewDecision.ACCEPT,
            reviewed_at="2026-08-02T12:03:00+00:00",
        )


def test_empty_timeline_creates_empty_summary_without_inventing_fact(tmp_path):
    _, _, memory, summaries, _ = _services(tmp_path, _snapshot())
    memory.rebuild(PATIENT_ID)

    summary = _generate(summaries)

    assert summary.items == []
    assert summary.source_timeline_event_ids == []
    assert summary.status == SummaryDraftStatus.SAFETY_REVIEWED


def test_summary_excludes_events_recorded_after_generation_cutoff(tmp_path):
    response = _response()
    future_recorded = _vomiting(
        1,
        observation_id="observation-recorded-after-summary",
        issued_time="2026-08-02T13:00:00+00:00",
    )
    _, _, memory, summaries, _ = _services(
        tmp_path,
        _snapshot(responses=[response], observations=[future_recorded]),
    )
    memory.rebuild(PATIENT_ID)

    summary = _generate(summaries)

    assert all(item.section != "key_changes" for item in summary.items)
    assert {item.section for item in summary.items} == {"overview"}


def test_doctor_accept_is_versioned_provenanced_and_idempotent(tmp_path):
    response = _response()
    observation = _vomiting(1, observation_id="observation-summary-accept")
    repository, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)

    first = reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/doctor",
        decision=DoctorReviewDecision.ACCEPT,
        reviewed_at="2026-08-02T12:10:00+00:00",
        note="已核对合成证据。",
    )
    repeated = reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/doctor",
        decision=DoctorReviewDecision.ACCEPT,
        reviewed_at="2026-08-02T12:10:00+00:00",
        note="已核对合成证据。",
    )

    assert repeated.review == first.review
    assert repeated.summary == first.summary
    assert first.idempotent_replay is False
    assert repeated.idempotent_replay is True
    assert first.summary.version == "2"
    assert first.summary.status == SummaryDraftStatus.DOCTOR_REVIEWED
    assert first.summary.items == draft.items
    assert first.review.result_summary_version == "2"
    assert repository.get_contract(
        "summary_draft", draft.summary_id, version="1"
    ) == draft
    assert repository.get_contract("summary_draft", draft.summary_id) == first.summary
    provenance = repository.get_fhir_resource(
        "Provenance",
        first.review.provenance_reference.removeprefix("Provenance/"),
    )
    assert provenance is not None
    assert provenance["agent"][0]["who"]["reference"] == "Practitioner/doctor"
    assert _generate(summaries, generated_at="2026-08-02T12:11:00+00:00") == (
        first.summary
    )


def test_doctor_modify_requires_real_change_and_only_existing_evidence(tmp_path):
    response = _response()
    observation = _vomiting(1, observation_id="observation-summary-modify")
    repository, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)
    modified = [
        item.model_copy(update={"text": f"医生核对后：{item.text}"})
        if index == 0
        else item
        for index, item in enumerate(draft.items)
    ]

    outcome = reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/doctor",
        decision=DoctorReviewDecision.MODIFY,
        reviewed_at="2026-08-02T12:15:00+00:00",
        note="修订措辞，未增加新事实。",
        modified_items=modified,
    )
    repeated = reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/doctor",
        decision=DoctorReviewDecision.MODIFY,
        reviewed_at="2026-08-02T12:15:00+00:00",
        note="修订措辞，未增加新事实。",
        modified_items=modified,
    )

    assert repeated.review == outcome.review
    assert repeated.summary == outcome.summary
    assert outcome.idempotent_replay is False
    assert repeated.idempotent_replay is True
    assert outcome.summary.items == modified
    assert outcome.review.amended_summary_id == draft.summary_id
    assert outcome.review.amended_summary_version == "2"

    invented = deepcopy(modified)
    invented[0] = SummaryEvidenceItem(
        item_id=invented[0].item_id,
        section=invented[0].section,
        text="尝试加入无来源事实。",
        evidence_refs=[
            EvidenceReference(
                evidence_id="invented-evidence",
                resource=ResourceReference(
                    reference="Observation/invented", version_id="1"
                ),
                role=EvidenceRole.SUPPORTING,
            )
        ],
    )
    other_repository, _, other_memory, other_summaries, other_reviews = _services(
        tmp_path / "invented", _snapshot(responses=[response], observations=[observation])
    )
    other_memory.rebuild(PATIENT_ID)
    other_draft = _generate(other_summaries)
    invented[0] = invented[0].model_copy(update={"item_id": other_draft.items[0].item_id})
    with pytest.raises(ValueError, match="evidence absent from source summary"):
        other_reviews.review(
            summary_id=other_draft.summary_id,
            summary_version=other_draft.version,
            reviewer_reference="Practitioner/doctor",
            decision=DoctorReviewDecision.MODIFY,
            reviewed_at="2026-08-02T12:15:00+00:00",
            note="不应接受。",
            modified_items=[invented[0], *other_draft.items[1:]],
        )
    assert other_repository.get_contract("summary_draft", other_draft.summary_id) == (
        other_draft
    )


def test_doctor_reject_requires_note_and_blocks_further_review(tmp_path):
    response = _response()
    observation = _vomiting(1, observation_id="observation-summary-reject")
    _, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)

    with pytest.raises(ValidationError, match="reject review requires a note"):
        reviews.review(
            summary_id=draft.summary_id,
            summary_version=draft.version,
            reviewer_reference="Practitioner/doctor",
            decision=DoctorReviewDecision.REJECT,
            reviewed_at="2026-08-02T12:20:00+00:00",
        )
    rejected = reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/doctor",
        decision=DoctorReviewDecision.REJECT,
        reviewed_at="2026-08-02T12:20:00+00:00",
        note="证据不足，拒绝该合成简报。",
    )
    assert rejected.summary.status == SummaryDraftStatus.REJECTED
    with pytest.raises(ValueError, match="requires a safety-reviewed summary"):
        reviews.review(
            summary_id=rejected.summary.summary_id,
            summary_version=rejected.summary.version,
            reviewer_reference="Practitioner/doctor-2",
            decision=DoctorReviewDecision.ACCEPT,
            reviewed_at="2026-08-02T12:21:00+00:00",
        )


def _doctor_bundle_counts(repository):
    with connect(repository.db_path) as connection:
        return {
            "provenance": connection.execute(
                "SELECT COUNT(*) AS count FROM layer4_fhir_resources "
                "WHERE resource_type='Provenance'"
            ).fetchone()["count"],
            "summaries": connection.execute(
                "SELECT COUNT(*) AS count FROM layer4_contract_records "
                "WHERE record_type='summary_draft'"
            ).fetchone()["count"],
            "reviews": connection.execute(
                "SELECT COUNT(*) AS count FROM layer4_contract_records "
                "WHERE record_type='doctor_review'"
            ).fetchone()["count"],
            "audits": connection.execute(
                "SELECT COUNT(*) AS count FROM audit_events "
                "WHERE event_type='doctor_reviewed_summary'"
            ).fetchone()["count"],
        }


@pytest.mark.parametrize(
    "fault_stage",
    [
        "before_provenance",
        "after_provenance",
        "after_summary",
        "after_review",
        "after_audit",
        "before_commit",
    ],
)
def test_doctor_review_faults_roll_back_the_entire_bundle(tmp_path, fault_stage):
    response = _response()
    observation = _vomiting(1, observation_id=f"observation-review-{fault_stage}")
    repository, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)
    before = _doctor_bundle_counts(repository)

    def inject(stage):
        if stage == fault_stage:
            raise RuntimeError(f"fault:{stage}")

    repository._doctor_review_bundle_fault = inject
    with pytest.raises(RuntimeError, match=fault_stage):
        reviews.review(
            summary_id=draft.summary_id,
            summary_version=draft.version,
            reviewer_reference="Practitioner/fault-doctor",
            decision=DoctorReviewDecision.ACCEPT,
            reviewed_at="2026-08-02T12:30:00+00:00",
        )

    assert _doctor_bundle_counts(repository) == before
    assert repository.get_contract("summary_draft", draft.summary_id) == draft


def test_doctor_review_replay_keeps_first_timestamp_and_adds_no_rows(tmp_path):
    response = _response()
    observation = _vomiting(1, observation_id="observation-review-replay")
    repository, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)

    first = reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/replay-doctor",
        decision=DoctorReviewDecision.ACCEPT,
        reviewed_at="2026-08-02T12:31:00+00:00",
        note="  已核对。  ",
    )
    after_first = _doctor_bundle_counts(repository)
    replay = reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/replay-doctor",
        decision=DoctorReviewDecision.ACCEPT,
        reviewed_at="2026-08-02T12:59:00+00:00",
        note="已核对。",
    )

    assert replay.idempotent_replay is True
    assert replay.review.reviewed_at == first.review.reviewed_at
    assert replay.summary.created_at == first.summary.created_at
    assert _doctor_bundle_counts(repository) == after_first

    with connect(repository.db_path) as connection:
        connection.execute(
            "DELETE FROM audit_events WHERE event_type='doctor_reviewed_summary'"
        )
    with pytest.raises(ConcurrentWriteConflict, match="partial replay"):
        reviews.review(
            summary_id=draft.summary_id,
            summary_version=draft.version,
            reviewer_reference="Practitioner/replay-doctor",
            decision=DoctorReviewDecision.ACCEPT,
            reviewed_at="2026-08-02T13:00:00+00:00",
            note="已核对。",
        )


def test_doctor_review_different_request_for_same_source_conflicts(tmp_path):
    response = _response()
    observation = _vomiting(1, observation_id="observation-review-conflict")
    repository, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)
    reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/conflict-doctor",
        decision=DoctorReviewDecision.ACCEPT,
        reviewed_at="2026-08-02T12:32:00+00:00",
        note="接受。",
    )
    before = _doctor_bundle_counts(repository)

    with pytest.raises(ConcurrentWriteConflict, match="stale summary"):
        reviews.review(
            summary_id=draft.summary_id,
            summary_version=draft.version,
            reviewer_reference="Practitioner/conflict-doctor",
            decision=DoctorReviewDecision.REJECT,
            reviewed_at="2026-08-02T12:33:00+00:00",
            note="改为拒绝。",
        )
    assert _doctor_bundle_counts(repository) == before


def test_concurrent_different_doctor_reviews_have_one_complete_winner(tmp_path):
    response = _response()
    observation = _vomiting(1, observation_id="observation-review-concurrent")
    repository, _, memory, summaries, _ = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)
    barrier = Barrier(2)
    original = repository.persist_doctor_review_bundle

    def synchronized(**kwargs):
        barrier.wait(timeout=5)
        return original(**kwargs)

    repository.persist_doctor_review_bundle = synchronized

    def submit(decision):
        return DoctorReviewService(repository).review(
            summary_id=draft.summary_id,
            summary_version=draft.version,
            reviewer_reference="Practitioner/concurrent-doctor",
            decision=decision,
            reviewed_at="2026-08-02T12:34:00+00:00",
            note="并发审阅。",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(submit, DoctorReviewDecision.ACCEPT),
            pool.submit(submit, DoctorReviewDecision.REJECT),
        ]
    outcomes = []
    conflicts = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except ConcurrentWriteConflict as exc:
            conflicts.append(exc)

    assert len(outcomes) == 1
    assert len(conflicts) == 1
    counts = _doctor_bundle_counts(repository)
    assert counts["summaries"] == 2
    assert counts["reviews"] == 1
    assert counts["audits"] == 1


def test_doctor_review_sqlite_busy_is_a_stable_conflict(tmp_path):
    response = _response()
    observation = _vomiting(1, observation_id="observation-review-busy")
    repository, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)
    before = _doctor_bundle_counts(repository)

    with connect(repository.db_path) as locked:
        locked.execute("BEGIN IMMEDIATE")
        with pytest.raises(ConcurrentWriteConflict, match="database is busy"):
            reviews.review(
                summary_id=draft.summary_id,
                summary_version=draft.version,
                reviewer_reference="Practitioner/busy-doctor",
                decision=DoctorReviewDecision.ACCEPT,
                reviewed_at="2026-08-02T12:35:00+00:00",
            )

    assert _doctor_bundle_counts(repository) == before


def test_summary_generation_and_review_provenance_pass_official_schema_when_available(
    tmp_path,
):
    schema_path = os.environ.get("FHIR_R4_SCHEMA_ZIP")
    if not schema_path:
        pytest.skip("set FHIR_R4_SCHEMA_ZIP to the official HL7 R4 schema archive")
    response = _response()
    observation = _vomiting(1, observation_id="observation-summary-schema")
    repository, _, memory, summaries, reviews = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    draft = _generate(summaries)
    reviews.review(
        summary_id=draft.summary_id,
        summary_version=draft.version,
        reviewer_reference="Practitioner/doctor",
        decision=DoctorReviewDecision.ACCEPT,
        reviewed_at="2026-08-02T12:10:00+00:00",
    )

    for resource in repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    ):
        validate_official_json_schema(resource, schema_path)


def test_summary_bundle_rolls_back_and_replays_after_commit(tmp_path):
    response = _response("response-summary-atomic")
    observation = _vomiting(1, observation_id="observation-summary-atomic")
    repository, _, memory, summaries, _ = _services(
        tmp_path, _snapshot(responses=[response], observations=[observation])
    )
    memory.rebuild(PATIENT_ID)
    provenance_before = repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    )

    def rollback_fault(stage):
        if stage == "summary:after_provenance":
            raise RuntimeError("fault:summary:after_provenance")

    repository._provenance_contract_bundle_fault = rollback_fault
    with pytest.raises(RuntimeError, match="summary:after_provenance"):
        _generate(summaries)
    assert repository.list_contracts("summary_draft", patient_id=PATIENT_ID) == []
    assert repository.list_fhir_resources(
        patient_id=PATIENT_ID, resource_type="Provenance", current_only=False
    ) == provenance_before

    def commit_fault(stage):
        if stage == "summary:after_commit":
            raise RuntimeError("fault:summary:after_commit")

    repository._provenance_contract_bundle_fault = commit_fault
    with pytest.raises(RuntimeError, match="summary:after_commit"):
        _generate(summaries)
    committed = repository.list_contracts(
        "summary_draft", patient_id=PATIENT_ID
    )
    assert len(committed) == 1
    assert len(
        repository.list_fhir_resources(
            patient_id=PATIENT_ID,
            resource_type="Provenance",
            current_only=False,
        )
    ) == len(provenance_before) + 1

    repository._provenance_contract_bundle_fault = lambda stage: None
    replay = _generate(summaries)
    assert replay == committed[0]
    assert len(repository.list_contracts("summary_draft", patient_id=PATIENT_ID)) == 1

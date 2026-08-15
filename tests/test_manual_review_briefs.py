from __future__ import annotations

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from continucare.adapters.sqlite_store import SQLiteStore
from continucare.care_agent import CareAgentService
from continucare.care_agent.model_api import SemanticModelConfig, UnconfiguredModelAdapter
from continucare.care_engine import CareEngine
from continucare.db import connect
from continucare.demo_data import DEMO_PATIENT_ID
from continucare.layer4 import (
    BRIEF_SUMMARY_KIND,
    DoctorReviewService,
    DoctorWorkbenchService,
    EvidenceArtifactType,
    Layer4InputReader,
    Layer4SQLiteStore,
    ManualReviewBriefService,
    WorkbenchAccessContext,
    WorkbenchPurpose,
    WorkbenchRole,
)
from continucare.layer4.contracts import DoctorReviewDecision, SummaryDraftStatus
from continucare.pathways import load_builtin_pathways
from continucare.services.confirmed_review import ConfirmedReviewService
from continucare.services.demo_scenarios import load_manual_review_scenario
from continucare.services.manual_review_workflow import ManualReviewWorkflowService


PATHWAY_CODE = "GLP1-14D"
PATHWAY_VERSION = "1.0.0"


def _after(resource, seconds: int = 1) -> str:
    return (
        datetime.fromisoformat(resource["meta"]["lastUpdated"])
        + timedelta(seconds=seconds)
    ).isoformat()


def _scenario(db_path, *, repository=None):
    interaction = load_manual_review_scenario(db_path)
    store = SQLiteStore(db_path)
    engine = CareEngine(store)
    agent = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        patient_timezone="Asia/Shanghai",
    )
    confirmed = ConfirmedReviewService(
        store, care_agent=agent, care_engine=engine
    ).accept_all(
        interaction.result.run_id,
        [item.candidate_id for item in interaction.result.candidates],
    )
    repository = repository or Layer4SQLiteStore(db_path)
    workflow = ManualReviewWorkflowService(store, layer4_store=repository)
    received = workflow.acknowledge(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        note="已收到合成任务。",
        occurred_at=_after(confirmed.task),
    )
    started = workflow.start(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        note="开始核对合成证据。",
        occurred_at=_after(received.task),
    )
    outcome = workflow.record_outcome(
        patient_id=DEMO_PATIENT_ID,
        task_id=confirmed.task["id"],
        outcome="evidence_consistent",
        note="这条护士自由备注不能成为简报临床事实。",
        occurred_at=_after(started.task),
    )
    briefs = ManualReviewBriefService(
        store,
        repository,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
    )
    return store, repository, workflow, outcome, briefs


def _counts(db_path):
    with connect(db_path) as connection:
        return {
            "summaries": connection.execute(
                "SELECT COUNT(*) FROM layer4_contract_records WHERE record_type='summary_draft'"
            ).fetchone()[0],
            "provenances": connection.execute(
                "SELECT COUNT(*) FROM layer4_fhir_resources WHERE resource_type='Provenance'"
            ).fetchone()[0],
            "audits": connection.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0],
            "alerts": connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
        }


def _access():
    return WorkbenchAccessContext(
        actor_reference="Practitioner/synthetic-doctor-review",
        role=WorkbenchRole.DOCTOR,
        purpose=WorkbenchPurpose.TREATMENT,
        permitted_patient_ids=[DEMO_PATIENT_ID],
        identity_verified=True,
    )


def test_pending_and_ready_briefs_are_verbatim_versioned_and_traceable(tmp_path):
    db_path = tmp_path / "brief-ready.db"
    store, repository, workflow, outcome, briefs = _scenario(db_path)
    generated_pending = _after(outcome.task)

    pending = briefs.generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=generated_pending,
    )
    retry = briefs.generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=(
            datetime.fromisoformat(generated_pending) + timedelta(seconds=1)
        ).isoformat(),
    )

    assert retry == pending
    assert pending.summary_kind == BRIEF_SUMMARY_KIND
    assert pending.status == SummaryDraftStatus.SAFETY_REVIEWED
    assert pending.generation_mode == "deterministic"
    assert pending.model_name is None
    assert pending.items[0].text == "我今天拉肚子。"
    assert pending.items[0].evidence_refs[0].evidence_text == "我今天拉肚子。"
    assert all(item.evidence_refs for item in pending.items)
    rendered = "\n".join(item.text for item in pending.items)
    assert "status=completed" in rendered
    assert "status=final" in rendered
    assert "derivedFrom=QuestionnaireResponse/" in rendered
    assert "status=completed；受控处理结果=" in rendered
    assert "临床评估=not_assessed" in rendered
    assert "readiness=pending-approval；尚不可发送；未发送" in rendered
    assert "这条护士自由备注" not in rendered

    approved = workflow.approve_draft(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        communication_id=outcome.communication["id"],
        note="明确批准合成草稿。",
        occurred_at=(
            datetime.fromisoformat(generated_pending) + timedelta(seconds=2)
        ).isoformat(),
    )
    assert briefs.is_stale(pending, as_of=approved.communication["meta"]["lastUpdated"])
    ready = briefs.generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=_after(approved.communication),
    )
    assert ready.summary_id == pending.summary_id
    assert ready.summary_id.startswith("summary-manual-review-")
    assert ready.version == "2"
    assert ready.period_end > pending.period_end
    ready_text = "\n".join(item.text for item in ready.items)
    assert "readiness=ready-to-send；已人工批准；尚未发送" in ready_text
    assert "患者已经收到" not in ready_text

    workbench = DoctorWorkbenchService(
        Layer4InputReader(store),
        repository,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
        summary_kind=BRIEF_SUMMARY_KIND,
    )
    before_read = _counts(db_path)
    view = workbench.query(
        patient_id=DEMO_PATIENT_ID,
        access=_access(),
        as_of=_after(approved.communication),
        generated_at=_after(approved.communication),
    )
    assert _counts(db_path) == before_read
    assert view.summary.version == "2"
    assert view.tasks == []
    root = f"urn:continucare:summary:{ready.summary_id}:version:{ready.version}"
    trace = workbench.trace_evidence(
        patient_id=DEMO_PATIENT_ID,
        access=_access(),
        root_reference=root,
        as_of=_after(approved.communication),
        max_depth=10,
        max_nodes=200,
    )
    assert trace.degraded is False
    assert trace.unresolved_references == []
    assert {
        "QuestionnaireResponse",
        "Observation",
        "Task",
        "Communication",
        "Provenance",
    }.issubset(
        {item.resource_type for item in trace.artifacts if item.resource_type}
    )
    assert {"followup_message", "audit_event"}.issubset(
        {item.record_type for item in trace.artifacts if item.record_type}
    )
    assert any(edge.relation == "provenance_exact_version" for edge in trace.edges)
    assert any(edge.relation == "provenance_resource_level" for edge in trace.edges)
    communication_reference = (
        f"Communication/{approved.communication['id']}/_history/"
        f"{approved.communication['meta']['versionId']}"
    )
    assert any(
        edge.source_reference == communication_reference
        and edge.relation == "provenance_exact_version"
        for edge in trace.edges
    )
    questionnaire_reference = next(
        item["valueReference"]["reference"]
        for item in outcome.task["input"]
        if item.get("valueReference", {}).get("reference", "").startswith(
            "QuestionnaireResponse/"
        )
    )
    questionnaire_reference = f"{questionnaire_reference}/_history/1"
    assert any(
        edge.source_reference == questionnaire_reference
        and edge.relation == "provenance_resource_level"
        for edge in trace.edges
    )
    assert all(
        item.artifact_type == EvidenceArtifactType.APPLICATION_RECORD
        for item in trace.artifacts
        if item.record_type in {"followup_message", "audit_event"}
    )
    assert all(
        "sent" not in item.payload and "received" not in item.payload
        for item in trace.artifacts
        if item.resource_type == "Communication"
    )
    assert before_read["alerts"] == 0
    assert load_builtin_pathways().get(PATHWAY_CODE).clinical_rules == []

    historical = workbench.query(
        patient_id=DEMO_PATIENT_ID,
        access=_access(),
        as_of=generated_pending,
        generated_at=_after(approved.communication),
    )
    assert historical.summary.version == "1"
    assert "pending-approval" in historical.summary.items[-1].text


def test_review_stays_bound_to_old_version_when_sources_change(tmp_path):
    db_path = tmp_path / "brief-review.db"
    _, repository, workflow, outcome, briefs = _scenario(db_path)
    pending = briefs.generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=_after(outcome.task),
    )
    reviewed = DoctorReviewService(repository).review(
        summary_id=pending.summary_id,
        summary_version=pending.version,
        reviewer_reference="Practitioner/synthetic-doctor-review",
        decision=DoctorReviewDecision.ACCEPT,
        reviewed_at=(
            datetime.fromisoformat(pending.created_at) + timedelta(seconds=1)
        ).isoformat(),
    ).summary
    same_sources = briefs.generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=(
            datetime.fromisoformat(reviewed.created_at) + timedelta(seconds=1)
        ).isoformat(),
    )
    assert same_sources == reviewed

    approved = workflow.approve_draft(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        communication_id=outcome.communication["id"],
        note="批准新来源版本。",
        occurred_at=(
            datetime.fromisoformat(reviewed.created_at) + timedelta(seconds=2)
        ).isoformat(),
    )
    refreshed = briefs.generate(
        patient_id=DEMO_PATIENT_ID,
        task_id=outcome.task["id"],
        generated_at=_after(approved.communication),
    )
    assert reviewed.version == "2"
    assert reviewed.status == SummaryDraftStatus.DOCTOR_REVIEWED
    assert refreshed.version == "3"
    assert refreshed.status == SummaryDraftStatus.SAFETY_REVIEWED
    assert repository.get_contract(
        "summary_draft", pending.summary_id, version="2"
    ) == reviewed


@pytest.mark.parametrize("terminal", ["rejected", "cancelled"])
def test_rejected_or_cancelled_task_cannot_generate_brief(tmp_path, terminal):
    db_path = tmp_path / f"brief-{terminal}.db"
    interaction = load_manual_review_scenario(db_path)
    store = SQLiteStore(db_path)
    engine = CareEngine(store)
    agent = CareAgentService(
        store,
        care_engine=engine,
        model_adapter=UnconfiguredModelAdapter(SemanticModelConfig()),
        patient_timezone="Asia/Shanghai",
    )
    confirmed = ConfirmedReviewService(
        store, care_agent=agent, care_engine=engine
    ).accept_all(
        interaction.result.run_id,
        [item.candidate_id for item in interaction.result.candidates],
    )
    workflow = ManualReviewWorkflowService(store)
    task = confirmed.task
    if terminal == "rejected":
        task = workflow.acknowledge(
            patient_id=DEMO_PATIENT_ID,
            task_id=task["id"],
            note="先接收。",
            occurred_at=_after(task),
        ).task
    closed = getattr(workflow, "reject" if terminal == "rejected" else "cancel")(
        patient_id=DEMO_PATIENT_ID,
        task_id=task["id"],
        note=f"合成任务{terminal}。",
        occurred_at=_after(task),
    )
    repository = Layer4SQLiteStore(db_path)
    briefs = ManualReviewBriefService(
        store,
        repository,
        pathway_code=PATHWAY_CODE,
        pathway_version=PATHWAY_VERSION,
    )
    before = _counts(db_path)
    with pytest.raises(ValueError, match="only a completed"):
        briefs.generate(
            patient_id=DEMO_PATIENT_ID,
            task_id=closed.task["id"],
            generated_at=_after(closed.task),
        )
    assert _counts(db_path) == before
    assert workflow.list_communications_for_task(
        DEMO_PATIENT_ID, closed.task["id"]
    ) == []


def test_missing_evidence_and_scope_mismatch_fail_without_side_effects(tmp_path):
    db_path = tmp_path / "brief-fail-closed.db"
    store, repository, _, outcome, briefs = _scenario(db_path)
    before = _counts(db_path)
    response_id = outcome.task["reasonReference"]["reference"].split("/", 1)[1]
    with connect(db_path) as connection:
        connection.execute(
            "DELETE FROM fhir_questionnaire_responses WHERE resource_id=?",
            (response_id,),
        )
    with pytest.raises(ValueError, match="response is missing"):
        briefs.generate(
            patient_id=DEMO_PATIENT_ID,
            task_id=outcome.task["id"],
            generated_at=_after(outcome.task),
        )
    assert _counts(db_path) == before

    wrong_path = ManualReviewBriefService(
        store,
        repository,
        pathway_code="OTHER",
        pathway_version="9.0.0",
    )
    with pytest.raises(ValueError, match="requested scope"):
        wrong_path.generate(
            patient_id=DEMO_PATIENT_ID,
            task_id=outcome.task["id"],
            generated_at=_after(outcome.task),
        )
    assert _counts(db_path) == before


class _FailingBriefStore(Layer4SQLiteStore):
    def _manual_review_brief_fault(self, version: str) -> None:
        raise RuntimeError("injected M5-C failure")


def test_atomic_brief_failure_rolls_back_summary_provenance_and_audit(tmp_path):
    db_path = tmp_path / "brief-rollback.db"
    repository = _FailingBriefStore(db_path)
    _, _, _, outcome, briefs = _scenario(db_path, repository=repository)
    before = _counts(db_path)

    with pytest.raises(RuntimeError, match="injected M5-C failure"):
        briefs.generate(
            patient_id=DEMO_PATIENT_ID,
            task_id=outcome.task["id"],
            generated_at=_after(outcome.task),
        )

    assert _counts(db_path) == before


def test_concurrent_same_source_generation_creates_one_version(tmp_path):
    db_path = tmp_path / "brief-concurrent.db"
    _, _, _, outcome, _ = _scenario(db_path)
    generated_at = _after(outcome.task)

    def generate():
        store = SQLiteStore(db_path)
        repository = Layer4SQLiteStore(db_path)
        return ManualReviewBriefService(
            store,
            repository,
            pathway_code=PATHWAY_CODE,
            pathway_version=PATHWAY_VERSION,
        ).generate(
            patient_id=DEMO_PATIENT_ID,
            task_id=outcome.task["id"],
            generated_at=generated_at,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        summaries = list(pool.map(lambda _: generate(), range(2)))

    assert summaries[0] == summaries[1]
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT COUNT(*) FROM layer4_contract_records
            WHERE record_type='summary_draft' AND record_id=?
            """,
            (summaries[0].summary_id,),
        ).fetchone()[0]
    assert rows == 1


def test_missing_provenance_or_required_audit_fails_closed(tmp_path):
    for missing in ("provenance", "audit"):
        db_path = tmp_path / f"brief-missing-{missing}.db"
        _, _, _, outcome, briefs = _scenario(db_path)
        with connect(db_path) as connection:
            if missing == "provenance":
                connection.execute(
                    """
                    DELETE FROM layer4_fhir_resources
                    WHERE resource_type='Provenance'
                      AND resource_json LIKE '%record-outcome%'
                    """
                )
            else:
                connection.execute(
                    """
                    DELETE FROM audit_events
                    WHERE event_type='manual_review_outcome_recorded'
                    """
                )
        before = _counts(db_path)
        expected = "Provenance" if missing == "provenance" else "audit"
        with pytest.raises(ValueError, match=expected):
            briefs.generate(
                patient_id=DEMO_PATIENT_ID,
                task_id=outcome.task["id"],
                generated_at=_after(outcome.task),
            )
        assert _counts(db_path) == before


def test_page_uses_read_only_workbench_and_no_controlled_llm():
    source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "pages"
        / "3_doctor_summary.py"
    ).read_text(encoding="utf-8")
    assert "DoctorWorkbenchService" in source
    assert "ManualReviewBriefService" in source
    assert "SummaryService" not in source
    assert "ControlledSummaryService" not in source
    assert "MockNotifier" not in source

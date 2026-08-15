"""Persistent-fact orchestration for the synthetic competition demo.

This module does not own a second workflow state machine.  It projects the
existing Layer 3/4 facts and provides an atomic, explicitly invoked reset/start
boundary for the local synthetic database.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import BaseModel, ConfigDict, Field

from continucare.agents.contracts import SemanticResult
from continucare.demo_data import DEMO_PATIENT_ID, MANUAL_REVIEW_MESSAGE
from continucare.layer4.contracts import Layer4SummaryDraft
from continucare.layer4.manual_reviews import (
    PENDING_APPROVAL,
    READY_TO_SEND,
    communication_readiness,
    is_manual_review_communication,
    is_manual_review_task,
)
from continucare.services.demo_scenarios import (
    load_layer2_scenario,
    load_manual_review_scenario,
)


class CompetitionDemoStage(StrEnum):
    NOT_STARTED = "not_started"
    CANDIDATE_READY = "candidate_ready"
    CANDIDATE_UNSURE = "candidate_unsure"
    CANDIDATE_REJECTED = "candidate_rejected"
    PATIENT_CONFIRMED = "patient_confirmed"
    TASK_REQUESTED = "task_requested"
    NURSE_RECEIVED = "nurse_received"
    NURSE_IN_PROGRESS = "nurse_in_progress"
    TASK_REJECTED = "task_rejected"
    TASK_CANCELLED = "task_cancelled"
    TASK_FAILED = "task_failed"
    TASK_ENTERED_IN_ERROR = "task_entered_in_error"
    COMMUNICATION_PENDING = "communication_pending"
    DOCTOR_BRIEF_PENDING = "doctor_brief_pending"
    COMMUNICATION_READY = "communication_ready"
    DOCTOR_BRIEF_READY = "doctor_brief_ready"
    STORY_COMPLETE = "story_complete"


MILESTONE_ORDER = tuple(item.value for item in CompetitionDemoStage)


class CompetitionDemoProgress(BaseModel):
    """Read-only projection of the existing persisted workflow facts."""

    model_config = ConfigDict(frozen=True)

    stage: CompetitionDemoStage = CompetitionDemoStage.NOT_STARTED
    milestones: dict[str, bool] = Field(
        default_factory=lambda: {item: False for item in MILESTONE_ORDER}
    )
    generation: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    communication_id: str | None = None
    summary_id: str | None = None
    summary_version: str | None = None
    session_status: str | None = None
    task_status: str | None = None
    communication_readiness: str | None = None
    candidate_count: int = 0
    candidate_decisions: dict[str, str] = Field(default_factory=dict)
    questionnaire_response_count: int = 0
    observation_count: int = 0
    manual_task_count: int = 0
    communication_count: int = 0
    manual_brief_count: int = 0
    provenance_count: int = 0
    audit_count: int = 0
    alert_count: int = 0
    approved_clinical_rule_count: int = 0
    knowledge_available: bool = False
    knowledge_error: str | None = None
    is_terminal: bool = False
    terminal_reason: str | None = None
    next_page: str = "app.py"
    next_label: str = "开始完整比赛 Demo"
    next_help: str = "明确开始后，系统只准备未确认候选。"
    integrity_issue: str | None = None


class CompetitionDemoStartError(RuntimeError):
    """Stable, non-sensitive error surfaced by the explicit start action."""


class CompetitionDemoConflict(ValueError):
    """The visible story generation changed before a requested write."""


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _json_rows(connection: sqlite3.Connection, query: str, params=()) -> list[dict]:
    return [json.loads(row[0]) for row in connection.execute(query, params).fetchall()]


def _summary_mentions(summary: Layer4SummaryDraft, resource: dict[str, Any]) -> bool:
    reference = f"{resource['resourceType']}/{resource['id']}"
    version = resource.get("meta", {}).get("versionId")
    return any(
        evidence.resource.reference == reference
        and evidence.resource.version_id == version
        for item in summary.items
        for evidence in item.evidence_refs
    )


def _knowledge_status() -> tuple[bool, str | None]:
    # The clinical orchestration layer deliberately does not import Knowledge.
    # Availability is rendered and verified inside the independent page.
    return False, None


def read_competition_demo(db_path: Path | str) -> CompetitionDemoProgress:
    """Derive current progress with a SQLite read-only connection.

    A missing database is a truthful ``not_started`` result and is never
    created by this function.
    """

    path = Path(db_path)
    knowledge_available, knowledge_error = _knowledge_status()
    empty = CompetitionDemoProgress(
        knowledge_available=knowledge_available,
        knowledge_error=knowledge_error,
    )
    if not path.is_file():
        return empty

    try:
        with _readonly_connection(path) as connection:
            run_row = connection.execute(
                """
                SELECT r.*, s.status AS session_status,
                       s.pathway_code, s.pathway_version,
                       s.questionnaire_response_id
                FROM agent_runs r
                JOIN care_sessions s ON s.session_id = r.session_id
                WHERE r.patient_id = ? AND r.input_text = ?
                ORDER BY r.completed_at DESC, r.run_id DESC
                LIMIT 1
                """,
                (DEMO_PATIENT_ID, MANUAL_REVIEW_MESSAGE),
            ).fetchone()
            if run_row is None:
                return empty

            result = SemanticResult.model_validate(json.loads(run_row["output_json"]))
            decisions = {
                row["action_id"]: row["decision"]
                for row in connection.execute(
                    """
                    SELECT action_id, decision
                    FROM conversation_action_resolutions
                    WHERE session_id = ?
                    """,
                    (run_row["session_id"],),
                ).fetchall()
            }
            response_id = run_row["questionnaire_response_id"]
            response = None
            if response_id:
                row = connection.execute(
                    """
                    SELECT resource_json FROM fhir_questionnaire_responses
                    WHERE resource_id = ? AND patient_id = ?
                    """,
                    (response_id, DEMO_PATIENT_ID),
                ).fetchone()
                response = json.loads(row[0]) if row else None
            observations = _json_rows(
                connection,
                """
                SELECT resource_json FROM fhir_observations
                WHERE patient_id = ? AND questionnaire_response_id = ?
                ORDER BY observation_id
                """,
                (DEMO_PATIENT_ID, response_id or ""),
            )

            all_tasks = _json_rows(
                connection,
                """
                SELECT resource_json FROM layer4_fhir_resources
                WHERE patient_id = ? AND resource_type = 'Task' AND is_current = 1
                """,
                (DEMO_PATIENT_ID,),
            )
            pathway_ref = (
                f"urn:continucare:pathway:{run_row['pathway_code']}"
                f"|{run_row['pathway_version']}"
            )
            manual_tasks = [
                item
                for item in all_tasks
                if is_manual_review_task(item)
                and pathway_ref
                in {ref.get("reference") for ref in item.get("basedOn", [])}
            ]
            manual_tasks.sort(
                key=lambda item: (
                    item.get("meta", {}).get("lastUpdated", ""), item["id"]
                ),
                reverse=True,
            )
            task = manual_tasks[0] if manual_tasks else None

            all_communications = _json_rows(
                connection,
                """
                SELECT resource_json FROM layer4_fhir_resources
                WHERE patient_id = ? AND resource_type = 'Communication'
                ORDER BY updated_at, resource_id, version_id
                """,
                (DEMO_PATIENT_ID,),
            )
            communications = [
                item
                for item in all_communications
                if is_manual_review_communication(item)
                and task is not None
                and any(
                    ref.get("reference", "").startswith(f"Task/{task['id']}/_history/")
                    for ref in item.get("basedOn", [])
                )
            ]
            current_communications = [
                item
                for item in communications
                if connection.execute(
                    """
                    SELECT is_current FROM layer4_fhir_resources
                    WHERE resource_type='Communication' AND resource_id=? AND version_id=?
                    """,
                    (item["id"], item["meta"]["versionId"]),
                ).fetchone()[0]
                == 1
            ]
            communication = current_communications[-1] if current_communications else None

            summary_rows = connection.execute(
                """
                SELECT record_json FROM layer4_contract_records
                WHERE patient_id = ? AND record_type='summary_draft'
                ORDER BY updated_at, record_id, record_version
                """,
                (DEMO_PATIENT_ID,),
            ).fetchall()
            summaries = []
            for row in summary_rows:
                candidate = Layer4SummaryDraft.model_validate(json.loads(row[0]))
                if (
                    candidate.summary_kind == "manual_review_brief"
                    and candidate.pathway_code == run_row["pathway_code"]
                    and candidate.pathway_version == run_row["pathway_version"]
                    and (task is None or _summary_mentions(candidate, task))
                ):
                    summaries.append(candidate)
            current_summary = max(
                summaries,
                key=lambda item: (item.created_at, item.summary_id, item.version),
                default=None,
            )

            alert_count = connection.execute(
                "SELECT COUNT(*) FROM alerts WHERE patient_id = ?",
                (DEMO_PATIENT_ID,),
            ).fetchone()[0]
            approved_rule_count = connection.execute(
                """
                SELECT COUNT(*) FROM layer4_contract_records
                WHERE record_type='clinical_rule' AND status IN ('approved', 'active')
                """
            ).fetchone()[0]
            provenance_count = connection.execute(
                """
                SELECT COUNT(*) FROM layer4_fhir_resources
                WHERE patient_id = ? AND resource_type='Provenance'
                """,
                (DEMO_PATIENT_ID,),
            ).fetchone()[0]
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE patient_id = ?",
                (DEMO_PATIENT_ID,),
            ).fetchone()[0]
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return empty.model_copy(
            update={"integrity_issue": "本地合成 Demo 数据不可读取；请明确重新开始。"}
        )

    expected_source = f"QuestionnaireResponse/{response_id}" if response_id else None
    patient_confirmed = bool(
        run_row["session_status"] == "completed"
        and response
        and response.get("status") == "completed"
        and observations
        and all(
            item.get("status") == "final"
            and expected_source
            in {ref.get("reference") for ref in item.get("derivedFrom", [])}
            for item in observations
        )
    )
    readiness = communication_readiness(communication) if communication else None
    has_pending_communication = any(
        communication_readiness(item) == PENDING_APPROVAL for item in communications
    )
    current_pending_brief = bool(
        communication
        and readiness == PENDING_APPROVAL
        and current_summary
        and _summary_mentions(current_summary, communication)
    )
    current_ready_brief = bool(
        communication
        and readiness == READY_TO_SEND
        and current_summary
        and _summary_mentions(current_summary, communication)
    )
    task_status = task.get("status") if task else None
    candidate_ids = {item.candidate_id for item in result.candidates}
    candidate_resolution_values = {
        candidate_id: decisions.get(candidate_id) for candidate_id in candidate_ids
    }
    has_pending_candidate = any(
        decision is None for decision in candidate_resolution_values.values()
    )
    all_candidates_rejected = bool(candidate_ids) and all(
        decision == "rejected" for decision in candidate_resolution_values.values()
    )
    has_unsure_candidate = any(
        decision == "unsure" for decision in candidate_resolution_values.values()
    )
    candidate_stage = CompetitionDemoStage.CANDIDATE_READY
    if all_candidates_rejected:
        candidate_stage = CompetitionDemoStage.CANDIDATE_REJECTED
    elif has_unsure_candidate and not has_pending_candidate:
        candidate_stage = CompetitionDemoStage.CANDIDATE_UNSURE

    milestones = {item: False for item in MILESTONE_ORDER}
    milestones.update(
        {
            CompetitionDemoStage.CANDIDATE_READY.value: bool(result.candidates),
            CompetitionDemoStage.CANDIDATE_UNSURE.value: candidate_stage
            == CompetitionDemoStage.CANDIDATE_UNSURE,
            CompetitionDemoStage.CANDIDATE_REJECTED.value: candidate_stage
            == CompetitionDemoStage.CANDIDATE_REJECTED,
            CompetitionDemoStage.PATIENT_CONFIRMED.value: patient_confirmed,
            CompetitionDemoStage.TASK_REQUESTED.value: task is not None,
            CompetitionDemoStage.NURSE_RECEIVED.value: task_status
            in {"received", "accepted", "in-progress", "completed"},
            CompetitionDemoStage.NURSE_IN_PROGRESS.value: task_status
            in {"accepted", "in-progress", "completed"},
            CompetitionDemoStage.TASK_REJECTED.value: task_status == "rejected",
            CompetitionDemoStage.TASK_CANCELLED.value: task_status == "cancelled",
            CompetitionDemoStage.TASK_FAILED.value: task_status == "failed",
            CompetitionDemoStage.TASK_ENTERED_IN_ERROR.value: task_status
            == "entered-in-error",
            CompetitionDemoStage.COMMUNICATION_PENDING.value: has_pending_communication,
            CompetitionDemoStage.DOCTOR_BRIEF_PENDING.value: current_pending_brief
            or any(
                communication_readiness(item) == PENDING_APPROVAL
                and any(_summary_mentions(summary, item) for summary in summaries)
                for item in communications
            ),
            CompetitionDemoStage.COMMUNICATION_READY.value: readiness
            == READY_TO_SEND,
            CompetitionDemoStage.DOCTOR_BRIEF_READY.value: current_ready_brief,
            CompetitionDemoStage.STORY_COMPLETE.value: bool(
                task_status == "completed" and current_ready_brief
            ),
        }
    )

    stage = candidate_stage
    next_page = "pages/1_patient_followup.py"
    next_label = "前往患者端明确确认"
    next_help = "患者确认前不会创建 QR、Observation 或护士任务。"
    is_terminal = False
    terminal_reason = None
    terminal_tasks = {
        "rejected": (
            CompetitionDemoStage.TASK_REJECTED,
            "护士已明确拒绝人工复核任务；流程已终止，未创建 Communication 或医生简报。",
        ),
        "cancelled": (
            CompetitionDemoStage.TASK_CANCELLED,
            "护士已明确取消人工复核任务；流程已终止，未创建 Communication 或医生简报。",
        ),
        "failed": (
            CompetitionDemoStage.TASK_FAILED,
            "人工复核任务以 failed 异常终止；流程已 fail-closed，未继续生成业务资源。",
        ),
        "entered-in-error": (
            CompetitionDemoStage.TASK_ENTERED_IN_ERROR,
            "人工复核任务已标记 entered-in-error；流程已 fail-closed，未继续生成业务资源。",
        ),
    }
    if task_status in terminal_tasks:
        stage, terminal_reason = terminal_tasks[task_status]
        is_terminal = True
    elif task is not None and task_status == "requested":
        stage = CompetitionDemoStage.TASK_REQUESTED
        next_page = "pages/2_nurse_risk_center.py"
        next_label = "前往护士端确认收到"
        next_help = "任务优先级为 routine，临床评估仍为 not_assessed。"
    elif task_status == "received":
        stage = CompetitionDemoStage.NURSE_RECEIVED
        next_page = "pages/2_nurse_risk_center.py"
        next_label = "接受并开始人工复核"
        next_help = "接受与开始是同一次明确动作。"
    elif task_status in {"accepted", "in-progress"}:
        stage = CompetitionDemoStage.NURSE_IN_PROGRESS
        next_page = "pages/2_nurse_risk_center.py"
        next_label = "记录受控结果并生成草稿"
        next_help = "草稿生成后仍须人工批准，且不会发送。"
    elif task_status == "completed" and readiness == PENDING_APPROVAL:
        if current_pending_brief:
            stage = CompetitionDemoStage.DOCTOR_BRIEF_PENDING
            next_page = "pages/2_nurse_risk_center.py"
            next_label = "返回护士端人工批准草稿"
            next_help = "批准只改变 readiness；ready-to-send 不等于 sent。"
        else:
            stage = CompetitionDemoStage.COMMUNICATION_PENDING
            next_page = "pages/3_doctor_summary.py"
            next_label = "前往医生端生成 pending 简报"
            next_help = "简报只在明确点击后生成。"
    elif task_status == "completed" and readiness == READY_TO_SEND:
        if current_ready_brief:
            stage = CompetitionDemoStage.STORY_COMPLETE
            is_terminal = True
            terminal_reason = (
                "合成 happy path 的 9 项持久化事实已完成；"
                "这不代表临床结论，Communication 仍未发送。"
            )
        else:
            stage = CompetitionDemoStage.COMMUNICATION_READY
            next_page = "pages/3_doctor_summary.py"
            next_label = "前往医生端生成或刷新 ready 简报"
            next_help = "当前来源已变化，旧简报保持不可变并显示陈旧。"
    elif patient_confirmed:
        stage = CompetitionDemoStage.PATIENT_CONFIRMED

    if stage == CompetitionDemoStage.CANDIDATE_REJECTED:
        is_terminal = True
        terminal_reason = (
            "所有候选均已由患者明确拒绝；未创建临床资源或护士任务。"
        )
    elif stage == CompetitionDemoStage.CANDIDATE_UNSURE:
        next_label = "返回患者端明确接受或拒绝"
        next_help = "暂不确定不是终态；患者仍可明确接受或拒绝现有候选。"

    if is_terminal:
        next_page = "pages/4_audit_log.py"
        next_label = "查看终态审计"
        next_help = (
            f"{terminal_reason} 如需新故事，请返回首页勾选确认后明确重新开始；"
            "系统不会自动重启。"
        )

    generation = f"{run_row['session_id']}:{run_row['run_id']}"
    return CompetitionDemoProgress(
        stage=stage,
        milestones=milestones,
        generation=generation,
        session_id=run_row["session_id"],
        run_id=run_row["run_id"],
        task_id=task["id"] if task else None,
        communication_id=communication["id"] if communication else None,
        summary_id=current_summary.summary_id if current_summary else None,
        summary_version=current_summary.version if current_summary else None,
        session_status=run_row["session_status"],
        task_status=task_status,
        communication_readiness=readiness,
        candidate_count=len(result.candidates),
        candidate_decisions=decisions,
        questionnaire_response_count=1 if response else 0,
        observation_count=len(observations),
        manual_task_count=len(manual_tasks),
        communication_count=len(communications),
        manual_brief_count=len(summaries),
        provenance_count=provenance_count,
        audit_count=audit_count,
        alert_count=alert_count,
        approved_clinical_rule_count=approved_rule_count,
        knowledge_available=knowledge_available,
        knowledge_error=knowledge_error,
        is_terminal=is_terminal,
        terminal_reason=terminal_reason,
        next_page=next_page,
        next_label=next_label,
        next_help=next_help,
    )


def _lock_path(db_path: Path) -> Path:
    return db_path.with_name(f".{db_path.name}.m5d.lock")


@contextmanager
def demo_write_guard(
    db_path: Path | str, *, expected_generation: str | None = None
) -> Iterator[None]:
    """Serialize local UI writes with reset/replace and reject stale stories."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(_lock_path(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if expected_generation is not None:
            current = read_competition_demo(path)
            if current.generation != expected_generation:
                raise CompetitionDemoConflict(
                    "比赛故事已在另一标签页重新开始，请刷新当前页面后继续。"
                )
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm"))


def _cleanup_exact_database_files(path: Path) -> None:
    for candidate in (path, *_sidecars(path)):
        if candidate.exists() and candidate.is_file():
            candidate.unlink()


def _atomic_stage_replace(
    db_path: Path,
    loader: Callable[[Path], Any],
    validator: Callable[[Path], None],
) -> Any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with demo_write_guard(db_path):
        prefix = f".{db_path.name}.m5d-"
        for orphan in db_path.parent.glob(f"{prefix}*"):
            if orphan.is_file():
                orphan.unlink()
        descriptor, staging_name = tempfile.mkstemp(
            prefix=prefix, suffix=".sqlite", dir=db_path.parent
        )
        os.close(descriptor)
        staging = Path(staging_name)
        try:
            result = loader(staging)
            os.chmod(staging, 0o600)
            if any(item.exists() for item in _sidecars(staging)):
                raise CompetitionDemoStartError("临时 Demo 数据未安全关闭，请重试。")
            validator(staging)
            staging_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(staging_fd)
            finally:
                os.close(staging_fd)

            # Explicit reset is the only operation allowed to remove exact
            # local SQLite sidecars.  The shared lock excludes all app writes.
            for sidecar in _sidecars(db_path):
                if sidecar.exists() and sidecar.is_file():
                    sidecar.unlink()
            os.replace(staging, db_path)
            directory_fd = os.open(db_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            if any(item.exists() for item in _sidecars(db_path)):
                raise CompetitionDemoStartError("Demo 数据替换未完成，请重试。")
            return result
        finally:
            _cleanup_exact_database_files(staging)


def _validate_candidate_start(staging: Path) -> None:
    progress = read_competition_demo(staging)
    if (
        progress.stage != CompetitionDemoStage.CANDIDATE_READY
        or progress.candidate_count < 1
        or progress.candidate_decisions
        or progress.questionnaire_response_count != 0
        or progress.observation_count != 0
        or progress.manual_task_count != 0
        or progress.communication_count != 0
        or progress.manual_brief_count != 0
        or progress.alert_count != 0
        or progress.approved_clinical_rule_count != 0
    ):
        raise CompetitionDemoStartError("完整比赛 Demo 起点校验失败，请重试。")


def start_competition_demo(db_path: Path | str) -> CompetitionDemoProgress:
    """Explicitly reset and prepare exactly one unreleased candidate story."""

    path = Path(db_path)
    try:
        _atomic_stage_replace(path, load_manual_review_scenario, _validate_candidate_start)
        progress = read_competition_demo(path)
        if progress.stage != CompetitionDemoStage.CANDIDATE_READY:
            raise CompetitionDemoStartError("完整比赛 Demo 起点未能恢复，请重试。")
        return progress
    except CompetitionDemoStartError:
        raise
    except Exception as exc:
        raise CompetitionDemoStartError(
            "完整比赛 Demo 暂时无法开始；旧故事未被替换，请明确重试。"
        ) from exc


def load_technical_demo_atomically(
    db_path: Path | str, scenario_label: str
) -> Any:
    """Keep legacy technical fixtures behind the same atomic reset boundary."""

    path = Path(db_path)

    def loader(staging: Path):
        return load_layer2_scenario(staging, scenario_label)

    try:
        return _atomic_stage_replace(path, loader, lambda candidate: _readonly_connection(candidate).close())
    except Exception as exc:
        raise CompetitionDemoStartError(
            "技术演示暂时无法载入；当前故事未被替换，请明确重试。"
        ) from exc

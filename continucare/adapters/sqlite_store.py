"""Persistent local DataStore backed by the standard sqlite3 module."""

from __future__ import annotations

import json
from pathlib import Path

from continucare.agents.contracts import AgentRunRecord
from continucare.db import connect, initialize_database
from continucare.fhir.r4 import FHIRValidationError, validate_r4_resource
from continucare.fhir.references import (
    validate_questionnaire_response_against_questionnaire,
)
from continucare.models import (
    Alert,
    AlertAction,
    AuditEvent,
    CareSession,
    CareSessionStatus,
    ConfirmedAnswerContext,
    ConfirmedSymptomReport,
    FollowUpMessage,
    Observation,
    Patient,
    Summary,
)
from continucare.repositories import (
    row_to_alert,
    row_to_alert_action,
    row_to_agent_run,
    row_to_audit_event,
    row_to_care_session,
    row_to_message,
    row_to_observation,
    row_to_patient,
    row_to_summary,
)
from continucare.pathways.fhir_artifacts import load_glp1_questionnaire


class SQLiteStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        initialize_database(self.db_path)

    def get_patient(self, patient_id: str) -> Patient | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM patients WHERE patient_id = ?", (patient_id,)
            ).fetchone()
        return row_to_patient(row) if row else None

    def save_care_session(self, session: CareSession) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO care_sessions (
                    session_id, patient_id, pathway_code, pathway_version,
                    questionnaire_canonical, questionnaire_version, status,
                    answers_json, questionnaire_response_id, created_at,
                    updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.patient_id,
                    session.pathway_code,
                    session.pathway_version,
                    session.questionnaire_canonical,
                    session.questionnaire_version,
                    session.status.value,
                    json.dumps(session.answers, ensure_ascii=False),
                    session.questionnaire_response_id,
                    session.created_at,
                    session.updated_at,
                    session.completed_at,
                ),
            )

    def get_care_session(self, session_id: str) -> CareSession | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM care_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row_to_care_session(row) if row else None

    def get_active_care_session(
        self, patient_id: str, pathway_code: str
    ) -> CareSession | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM care_sessions
                WHERE patient_id = ? AND pathway_code = ? AND status = 'in_progress'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (patient_id, pathway_code),
            ).fetchone()
        return row_to_care_session(row) if row else None

    def update_care_session(
        self,
        session_id: str,
        *,
        answers: dict,
        status: CareSessionStatus,
        updated_at: str,
        questionnaire_response_id: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE care_sessions
                SET answers_json = ?, status = ?, updated_at = ?,
                    questionnaire_response_id = COALESCE(?, questionnaire_response_id),
                    completed_at = COALESCE(?, completed_at)
                WHERE session_id = ?
                """,
                (
                    json.dumps(answers, ensure_ascii=False),
                    status.value,
                    updated_at,
                    questionnaire_response_id,
                    completed_at,
                    session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"care session {session_id!r} was not found")

    def list_care_sessions(self, patient_id: str) -> list[CareSession]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM care_sessions
                WHERE patient_id = ? ORDER BY updated_at DESC
                """,
                (patient_id,),
            ).fetchall()
        return [row_to_care_session(row) for row in rows]

    def save_agent_run(self, record: AgentRunRecord) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, task_id, patient_id, session_id, agent_name,
                    agent_version, mode, input_text, input_hash, output_json,
                    status, model_provider, model_name, prompt_version,
                    started_at, completed_at, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.task_id,
                    record.patient_id,
                    record.session_id,
                    record.agent_name,
                    record.agent_version,
                    record.mode,
                    record.input_text,
                    record.input_hash,
                    json.dumps(record.output_json, ensure_ascii=False),
                    record.status,
                    record.model_provider,
                    record.model_name,
                    record.prompt_version,
                    record.started_at,
                    record.completed_at,
                    record.error_code,
                ),
            )

    def get_agent_run(self, run_id: str) -> AgentRunRecord | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row_to_agent_run(row) if row else None

    def get_agent_run_by_task(self, task_id: str) -> AgentRunRecord | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row_to_agent_run(row) if row else None

    def list_agent_runs(self, session_id: str) -> list[AgentRunRecord]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_runs WHERE session_id = ?
                ORDER BY completed_at DESC
                """,
                (session_id,),
            ).fetchall()
        return [row_to_agent_run(row) for row in rows]

    def resolve_conversation_action(
        self,
        *,
        action_id: str,
        source_run_id: str,
        session_id: str,
        decision: str,
        resolved_at: str,
        option_id: str | None = None,
        response_run_id: str | None = None,
        response_text: str | None = None,
    ) -> None:
        """Close a candidate/clarification once, preserving the first decision."""

        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO conversation_action_resolutions (
                    action_id, source_run_id, session_id, response_run_id,
                    decision, option_id, response_text, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO NOTHING
                """,
                (
                    action_id,
                    source_run_id,
                    session_id,
                    response_run_id,
                    decision,
                    option_id,
                    response_text,
                    resolved_at,
                ),
            )

    def resolved_conversation_action_ids(self, session_id: str) -> set[str]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT action_id FROM conversation_action_resolutions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()
        return {row["action_id"] for row in rows}

    def save_confirmed_answer_context(self, context: ConfirmedAnswerContext) -> None:
        """Append a revision and keep exactly one active context per answer."""

        with connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE confirmed_answer_contexts SET status = 'superseded'
                WHERE session_id = ? AND link_id = ? AND status = 'active'
                """,
                (context.session_id, context.link_id),
            )
            connection.execute(
                """
                INSERT INTO confirmed_answer_contexts (
                    answer_context_id, session_id, link_id, answer_json,
                    source_run_id, followup_occurrence_id, patient_timezone,
                    reported_at, effective_start, effective_end, temporal_kind,
                    resolution_basis, raw_text, terminology_match_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(answer_context_id) DO UPDATE SET status = 'active'
                """,
                (
                    context.answer_context_id,
                    context.session_id,
                    context.link_id,
                    json.dumps(context.answer, ensure_ascii=False),
                    context.source_run_id,
                    context.followup_occurrence_id,
                    context.patient_timezone,
                    context.reported_at,
                    context.effective_start,
                    context.effective_end,
                    context.temporal_kind,
                    context.resolution_basis,
                    context.raw_text,
                    (
                        json.dumps(context.terminology_match, ensure_ascii=False)
                        if context.terminology_match is not None
                        else None
                    ),
                    context.status,
                    context.created_at,
                ),
            )

    def list_active_answer_contexts(
        self, session_id: str
    ) -> list[ConfirmedAnswerContext]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM confirmed_answer_contexts
                WHERE session_id = ? AND status = 'active'
                ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
        return [
            ConfirmedAnswerContext(
                answer_context_id=row["answer_context_id"],
                session_id=row["session_id"],
                link_id=row["link_id"],
                answer=json.loads(row["answer_json"]),
                source_run_id=row["source_run_id"],
                followup_occurrence_id=row["followup_occurrence_id"],
                patient_timezone=row["patient_timezone"],
                reported_at=row["reported_at"],
                effective_start=row["effective_start"],
                effective_end=row["effective_end"],
                temporal_kind=row["temporal_kind"],
                resolution_basis=row["resolution_basis"],
                raw_text=row["raw_text"],
                terminology_match=(
                    json.loads(row["terminology_match_json"])
                    if row["terminology_match_json"]
                    else None
                ),
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def save_confirmed_symptom_report(
        self, report: ConfirmedSymptomReport
    ) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO confirmed_symptom_reports (
                    report_id, session_id, concept_id, preferred_zh, coding_json,
                    terminology_match_json, source_kind, source_run_id, evidence_text,
                    evidence_start, evidence_end, followup_occurrence_id,
                    patient_timezone, reported_at, effective_start, effective_end,
                    temporal_kind, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, concept_id) DO UPDATE SET
                    report_id = excluded.report_id,
                    preferred_zh = excluded.preferred_zh,
                    coding_json = excluded.coding_json,
                    terminology_match_json = excluded.terminology_match_json,
                    source_kind = excluded.source_kind,
                    source_run_id = excluded.source_run_id,
                    evidence_text = excluded.evidence_text,
                    evidence_start = excluded.evidence_start,
                    evidence_end = excluded.evidence_end,
                    followup_occurrence_id = excluded.followup_occurrence_id,
                    patient_timezone = excluded.patient_timezone,
                    reported_at = excluded.reported_at,
                    effective_start = excluded.effective_start,
                    effective_end = excluded.effective_end,
                    temporal_kind = excluded.temporal_kind,
                    status = 'active',
                    created_at = excluded.created_at
                """,
                (
                    report.report_id,
                    report.session_id,
                    report.concept_id,
                    report.preferred_zh,
                    json.dumps(report.coding, ensure_ascii=False),
                    json.dumps(report.terminology_match, ensure_ascii=False),
                    report.source_kind,
                    report.source_run_id,
                    report.evidence_text,
                    report.evidence_start,
                    report.evidence_end,
                    report.followup_occurrence_id,
                    report.patient_timezone,
                    report.reported_at,
                    report.effective_start,
                    report.effective_end,
                    report.temporal_kind,
                    report.status,
                    report.created_at,
                ),
            )

    def list_active_symptom_reports(
        self, session_id: str
    ) -> list[ConfirmedSymptomReport]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM confirmed_symptom_reports
                WHERE session_id = ? AND status = 'active'
                ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
        return [
            ConfirmedSymptomReport(
                report_id=row["report_id"],
                session_id=row["session_id"],
                concept_id=row["concept_id"],
                preferred_zh=row["preferred_zh"],
                coding=json.loads(row["coding_json"]),
                terminology_match=json.loads(row["terminology_match_json"]),
                source_kind=row["source_kind"],
                source_run_id=row["source_run_id"],
                evidence_text=row["evidence_text"],
                evidence_start=row["evidence_start"],
                evidence_end=row["evidence_end"],
                followup_occurrence_id=row["followup_occurrence_id"],
                patient_timezone=row["patient_timezone"],
                reported_at=row["reported_at"],
                effective_start=row["effective_start"],
                effective_end=row["effective_end"],
                temporal_kind=row["temporal_kind"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def complete_care_session_submission(
        self,
        *,
        session: CareSession,
        message: FollowUpMessage,
        questionnaire_response: dict,
        questionnaire: dict,
        observations: list[Observation],
        completed_at: str,
    ) -> None:
        """Atomically persist the Layer-2 response, facts, and session transition."""

        resource = validate_questionnaire_response_against_questionnaire(
            questionnaire_response, questionnaire
        )
        patient_id = resource["subject"]["reference"].removeprefix("Patient/")
        if patient_id != session.patient_id or message.patient_id != session.patient_id:
            raise FHIRValidationError(
                "care session, message and QuestionnaireResponse patient must match"
            )
        if message.message_id != resource["id"]:
            raise FHIRValidationError(
                "follow-up message id must match QuestionnaireResponse.id"
            )

        validated_observations: list[Observation] = []
        for item in observations:
            normalized = validate_r4_resource(
                item.as_fhir(), expected_resource_type="Observation"
            )
            validated = Observation(resource=normalized, evidence=item.evidence)
            if validated.patient_id != session.patient_id:
                raise FHIRValidationError("Observation patient must match care session")
            validated_observations.append(validated)

        with connect(self.db_path) as connection:
            current = connection.execute(
                "SELECT status FROM care_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if current is None:
                raise LookupError(f"care session {session.session_id!r} was not found")
            if current["status"] != CareSessionStatus.IN_PROGRESS.value:
                raise ValueError("只有进行中的随访可以提交")

            connection.execute(
                """
                INSERT INTO followup_messages (
                    message_id, patient_id, message_text, submitted_at,
                    source, processing_status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(message.model_dump().values()),
            )
            connection.execute(
                """
                INSERT INTO fhir_questionnaire_responses (
                    resource_id, patient_id, message_id, resource_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    resource["id"],
                    patient_id,
                    message.message_id,
                    json.dumps(resource, ensure_ascii=False, separators=(",", ":")),
                    resource["authored"],
                ),
            )
            for item in validated_observations:
                connection.execute(
                    """
                    INSERT INTO fhir_observations (
                        observation_id, patient_id, questionnaire_response_id,
                        effective_time, resource_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.observation_id,
                        item.patient_id,
                        item.message_id,
                        item.effective_time,
                        json.dumps(
                            item.as_fhir(), ensure_ascii=False, separators=(",", ":")
                        ),
                        item.created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO observation_evidence (
                        observation_id, confidence_tier, evidence_text,
                        evidence_start, evidence_end, recorded_at, source_kind,
                        terminology_match_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.observation_id,
                        item.confidence_tier.value,
                        item.evidence_text,
                        item.evidence_start,
                        item.evidence_end,
                        item.created_at,
                        item.evidence.source_kind,
                        (
                            json.dumps(
                                item.evidence.terminology_match, ensure_ascii=False
                            )
                            if item.evidence.terminology_match is not None
                            else None
                        ),
                    ),
                )
            cursor = connection.execute(
                """
                UPDATE care_sessions
                SET answers_json = ?, status = 'completed', updated_at = ?,
                    questionnaire_response_id = ?, completed_at = ?
                WHERE session_id = ? AND status = 'in_progress'
                """,
                (
                    json.dumps(session.answers, ensure_ascii=False),
                    completed_at,
                    resource["id"],
                    completed_at,
                    session.session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("随访状态已变化，请刷新后重试")

    def save_message(self, message: FollowUpMessage) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO followup_messages (
                    message_id, patient_id, message_text, submitted_at,
                    source, processing_status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(message.model_dump().values()),
            )

    def update_message_status(self, message_id: str, status: str) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                "UPDATE followup_messages SET processing_status = ? WHERE message_id = ?",
                (status, message_id),
            )

    def get_message(self, message_id: str) -> FollowUpMessage | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM followup_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return row_to_message(row) if row else None

    def save_questionnaire_response(
        self, resource: dict, questionnaire: dict | None = None
    ) -> None:
        resource = validate_questionnaire_response_against_questionnaire(
            resource, questionnaire or load_glp1_questionnaire()
        )
        patient_reference = resource["subject"]["reference"]
        patient_id = patient_reference.removeprefix("Patient/")
        with connect(self.db_path) as connection:
            message_row = connection.execute(
                "SELECT patient_id FROM followup_messages WHERE message_id = ?",
                (resource["id"],),
            ).fetchone()
            if message_row is None:
                raise FHIRValidationError(
                    "QuestionnaireResponse.id must match a stored follow-up message"
                )
            if message_row["patient_id"] != patient_id:
                raise FHIRValidationError(
                    "QuestionnaireResponse.subject must match the source message patient"
                )
            connection.execute(
                """
                INSERT INTO fhir_questionnaire_responses (
                    resource_id, patient_id, message_id, resource_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    resource["id"],
                    patient_id,
                    resource["id"],
                    json.dumps(resource, ensure_ascii=False, separators=(",", ":")),
                    resource["authored"],
                ),
            )

    def get_questionnaire_response(
        self, resource_id: str, questionnaire: dict | None = None
    ) -> dict | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT resource_json FROM fhir_questionnaire_responses
                WHERE resource_id = ?
                """,
                (resource_id,),
            ).fetchone()
        if row is None:
            return None
        return validate_questionnaire_response_against_questionnaire(
            json.loads(row["resource_json"]),
            questionnaire or load_glp1_questionnaire(),
        )

    def list_completed_questionnaire_responses(
        self, patient_id: str
    ) -> list[dict]:
        """Return only responses finalized by a completed CareSession.

        This is the Layer-4 read boundary. Compatibility free-text responses
        and in-progress session state are intentionally excluded.
        """

        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT q.resource_json
                FROM fhir_questionnaire_responses q
                JOIN care_sessions c
                  ON c.questionnaire_response_id = q.resource_id
                WHERE q.patient_id = ? AND c.status = 'completed'
                ORDER BY q.created_at DESC
                """,
                (patient_id,),
            ).fetchall()
        return [
            validate_questionnaire_response_against_questionnaire(
                json.loads(row["resource_json"]), load_glp1_questionnaire()
            )
            for row in rows
        ]

    def list_messages(self, patient_id: str) -> list[FollowUpMessage]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM followup_messages
                WHERE patient_id = ? ORDER BY submitted_at DESC
                """,
                (patient_id,),
            ).fetchall()
        return [row_to_message(row) for row in rows]

    def save_observations(self, observations: list[Observation]) -> None:
        if not observations:
            return
        with connect(self.db_path) as connection:
            for item in observations:
                resource = validate_r4_resource(
                    item.as_fhir(), expected_resource_type="Observation"
                )
                item = Observation(resource=resource, evidence=item.evidence)
                connection.execute(
                    """
                    INSERT INTO fhir_observations (
                        observation_id, patient_id, questionnaire_response_id,
                        effective_time, resource_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.observation_id,
                        item.patient_id,
                        item.message_id,
                        item.effective_time,
                        json.dumps(
                            item.as_fhir(), ensure_ascii=False, separators=(",", ":")
                        ),
                        item.created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO observation_evidence (
                        observation_id, confidence_tier, evidence_text,
                        evidence_start, evidence_end, recorded_at, source_kind,
                        terminology_match_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.observation_id,
                        item.confidence_tier.value,
                        item.evidence_text,
                        item.evidence_start,
                        item.evidence_end,
                        item.created_at,
                        item.evidence.source_kind,
                        (
                            json.dumps(
                                item.evidence.terminology_match, ensure_ascii=False
                            )
                            if item.evidence.terminology_match is not None
                            else None
                        ),
                    ),
                )

    def list_observations(self, patient_id: str) -> list[Observation]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT o.*, e.confidence_tier, e.evidence_text,
                       e.evidence_start, e.evidence_end, e.recorded_at,
                       e.source_kind, e.terminology_match_json
                FROM fhir_observations o
                JOIN observation_evidence e USING (observation_id)
                WHERE o.patient_id = ? ORDER BY o.effective_time DESC
                """,
                (patient_id,),
            ).fetchall()
        return [row_to_observation(row) for row in rows]

    def list_final_observations(self, patient_id: str) -> list[Observation]:
        """Return only Observations derived from completed CareSession responses."""

        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT o.*, e.confidence_tier, e.evidence_text,
                       e.evidence_start, e.evidence_end, e.recorded_at,
                       e.source_kind, e.terminology_match_json
                FROM fhir_observations o
                JOIN observation_evidence e USING (observation_id)
                JOIN care_sessions c
                  ON c.questionnaire_response_id = o.questionnaire_response_id
                WHERE o.patient_id = ? AND c.status = 'completed'
                ORDER BY o.effective_time DESC
                """,
                (patient_id,),
            ).fetchall()
        return [row_to_observation(row) for row in rows]

    def list_observations_for_message(self, message_id: str) -> list[Observation]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT o.*, e.confidence_tier, e.evidence_text,
                       e.evidence_start, e.evidence_end, e.recorded_at,
                       e.source_kind, e.terminology_match_json
                FROM fhir_observations o
                JOIN observation_evidence e USING (observation_id)
                WHERE o.questionnaire_response_id = ? ORDER BY e.evidence_start
                """,
                (message_id,),
            ).fetchall()
        return [row_to_observation(row) for row in rows]

    def get_observation(self, observation_id: str) -> Observation | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT o.*, e.confidence_tier, e.evidence_text,
                       e.evidence_start, e.evidence_end, e.recorded_at,
                       e.source_kind, e.terminology_match_json
                FROM fhir_observations o
                JOIN observation_evidence e USING (observation_id)
                WHERE o.observation_id = ?
                """,
                (observation_id,),
            ).fetchone()
        return row_to_observation(row) if row else None

    def save_alert(self, alert: Alert) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO alerts (
                    alert_id, patient_id, severity, title, trigger_rule_id,
                    trigger_reason, evidence_refs_json, owner_role, status,
                    sla_due_at, created_at, resolved_at, resolution_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.alert_id,
                    alert.patient_id,
                    alert.severity,
                    alert.title,
                    alert.trigger_rule_id,
                    alert.trigger_reason,
                    json.dumps(alert.evidence_refs, ensure_ascii=False),
                    alert.owner_role,
                    alert.status.value,
                    alert.sla_due_at,
                    alert.created_at,
                    alert.resolved_at,
                    alert.resolution_reason,
                ),
            )

    def update_alert(self, alert: Alert) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE alerts SET status = ?, owner_role = ?, resolved_at = ?,
                    resolution_reason = ? WHERE alert_id = ?
                """,
                (
                    alert.status.value,
                    alert.owner_role,
                    alert.resolved_at,
                    alert.resolution_reason,
                    alert.alert_id,
                ),
            )

    def get_alert(self, alert_id: str) -> Alert | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)
            ).fetchone()
        return row_to_alert(row) if row else None

    def list_alerts(self, patient_id: str | None = None) -> list[Alert]:
        query = "SELECT * FROM alerts"
        params: tuple[str, ...] = ()
        if patient_id:
            query += " WHERE patient_id = ?"
            params = (patient_id,)
        query += " ORDER BY created_at DESC"
        with connect(self.db_path) as connection:
            rows = connection.execute(query, params).fetchall()
        return [row_to_alert(row) for row in rows]

    def save_alert_action(self, action: AlertAction) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO alert_actions (
                    action_id, alert_id, action_type, actor_role, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(action.model_dump().values()),
            )

    def list_alert_actions(self, alert_id: str) -> list[AlertAction]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM alert_actions
                WHERE alert_id = ? ORDER BY created_at
                """,
                (alert_id,),
            ).fetchall()
        return [row_to_alert_action(row) for row in rows]

    def get_alert_action(self, action_id: str) -> AlertAction | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM alert_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return row_to_alert_action(row) if row else None

    def save_summary(self, summary: Summary) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO summaries (
                    summary_id, patient_id, period_start, period_end, status,
                    summary_json, created_at, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.summary_id,
                    summary.patient_id,
                    summary.period_start,
                    summary.period_end,
                    summary.status,
                    summary.summary_json.model_dump_json(),
                    summary.created_at,
                    summary.reviewed_at,
                ),
            )

    def update_summary_review(self, summary_id: str, reviewed_at: str) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE summaries SET status = 'reviewed', reviewed_at = ?
                WHERE summary_id = ?
                """,
                (reviewed_at, summary_id),
            )

    def get_latest_summary(self, patient_id: str) -> Summary | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT * FROM summaries WHERE patient_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (patient_id,),
            ).fetchone()
        return row_to_summary(row) if row else None

    def get_summary(self, summary_id: str) -> Summary | None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM summaries WHERE summary_id = ?", (summary_id,)
            ).fetchone()
        return row_to_summary(row) if row else None

    def append_audit_event(self, event: AuditEvent) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, patient_id, entity_type, entity_id, event_type,
                    actor_type, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.patient_id,
                    event.entity_type,
                    event.entity_id,
                    event.event_type,
                    event.actor_type,
                    json.dumps(event.details_json, ensure_ascii=False),
                    event.created_at,
                ),
            )

    def list_audit_events(self, patient_id: str | None = None) -> list[AuditEvent]:
        query = "SELECT * FROM audit_events"
        params: tuple[str, ...] = ()
        if patient_id:
            query += " WHERE patient_id = ?"
            params = (patient_id,)
        query += " ORDER BY created_at DESC"
        with connect(self.db_path) as connection:
            rows = connection.execute(query, params).fetchall()
        return [row_to_audit_event(row) for row in rows]

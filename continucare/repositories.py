"""SQLite row mappings kept separate from business services."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from continucare.agents.contracts import AgentRunRecord
from continucare.models import (
    Alert,
    AlertAction,
    AuditEvent,
    CareSession,
    FollowUpMessage,
    Observation,
    Patient,
    Summary,
)


def row_to_patient(row: sqlite3.Row) -> Patient:
    return Patient(**{**dict(row), "synthetic": bool(row["synthetic"])})


def row_to_message(row: sqlite3.Row) -> FollowUpMessage:
    return FollowUpMessage(**dict(row))


def row_to_care_session(row: sqlite3.Row) -> CareSession:
    data = dict(row)
    data["answers"] = json.loads(data.pop("answers_json"))
    return CareSession(**data)


def row_to_agent_run(row: sqlite3.Row) -> AgentRunRecord:
    data = dict(row)
    data["output_json"] = json.loads(data["output_json"])
    return AgentRunRecord(**data)


def row_to_observation(row: sqlite3.Row) -> Observation:
    data = dict(row)
    return Observation(
        resource=json.loads(data["resource_json"]),
        evidence={
            "questionnaire_response_id": data["questionnaire_response_id"],
            "confidence_tier": data["confidence_tier"],
            "evidence_text": data["evidence_text"],
            "evidence_start": data["evidence_start"],
            "evidence_end": data["evidence_end"],
            "recorded_at": data["recorded_at"],
            "source_kind": data.get("source_kind") or "pathway_monitored",
            "terminology_match": (
                json.loads(data["terminology_match_json"])
                if data.get("terminology_match_json")
                else None
            ),
            "metric_id": data.get("metric_id"),
            "evidence_claim_ids": (
                json.loads(data["evidence_claim_ids_json"])
                if data.get("evidence_claim_ids_json")
                else []
            ),
            "knowledge_release_id": data.get("knowledge_release_id"),
            "observation_mapping_sha256": data.get("observation_mapping_sha256"),
        },
    )


def row_to_alert(row: sqlite3.Row) -> Alert:
    data = dict(row)
    data["evidence_refs"] = json.loads(data.pop("evidence_refs_json"))
    return Alert(**data)


def row_to_alert_action(row: sqlite3.Row) -> AlertAction:
    return AlertAction(**dict(row))


def row_to_summary(row: sqlite3.Row) -> Summary:
    data = dict(row)
    data["summary_json"] = json.loads(data["summary_json"])
    return Summary(**data)


def row_to_audit_event(row: sqlite3.Row) -> AuditEvent:
    data = dict(row)
    data["details_json"] = json.loads(data["details_json"])
    return AuditEvent(**data)


def placeholders(values: Iterable[Any]) -> str:
    return ", ".join("?" for _ in values)

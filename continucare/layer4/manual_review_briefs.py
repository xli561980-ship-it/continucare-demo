"""Deterministic, evidence-locked M5-C doctor briefs for manual review."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from continucare.layer4.contracts import (
    EvidenceReference,
    EvidenceRole,
    Layer4SummaryDraft,
    ResourceReference,
    SummaryDraftStatus,
    SummaryEvidenceItem,
)
from continucare.layer4.fhir import build_provenance, validate_layer4_fhir_resource
from continucare.layer4.manual_reviews import (
    EVIDENCE_DIGEST_EXTENSION_URL,
    MANUAL_REVIEW_OUTCOME_LABELS,
    PENDING_APPROVAL,
    READY_TO_SEND,
    admit_final_patient_report,
    communication_readiness,
    is_manual_review_communication,
    is_manual_review_task,
)
from continucare.layer4.repository import Layer4Repository
from continucare.models import AuditEvent, FollowUpMessage


BRIEF_GENERATOR_VERSION = "manual-review-brief-v1"
BRIEF_AGENT_REFERENCE = "Device/continucare-manual-review-brief"
BRIEF_SUMMARY_KIND = "manual_review_brief"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(31).join(parts).encode()).hexdigest()[:24]}"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("M5-C brief times must include a timezone offset")
    return parsed


def _versioned(resource: dict[str, Any]) -> str:
    version = resource.get("meta", {}).get("versionId") or "1"
    return f"{resource['resourceType']}/{resource['id']}/_history/{version}"


def _task_output(task: dict[str, Any], code: str) -> Any:
    matches = []
    for item in task.get("output", []):
        codes = {
            coding.get("code")
            for coding in item.get("type", {}).get("coding", [])
        }
        if code in codes:
            matches.append(item)
    if len(matches) != 1:
        raise ValueError(f"manual review Task requires exactly one {code} output")
    item = matches[0]
    values = [
        item[key]
        for key in ("valueCode", "valueString")
        if key in item
    ]
    values.extend(
        [item.get("valueReference", {}).get("reference")]
        if item.get("valueReference", {}).get("reference")
        else []
    )
    if len(values) != 1:
        raise ValueError(f"manual review Task {code} output is ambiguous")
    return values[0]


def _extension(resource: dict[str, Any], url: str) -> str:
    values = [
        item.get("valueString")
        for item in resource.get("extension", [])
        if item.get("url") == url
    ]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"resource requires exactly one {url} extension")
    return cast(str, values[0])


def _patient_quote(response: dict[str, Any]) -> str:
    items = [
        item for item in response.get("item", []) if item.get("linkId") == "free-text-report"
    ]
    if len(items) != 1:
        raise ValueError("completed response requires exactly one free-text-report item")
    answers = items[0].get("answer", [])
    if len(answers) != 1 or set(answers[0]) != {"valueString"}:
        raise ValueError("free-text-report requires exactly one string answer")
    quote = answers[0]["valueString"]
    if not isinstance(quote, str) or not quote:
        raise ValueError("patient quote cannot be blank")
    return quote


def _observation_value(resource: dict[str, Any]) -> str:
    value_fields = [key for key in resource if key.startswith("value")]
    if len(value_fields) != 1:
        raise ValueError("final Observation requires exactly one value[x]")
    key = value_fields[0]
    value = resource[key]
    return f"{key}={_canonical(value) if isinstance(value, (dict, list)) else value}"


def _observation_time(resource: dict[str, Any]) -> str:
    if resource.get("effectiveDateTime"):
        return resource["effectiveDateTime"]
    period = resource.get("effectivePeriod", {})
    value = period.get("end") or period.get("start") or resource.get("issued")
    if not value:
        raise ValueError("final Observation requires an effective time")
    return value


def _evidence(
    reference: str,
    *,
    evidence_id: str,
    effective_start: str | None = None,
    effective_end: str | None = None,
    evidence_text: str | None = None,
    role: EvidenceRole = EvidenceRole.SOURCE,
) -> EvidenceReference:
    parts = reference.split("/_history/", 1)
    return EvidenceReference(
        evidence_id=evidence_id,
        resource=ResourceReference(
            reference=parts[0], version_id=parts[1] if len(parts) == 2 else None
        ),
        role=role,
        effective_start=effective_start,
        effective_end=effective_end,
        evidence_text=evidence_text,
    )


@dataclass(frozen=True)
class ManualReviewBriefSnapshot:
    patient_id: str
    task: dict[str, Any]
    communication: dict[str, Any]
    response: dict[str, Any]
    observations: list[dict[str, Any]]
    message: FollowUpMessage
    quote: str
    outcome: str
    outcome_label: str
    readiness: str
    provenances: list[dict[str, Any]]
    audits: list[AuditEvent]
    source_digest: str
    period_start: str
    period_end: str


class ManualReviewBriefService:
    """Create a brief only through an explicit, atomic, deterministic action."""

    def __init__(
        self,
        store,
        repository: Layer4Repository,
        *,
        pathway_code: str,
        pathway_version: str,
    ):
        self.store = store
        self.repository = repository
        self.pathway_code = pathway_code
        self.pathway_version = pathway_version

    def list_manual_tasks(
        self, patient_id: str, *, as_of: str
    ) -> list[dict[str, Any]]:
        cutoff = _instant(as_of)
        pathway_ref = (
            f"urn:continucare:pathway:{self.pathway_code}|{self.pathway_version}"
        )
        resources = self.repository.list_fhir_resources(
            patient_id=patient_id, resource_type="Task", current_only=False
        )
        candidates = [
            item
            for item in resources
            if is_manual_review_task(item)
            and item.get("for", {}).get("reference") == f"Patient/{patient_id}"
            and pathway_ref
            in {entry.get("reference") for entry in item.get("basedOn", [])}
            and _instant(item["meta"]["lastUpdated"]) <= cutoff
        ]
        selected: dict[str, dict[str, Any]] = {}
        for item in candidates:
            current = selected.get(item["id"])
            if current is None or (
                _instant(item["meta"]["lastUpdated"]),
                int(item["meta"]["versionId"]),
            ) > (
                _instant(current["meta"]["lastUpdated"]),
                int(current["meta"]["versionId"]),
            ):
                selected[item["id"]] = item
        return sorted(
            selected.values(),
            key=lambda item: (_instant(item["meta"]["lastUpdated"]), item["id"]),
            reverse=True,
        )

    def inspect(
        self, *, patient_id: str, task_id: str, as_of: str
    ) -> ManualReviewBriefSnapshot:
        tasks = [
            item
            for item in self.list_manual_tasks(patient_id, as_of=as_of)
            if item["id"] == task_id
        ]
        if len(tasks) != 1:
            raise ValueError("manual review Task was not found in the requested scope")
        task = tasks[0]
        if task.get("status") != "completed":
            raise ValueError("only a completed manual review Task can enter the brief")

        response_ref = task.get("reasonReference", {}).get("reference", "")
        if not response_ref.startswith("QuestionnaireResponse/"):
            raise ValueError("manual review Task lacks its completed response")
        response = self.store.get_questionnaire_response(response_ref.split("/", 1)[1])
        if response is None:
            raise ValueError("manual review brief response is missing")
        input_refs = {
            item.get("valueReference", {}).get("reference")
            for item in task.get("input", [])
            if item.get("valueReference", {}).get("reference")
        }
        observation_refs = sorted(
            item for item in input_refs if item.startswith("Observation/")
        )
        observations = []
        for reference in observation_refs:
            item = self.store.get_observation(reference.split("/", 1)[1])
            if item is None:
                raise ValueError("manual review brief Observation is missing")
            observations.append(item.as_fhir())
        response, observations = admit_final_patient_report(
            patient_id=patient_id,
            questionnaire_response=response,
            observations=observations,
        )
        if input_refs != {response_ref, *observation_refs}:
            raise ValueError("manual review Task evidence references changed")
        quote = _patient_quote(response)
        message = self.store.get_message(response["id"])
        if message is None or message.patient_id != patient_id:
            raise ValueError("manual review brief patient message is missing")

        outcome = cast(str, _task_output(task, "review-outcome"))
        if outcome not in MANUAL_REVIEW_OUTCOME_LABELS:
            raise ValueError("manual review Task outcome is not controlled")
        task_digest = cast(str, _task_output(task, "evidence-digest"))
        communication_ref = cast(str, _task_output(task, "communication-draft"))
        if not communication_ref.startswith("Communication/"):
            raise ValueError("manual review Task lacks its Communication draft")
        evidence_digest = _sha256(
            {
                "message": message.model_dump(mode="json"),
                "questionnaire_response": response,
                "observations": sorted(observations, key=lambda item: item["id"]),
            }
        )
        if task_digest != evidence_digest:
            raise ValueError("manual review Task evidence digest mismatch")

        communications = [
            item
            for item in self.repository.list_fhir_resources(
                patient_id=patient_id,
                resource_type="Communication",
                current_only=False,
            )
            if item["id"] == communication_ref.split("/", 1)[1]
            and _instant(item["meta"]["lastUpdated"]) <= _instant(as_of)
        ]
        if not communications:
            raise ValueError("manual review Communication is missing")
        communication = max(
            communications,
            key=lambda item: (
                _instant(item["meta"]["lastUpdated"]),
                int(item["meta"]["versionId"]),
            ),
        )
        if not is_manual_review_communication(communication):
            raise ValueError("manual review Communication class mismatch")
        if communication.get("subject", {}).get("reference") != f"Patient/{patient_id}":
            raise ValueError("manual review Communication patient mismatch")
        readiness = communication_readiness(communication)
        if readiness not in {PENDING_APPROVAL, READY_TO_SEND}:
            raise ValueError("manual review Communication readiness is invalid")
        if communication.get("status") != "preparation":
            raise ValueError("manual review Communication must remain preparation")
        if "sent" in communication or "received" in communication:
            raise ValueError("manual review Communication must remain unsent")
        task_ref = _versioned(task)
        if {item.get("reference") for item in communication.get("basedOn", [])} != {
            task_ref
        }:
            raise ValueError("manual review Communication Task reference changed")
        expected_about = {
            f"urn:continucare:followup-message:{message.message_id}",
            response_ref,
            *observation_refs,
        }
        if {item.get("reference") for item in communication.get("about", [])} != expected_about:
            raise ValueError("manual review Communication evidence references changed")
        if _extension(communication, EVIDENCE_DIGEST_EXTENSION_URL) != evidence_digest:
            raise ValueError("manual review Communication evidence digest mismatch")
        if readiness == READY_TO_SEND and len(communication.get("note", [])) != 1:
            raise ValueError("ready-to-send Communication requires one approval record")

        provenances = self._required_provenances(
            patient_id=patient_id,
            task=task,
            communication=communication,
            response=response,
            observations=observations,
            as_of=as_of,
        )
        audits = self._required_audits(
            patient_id=patient_id,
            task=task,
            response=response,
            readiness=readiness,
            as_of=as_of,
        )
        source_payload = {
            "generator_version": BRIEF_GENERATOR_VERSION,
            "patient_id": patient_id,
            "pathway_code": self.pathway_code,
            "pathway_version": self.pathway_version,
            "message": message.model_dump(mode="json"),
            "response": response,
            "observations": sorted(observations, key=lambda item: item["id"]),
            "task": task,
            "communication": communication,
            "provenances": sorted(provenances, key=_versioned),
            "audits": sorted(
                (item.model_dump(mode="json") for item in audits),
                key=lambda item: item["event_id"],
            ),
        }
        times = [
            response["authored"],
            task.get("authoredOn") or task["meta"]["lastUpdated"],
            task["meta"]["lastUpdated"],
            communication["meta"]["lastUpdated"],
            *(_observation_time(item) for item in observations),
            *(item["recorded"] for item in provenances),
        ]
        return ManualReviewBriefSnapshot(
            patient_id=patient_id,
            task=task,
            communication=communication,
            response=response,
            observations=observations,
            message=message,
            quote=quote,
            outcome=outcome,
            outcome_label=MANUAL_REVIEW_OUTCOME_LABELS[outcome],
            readiness=readiness,
            provenances=provenances,
            audits=audits,
            source_digest=_sha256(source_payload),
            period_start=min(times, key=_instant),
            period_end=max(times, key=_instant),
        )

    def generate(
        self,
        *,
        patient_id: str,
        task_id: str,
        generated_at: str,
    ) -> Layer4SummaryDraft:
        snapshot = self.inspect(
            patient_id=patient_id, task_id=task_id, as_of=generated_at
        )
        if _instant(generated_at) < _instant(snapshot.period_end):
            raise ValueError("brief generation time precedes its source evidence")
        summary_id = _stable_id(
            "summary-manual-review",
            patient_id,
            self.pathway_code,
            self.pathway_version,
            task_id,
        )
        current_record = self.repository.get_contract("summary_draft", summary_id)
        current = cast(Layer4SummaryDraft | None, current_record)
        if current is not None:
            if (
                current.patient_id != patient_id
                or current.pathway_code != self.pathway_code
                or current.pathway_version != self.pathway_version
                or current.summary_kind != BRIEF_SUMMARY_KIND
            ):
                raise ValueError("stored M5-C Summary identity is outside the requested scope")
            if current.source_evidence_digest == snapshot.source_digest:
                return current
            try:
                version = str(int(current.version) + 1)
            except ValueError as exc:
                raise ValueError("M5-C Summary versions must be numeric") from exc
        else:
            version = "1"

        items = self._summary_items(snapshot)
        provenance_id = _stable_id(
            "provenance-manual-brief", summary_id, version, snapshot.source_digest
        )
        provenance_ref = ResourceReference(
            reference=f"Provenance/{provenance_id}", version_id="1"
        )
        direct_refs = {
            f"urn:continucare:followup-message:{snapshot.message.message_id}",
            *(
                f"urn:continucare:audit-event:{item.event_id}"
                for item in snapshot.audits
            ),
            _versioned(snapshot.response),
            _versioned(snapshot.task),
            _versioned(snapshot.communication),
            *(_versioned(item) for item in snapshot.observations),
            *(_versioned(item) for item in snapshot.provenances),
        }
        summary = Layer4SummaryDraft(
            summary_id=summary_id,
            version=version,
            patient_id=patient_id,
            pathway_code=self.pathway_code,
            pathway_version=self.pathway_version,
            summary_kind=BRIEF_SUMMARY_KIND,
            period_start=snapshot.period_start,
            period_end=snapshot.period_end,
            status=SummaryDraftStatus.SAFETY_REVIEWED,
            items=items,
            provenance_refs=[provenance_ref],
            generation_mode="deterministic",
            generator_version=BRIEF_GENERATOR_VERSION,
            source_fact_ids=[
                _stable_id("brief-source", item) for item in sorted(direct_refs)
            ],
            source_evidence_digest=snapshot.source_digest,
            created_at=generated_at,
        )
        summary_reference = f"urn:continucare:summary:{summary_id}:version:{version}"
        provenance = build_provenance(
            target_references=[summary_reference],
            recorded_at=generated_at,
            agent_reference=BRIEF_AGENT_REFERENCE,
            agent_role_code="assembler",
            agent_role_display="Deterministic assembler",
            provenance_id=provenance_id,
            activity_code="TRANSFORM",
            activity_display="deterministic manual-review doctor brief",
            entity_source_references=sorted(direct_refs),
        )
        audit = AuditEvent(
            event_id=f"audit_{_sha256({'summary': summary_reference, 'source': snapshot.source_digest})[:32]}",
            patient_id=patient_id,
            entity_type="Layer4SummaryDraft",
            entity_id=summary_id,
            event_type="manual_review_brief_generated",
            actor_type="deterministic_workflow",
            details_json={
                "summary_ref": summary_reference,
                "summary_version": version,
                "task_ref": _versioned(snapshot.task),
                "communication_ref": _versioned(snapshot.communication),
                "communication_readiness": snapshot.readiness,
                "source_evidence_digest": snapshot.source_digest,
                "clinical_assessment": "not_assessed",
            },
            created_at=generated_at,
        )
        try:
            created = self.repository.persist_manual_review_brief(
                patient_id=patient_id,
                expected_task=snapshot.task,
                expected_communication=snapshot.communication,
                expected_questionnaire_response=snapshot.response,
                expected_observations=snapshot.observations,
                expected_message=snapshot.message,
                expected_provenances=snapshot.provenances,
                expected_audits=snapshot.audits,
                expected_current_summary=current,
                summary=summary,
                summary_provenance=provenance,
                audit_event=audit,
            )
        except ValueError:
            replay = self.repository.get_contract("summary_draft", summary_id)
            if (
                replay is not None
                and cast(Layer4SummaryDraft, replay).source_evidence_digest
                == snapshot.source_digest
            ):
                return cast(Layer4SummaryDraft, replay)
            raise
        if not created:
            replay = self.repository.get_contract(
                "summary_draft", summary_id, version=version
            )
            if replay is not None:
                return cast(Layer4SummaryDraft, replay)
            raise RuntimeError("M5-C idempotent brief replay is incomplete")
        return summary

    def is_stale(self, summary: Layer4SummaryDraft, *, as_of: str) -> bool:
        task_refs = sorted(
            {
                item.resource.reference.split("/", 2)[1]
                for summary_item in summary.items
                for item in summary_item.evidence_refs
                if item.resource.reference.startswith("Task/")
            }
        )
        if len(task_refs) != 1:
            return True
        try:
            snapshot = self.inspect(
                patient_id=summary.patient_id,
                task_id=task_refs[0],
                as_of=as_of,
            )
        except ValueError:
            return True
        return snapshot.source_digest != summary.source_evidence_digest

    def _required_provenances(
        self,
        *,
        patient_id: str,
        task: dict[str, Any],
        communication: dict[str, Any],
        response: dict[str, Any],
        observations: list[dict[str, Any]],
        as_of: str,
    ) -> list[dict[str, Any]]:
        available = [
            item
            for item in self.repository.list_fhir_resources(
                patient_id=patient_id,
                resource_type="Provenance",
                current_only=False,
            )
            if _instant(item["recorded"]) <= _instant(as_of)
        ]
        creation_targets = {
            f"QuestionnaireResponse/{response['id']}",
            f"Task/{task['id']}",
            *(f"Observation/{item['id']}" for item in observations),
        }
        selected = [
            item
            for item in available
            if creation_targets.issubset(
                {target.get("reference") for target in item.get("target", [])}
            )
        ]
        if len(selected) != 1:
            raise ValueError("M5-C requires one patient-confirmation Provenance")

        task_history = [
            item
            for item in self.repository.list_fhir_resources(
                patient_id=patient_id, resource_type="Task", current_only=False
            )
            if item["id"] == task["id"]
            and int(item["meta"]["versionId"]) <= int(task["meta"]["versionId"])
        ]
        for version in task_history:
            if version["meta"]["versionId"] == "1":
                continue
            target = _versioned(version)
            matches = [
                item
                for item in available
                if target
                in {entry.get("reference") for entry in item.get("target", [])}
            ]
            if len(matches) != 1:
                raise ValueError(f"M5-C Task version lacks exact Provenance: {target}")
            selected.extend(matches)

        communication_history = [
            item
            for item in self.repository.list_fhir_resources(
                patient_id=patient_id,
                resource_type="Communication",
                current_only=False,
            )
            if item["id"] == communication["id"]
            and int(item["meta"]["versionId"])
            <= int(communication["meta"]["versionId"])
        ]
        for version in communication_history:
            target = _versioned(version)
            matches = [
                item
                for item in available
                if target
                in {entry.get("reference") for entry in item.get("target", [])}
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"M5-C Communication version lacks exact Provenance: {target}"
                )
            selected.extend(matches)
        return sorted(
            {(_versioned(item)): item for item in selected}.values(), key=_versioned
        )

    def _required_audits(
        self,
        *,
        patient_id: str,
        task: dict[str, Any],
        response: dict[str, Any],
        readiness: str,
        as_of: str,
    ) -> list[AuditEvent]:
        available = [
            item
            for item in self.store.list_audit_events(patient_id)
            if _instant(item.created_at) <= _instant(as_of)
        ]
        response_events = [
            item
            for item in available
            if item.event_type == "questionnaire_response_completed"
            and item.entity_type == "QuestionnaireResponse"
            and item.entity_id == response["id"]
        ]
        if len(response_events) != 1:
            raise ValueError("M5-C requires one QuestionnaireResponse completion audit")
        session_id = response_events[0].details_json.get("session_id")
        requirements = {
            "semantic_candidate_patient_decision": lambda item: (
                item.details_json.get("session_id") == session_id
                and item.details_json.get("decision") == "accepted_for_manual_review"
            ),
            "manual_review_task_created": lambda item: item.entity_id == task["id"],
            "manual_review_task_acknowledged": lambda item: item.entity_id == task["id"],
            "manual_review_task_started": lambda item: item.entity_id == task["id"],
            "manual_review_outcome_recorded": lambda item: item.entity_id == task["id"],
        }
        if readiness == READY_TO_SEND:
            requirements["manual_review_communication_approved"] = (
                lambda item: item.entity_id == task["id"]
            )
        selected = [response_events[0]]
        for event_type, predicate in requirements.items():
            matches = [
                item
                for item in available
                if item.event_type == event_type and predicate(item)
            ]
            if len(matches) != 1:
                raise ValueError(f"M5-C requires one {event_type} audit")
            selected.append(matches[0])
        return sorted(selected, key=lambda item: (item.created_at, item.event_id))

    @staticmethod
    def _summary_items(snapshot: ManualReviewBriefSnapshot) -> list[SummaryEvidenceItem]:
        response_ref = _versioned(snapshot.response)
        response_time = snapshot.response["authored"]
        quote_evidence = _evidence(
            response_ref,
            evidence_id=_stable_id("evidence", response_ref, "verbatim-quote"),
            effective_start=response_time,
            effective_end=response_time,
            evidence_text=snapshot.quote,
        )
        items = [
            SummaryEvidenceItem(
                item_id=_stable_id("brief-quote", response_ref, snapshot.quote),
                section="overview",
                text=snapshot.quote,
                evidence_refs=[quote_evidence],
            ),
            SummaryEvidenceItem(
                item_id=_stable_id("brief-response", response_ref),
                section="overview",
                text=(
                    f"{response_ref}：status=completed；authored={response_time}。"
                ),
                evidence_refs=[
                    _evidence(
                        response_ref,
                        evidence_id=_stable_id("evidence", response_ref, "completed"),
                        effective_start=response_time,
                        effective_end=response_time,
                    )
                ],
            ),
        ]
        for observation in sorted(snapshot.observations, key=lambda item: item["id"]):
            observation_ref = _versioned(observation)
            effective = _observation_time(observation)
            coding = observation.get("code", {}).get("coding", [])
            if not coding or not coding[0].get("system") or not coding[0].get("code"):
                raise ValueError("final Observation requires exact coding")
            first = coding[0]
            derived = f"QuestionnaireResponse/{snapshot.response['id']}"
            text = (
                f"{observation_ref}：status=final；effective={effective}；"
                f"coding={first['system']}|{first['code']}"
                f" ({first.get('display') or observation.get('code', {}).get('text') or first['code']})；"
                f"{_observation_value(observation)}；derivedFrom={derived}。"
            )
            items.append(
                SummaryEvidenceItem(
                    item_id=_stable_id("brief-observation", observation_ref, text),
                    section="key_changes",
                    text=text,
                    evidence_refs=[
                        _evidence(
                            observation_ref,
                            evidence_id=_stable_id("evidence", observation_ref, "final"),
                            effective_start=effective,
                            effective_end=effective,
                        ),
                        _evidence(
                            response_ref,
                            evidence_id=_stable_id(
                                "evidence", observation_ref, "derived-from", response_ref
                            ),
                            effective_start=response_time,
                            effective_end=response_time,
                            role=EvidenceRole.SUPPORTING,
                        ),
                    ],
                )
            )
        task_ref = _versioned(snapshot.task)
        task_time = snapshot.task["meta"]["lastUpdated"]
        items.append(
            SummaryEvidenceItem(
                item_id=_stable_id("brief-task", task_ref, snapshot.outcome),
                section="tasks_and_actions",
                text=(
                    f"{task_ref}：status=completed；受控处理结果={snapshot.outcome_label}；"
                    "临床评估=not_assessed。"
                ),
                evidence_refs=[
                    _evidence(
                        task_ref,
                        evidence_id=_stable_id("evidence", task_ref, "controlled-outcome"),
                        effective_start=task_time,
                        effective_end=task_time,
                    )
                ],
            )
        )
        communication_ref = _versioned(snapshot.communication)
        communication_time = snapshot.communication["meta"]["lastUpdated"]
        readiness_text = (
            "readiness=pending-approval；尚不可发送；未发送。"
            if snapshot.readiness == PENDING_APPROVAL
            else "readiness=ready-to-send；已人工批准；尚未发送。"
        )
        items.append(
            SummaryEvidenceItem(
                item_id=_stable_id(
                    "brief-communication", communication_ref, snapshot.readiness
                ),
                section="tasks_and_actions",
                text=f"{communication_ref}：status=preparation；{readiness_text}",
                evidence_refs=[
                    _evidence(
                        communication_ref,
                        evidence_id=_stable_id(
                            "evidence", communication_ref, "readiness"
                        ),
                        effective_start=communication_time,
                        effective_end=communication_time,
                    ),
                    _evidence(
                        task_ref,
                        evidence_id=_stable_id(
                            "evidence", communication_ref, "based-on", task_ref
                        ),
                        effective_start=task_time,
                        effective_end=task_time,
                        role=EvidenceRole.SUPPORTING,
                    ),
                ],
            )
        )
        return items

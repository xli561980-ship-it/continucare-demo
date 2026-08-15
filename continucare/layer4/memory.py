"""Deterministic Clinical Memory ingestion and Timeline projection."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, cast

from continucare.layer4.contracts import (
    ClinicalMemoryBuildResult,
    EvidenceReference,
    EvidenceRole,
    MemoryEvent,
    MemoryEventKind,
    MissingDataExpectation,
    ResourceReference,
    RevisionLink,
    RevisionRelationship,
    TimelineEvent,
    TimelineEventState,
)
from continucare.layer4.fhir import build_provenance
from continucare.layer4.inputs import Layer4InputReader
from continucare.layer4.repository import Layer4Repository
from continucare.models import AuditEvent


MEMORY_AGENT_REFERENCE = "Device/continucare-clinical-memory"


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Clinical Memory times must include a timezone offset")
    return parsed


def _resource_reference(resource: dict[str, Any]) -> ResourceReference:
    resource_type = resource.get("resourceType")
    resource_id = resource.get("id")
    if not resource_type or not resource_id:
        raise ValueError("Clinical Memory source requires resourceType and id")
    return ResourceReference(
        reference=f"{resource_type}/{resource_id}",
        version_id=resource.get("meta", {}).get("versionId") or "1",
    )


def _versioned_reference(reference: ResourceReference) -> str:
    if reference.reference.startswith("urn:"):
        return reference.reference
    if reference.version_id:
        return f"{reference.reference}/_history/{reference.version_id}"
    return reference.reference


def _event_revision_state(
    states: dict[str, TimelineEventState],
    *,
    source: ResourceReference,
    memory_event_id: str,
    conflict_evidence: Iterable[EvidenceReference] = (),
) -> TimelineEventState | None:
    direct = states.get(_versioned_reference(source)) or states.get(
        f"urn:continucare:memory-event:{memory_event_id}"
    )
    if direct is not None:
        return direct
    evidence_states = [
        states[_versioned_reference(item.resource)]
        for item in conflict_evidence
        if _versioned_reference(item.resource) in states
    ]
    if TimelineEventState.ENTERED_IN_ERROR in evidence_states:
        return TimelineEventState.ENTERED_IN_ERROR
    if evidence_states:
        return TimelineEventState.SUPERSEDED
    return None


def _resource_times(resource: dict[str, Any]) -> tuple[str, str, str]:
    resource_type = resource["resourceType"]
    if resource_type == "Observation":
        if "effectivePeriod" in resource:
            period = resource["effectivePeriod"]
            start = period.get("start") or period.get("end") or resource["issued"]
            end = period.get("end") or period.get("start") or resource["issued"]
        else:
            start = end = resource.get("effectiveDateTime") or resource["issued"]
        return start, end, resource["issued"]
    if resource_type == "QuestionnaireResponse":
        authored = resource["authored"]
        return authored, authored, authored
    if resource_type == "Communication":
        sent = (
            resource.get("sent")
            or resource.get("received")
            or resource["meta"]["lastUpdated"]
        )
        return sent, sent, resource["meta"]["lastUpdated"]
    if resource_type == "Task":
        period = resource.get("executionPeriod", {})
        authored = resource.get("authoredOn") or resource["meta"]["lastUpdated"]
        start = period.get("start") or authored
        end = period.get("end") or start
        return start, end, resource["meta"]["lastUpdated"]
    raise ValueError(f"unsupported Clinical Memory resource {resource_type!r}")


def _observation_code(resource: dict[str, Any]) -> tuple[str, str, str]:
    coding = resource.get("code", {}).get("coding", [])
    if not coding:
        raise ValueError("Observation requires a coded concept for Clinical Memory")
    first = coding[0]
    system = first.get("system")
    code = first.get("code")
    if not system or not code:
        raise ValueError("Observation coding requires system and code")
    display = (
        first.get("display")
        or resource.get("code", {}).get("text")
        or f"{system}|{code}"
    )
    return system, code, display


def _observation_value(resource: dict[str, Any]) -> tuple[str, str]:
    if "valueQuantity" in resource:
        value = resource["valueQuantity"]
        display = f"{value.get('value')} {value.get('unit') or value.get('code') or ''}".strip()
        return json.dumps(value, sort_keys=True, ensure_ascii=False), display
    if "valueCodeableConcept" in resource:
        concept = resource["valueCodeableConcept"]
        coding = concept.get("coding", [])
        display = (
            (coding[0].get("display") or coding[0].get("code"))
            if coding
            else concept.get("text")
        )
        return json.dumps(concept, sort_keys=True, ensure_ascii=False), str(display)
    for field in (
        "valueBoolean",
        "valueInteger",
        "valueDecimal",
        "valueString",
        "valueDateTime",
        "valueTime",
    ):
        if field in resource:
            value = resource[field]
            return json.dumps(value, ensure_ascii=False), str(value)
    return "<missing>", "未记录值"


def _event_text(resource: dict[str, Any]) -> tuple[str, str]:
    resource_type = resource["resourceType"]
    if resource_type == "QuestionnaireResponse":
        version = resource.get("questionnaire", "已发布问卷")
        return "随访问卷已完成", f"患者完成问卷 {version}。"
    if resource_type == "Observation":
        _, _, display = _observation_code(resource)
        _, value_display = _observation_value(resource)
        return display, f"患者确认记录：{display} = {value_display}。"
    if resource_type == "Communication":
        payload = resource.get("payload", [])
        text = "；".join(
            str(item.get("contentString"))
            for item in payload
            if item.get("contentString")
        )
        return "沟通记录", text or "已记录一次沟通。"
    if resource_type == "Task":
        code = resource.get("code", {})
        title = code.get("text") or "医护工作任务"
        description = resource.get("description")
        summary = f"任务状态：{resource['status']}。"
        if description:
            summary += f" {description}"
        notes = resource.get("note", [])
        if notes:
            latest = notes[-1]
            summary += f" 最新处理记录：{latest.get('text', '')}"
        return str(title)[:300], summary[:2000]
    raise ValueError(f"unsupported Clinical Memory resource {resource_type!r}")


def _event_kind(resource_type: str) -> MemoryEventKind:
    return {
        "QuestionnaireResponse": MemoryEventKind.QUESTIONNAIRE_RESPONSE,
        "Observation": MemoryEventKind.OBSERVATION,
        "Communication": MemoryEventKind.COMMUNICATION,
        "Task": MemoryEventKind.TASK,
    }[resource_type]


class ClinicalMemoryService:
    """Build re-playable memory from accepted resources without using an LLM."""

    def __init__(
        self,
        input_reader: Layer4InputReader,
        repository: Layer4Repository,
        *,
        pathway_code: str,
        pathway_version: str,
    ):
        self.input_reader = input_reader
        self.repository = repository
        self.pathway_code = pathway_code
        self.pathway_version = pathway_version

    def rebuild(
        self,
        patient_id: str,
        *,
        missing_expectations: Iterable[MissingDataExpectation] = (),
    ) -> ClinicalMemoryBuildResult:
        snapshot = self.input_reader.read(
            patient_id,
            pathway_code=self.pathway_code,
            pathway_version=self.pathway_version,
        )
        if (
            snapshot.pathway_code != self.pathway_code
            or snapshot.pathway_version != self.pathway_version
        ):
            raise ValueError("Layer-4 input snapshot Pathway does not match memory service")
        memory_ids: list[str] = []
        timeline_ids: list[str] = []
        provenance_ids: list[str] = []

        resources = [*snapshot.questionnaire_responses, *snapshot.observations]
        pathway_reference = (
            f"urn:continucare:pathway:{self.pathway_code}|{self.pathway_version}"
        )
        workflow_history = self.repository.list_fhir_resources(
            patient_id=patient_id, current_only=False
        )
        pathway_task_references = {
            f"Task/{item['id']}/_history/{item['meta']['versionId']}"
            for item in workflow_history
            if item["resourceType"] == "Task"
            and self._has_exact_pathway(item, pathway_reference)
        }
        for item in self.repository.list_fhir_resources(
            patient_id=patient_id, current_only=True
        ):
            if item["resourceType"] == "Task" and self._has_exact_pathway(
                item, pathway_reference
            ):
                resources.append(item)
            elif item["resourceType"] == "Communication" and (
                self._communication_has_exact_task(
                    item, pathway_task_references
                )
            ):
                resources.append(item)
        resources.sort(
            key=lambda item: (
                _instant(_resource_times(item)[0]),
                item["resourceType"],
                item["id"],
            )
        )
        for resource in resources:
            memory, timeline, provenance_id = self._persist_resource_event(
                patient_id, resource
            )
            memory_ids.append(memory.event_id)
            timeline_ids.append(timeline.timeline_event_id)
            provenance_ids.append(provenance_id)

        for audit in sorted(snapshot.audit_events, key=lambda item: item.created_at):
            if audit.patient_id != patient_id:
                continue
            memory, timeline, provenance_id = self._persist_audit_event(audit)
            memory_ids.append(memory.event_id)
            timeline_ids.append(timeline.timeline_event_id)
            provenance_ids.append(provenance_id)

        conflicts = self._persist_conflicts(patient_id, snapshot.observations)
        for memory, timeline, provenance_id in conflicts:
            memory_ids.append(memory.event_id)
            timeline_ids.append(timeline.timeline_event_id)
            provenance_ids.append(provenance_id)

        missing = self._persist_missing(
            patient_id,
            snapshot.observations,
            list(missing_expectations),
            as_of=snapshot.assembled_at,
        )
        for memory, timeline, provenance_id in missing:
            memory_ids.append(memory.event_id)
            timeline_ids.append(timeline.timeline_event_id)
            provenance_ids.append(provenance_id)

        return ClinicalMemoryBuildResult(
            patient_id=patient_id,
            memory_event_ids=sorted(set(memory_ids)),
            timeline_event_ids=sorted(set(timeline_ids)),
            provenance_ids=sorted(set(provenance_ids)),
            conflict_event_ids=sorted(item[0].event_id for item in conflicts),
            missing_event_ids=sorted(item[0].event_id for item in missing),
        )

    @staticmethod
    def _has_exact_pathway(resource: dict[str, Any], expected: str) -> bool:
        pathway_references = [
            reference
            for reference in (
                item.get("reference") for item in resource.get("basedOn", [])
            )
            if isinstance(reference, str)
            and reference.startswith("urn:continucare:pathway:")
        ]
        return pathway_references == [expected]

    @staticmethod
    def _communication_has_exact_task(
        resource: dict[str, Any], admitted_task_references: set[str]
    ) -> bool:
        task_references = [
            reference
            for reference in (
                item.get("reference") for item in resource.get("basedOn", [])
            )
            if isinstance(reference, str) and reference.startswith("Task/")
        ]
        return len(task_references) == 1 and task_references[0] in admitted_task_references

    def list_timeline(
        self,
        patient_id: str,
        *,
        include_audit: bool = False,
        include_history: bool = False,
    ) -> list[TimelineEvent]:
        records = self.repository.list_contracts(
            "timeline_event", patient_id=patient_id, current_only=True
        )
        timeline = [
            cast(TimelineEvent, item)
            for item in records
            if cast(TimelineEvent, item).pathway_code == self.pathway_code
            and cast(TimelineEvent, item).pathway_version == self.pathway_version
        ]
        revision_states = self._revision_states(patient_id)
        timeline = [self._timeline_with_revision_state(item, revision_states) for item in timeline]
        if not include_audit:
            timeline = [item for item in timeline if item.kind != MemoryEventKind.AUDIT]
        if not include_history:
            timeline = [
                item
                for item in timeline
                if item.state
                not in {
                    TimelineEventState.SUPERSEDED,
                    TimelineEventState.ENTERED_IN_ERROR,
                }
            ]
        return sorted(
            timeline,
            key=lambda item: (
                _instant(item.effective_start),
                _instant(item.recorded_at),
                item.timeline_event_id,
            ),
            reverse=True,
        )

    def list_memory(
        self, patient_id: str, *, include_history: bool = False
    ) -> list[MemoryEvent]:
        records = self.repository.list_contracts(
            "memory_event", patient_id=patient_id, current_only=True
        )
        revision_states = self._revision_states(patient_id)
        memory = [
            cast(MemoryEvent, item)
            for item in records
            if cast(MemoryEvent, item).pathway_code == self.pathway_code
            and cast(MemoryEvent, item).pathway_version == self.pathway_version
        ]
        memory = [
            item.model_copy(update={"current": False})
            if _event_revision_state(
                revision_states,
                source=item.source,
                memory_event_id=item.event_id,
                conflict_evidence=(
                    item.evidence_refs
                    if item.kind == MemoryEventKind.CONFLICT
                    else ()
                ),
            )
            else item
            for item in memory
        ]
        if not include_history:
            memory = [item for item in memory if item.current]
        return sorted(
            memory,
            key=lambda item: (
                _instant(item.effective_start),
                _instant(item.recorded_at),
                item.event_id,
            ),
            reverse=True,
        )

    @staticmethod
    def _timeline_with_revision_state(
        item: TimelineEvent,
        revision_states: dict[str, TimelineEventState],
    ) -> TimelineEvent:
        state = _event_revision_state(
            revision_states,
            source=item.source,
            memory_event_id=item.memory_event_id,
            conflict_evidence=(
                item.evidence_refs if item.kind == MemoryEventKind.CONFLICT else ()
            ),
        )
        if state is None:
            return item
        return item.model_copy(update={"state": state})

    def record_revision(
        self,
        *,
        patient_id: str,
        predecessor: ResourceReference,
        successor: ResourceReference,
        relationship: RevisionRelationship,
        reason: str,
        actor_reference: str,
        recorded_at: str,
    ) -> RevisionLink:
        link, provenance = self._build_revision_bundle(
            patient_id=patient_id,
            predecessor=predecessor,
            successor=successor,
            relationship=relationship,
            reason=reason,
            actor_reference=actor_reference,
            recorded_at=recorded_at,
        )
        self.repository.persist_revision_link_bundle(
            link=link,
            provenance=provenance,
        )
        return link

    @staticmethod
    def _build_revision_bundle(
        *,
        patient_id: str,
        predecessor: ResourceReference,
        successor: ResourceReference,
        relationship: RevisionRelationship,
        reason: str,
        actor_reference: str,
        recorded_at: str,
    ) -> tuple[RevisionLink, dict[str, Any]]:
        link_id = _stable_id(
            "revision",
            patient_id,
            _versioned_reference(predecessor),
            _versioned_reference(successor),
            relationship.value,
        )
        provenance_id = _stable_id("provenance", "revision", link_id)
        provenance = build_provenance(
            target_references=[_versioned_reference(successor)],
            recorded_at=recorded_at,
            agent_reference=actor_reference,
            agent_role_code="author",
            agent_role_display="Author",
            provenance_id=provenance_id,
            activity_code="UPDATE",
            activity_display=relationship.value,
            entity_source_references=[_versioned_reference(predecessor)],
        )
        link = RevisionLink(
            link_id=link_id,
            patient_id=patient_id,
            predecessor=predecessor,
            successor=successor,
            relationship=relationship,
            reason=reason,
            actor_reference=actor_reference,
            provenance_reference=f"Provenance/{provenance_id}",
            created_at=recorded_at,
        )
        return link, provenance

    def _revision_states(self, patient_id: str) -> dict[str, TimelineEventState]:
        scoped_memory = self.repository.list_contracts(
            "memory_event", patient_id=patient_id, current_only=True
        )
        scoped_sources = {
            _versioned_reference(cast(MemoryEvent, item).source)
            for item in scoped_memory
            if cast(MemoryEvent, item).pathway_code == self.pathway_code
            and cast(MemoryEvent, item).pathway_version == self.pathway_version
        }
        scoped_sources.update(
            f"urn:continucare:memory-event:{cast(MemoryEvent, item).event_id}"
            for item in scoped_memory
            if cast(MemoryEvent, item).pathway_code == self.pathway_code
            and cast(MemoryEvent, item).pathway_version == self.pathway_version
        )
        records = self.repository.list_contracts(
            "revision_link", patient_id=patient_id, current_only=True
        )
        states: dict[str, TimelineEventState] = {}
        for record in records:
            link = cast(RevisionLink, record)
            if _versioned_reference(link.predecessor) not in scoped_sources:
                continue
            state = (
                TimelineEventState.ENTERED_IN_ERROR
                if link.relationship
                in {
                    RevisionRelationship.RETRACTS,
                    RevisionRelationship.ENTERED_IN_ERROR,
                }
                else TimelineEventState.SUPERSEDED
            )
            states[_versioned_reference(link.predecessor)] = state
        return states

    def _persist_resource_event(
        self, patient_id: str, resource: dict[str, Any]
    ) -> tuple[MemoryEvent, TimelineEvent, str]:
        source = _resource_reference(resource)
        effective_start, effective_end, recorded_at = _resource_times(resource)
        title, summary = _event_text(resource)
        title = title[:300]
        summary = summary[:2000]
        evidence = [
            EvidenceReference(
                evidence_id=_stable_id(
                    "evidence", _versioned_reference(source), "source"
                ),
                resource=source,
                role=EvidenceRole.SOURCE,
                effective_start=effective_start,
                effective_end=effective_end,
            )
        ]
        for derived in resource.get("derivedFrom", []):
            reference = derived.get("reference")
            if reference:
                evidence.append(
                    EvidenceReference(
                        evidence_id=_stable_id("evidence", reference, "derived-from"),
                        resource=ResourceReference(reference=reference),
                        role=EvidenceRole.SUPPORTING,
                        effective_start=effective_start,
                        effective_end=effective_end,
                    )
                )
        reason = resource.get("reasonReference", {}).get("reference")
        if reason:
            evidence.append(
                EvidenceReference(
                    evidence_id=_stable_id("evidence", reason, "trigger"),
                    resource=ResourceReference(reference=reason),
                    role=EvidenceRole.TRIGGER,
                    effective_start=effective_start,
                    effective_end=effective_end,
                )
            )
        entered_in_error = resource.get("status") == "entered-in-error"
        revision_bundles = (
            self._workflow_revision_bundles(
                patient_id=patient_id,
                successor=source,
                recorded_at=recorded_at,
            )
            if resource["resourceType"] in {"Communication", "Task"}
            else []
        )
        return self._persist_event(
            patient_id=patient_id,
            kind=_event_kind(resource["resourceType"]),
            source=source,
            source_status=resource.get("status", "recorded"),
            effective_start=effective_start,
            effective_end=effective_end,
            recorded_at=recorded_at,
            title=title,
            summary=summary,
            evidence=evidence,
            state=(
                TimelineEventState.ENTERED_IN_ERROR
                if entered_in_error
                else TimelineEventState.CURRENT
            ),
            current=not entered_in_error,
            revision_bundles=revision_bundles,
        )

    def _persist_audit_event(
        self, audit: AuditEvent
    ) -> tuple[MemoryEvent, TimelineEvent, str]:
        source = ResourceReference(reference=f"AuditEvent/{audit.event_id}", version_id="1")
        evidence = [
            EvidenceReference(
                evidence_id=_stable_id("evidence", source.reference, "audit"),
                resource=source,
                role=EvidenceRole.REVIEW,
                effective_start=audit.created_at,
                effective_end=audit.created_at,
            )
        ]
        return self._persist_event(
            patient_id=cast(str, audit.patient_id),
            kind=MemoryEventKind.AUDIT,
            source=source,
            source_status="recorded",
            effective_start=audit.created_at,
            effective_end=audit.created_at,
            recorded_at=audit.created_at,
            title=f"审计事件：{audit.event_type}",
            summary=f"{audit.actor_type} 记录了 {audit.event_type}。",
            evidence=evidence,
        )

    def _persist_event(
        self,
        *,
        patient_id: str,
        kind: MemoryEventKind,
        source: ResourceReference,
        source_status: str,
        effective_start: str,
        effective_end: str,
        recorded_at: str,
        title: str,
        summary: str,
        evidence: list[EvidenceReference],
        state: TimelineEventState = TimelineEventState.CURRENT,
        current: bool = True,
        conflict_group_id: str | None = None,
        expectation_id: str | None = None,
        revision_bundles: list[tuple[RevisionLink, dict[str, Any]]] | None = None,
    ) -> tuple[MemoryEvent, TimelineEvent, str]:
        source_version = _versioned_reference(source)
        deduplication_key = "|".join(
            (
                patient_id,
                self.pathway_code,
                self.pathway_version,
                kind.value,
                source_version,
                conflict_group_id or "",
                expectation_id or "",
            )
        )
        event_id = _stable_id("memory", deduplication_key)
        timeline_id = _stable_id("timeline", event_id)
        provenance_id = _stable_id("provenance", event_id)
        provenance = build_provenance(
            target_references=[
                f"urn:continucare:memory-event:{event_id}",
                f"urn:continucare:timeline-event:{timeline_id}",
            ],
            recorded_at=recorded_at,
            agent_reference=MEMORY_AGENT_REFERENCE,
            agent_role_code="assembler",
            agent_role_display="Assembler",
            provenance_id=provenance_id,
            activity_code="TRANSFORM",
            activity_display="deterministic projection",
            entity_source_references=[source_version],
        )
        provenance_reference = ResourceReference(
            reference=f"Provenance/{provenance_id}", version_id="1"
        )
        memory = MemoryEvent(
            event_id=event_id,
            patient_id=patient_id,
            pathway_code=self.pathway_code,
            pathway_version=self.pathway_version,
            kind=kind,
            source=source,
            source_status=source_status,
            effective_start=effective_start,
            effective_end=effective_end,
            recorded_at=recorded_at,
            deduplication_key=deduplication_key,
            evidence_refs=evidence,
            provenance_refs=[provenance_reference],
            current=current,
            conflict_group_id=conflict_group_id,
            expectation_id=expectation_id,
        )
        timeline = TimelineEvent(
            timeline_event_id=timeline_id,
            patient_id=patient_id,
            memory_event_id=event_id,
            pathway_code=self.pathway_code,
            pathway_version=self.pathway_version,
            kind=kind,
            title=title,
            summary=summary,
            effective_start=effective_start,
            effective_end=effective_end,
            recorded_at=recorded_at,
            state=state,
            source=source,
            evidence_refs=evidence,
            conflict_group_id=conflict_group_id,
            expectation_id=expectation_id,
        )
        self.repository.persist_memory_projection_bundle(
            memory=memory,
            timeline=timeline,
            provenance=provenance,
            revision_bundles=revision_bundles,
        )
        return memory, timeline, provenance_id

    def _workflow_revision_bundles(
        self,
        *,
        patient_id: str,
        successor: ResourceReference,
        recorded_at: str,
    ) -> list[tuple[RevisionLink, dict[str, Any]]]:
        records = self.repository.list_contracts(
            "memory_event", patient_id=patient_id, current_only=True
        )
        prior = sorted(
            (
                cast(MemoryEvent, record)
                for record in records
                if cast(MemoryEvent, record).pathway_code == self.pathway_code
                and cast(MemoryEvent, record).pathway_version == self.pathway_version
                and cast(MemoryEvent, record).source.reference == successor.reference
                and cast(MemoryEvent, record).source.version_id != successor.version_id
            ),
            key=lambda item: _versioned_reference(item.source),
        )
        return [
            self._build_revision_bundle(
                patient_id=patient_id,
                predecessor=event.source,
                successor=successor,
                relationship=RevisionRelationship.SUPERSEDES,
                reason="Current FHIR workflow resource version supersedes prior version.",
                actor_reference=MEMORY_AGENT_REFERENCE,
                recorded_at=recorded_at,
            )
            for event in prior
        ]

    def _persist_conflicts(
        self, patient_id: str, observations: list[dict[str, Any]]
    ) -> list[tuple[MemoryEvent, TimelineEvent, str]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for resource in observations:
            system, code, _ = _observation_code(resource)
            grouped[(system, code)].append(resource)

        results: list[tuple[MemoryEvent, TimelineEvent, str]] = []
        for resources in grouped.values():
            ordered = sorted(resources, key=lambda item: _instant(_resource_times(item)[0]))
            components: list[list[dict[str, Any]]] = []
            for resource in ordered:
                start, end, _ = _resource_times(resource)
                if not components:
                    components.append([resource])
                    continue
                component_end = max(
                    _instant(_resource_times(item)[1]) for item in components[-1]
                )
                if _instant(start) <= component_end:
                    components[-1].append(resource)
                else:
                    components.append([resource])
            for component in components:
                value_keys = {_observation_value(item)[0] for item in component}
                if len(component) < 2 or len(value_keys) < 2:
                    continue
                refs = sorted(
                    (_resource_reference(item) for item in component),
                    key=_versioned_reference,
                )
                conflict_group_id = _stable_id(
                    "conflict",
                    *(_versioned_reference(item) for item in refs),
                )
                starts, ends, recorded = zip(
                    *(_resource_times(item) for item in component), strict=True
                )
                _, _, display = _observation_code(component[0])
                evidence = [
                    EvidenceReference(
                        evidence_id=_stable_id(
                            "evidence", _versioned_reference(item), "conflict"
                        ),
                        resource=item,
                        role=EvidenceRole.CONTRADICTING,
                        effective_start=min(starts, key=_instant),
                        effective_end=max(ends, key=_instant),
                    )
                    for item in refs
                ]
                results.append(
                    self._persist_event(
                        patient_id=patient_id,
                        kind=MemoryEventKind.CONFLICT,
                        source=refs[0],
                        source_status="conflict",
                        effective_start=min(starts, key=_instant),
                        effective_end=max(ends, key=_instant),
                        recorded_at=max(recorded, key=_instant),
                        title=f"{display}记录存在冲突",
                        summary=(
                            f"同一临床时间窗内存在不一致的{display}记录；"
                            "系统未选择其中任何一个值，等待人工确认。"
                        ),
                        evidence=evidence,
                        state=TimelineEventState.CONFLICT,
                        conflict_group_id=conflict_group_id,
                    )
                )
        return results

    def _persist_missing(
        self,
        patient_id: str,
        observations: list[dict[str, Any]],
        expectations: list[MissingDataExpectation],
        *,
        as_of: str,
    ) -> list[tuple[MemoryEvent, TimelineEvent, str]]:
        results: list[tuple[MemoryEvent, TimelineEvent, str]] = []
        for expectation in sorted(expectations, key=lambda item: item.expectation_id):
            if expectation.patient_id != patient_id:
                raise ValueError("missing-data expectation patient does not match rebuild")
            if (
                expectation.pathway_code != self.pathway_code
                or expectation.pathway_version != self.pathway_version
            ):
                raise ValueError("missing-data expectation pathway does not match service")
            if _instant(expectation.period_end) > _instant(as_of):
                continue
            matched_resources: list[dict[str, Any]] = []
            for resource in observations:
                system, code, _ = _observation_code(resource)
                if (system, code) != (expectation.code_system, expectation.code):
                    continue
                start, end, _ = _resource_times(resource)
                if (
                    _instant(start) <= _instant(expectation.period_end)
                    and _instant(end) >= _instant(expectation.period_start)
                ):
                    matched_resources.append(resource)
            if matched_resources:
                matched_resource = min(
                    matched_resources,
                    key=lambda item: (
                        _instant(_resource_times(item)[0]),
                        _versioned_reference(_resource_reference(item)),
                    ),
                )
                self._supersede_satisfied_missing_event(
                    patient_id=patient_id,
                    expectation=expectation,
                    matched_resource=matched_resource,
                )
                continue
            source = expectation.pathway_reference
            evidence = [
                EvidenceReference(
                    evidence_id=_stable_id(
                        "evidence", expectation.expectation_id, "missing"
                    ),
                    resource=source,
                    role=EvidenceRole.SOURCE,
                    effective_start=expectation.period_start,
                    effective_end=expectation.period_end,
                )
            ]
            results.append(
                self._persist_event(
                    patient_id=patient_id,
                    kind=MemoryEventKind.MISSING_DATA,
                    source=source,
                    source_status="missing",
                    effective_start=expectation.period_start,
                    effective_end=expectation.period_end,
                    recorded_at=expectation.period_end,
                    title=f"缺少{expectation.display}记录",
                    summary=(
                        f"在指定时间窗内未找到已确认的{expectation.display}记录；"
                        "这只表示数据缺失，不代表患者状态。"
                    ),
                    evidence=evidence,
                    expectation_id=expectation.expectation_id,
                )
            )
        return results

    def _supersede_satisfied_missing_event(
        self,
        *,
        patient_id: str,
        expectation: MissingDataExpectation,
        matched_resource: dict[str, Any],
    ) -> None:
        records = self.repository.list_contracts(
            "memory_event", patient_id=patient_id, current_only=True
        )
        successor = _resource_reference(matched_resource)
        _, _, recorded_at = _resource_times(matched_resource)
        for record in records:
            event = cast(MemoryEvent, record)
            if (
                event.kind != MemoryEventKind.MISSING_DATA
                or event.expectation_id != expectation.expectation_id
            ):
                continue
            self.record_revision(
                patient_id=patient_id,
                predecessor=ResourceReference(
                    reference=f"urn:continucare:memory-event:{event.event_id}",
                    version_id="1",
                ),
                successor=successor,
                relationship=RevisionRelationship.SUPERSEDES,
                reason="Expected observation arrived; missing-data marker superseded.",
                actor_reference=MEMORY_AGENT_REFERENCE,
                recorded_at=recorded_at,
            )

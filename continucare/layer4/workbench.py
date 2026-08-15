"""Read-only Doctor Workbench composition and evidence replay."""

from __future__ import annotations

import hashlib
from collections import deque
from datetime import datetime
from typing import Any, cast

from continucare.db import utc_now_iso
from continucare.layer4.contracts import (
    ClinicalRuleDefinition,
    ClinicalStateSnapshot,
    ComponentReadStatus,
    DoctorWorkbenchView,
    EvidenceArtifactType,
    EvidenceTrace,
    EvidenceTraceArtifact,
    EvidenceTraceEdge,
    Layer4SummaryDraft,
    MemoryEventKind,
    ResourceReference,
    RevisionLink,
    RevisionRelationship,
    RuleLifecycle,
    TimelineEvent,
    TimelineEventState,
    WorkbenchAccessContext,
    WorkbenchComponent,
    WorkbenchComponentResult,
    WorkbenchRole,
)
from continucare.layer4.inputs import Layer4InputReader
from continucare.layer4.repository import Layer4Repository
from continucare.layer4.manual_reviews import is_clinical_rule_task
from continucare.layer4.rules import RULE_ENGINE_REFERENCE
from continucare.layer4.tasks import is_task_transition_allowed
from continucare.pathways import PathwayNotFoundError, load_builtin_pathways


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("workbench times must include a timezone offset")
    return parsed


def _versioned_reference(reference: ResourceReference) -> str:
    if reference.reference.startswith("urn:"):
        return reference.reference
    if reference.version_id:
        return f"{reference.reference}/_history/{reference.version_id}"
    return reference.reference


def _numeric_version(value: str) -> tuple[int, str]:
    try:
        return int(value), value
    except ValueError:
        return -1, value


def _references(resource: dict[str, Any], field: str) -> list[str]:
    return [
        reference
        for reference in (
            item.get("reference") for item in resource.get(field, [])
        )
        if isinstance(reference, str)
    ]


def _task_evidence_references(task: dict[str, Any]) -> list[str]:
    references: list[str] = []
    for item in task.get("input", []):
        codings = item.get("type", {}).get("coding", [])
        if not any(
            coding.get("system") == "urn:continucare:task-input"
            and coding.get("code") == "rule-evidence"
            for coding in codings
        ):
            continue
        reference = item.get("valueReference", {}).get("reference")
        if isinstance(reference, str):
            references.append(reference)
    return references


def _provenance_activity(resource: dict[str, Any]) -> str | None:
    codings = resource.get("activity", {}).get("coding", [])
    values = [item.get("code") for item in codings if item.get("code")]
    return values[0] if len(values) == 1 else None


def _provenance_agent(resource: dict[str, Any]) -> str | None:
    agents = [
        item.get("who", {}).get("reference") for item in resource.get("agent", [])
    ]
    values = [item for item in agents if isinstance(item, str) and item]
    return values[0] if len(values) == 1 else None


def admit_clinical_rule_task(
    task: dict[str, Any],
    *,
    patient_id: str,
    pathway_code: str,
    pathway_version: str,
    cutoff: datetime,
    repository: Layer4Repository,
    admitted_observation_refs: set[str],
) -> dict[str, Any]:
    """Admit only a complete, version-continuous approved-rule Task chain."""

    if task.get("resourceType") != "Task" or not is_clinical_rule_task(task):
        raise ValueError("Task is not a clinical-rule Task")
    if task.get("status") == "entered-in-error":
        raise ValueError("entered-in-error Task is not admitted")
    if task.get("for", {}).get("reference") != f"Patient/{patient_id}":
        raise ValueError("Task patient does not match Workbench patient")

    identifiers = [
        item.get("value")
        for item in task.get("identifier", [])
        if item.get("system") == "urn:continucare:clinical-rule"
    ]
    if len(identifiers) != 1 or not isinstance(identifiers[0], str):
        raise ValueError("Task requires one clinical-rule identifier")
    try:
        rule_id, rule_version = identifiers[0].rsplit("|", 1)
    except ValueError as exc:
        raise ValueError("Task clinical-rule identifier is not parseable") from exc
    if not rule_id or not rule_version:
        raise ValueError("Task clinical-rule identifier is incomplete")

    rule_reference = f"urn:continucare:clinical-rule:{rule_id}|{rule_version}"
    pathway_reference = f"urn:continucare:pathway:{pathway_code}|{pathway_version}"
    based_on = _references(task, "basedOn")
    rule_references = [
        item for item in based_on if item.startswith("urn:continucare:clinical-rule:")
    ]
    pathway_references = [
        item for item in based_on if item.startswith("urn:continucare:pathway:")
    ]
    if rule_references != [rule_reference] or pathway_references != [pathway_reference]:
        raise ValueError("Task basedOn does not prove one exact rule and Pathway")

    current_rule = repository.get_contract("clinical_rule", rule_id)
    if current_rule is None:
        raise ValueError("Task ClinicalRule does not exist")
    rule = cast(ClinicalRuleDefinition, current_rule)
    if rule.version != rule_version:
        raise ValueError("Task ClinicalRule version is not current")
    if rule.lifecycle != RuleLifecycle.ACTIVE or not rule.approval.fully_approved():
        raise ValueError("Task ClinicalRule is not active and dual-approved")
    if (
        rule.applicability.pathway_code != pathway_code
        or rule.applicability.pathway_version != pathway_version
    ):
        raise ValueError("Task ClinicalRule Pathway does not match Workbench")

    version_value = task.get("meta", {}).get("versionId")
    try:
        selected_version = int(version_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Task requires a positive numeric version") from exc
    if selected_version < 1:
        raise ValueError("Task requires a positive numeric version")

    versions: list[dict[str, Any]] = []
    for number in range(1, selected_version + 1):
        item = repository.get_fhir_resource(
            "Task", task["id"], version_id=str(number)
        )
        if item is None:
            raise ValueError("Task version chain is not continuous")
        if item.get("for", {}).get("reference") != f"Patient/{patient_id}":
            raise ValueError("Task version chain changes patient")
        if _instant(item["meta"]["lastUpdated"]) > cutoff:
            raise ValueError("Task version is newer than Workbench cutoff")
        versions.append(item)
    if versions[-1] != task:
        raise ValueError("Task payload does not match its stored exact version")

    created = versions[0]
    created_at = _instant(created["meta"]["lastUpdated"])
    if _instant(created.get("authoredOn") or created["meta"]["lastUpdated"]) != created_at:
        raise ValueError("Task v1 authoredOn does not match creation version")
    if _instant(rule.created_at) > created_at:
        raise ValueError("Task predates its ClinicalRule")
    approval_times = [
        rule.approval.clinical_approved_at,
        rule.approval.terminology_approved_at,
    ]
    if any(value is None or _instant(value) > created_at for value in approval_times):
        raise ValueError("Task predates required ClinicalRule approval")

    created_evidence = _task_evidence_references(created)
    if (
        not created_evidence
        or len(created_evidence) != len(set(created_evidence))
        or not set(created_evidence).issubset(admitted_observation_refs)
    ):
        raise ValueError("Task evidence is not Pathway-admitted Observation evidence")
    if created.get("reasonReference", {}).get("reference") not in created_evidence:
        raise ValueError("Task trigger is not one of its admitted evidence references")
    for item in versions[1:]:
        if (
            item.get("identifier") != created.get("identifier")
            or item.get("basedOn") != created.get("basedOn")
            or item.get("input") != created.get("input")
            or item.get("reasonReference") != created.get("reasonReference")
        ):
            raise ValueError("Task version chain changes governed rule evidence")

    provenances = [
        item
        for item in repository.list_fhir_resources(
            patient_id=patient_id,
            resource_type="Provenance",
            current_only=False,
        )
        if _instant(item["recorded"]) <= cutoff
    ]
    creation_target = f"Task/{task['id']}/_history/1"
    creation_matches = [
        item
        for item in provenances
        if creation_target in _references(item, "target")
        and _provenance_activity(item) == "EXECUTE"
        and _provenance_agent(item) == RULE_ENGINE_REFERENCE
    ]
    if len(creation_matches) != 1:
        raise ValueError("Task v1 requires one RuleEngine EXECUTE Provenance")
    creation_provenance = creation_matches[0]
    if _instant(creation_provenance["recorded"]) != created_at:
        raise ValueError("Task v1 Provenance time does not match creation")
    creation_entities = {
        item.get("what", {}).get("reference")
        for item in creation_provenance.get("entity", [])
        if item.get("what", {}).get("reference")
    }
    if creation_entities != {rule_reference, *created_evidence}:
        raise ValueError("Task v1 Provenance rule/evidence entities do not match Task")

    for number, (previous, current) in enumerate(
        zip(versions, versions[1:]), start=2
    ):
        if not is_task_transition_allowed(previous["status"], current["status"]):
            raise ValueError("Task version chain contains an illegal status transition")
        if _instant(current["meta"]["lastUpdated"]) <= _instant(
            previous["meta"]["lastUpdated"]
        ):
            raise ValueError("Task version times are not strictly increasing")
        target = f"Task/{task['id']}/_history/{number}"
        predecessor = f"Task/{task['id']}/_history/{number - 1}"
        notes = current.get("note", [])
        transition_actor = (
            notes[-1].get("authorReference", {}).get("reference") if notes else None
        )
        matches = [
            item
            for item in provenances
            if _references(item, "target") == [target]
            and _provenance_activity(item) == "UPDATE"
            and transition_actor
            and _provenance_agent(item) == transition_actor
            and {
                entity.get("what", {}).get("reference")
                for entity in item.get("entity", [])
            }
            == {predecessor}
        ]
        if len(matches) != 1:
            raise ValueError("Task transition Provenance chain is incomplete")
        if _instant(matches[0]["recorded"]) != _instant(
            current["meta"]["lastUpdated"]
        ):
            raise ValueError("Task transition Provenance time does not match version")
    return task


class DoctorWorkbenchService:
    """Compose existing immutable records; never generates or mutates clinical data."""

    _ALLOWED_ROLES = {WorkbenchRole.DOCTOR, WorkbenchRole.CLINICAL_AUDITOR}

    def __init__(
        self,
        input_reader: Layer4InputReader,
        repository: Layer4Repository,
        *,
        pathway_code: str,
        pathway_version: str,
        summary_kind: str | None = None,
    ):
        self.input_reader = input_reader
        self.repository = repository
        self.pathway_code = pathway_code
        self.pathway_version = pathway_version
        if summary_kind not in {None, "timeline_evidence", "manual_review_brief"}:
            raise ValueError("unsupported workbench summary kind")
        self.summary_kind = summary_kind

    def query(
        self,
        *,
        patient_id: str,
        access: WorkbenchAccessContext,
        as_of: str,
        generated_at: str | None = None,
    ) -> DoctorWorkbenchView:
        self._authorize(patient_id, access)
        cutoff = _instant(as_of)
        generated = generated_at or utc_now_iso()
        if _instant(generated) < cutoff:
            raise ValueError("workbench generated_at cannot precede as_of")

        timeline, timeline_result = self._read_component(
            WorkbenchComponent.TIMELINE,
            lambda: self._timeline_as_of(patient_id, cutoff),
            empty_value=[],
        )
        state, state_result = self._read_component(
            WorkbenchComponent.STATE,
            lambda: self._state_as_of(patient_id, cutoff),
            empty_value=None,
        )
        summary, summary_result = self._read_component(
            WorkbenchComponent.SUMMARY,
            lambda: self._summary_as_of(patient_id, cutoff),
            empty_value=None,
        )
        tasks, task_result = self._read_component(
            WorkbenchComponent.TASKS,
            lambda: self._tasks_as_of(patient_id, cutoff),
            empty_value=[],
        )
        component_results = [
            timeline_result,
            state_result,
            summary_result,
            task_result,
        ]
        evidence_roots = self._evidence_roots(
            timeline=cast(list[TimelineEvent], timeline),
            state=cast(ClinicalStateSnapshot | None, state),
            summary=cast(Layer4SummaryDraft | None, summary),
            tasks=cast(list[dict[str, Any]], tasks),
        )
        view_id = _stable_id(
            "workbench-view",
            patient_id,
            self.pathway_code,
            self.pathway_version,
            as_of,
            *evidence_roots,
            *(f"{item.component.value}:{item.status.value}" for item in component_results),
        )
        return DoctorWorkbenchView(
            view_id=view_id,
            patient_id=patient_id,
            pathway_code=self.pathway_code,
            pathway_version=self.pathway_version,
            as_of=as_of,
            generated_at=generated,
            actor_reference=access.actor_reference,
            role=access.role,
            timeline=cast(list[TimelineEvent], timeline),
            state_snapshot=cast(ClinicalStateSnapshot | None, state),
            summary=cast(Layer4SummaryDraft | None, summary),
            tasks=cast(list[dict[str, Any]], tasks),
            evidence_roots=evidence_roots,
            components=component_results,
            degraded=any(
                item.status == ComponentReadStatus.DEGRADED
                for item in component_results
            ),
        )

    def trace_evidence(
        self,
        *,
        patient_id: str,
        access: WorkbenchAccessContext,
        root_reference: str,
        as_of: str,
        max_depth: int = 5,
        max_nodes: int = 100,
    ) -> EvidenceTrace:
        self._authorize(patient_id, access)
        cutoff = _instant(as_of)
        root = root_reference.strip()
        if not root:
            raise ValueError("evidence trace requires a root reference")
        if not 1 <= max_depth <= 10:
            raise ValueError("evidence max_depth must be between 1 and 10")
        if not 1 <= max_nodes <= 500:
            raise ValueError("evidence max_nodes must be between 1 and 500")

        queue: deque[tuple[str, int]] = deque([(root, 0)])
        seen: set[str] = set()
        artifacts: list[EvidenceTraceArtifact] = []
        edges: list[EvidenceTraceEdge] = []
        unresolved: set[str] = set()
        degraded = False
        truncated = False
        while queue:
            reference, depth = queue.popleft()
            if reference in seen:
                continue
            if len(seen) >= max_nodes:
                truncated = True
                break
            seen.add(reference)
            try:
                artifact = self._resolve_artifact(
                    patient_id=patient_id,
                    reference=reference,
                    cutoff=cutoff,
                )
            except PermissionError:
                raise
            except Exception:
                degraded = True
                unresolved.add(reference)
                continue
            if artifact is None:
                unresolved.add(reference)
                continue
            artifacts.append(artifact)
            relations, relation_degraded = self._artifact_relations(
                artifact, patient_id=patient_id, cutoff=cutoff
            )
            degraded = degraded or relation_degraded
            if depth >= max_depth:
                if relations:
                    truncated = True
                continue
            for relation, target in relations:
                edges.append(
                    EvidenceTraceEdge(
                        source_reference=reference,
                        target_reference=target,
                        relation=relation,
                    )
                )
                if target not in seen:
                    queue.append((target, depth + 1))

        trace_id = _stable_id(
            "evidence-trace",
            patient_id,
            root,
            as_of,
            *(sorted(item.reference for item in artifacts)),
            *(sorted(unresolved)),
        )
        return EvidenceTrace(
            trace_id=trace_id,
            patient_id=patient_id,
            root_reference=root,
            as_of=as_of,
            actor_reference=access.actor_reference,
            artifacts=artifacts,
            edges=edges,
            unresolved_references=sorted(unresolved),
            degraded=degraded,
            reason_codes=["evidence_source_unavailable"] if degraded else [],
            truncated=truncated,
        )

    @staticmethod
    def _authorize(patient_id: str, access: WorkbenchAccessContext) -> None:
        if not access.identity_verified:
            raise PermissionError("workbench requires a verified identity assertion")
        if access.role not in DoctorWorkbenchService._ALLOWED_ROLES:
            raise PermissionError("role is not permitted to access Doctor Workbench")
        if patient_id not in access.permitted_patient_ids:
            raise PermissionError("actor is not permitted to access this patient")

    @staticmethod
    def _read_component(component, loader, *, empty_value):
        try:
            value = loader()
        except Exception:
            return empty_value, WorkbenchComponentResult(
                component=component,
                status=ComponentReadStatus.DEGRADED,
                reason_code=f"{component.value}_source_unavailable",
                message=(
                    f"{component.value} 暂时不可用；其他只读组件仍可查看，"
                    "系统没有补造数据。"
                ),
            )
        return value, WorkbenchComponentResult(
            component=component,
            status=(
                ComponentReadStatus.AVAILABLE
                if value is not None and value != []
                else ComponentReadStatus.EMPTY
            ),
        )

    def _timeline_as_of(
        self, patient_id: str, cutoff: datetime
    ) -> list[TimelineEvent]:
        records = self.repository.list_contracts(
            "timeline_event", patient_id=patient_id, current_only=True
        )
        timeline = [
            cast(TimelineEvent, item)
            for item in records
            if cast(TimelineEvent, item).pathway_code == self.pathway_code
            and cast(TimelineEvent, item).pathway_version == self.pathway_version
            and cast(TimelineEvent, item).kind != MemoryEventKind.AUDIT
            and _instant(cast(TimelineEvent, item).recorded_at) <= cutoff
            and _instant(cast(TimelineEvent, item).effective_start) <= cutoff
        ]
        revision_records = self.repository.list_contracts(
            "revision_link", patient_id=patient_id, current_only=True
        )
        revision_states: dict[str, TimelineEventState] = {}
        for record in revision_records:
            link = cast(RevisionLink, record)
            if _instant(link.created_at) > cutoff:
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
            revision_states[_versioned_reference(link.predecessor)] = state

        visible: list[TimelineEvent] = []
        for event in timeline:
            state = revision_states.get(_versioned_reference(event.source))
            if state is None and event.kind == MemoryEventKind.CONFLICT:
                evidence_states = [
                    revision_states[_versioned_reference(item.resource)]
                    for item in event.evidence_refs
                    if _versioned_reference(item.resource) in revision_states
                ]
                if TimelineEventState.ENTERED_IN_ERROR in evidence_states:
                    state = TimelineEventState.ENTERED_IN_ERROR
                elif evidence_states:
                    state = TimelineEventState.SUPERSEDED
            projected = event.model_copy(update={"state": state}) if state else event
            if projected.state in {
                TimelineEventState.SUPERSEDED,
                TimelineEventState.ENTERED_IN_ERROR,
            }:
                continue
            visible.append(projected)
        return sorted(
            visible,
            key=lambda item: (
                _instant(item.effective_start),
                _instant(item.recorded_at),
                item.timeline_event_id,
            ),
            reverse=True,
        )

    def _state_as_of(
        self, patient_id: str, cutoff: datetime
    ) -> ClinicalStateSnapshot | None:
        records = self.repository.list_contracts(
            "state_snapshot",
            patient_id=patient_id,
            pathway_code=self.pathway_code,
            current_only=False,
        )
        candidates = [
            cast(ClinicalStateSnapshot, item)
            for item in records
            if cast(ClinicalStateSnapshot, item).pathway_version
            == self.pathway_version
            and _instant(cast(ClinicalStateSnapshot, item).as_of) <= cutoff
            and _instant(cast(ClinicalStateSnapshot, item).created_at) <= cutoff
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                _instant(item.as_of),
                _instant(item.created_at),
                _numeric_version(item.version),
            ),
        )

    def _summary_as_of(
        self, patient_id: str, cutoff: datetime
    ) -> Layer4SummaryDraft | None:
        records = self.repository.list_contracts(
            "summary_draft", patient_id=patient_id, current_only=False
        )
        candidates = [
            cast(Layer4SummaryDraft, item)
            for item in records
            if cast(Layer4SummaryDraft, item).pathway_code == self.pathway_code
            and cast(Layer4SummaryDraft, item).pathway_version
            == self.pathway_version
            and (
                self.summary_kind is None
                or cast(Layer4SummaryDraft, item).summary_kind == self.summary_kind
            )
            and (
                (
                    cast(Layer4SummaryDraft, item).summary_kind
                    == "timeline_evidence"
                    and cast(Layer4SummaryDraft, item).summary_id.startswith(
                        "summary-v2-"
                    )
                )
                or (
                    cast(Layer4SummaryDraft, item).summary_kind
                    == "manual_review_brief"
                    and cast(Layer4SummaryDraft, item).summary_id.startswith(
                        "summary-manual-review-"
                    )
                )
            )
            and _instant(cast(Layer4SummaryDraft, item).period_end) <= cutoff
            and _instant(cast(Layer4SummaryDraft, item).created_at) <= cutoff
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                _instant(item.period_end),
                _instant(item.created_at),
                _numeric_version(item.version),
                item.summary_id,
            ),
        )

    def _tasks_as_of(
        self, patient_id: str, cutoff: datetime
    ) -> list[dict[str, Any]]:
        resources = self.repository.list_fhir_resources(
            patient_id=patient_id,
            resource_type="Task",
            current_only=False,
        )
        candidates = [
            item
            for item in resources
            if _instant(item["meta"]["lastUpdated"]) <= cutoff
        ]
        selected: dict[str, dict[str, Any]] = {}
        for item in candidates:
            current = selected.get(item["id"])
            if current is None or (
                _instant(item["meta"]["lastUpdated"]),
                _numeric_version(item["meta"]["versionId"]),
            ) > (
                _instant(current["meta"]["lastUpdated"]),
                _numeric_version(current["meta"]["versionId"]),
            ):
                selected[item["id"]] = item
        snapshot = self.input_reader.read(
            patient_id,
            pathway_code=self.pathway_code,
            pathway_version=self.pathway_version,
            assembled_at=cutoff.isoformat(),
        )
        admitted_observation_refs = {
            f"Observation/{item['id']}/_history/"
            f"{item.get('meta', {}).get('versionId') or '1'}"
            for item in snapshot.observations
        }
        admitted: list[dict[str, Any]] = []
        for item in selected.values():
            try:
                admitted.append(
                    admit_clinical_rule_task(
                        item,
                        patient_id=patient_id,
                        pathway_code=self.pathway_code,
                        pathway_version=self.pathway_version,
                        cutoff=cutoff,
                        repository=self.repository,
                        admitted_observation_refs=admitted_observation_refs,
                    )
                )
            except (TypeError, ValueError):
                continue
        return sorted(
            admitted,
            key=lambda item: (
                _instant(item.get("authoredOn") or item["meta"]["lastUpdated"]),
                item["id"],
            ),
            reverse=True,
        )

    @staticmethod
    def _evidence_roots(
        *,
        timeline: list[TimelineEvent],
        state: ClinicalStateSnapshot | None,
        summary: Layer4SummaryDraft | None,
        tasks: list[dict[str, Any]],
    ) -> list[str]:
        roots = {
            f"urn:continucare:timeline-event:{item.timeline_event_id}"
            for item in timeline
        }
        if state is not None:
            roots.add(
                f"urn:continucare:state-snapshot:{state.snapshot_id}"
                f":version:{state.version}"
            )
        if summary is not None:
            roots.add(
                f"urn:continucare:summary:{summary.summary_id}"
                f":version:{summary.version}"
            )
        roots.update(
            f"Task/{item['id']}/_history/{item['meta']['versionId']}"
            for item in tasks
        )
        return sorted(roots)

    def _resolve_artifact(
        self, *, patient_id: str, reference: str, cutoff: datetime
    ) -> EvidenceTraceArtifact | None:
        if reference.startswith("urn:continucare:"):
            return self._resolve_contract_urn(patient_id, reference, cutoff)
        parts = reference.split("/")
        if len(parts) < 2:
            return None
        resource_type, resource_id = parts[0], parts[1]
        version = parts[3] if len(parts) >= 4 and parts[2] == "_history" else None
        if resource_type in {"Communication", "Provenance", "Task"}:
            resources = self.repository.list_fhir_resources(
                patient_id=patient_id,
                resource_type=resource_type,
                current_only=False,
            )
            candidates = [
                item
                for item in resources
                if item.get("id") == resource_id
                and (version is None or item.get("meta", {}).get("versionId") == version)
                and _instant(item["meta"]["lastUpdated"]) <= cutoff
            ]
            if not candidates:
                return None
            resource = max(
                candidates,
                key=lambda item: (
                    _instant(item["meta"]["lastUpdated"]),
                    _numeric_version(item["meta"]["versionId"]),
                ),
            )
            return EvidenceTraceArtifact(
                reference=reference,
                artifact_type=EvidenceArtifactType.FHIR_RESOURCE,
                resource_type=resource_type,
                version=resource["meta"]["versionId"],
                payload=resource,
            )
        if resource_type in {"Observation", "QuestionnaireResponse"}:
            snapshot = self.input_reader.read(
                patient_id,
                pathway_code=self.pathway_code,
                pathway_version=self.pathway_version,
                assembled_at=cutoff.isoformat(),
            )
            resources = (
                snapshot.observations
                if resource_type == "Observation"
                else snapshot.questionnaire_responses
            )
            for resource in resources:
                resource_version = resource.get("meta", {}).get("versionId") or "1"
                clinical_time = (
                    resource.get("issued")
                    if resource_type == "Observation"
                    else resource.get("authored")
                )
                if (
                    resource.get("id") == resource_id
                    and (version is None or version == resource_version)
                    and clinical_time
                    and _instant(clinical_time) <= cutoff
                ):
                    return EvidenceTraceArtifact(
                        reference=reference,
                        artifact_type=EvidenceArtifactType.FHIR_RESOURCE,
                        resource_type=resource_type,
                        version=resource_version,
                        payload=resource,
                    )
        return None

    def _resolve_contract_urn(
        self, patient_id: str, reference: str, cutoff: datetime
    ) -> EvidenceTraceArtifact | None:
        message_prefix = "urn:continucare:followup-message:"
        if reference.startswith(message_prefix):
            message_id = reference.removeprefix(message_prefix)
            message = self.input_reader.store.get_message(message_id)
            if (
                message is None
                or message.patient_id != patient_id
                or _instant(message.submitted_at) > cutoff
            ):
                return None
            return EvidenceTraceArtifact(
                reference=reference,
                artifact_type=EvidenceArtifactType.APPLICATION_RECORD,
                record_type="followup_message",
                version="1",
                payload=message.model_dump(mode="json"),
            )

        audit_prefix = "urn:continucare:audit-event:"
        if reference.startswith(audit_prefix):
            event_id = reference.removeprefix(audit_prefix)
            event = next(
                (
                    item
                    for item in self.input_reader.store.list_pathway_audit_events(
                        patient_id,
                        pathway_code=self.pathway_code,
                        pathway_version=self.pathway_version,
                    )
                    if item.event_id == event_id and _instant(item.created_at) <= cutoff
                ),
                None,
            )
            if event is None:
                return None
            return EvidenceTraceArtifact(
                reference=reference,
                artifact_type=EvidenceArtifactType.APPLICATION_RECORD,
                record_type="audit_event",
                version="1",
                payload=event.model_dump(mode="json"),
            )

        pathway_prefix = "urn:continucare:pathway:"
        if reference.startswith(pathway_prefix) and "|" in reference:
            code, version = reference.removeprefix(pathway_prefix).rsplit("|", 1)
            if code != self.pathway_code or version != self.pathway_version:
                return None
            try:
                pathway = load_builtin_pathways().get(code, version)
            except PathwayNotFoundError:
                return None
            return EvidenceTraceArtifact(
                reference=reference,
                artifact_type=EvidenceArtifactType.APPLICATION_RECORD,
                record_type="pathway_definition",
                version=version,
                payload=pathway.model_dump(mode="json"),
            )

        mappings = (
            ("urn:continucare:timeline-event:", "timeline_event", "timeline_event_id"),
            ("urn:continucare:memory-event:", "memory_event", "event_id"),
        )
        for prefix, record_type, id_field in mappings:
            if not reference.startswith(prefix):
                continue
            record_id = reference.removeprefix(prefix)
            record = self._patient_contract(
                record_type, id_field, record_id, patient_id, cutoff
            )
            return self._contract_artifact(reference, record_type, record)

        versioned_mappings = (
            ("urn:continucare:state-snapshot:", "state_snapshot", "snapshot_id"),
            ("urn:continucare:summary:", "summary_draft", "summary_id"),
            ("urn:continucare:doctor-review:", "doctor_review", "review_id"),
        )
        for prefix, record_type, id_field in versioned_mappings:
            if not reference.startswith(prefix):
                continue
            value = reference.removeprefix(prefix)
            if ":version:" not in value:
                return None
            record_id, version = value.rsplit(":version:", 1)
            record = self._patient_contract(
                record_type,
                id_field,
                record_id,
                patient_id,
                cutoff,
                version=version,
            )
            return self._contract_artifact(reference, record_type, record)

        metric_prefix = "urn:continucare:metric-definition:"
        if reference.startswith(metric_prefix) and ":version:" in reference:
            value = reference.removeprefix(metric_prefix)
            metric_id, version = value.rsplit(":version:", 1)
            snapshots = self.repository.list_contracts(
                "state_snapshot", patient_id=patient_id, current_only=False
            )
            for item in snapshots:
                state = cast(ClinicalStateSnapshot, item)
                if _instant(state.created_at) > cutoff:
                    continue
                for definition in state.metric_definitions:
                    if definition.metric_id == metric_id and definition.version == version:
                        return EvidenceTraceArtifact(
                            reference=reference,
                            artifact_type=EvidenceArtifactType.METRIC_DEFINITION,
                            record_type="state_metric_definition",
                            version=version,
                            payload=definition.model_dump(mode="json"),
                        )
            return None

        rule_prefix = "urn:continucare:clinical-rule:"
        if reference.startswith(rule_prefix) and "|" in reference:
            rule_id, version = reference.removeprefix(rule_prefix).rsplit("|", 1)
            records = self.repository.list_contracts(
                "clinical_rule", current_only=False
            )
            for item in records:
                rule = cast(ClinicalRuleDefinition, item)
                if (
                    rule.rule_id == rule_id
                    and rule.version == version
                    and _instant(rule.created_at) <= cutoff
                ):
                    return self._contract_artifact(reference, "clinical_rule", rule)
        return None

    def _patient_contract(
        self,
        record_type: str,
        id_field: str,
        record_id: str,
        patient_id: str,
        cutoff: datetime,
        *,
        version: str | None = None,
    ):
        records = self.repository.list_contracts(
            record_type, patient_id=patient_id, current_only=False
        )
        candidates = []
        for item in records:
            if getattr(item, id_field) != record_id:
                continue
            item_version = getattr(item, "version", "1")
            if version is not None and item_version != version:
                continue
            item_time = self._contract_time(item)
            if item_time <= cutoff:
                candidates.append(item)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                self._contract_time(item),
                _numeric_version(getattr(item, "version", "1")),
            ),
        )

    @staticmethod
    def _contract_time(record) -> datetime:
        for field in (
            "created_at",
            "reviewed_at",
            "recorded_at",
            "effective_start",
        ):
            value = getattr(record, field, None)
            if value:
                return _instant(value)
        raise ValueError("contract record has no replay time")

    @staticmethod
    def _contract_artifact(reference, record_type, record):
        if record is None:
            return None
        return EvidenceTraceArtifact(
            reference=reference,
            artifact_type=EvidenceArtifactType.CONTRACT_RECORD,
            record_type=record_type,
            version=getattr(record, "version", "1"),
            payload=record.model_dump(mode="json"),
        )

    def _artifact_relations(
        self,
        artifact: EvidenceTraceArtifact,
        *,
        patient_id: str,
        cutoff: datetime,
    ) -> tuple[list[tuple[str, str]], bool]:
        relations: set[tuple[str, str]] = set()
        degraded = False

        def walk(value: Any, *, key: str = "reference") -> None:
            if isinstance(value, dict):
                for child_key, child in value.items():
                    if child_key == "reference" and isinstance(child, str):
                        if DoctorWorkbenchService._traceable_reference(child):
                            relations.add((key, child))
                    elif child_key.endswith("_reference") and isinstance(child, str):
                        if DoctorWorkbenchService._traceable_reference(child):
                            relations.add((child_key, child))
                    else:
                        walk(child, key=child_key)
            elif isinstance(value, list):
                for child in value:
                    walk(child, key=key)

        payload = artifact.payload
        walk(payload)
        if artifact.record_type == "timeline_event":
            relations.add(
                (
                    "memory_event",
                    f"urn:continucare:memory-event:{payload['memory_event_id']}",
                )
            )
        if artifact.record_type == "summary_draft":
            relations.update(
                ("source_timeline_event", f"urn:continucare:timeline-event:{item}")
                for item in payload.get("source_timeline_event_ids", [])
            )
        if artifact.record_type == "doctor_review":
            relations.add(
                (
                    "reviewed_summary",
                    f"urn:continucare:summary:{payload['summary_id']}"
                    f":version:{payload['summary_version']}",
                )
            )
            if payload.get("result_summary_id") and payload.get("result_summary_version"):
                relations.add(
                    (
                        "result_summary",
                        f"urn:continucare:summary:{payload['result_summary_id']}"
                        f":version:{payload['result_summary_version']}",
                    )
                )
        if artifact.resource_type in {
            "Observation",
            "QuestionnaireResponse",
            "Communication",
            "Task",
        }:
            resource = artifact.payload
            exact_reference = (
                f"{artifact.resource_type}/{resource['id']}/_history/"
                f"{artifact.version or '1'}"
            )
            resource_reference = f"{artifact.resource_type}/{resource['id']}"
            try:
                provenance_resources = self.repository.list_fhir_resources(
                    patient_id=patient_id,
                    resource_type="Provenance",
                    current_only=False,
                )
                for resource in provenance_resources:
                    if _instant(resource["recorded"]) > cutoff:
                        continue
                    targets = {
                        item.get("reference") for item in resource.get("target", [])
                    }
                    relation = None
                    if exact_reference in targets:
                        relation = "provenance_exact_version"
                    elif resource_reference in targets:
                        relation = "provenance_resource_level"
                    if relation:
                        relations.add(
                            (
                                relation,
                                f"Provenance/{resource['id']}/_history/"
                                f"{resource['meta']['versionId']}",
                            )
                        )
            except Exception:
                degraded = True
        return sorted(relations), degraded

    @staticmethod
    def _traceable_reference(reference: str) -> bool:
        return reference.startswith(
            (
                "Observation/",
                "QuestionnaireResponse/",
                "Communication/",
                "Task/",
                "Provenance/",
                "PlanDefinition/",
                "urn:continucare:",
            )
        )

"""Fail-closed deterministic execution for explicitly approved clinical rules."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, cast

from continucare.fhir.r4 import validate_r4_resource
from continucare.layer4.contracts import (
    ClinicalRuleDefinition,
    EvidenceReference,
    EvidenceRole,
    ResourceReference,
    RuleCondition,
    RuleConditionExplanation,
    RuleConditionStatus,
    RuleEvaluationBatch,
    RuleEvaluationResult,
    RuleEvaluationStatus,
    RuleObservationInput,
    RuleOperator,
    RuleLifecycle,
)
from continucare.layer4.fhir import build_provenance, build_workflow_task
from continucare.layer4.inputs import Layer4InputReader
from continucare.layer4.repository import Layer4Repository


RULE_ENGINE_REFERENCE = "Device/continucare-approved-rule-engine"


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("rule evaluation times must include a timezone offset")
    return parsed


def _resource_reference(resource: dict[str, Any]) -> ResourceReference:
    resource_id = resource.get("id")
    if not resource_id:
        raise ValueError("rule evidence Observation requires an id")
    return ResourceReference(
        reference=f"Observation/{resource_id}",
        version_id=resource.get("meta", {}).get("versionId") or "1",
    )


def _versioned_reference(reference: ResourceReference) -> str:
    if reference.reference.startswith("urn:"):
        return reference.reference
    if reference.version_id:
        return f"{reference.reference}/_history/{reference.version_id}"
    return reference.reference


def _observation_period(resource: dict[str, Any]) -> tuple[str, str, str]:
    issued = resource.get("issued") or resource.get("meta", {}).get("lastUpdated")
    if "effectivePeriod" in resource:
        period = resource["effectivePeriod"]
        start = period.get("start") or period.get("end") or issued
        end = period.get("end") or period.get("start") or issued
    else:
        start = end = resource.get("effectiveDateTime") or issued
    if not start or not end or not issued:
        raise ValueError(
            "rule evidence Observation requires effective time and issued/meta.lastUpdated"
        )
    _instant(start)
    _instant(end)
    _instant(issued)
    return start, end, issued


def _matches_code(resource: dict[str, Any], rule_input: RuleObservationInput) -> bool:
    return any(
        item.get("system") == rule_input.code_system
        and item.get("code") == rule_input.code
        for item in resource.get("code", {}).get("coding", [])
    )


def _observation_value(resource: dict[str, Any]) -> tuple[Any, str | None]:
    if "valueQuantity" in resource:
        quantity = resource["valueQuantity"]
        if "value" not in quantity:
            return None, quantity.get("code") or quantity.get("unit")
        return quantity["value"], quantity.get("code") or quantity.get("unit")
    if "valueCodeableConcept" in resource:
        codings = resource["valueCodeableConcept"].get("coding", [])
        codes = [item["code"] for item in codings if item.get("code")]
        return codes, None
    for field in (
        "valueBoolean",
        "valueInteger",
        "valueDecimal",
        "valueString",
        "valueDateTime",
        "valueTime",
    ):
        if field in resource:
            return resource[field], None
    return None, None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _compare(actual: Any, expected: Any, operator: RuleOperator) -> bool | None:
    if operator == RuleOperator.IN:
        if not isinstance(expected, (list, tuple)):
            return None
        if isinstance(actual, list):
            return any(item in expected for item in actual)
        return actual in expected
    if operator in {RuleOperator.EQ, RuleOperator.NE}:
        if isinstance(actual, list):
            matched = expected in actual
        else:
            actual_number = _decimal(actual)
            expected_number = _decimal(expected)
            matched = (
                actual_number == expected_number
                if actual_number is not None and expected_number is not None
                else actual == expected
            )
        return matched if operator == RuleOperator.EQ else not matched
    actual_number = _decimal(actual)
    expected_number = _decimal(expected)
    if actual_number is None or expected_number is None:
        return None
    return {
        RuleOperator.GT: actual_number > expected_number,
        RuleOperator.GTE: actual_number >= expected_number,
        RuleOperator.LT: actual_number < expected_number,
        RuleOperator.LTE: actual_number <= expected_number,
    }.get(operator)


class ApprovedRuleEngine:
    """Evaluate only active, dual-approved rules against final Observations."""

    def __init__(
        self,
        repository: Layer4Repository,
        *,
        input_reader: Layer4InputReader,
        requester_reference: str,
        owner_references: dict[str, str],
    ):
        if not requester_reference.strip():
            raise ValueError("rule engine requester_reference cannot be blank")
        self.repository = repository
        self.input_reader = input_reader
        self.requester_reference = requester_reference
        self.owner_references = dict(owner_references)

    def evaluate(
        self,
        *,
        patient_id: str,
        observations: Iterable[dict[str, Any]],
        pathway_code: str,
        pathway_version: str,
        evaluated_at: str,
        region: str,
        synthetic_data: bool,
        product_code: str | None = None,
    ) -> RuleEvaluationBatch:
        at = _instant(evaluated_at)
        snapshot = self.input_reader.read(
            patient_id,
            pathway_code=pathway_code,
            pathway_version=pathway_version,
            assembled_at=evaluated_at,
        )
        if (
            snapshot.pathway_code != pathway_code
            or snapshot.pathway_version != pathway_version
        ):
            raise ValueError("rule input snapshot Pathway does not match evaluation")
        normalized = self._validate_observations(
            patient_id,
            observations,
            admitted_observations=snapshot.observations,
        )
        records = self.repository.list_contracts(
            "clinical_rule",
            pathway_code=pathway_code,
            status=RuleLifecycle.ACTIVE.value,
            current_only=True,
        )
        rules = sorted(
            (cast(ClinicalRuleDefinition, item) for item in records),
            key=lambda item: (item.rule_id, item.version),
        )
        rules = [
            item
            for item in rules
            if item.lifecycle == RuleLifecycle.ACTIVE
            and item.approval.fully_approved()
            and item.applicability.pathway_version == pathway_version
        ]
        batch_id = _stable_id(
            "rule-batch", patient_id, pathway_code, pathway_version, evaluated_at
        )
        if not rules:
            provenance_reference = self._persist_no_rule_provenance(
                patient_id=patient_id,
                pathway_code=pathway_code,
                pathway_version=pathway_version,
                evaluated_at=evaluated_at,
                batch_id=batch_id,
            )
            return RuleEvaluationBatch(
                batch_id=batch_id,
                patient_id=patient_id,
                pathway_code=pathway_code,
                pathway_version=pathway_version,
                status=RuleEvaluationStatus.NOT_ASSESSED,
                reason_codes=["no_active_dual_approved_rule"],
                provenance_references=[provenance_reference],
                evaluated_at=evaluated_at,
            )

        results = [
            self._evaluate_rule(
                rule=rule,
                patient_id=patient_id,
                observations=normalized,
                pathway_code=pathway_code,
                pathway_version=pathway_version,
                evaluated_at=evaluated_at,
                evaluated_at_instant=at,
                region=region,
                synthetic_data=synthetic_data,
                product_code=product_code,
            )
            for rule in rules
        ]
        if any(item.status == RuleEvaluationStatus.MATCHED for item in results):
            status = RuleEvaluationStatus.MATCHED
        elif any(
            item.status == RuleEvaluationStatus.NOT_ASSESSED for item in results
        ):
            status = RuleEvaluationStatus.NOT_ASSESSED
        else:
            status = RuleEvaluationStatus.NO_MATCH
        return RuleEvaluationBatch(
            batch_id=batch_id,
            patient_id=patient_id,
            pathway_code=pathway_code,
            pathway_version=pathway_version,
            status=status,
            reason_codes=sorted(
                {reason for item in results for reason in item.reason_codes}
            ),
            evaluations=results,
            task_references=sorted(
                {item.task_reference for item in results if item.task_reference}
            ),
            provenance_references=sorted(
                {item.provenance_reference for item in results}
            ),
            evaluated_at=evaluated_at,
        )

    def _validate_observations(
        self,
        patient_id: str,
        observations: Iterable[dict[str, Any]],
        *,
        admitted_observations: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        admitted = {
            item["id"]: validate_r4_resource(
                item, expected_resource_type="Observation"
            )
            for item in admitted_observations
        }
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for resource in observations:
            item = validate_r4_resource(
                resource, expected_resource_type="Observation"
            )
            if item.get("status") != "final":
                raise ValueError("approved rule engine only accepts final Observation")
            if item.get("subject", {}).get("reference") != f"Patient/{patient_id}":
                raise ValueError("rule evidence Observation patient does not match")
            observation_id = item.get("id")
            if not observation_id or admitted.get(observation_id) != item:
                raise ValueError(
                    "approved rule engine only accepts Pathway-admitted Observation"
                )
            if observation_id in seen:
                raise ValueError("rule evidence contains a duplicate Observation")
            seen.add(observation_id)
            _observation_period(item)
            normalized.append(item)
        return normalized

    def _evaluate_rule(
        self,
        *,
        rule: ClinicalRuleDefinition,
        patient_id: str,
        observations: list[dict[str, Any]],
        pathway_code: str,
        pathway_version: str,
        evaluated_at: str,
        evaluated_at_instant: datetime,
        region: str,
        synthetic_data: bool,
        product_code: str | None,
    ) -> RuleEvaluationResult:
        applicability_reasons = self._applicability_reasons(
            rule,
            region=region,
            synthetic_data=synthetic_data,
            product_code=product_code,
        )
        inputs = {item.input_id: item for item in rule.inputs}
        conditions = [
            self._evaluate_condition(
                index=index,
                condition=condition,
                rule_input=inputs[condition.input_id],
                observations=observations,
                evaluated_at=evaluated_at_instant,
                applicability_reasons=applicability_reasons,
            )
            for index, condition in enumerate(rule.conditions)
        ]
        status = self._evaluation_status(rule, conditions, applicability_reasons)
        reason_codes = sorted(
            {
                *applicability_reasons,
                *(
                    item.reason_code
                    for item in conditions
                    if item.reason_code is not None
                ),
            }
        )
        evidence = self._unique_evidence(conditions)
        owner_reference = self.owner_references.get(rule.action.owner_role)
        if status == RuleEvaluationStatus.MATCHED and not owner_reference:
            status = RuleEvaluationStatus.NOT_ASSESSED
            reason_codes = sorted({*reason_codes, "owner_role_unmapped"})
        evidence_versions = sorted(
            _versioned_reference(item.resource) for item in evidence
        )
        evaluation_id = _stable_id(
            "rule-evaluation",
            patient_id,
            rule.rule_id,
            rule.version,
            evaluated_at,
            status.value,
            *evidence_versions,
        )

        task: dict[str, Any] | None = None
        task_created = False
        task_built = False
        if status == RuleEvaluationStatus.MATCHED:
            evaluation_task_id = _stable_id(
                "task", patient_id, rule.rule_id, rule.version, evaluation_id
            )
            task = self._find_deduplicated_task(
                patient_id=patient_id,
                rule=rule,
                evaluated_at=evaluated_at_instant,
            )
            if task is None:
                task = self._build_task(
                    patient_id=patient_id,
                    pathway_code=pathway_code,
                    rule=rule,
                    evidence_versions=evidence_versions,
                    evaluation_id=evaluation_id,
                    evaluated_at=evaluated_at,
                    owner_reference=cast(str, owner_reference),
                )
                task_created = True
                task_built = True
            else:
                task_created = task["id"] == evaluation_task_id

        provenance_id = _stable_id("provenance", evaluation_id)
        targets = [f"urn:continucare:rule-evaluation:{evaluation_id}"]
        if task is not None:
            targets.append(
                f"Task/{task['id']}/_history/{task['meta']['versionId']}"
            )
        provenance = build_provenance(
            target_references=targets,
            recorded_at=evaluated_at,
            agent_reference=RULE_ENGINE_REFERENCE,
            agent_role_code="author",
            agent_role_display="Author",
            provenance_id=provenance_id,
            activity_code="EXECUTE",
            activity_display=f"rule evaluation: {status.value}",
            entity_source_references=[
                f"urn:continucare:clinical-rule:{rule.rule_id}|{rule.version}",
                *evidence_versions,
            ],
        )
        if task_built:
            self.repository.persist_fhir_creation_bundle(
                resources=[cast(dict[str, Any], task), provenance],
                patient_id=patient_id,
            )
        else:
            # A deduplicated Task remains the target of a new, independent
            # evaluation fact. This does not repair or rewrite its creation
            # history; it records only the current evaluation.
            self.repository.save_fhir_resource(provenance, patient_id=patient_id)
        return RuleEvaluationResult(
            evaluation_id=evaluation_id,
            patient_id=patient_id,
            pathway_code=pathway_code,
            pathway_version=pathway_version,
            rule_id=rule.rule_id,
            rule_version=rule.version,
            status=status,
            condition_logic=rule.condition_logic,
            conditions=conditions,
            reason_codes=reason_codes,
            evidence_refs=evidence,
            task_reference=f"Task/{task['id']}" if task is not None else None,
            task_created=task_created,
            provenance_reference=f"Provenance/{provenance_id}",
            evaluated_at=evaluated_at,
        )

    @staticmethod
    def _applicability_reasons(
        rule: ClinicalRuleDefinition,
        *,
        region: str,
        synthetic_data: bool,
        product_code: str | None,
    ) -> list[str]:
        reasons: list[str] = []
        applicability = rule.applicability
        if applicability.region != region:
            reasons.append("region_mismatch")
        if applicability.synthetic_only != synthetic_data:
            reasons.append("data_environment_mismatch")
        if applicability.product_code and applicability.product_code != product_code:
            reasons.append("product_mismatch")
        return reasons

    def _evaluate_condition(
        self,
        *,
        index: int,
        condition: RuleCondition,
        rule_input: RuleObservationInput,
        observations: list[dict[str, Any]],
        evaluated_at: datetime,
        applicability_reasons: list[str],
    ) -> RuleConditionExplanation:
        unit = condition.unit or rule_input.unit
        if applicability_reasons:
            return RuleConditionExplanation(
                condition_index=index,
                input_id=condition.input_id,
                operator=condition.operator,
                status=RuleConditionStatus.NOT_ASSESSED,
                expected_value=condition.expected_value,
                unit=unit,
                reason_code="applicability_mismatch",
            )
        selected = self._select_observation(
            rule_input, observations=observations, evaluated_at=evaluated_at
        )
        if selected is None:
            return RuleConditionExplanation(
                condition_index=index,
                input_id=condition.input_id,
                operator=condition.operator,
                status=RuleConditionStatus.NOT_ASSESSED,
                expected_value=condition.expected_value,
                unit=unit,
                reason_code="required_input_missing",
            )
        start, end, _ = _observation_period(selected)
        reference = _resource_reference(selected)
        evidence = EvidenceReference(
            evidence_id=_stable_id(
                "evidence", condition.input_id, _versioned_reference(reference)
            ),
            resource=reference,
            role=EvidenceRole.TRIGGER,
            effective_start=start,
            effective_end=end,
        )
        actual, actual_unit = _observation_value(selected)
        if actual is None:
            return RuleConditionExplanation(
                condition_index=index,
                input_id=condition.input_id,
                operator=condition.operator,
                status=RuleConditionStatus.NOT_ASSESSED,
                expected_value=condition.expected_value,
                actual_value=actual,
                unit=unit,
                reason_code="unsupported_or_missing_value",
                evidence_refs=[evidence],
            )
        if unit and actual_unit != unit:
            return RuleConditionExplanation(
                condition_index=index,
                input_id=condition.input_id,
                operator=condition.operator,
                status=RuleConditionStatus.NOT_ASSESSED,
                expected_value=condition.expected_value,
                actual_value=actual,
                unit=actual_unit,
                reason_code="unit_mismatch",
                evidence_refs=[evidence],
            )
        matched = _compare(actual, condition.expected_value, condition.operator)
        if matched is None:
            condition_status = RuleConditionStatus.NOT_ASSESSED
            reason = "unsupported_comparison"
        elif matched:
            condition_status = RuleConditionStatus.MATCHED
            reason = None
        else:
            condition_status = RuleConditionStatus.NOT_MATCHED
            reason = "condition_not_matched"
        return RuleConditionExplanation(
            condition_index=index,
            input_id=condition.input_id,
            operator=condition.operator,
            status=condition_status,
            expected_value=condition.expected_value,
            actual_value=actual,
            unit=actual_unit or unit,
            reason_code=reason,
            evidence_refs=[evidence],
        )

    @staticmethod
    def _select_observation(
        rule_input: RuleObservationInput,
        *,
        observations: list[dict[str, Any]],
        evaluated_at: datetime,
    ) -> dict[str, Any] | None:
        window_start = evaluated_at - timedelta(hours=rule_input.lookback_hours)
        candidates: list[dict[str, Any]] = []
        for resource in observations:
            if resource["status"] not in rule_input.accepted_statuses:
                continue
            if not _matches_code(resource, rule_input):
                continue
            start, end, issued = _observation_period(resource)
            if (
                _instant(start) <= evaluated_at
                and _instant(end) >= window_start
                and _instant(issued) <= evaluated_at
            ):
                candidates.append(resource)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                _instant(_observation_period(item)[1]),
                _instant(_observation_period(item)[2]),
                _versioned_reference(_resource_reference(item)),
            ),
        )

    @staticmethod
    def _evaluation_status(
        rule: ClinicalRuleDefinition,
        conditions: list[RuleConditionExplanation],
        applicability_reasons: list[str],
    ) -> RuleEvaluationStatus:
        if applicability_reasons or any(
            item.status == RuleConditionStatus.NOT_ASSESSED for item in conditions
        ):
            if rule.condition_logic == "any" and any(
                item.status == RuleConditionStatus.MATCHED for item in conditions
            ):
                return RuleEvaluationStatus.MATCHED
            return RuleEvaluationStatus.NOT_ASSESSED
        matched = [item.status == RuleConditionStatus.MATCHED for item in conditions]
        is_match = all(matched) if rule.condition_logic == "all" else any(matched)
        return (
            RuleEvaluationStatus.MATCHED
            if is_match
            else RuleEvaluationStatus.NO_MATCH
        )

    @staticmethod
    def _unique_evidence(
        conditions: list[RuleConditionExplanation],
    ) -> list[EvidenceReference]:
        evidence: dict[str, EvidenceReference] = {}
        for condition in conditions:
            for item in condition.evidence_refs:
                evidence[_versioned_reference(item.resource)] = item
        return [evidence[key] for key in sorted(evidence)]

    def _find_deduplicated_task(
        self,
        *,
        patient_id: str,
        rule: ClinicalRuleDefinition,
        evaluated_at: datetime,
    ) -> dict[str, Any] | None:
        expected_identifier = f"{rule.rule_id}|{rule.version}"
        cutoff = evaluated_at - timedelta(
            hours=rule.action.deduplication_window_hours
        )
        candidates: list[dict[str, Any]] = []
        for task in self.repository.list_fhir_resources(
            patient_id=patient_id, resource_type="Task", current_only=True
        ):
            if task.get("status") == "entered-in-error":
                continue
            if not any(
                item.get("system") == "urn:continucare:clinical-rule"
                and item.get("value") == expected_identifier
                for item in task.get("identifier", [])
            ):
                continue
            authored = _instant(task["authoredOn"])
            if cutoff <= authored <= evaluated_at:
                candidates.append(task)
        if not candidates:
            return None
        return max(candidates, key=lambda item: (_instant(item["authoredOn"]), item["id"]))

    def _build_task(
        self,
        *,
        patient_id: str,
        pathway_code: str,
        rule: ClinicalRuleDefinition,
        evidence_versions: list[str],
        evaluation_id: str,
        evaluated_at: str,
        owner_reference: str,
    ) -> dict[str, Any]:
        due_at = (_instant(evaluated_at) + timedelta(hours=rule.action.sla_hours)).isoformat()
        task_id = _stable_id(
            "task", patient_id, rule.rule_id, rule.version, evaluation_id
        )
        task = build_workflow_task(
            patient_id=patient_id,
            rule_id=rule.rule_id,
            rule_version=rule.version,
            task_code_system=rule.action.task_code_system,
            task_code=rule.action.task_code,
            task_code_display=rule.action.task_code_display,
            description=rule.action.description,
            requester_reference=self.requester_reference,
            owner_reference=owner_reference,
            authored_on=evaluated_at,
            trigger_reference=evidence_versions[0],
            due_at=due_at,
            priority=rule.action.priority,
            task_id=task_id,
            based_on_references=[
                f"urn:continucare:clinical-rule:{rule.rule_id}|{rule.version}",
                f"urn:continucare:pathway:{pathway_code}|{rule.applicability.pathway_version}",
            ],
            evidence_references=evidence_versions,
        )
        return task

    def _persist_no_rule_provenance(
        self,
        *,
        patient_id: str,
        pathway_code: str,
        pathway_version: str,
        evaluated_at: str,
        batch_id: str,
    ) -> str:
        provenance_id = _stable_id("provenance", batch_id)
        provenance = build_provenance(
            target_references=[f"urn:continucare:rule-evaluation-batch:{batch_id}"],
            recorded_at=evaluated_at,
            agent_reference=RULE_ENGINE_REFERENCE,
            agent_role_code="author",
            agent_role_display="Author",
            provenance_id=provenance_id,
            activity_code="EXECUTE",
            activity_display="rule evaluation: not_assessed",
            entity_source_references=[
                f"urn:continucare:pathway:{pathway_code}|{pathway_version}"
            ],
        )
        self.repository.save_fhir_resource(provenance, patient_id=patient_id)
        return f"Provenance/{provenance_id}"

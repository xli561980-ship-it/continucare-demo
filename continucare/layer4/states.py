"""Deterministic clinical-state snapshots and raw numeric trend calculations."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, cast

from continucare.fhir.r4 import validate_r4_resource
from continucare.layer4.contracts import (
    ClinicalStateSnapshot,
    EvidenceReference,
    EvidenceRole,
    MetricState,
    MetricStateStatus,
    NumericTrend,
    ResourceReference,
    RevisionLink,
    StateMetricDefinition,
    TrendCalculationStatus,
    TrendDirection,
)
from continucare.layer4.fhir import build_provenance
from continucare.layer4.inputs import Layer4InputReader
from continucare.layer4.repository import Layer4Repository


STATE_AGENT_REFERENCE = "Device/continucare-clinical-state"


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("clinical-state times must include a timezone offset")
    return parsed


def _resource_reference(resource: dict[str, Any]) -> ResourceReference:
    return ResourceReference(
        reference=f"Observation/{resource['id']}",
        version_id=resource.get("meta", {}).get("versionId") or "1",
    )


def _versioned_reference(reference: ResourceReference) -> str:
    if reference.reference.startswith("urn:"):
        return reference.reference
    if reference.version_id:
        return f"{reference.reference}/_history/{reference.version_id}"
    return reference.reference


def _observation_times(resource: dict[str, Any]) -> tuple[str, str, str]:
    issued = resource.get("issued")
    if not issued:
        raise ValueError("state calculation requires Observation.issued")
    if "effectivePeriod" in resource:
        period = resource["effectivePeriod"]
        start = period.get("start") or period.get("end") or issued
        end = period.get("end") or period.get("start") or issued
    else:
        start = end = resource.get("effectiveDateTime") or issued
    if _instant(end) < _instant(start):
        raise ValueError("Observation effective end cannot precede start")
    return start, end, issued


def _matches_code(resource: dict[str, Any], definition: StateMetricDefinition) -> bool:
    return any(
        item.get("system") == definition.code_system
        and item.get("code") == definition.code
        for item in resource.get("code", {}).get("coding", [])
    )


@dataclass(frozen=True)
class _Value:
    value: Any
    canonical: str
    numeric: Decimal | None
    kind: str
    unit: str | None
    unit_system: str | None
    display: str


def _observation_value(resource: dict[str, Any]) -> _Value | None:
    if "valueQuantity" in resource:
        quantity = resource["valueQuantity"]
        if "value" not in quantity:
            return None
        raw = quantity["value"]
        try:
            numeric = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Observation quantity value must be numeric") from exc
        unit = quantity.get("code") or quantity.get("unit")
        unit_system = quantity.get("system")
        canonical = json.dumps(
            {
                "kind": "quantity",
                "value": _decimal_text(numeric),
                "unit": unit,
                "unit_system": unit_system,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return _Value(
            value=raw,
            canonical=canonical,
            numeric=numeric,
            kind="quantity",
            unit=unit,
            unit_system=unit_system,
            display=f"{raw} {unit or ''}".strip(),
        )
    scalar_fields = (
        ("valueInteger", "integer", True),
        ("valueDecimal", "decimal", True),
        ("valueBoolean", "boolean", False),
        ("valueString", "string", False),
        ("valueDateTime", "datetime", False),
        ("valueTime", "time", False),
    )
    for field, kind, numeric_kind in scalar_fields:
        if field not in resource:
            continue
        raw = resource[field]
        numeric = Decimal(str(raw)) if numeric_kind and not isinstance(raw, bool) else None
        canonical = json.dumps(
            {"kind": kind, "value": raw},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _Value(
            value=raw,
            canonical=canonical,
            numeric=numeric,
            kind=kind,
            unit=None,
            unit_system=None,
            display=str(raw),
        )
    if "valueCodeableConcept" in resource:
        concept = resource["valueCodeableConcept"]
        coding = concept.get("coding", [])
        value = coding[0].get("code") if coding else concept.get("text")
        return _Value(
            value=value,
            canonical=json.dumps(
                {"kind": "codeable_concept", "value": concept},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            numeric=None,
            kind="codeable_concept",
            unit=None,
            unit_system=None,
            display=str(value),
        )
    return None


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class _Point:
    resource: dict[str, Any]
    reference: ResourceReference
    start: str
    end: str
    issued: str
    value: _Value | None

    @property
    def period_key(self) -> tuple[str, str]:
        return self.start, self.end


def _point_sort_key(point: _Point) -> tuple[datetime, datetime, datetime, str]:
    return (
        _instant(point.end),
        _instant(point.start),
        _instant(point.issued),
        _versioned_reference(point.reference),
    )


def _evidence(point: _Point, *, role: EvidenceRole) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=_stable_id(
            "evidence", _versioned_reference(point.reference), role.value
        ),
        resource=point.reference,
        role=role,
        effective_start=point.start,
        effective_end=point.end,
        evidence_text=point.value.display if point.value else "Observation has no value",
    )


def _definition_reference(definition: StateMetricDefinition) -> ResourceReference:
    return ResourceReference(
        reference=(
            f"urn:continucare:metric-definition:{definition.metric_id}"
            f":version:{definition.version}"
        ),
        version_id=definition.version,
        display=definition.display,
    )


class ClinicalStateService:
    """Create evidence-bound state snapshots without clinical interpretation."""

    ALGORITHM_VERSION = "clinical-state-v1"

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

    def build(
        self,
        *,
        patient_id: str,
        definitions: Iterable[StateMetricDefinition],
        as_of: str | None = None,
        generated_at: str | None = None,
    ) -> ClinicalStateSnapshot:
        snapshot = self.input_reader.read(
            patient_id,
            pathway_code=self.pathway_code,
            pathway_version=self.pathway_version,
        )
        if (
            snapshot.pathway_code != self.pathway_code
            or snapshot.pathway_version != self.pathway_version
        ):
            raise ValueError("Layer-4 input snapshot Pathway does not match state service")
        cutoff_text = as_of or snapshot.assembled_at
        generated_text = generated_at or cutoff_text
        cutoff = _instant(cutoff_text)
        if _instant(generated_text) < cutoff:
            raise ValueError("state snapshot generated_at cannot precede as_of")
        configured = sorted(
            (StateMetricDefinition.model_validate(item) for item in definitions),
            key=lambda item: item.metric_id,
        )
        if not configured:
            raise ValueError("state snapshot requires at least one metric definition")
        metric_ids = [item.metric_id for item in configured]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("state snapshot contains duplicate metric definitions")
        for definition in configured:
            if (
                definition.pathway_code != self.pathway_code
                or definition.pathway_version != self.pathway_version
            ):
                raise ValueError("metric definition pathway does not match service")

        points = self._eligible_points(
            patient_id=patient_id,
            observations=snapshot.observations,
            cutoff=cutoff,
        )
        states: list[MetricState] = []
        trends: list[NumericTrend] = []
        used: dict[str, ResourceReference] = {}
        for definition in configured:
            matched = [
                point for point in points if _matches_code(point.resource, definition)
            ]
            state, state_points = self._metric_state(
                definition=definition,
                points=matched,
                as_of=cutoff_text,
            )
            trend, trend_points = self._numeric_trend(
                definition=definition,
                points=matched,
                as_of=cutoff_text,
            )
            states.append(state)
            trends.append(trend)
            for point in [*state_points, *trend_points]:
                used[_versioned_reference(point.reference)] = point.reference

        definition_refs = [_definition_reference(item) for item in configured]
        source_refs = [used[key] for key in sorted(used)]
        definition_digest = hashlib.sha256(
            json.dumps(
                [item.model_dump(mode="json") for item in configured],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        snapshot_id = _stable_id(
            "state-snapshot",
            patient_id,
            self.pathway_code,
            self.pathway_version,
            cutoff_text,
            definition_digest,
        )
        current = self.repository.get_contract("state_snapshot", snapshot_id)
        if current is not None:
            persisted = cast(ClinicalStateSnapshot, current)
            if self._same_projection(
                persisted,
                states=states,
                trends=trends,
                source_refs=source_refs,
                definition_refs=definition_refs,
                definitions=configured,
            ):
                return persisted
            try:
                version = str(int(persisted.version) + 1)
            except ValueError as exc:
                raise ValueError("state snapshot versions must be numeric") from exc
        else:
            version = "1"

        provenance_id = _stable_id("provenance", snapshot_id, version)
        provenance = build_provenance(
            target_references=[
                f"urn:continucare:state-snapshot:{snapshot_id}:version:{version}"
            ],
            recorded_at=generated_text,
            agent_reference=STATE_AGENT_REFERENCE,
            agent_role_code="assembler",
            agent_role_display="Assembler",
            provenance_id=provenance_id,
            activity_code="TRANSFORM",
            activity_display="deterministic state and endpoint delta",
            entity_source_references=sorted(
                {
                    *(_versioned_reference(item) for item in source_refs),
                    *(_versioned_reference(item) for item in definition_refs),
                }
            ),
        )
        result = ClinicalStateSnapshot(
            snapshot_id=snapshot_id,
            version=version,
            patient_id=patient_id,
            pathway_code=self.pathway_code,
            pathway_version=self.pathway_version,
            as_of=cutoff_text,
            metric_definitions=configured,
            metric_definition_refs=definition_refs,
            states=states,
            trends=trends,
            source_observation_refs=source_refs,
            provenance_refs=[
                ResourceReference(
                    reference=f"Provenance/{provenance_id}", version_id="1"
                )
            ],
            algorithm_version=self.ALGORITHM_VERSION,
            created_at=generated_text,
        )
        self.repository.persist_state_snapshot_bundle(
            expected_current=cast(ClinicalStateSnapshot | None, current),
            snapshot=result,
            provenance=provenance,
        )
        return result

    def _eligible_points(
        self,
        *,
        patient_id: str,
        observations: Iterable[dict[str, Any]],
        cutoff: datetime,
    ) -> list[_Point]:
        superseded = self._superseded_references(patient_id)
        points: dict[str, _Point] = {}
        for candidate in observations:
            resource = validate_r4_resource(
                candidate, expected_resource_type="Observation"
            )
            if resource.get("status") != "final":
                raise ValueError("state calculation only accepts final Observation")
            if resource.get("subject", {}).get("reference") != f"Patient/{patient_id}":
                raise ValueError("Observation patient does not match state snapshot")
            reference = _resource_reference(resource)
            versioned = _versioned_reference(reference)
            if versioned in superseded:
                continue
            start, end, issued = _observation_times(resource)
            if _instant(start) > cutoff or _instant(end) > cutoff or _instant(issued) > cutoff:
                continue
            points[versioned] = _Point(
                resource=resource,
                reference=reference,
                start=start,
                end=end,
                issued=issued,
                value=_observation_value(resource),
            )
        return sorted(points.values(), key=_point_sort_key)

    def _superseded_references(self, patient_id: str) -> set[str]:
        records = self.repository.list_contracts(
            "revision_link", patient_id=patient_id, current_only=True
        )
        return {
            _versioned_reference(cast(RevisionLink, item).predecessor)
            for item in records
        }

    def _metric_state(
        self,
        *,
        definition: StateMetricDefinition,
        points: list[_Point],
        as_of: str,
    ) -> tuple[MetricState, list[_Point]]:
        cutoff = _instant(as_of)
        earliest = cutoff - timedelta(hours=definition.lookback_hours)
        within = [point for point in points if _instant(point.end) >= earliest]
        if not within:
            return (
                MetricState(
                    metric_id=definition.metric_id,
                    metric_version=definition.version,
                    status=MetricStateStatus.UNKNOWN,
                    as_of=as_of,
                    unit=definition.unit,
                    unit_system=definition.unit_system,
                    reason_codes=["no_eligible_observation"],
                ),
                [],
            )
        latest_period = max(
            (point.period_key for point in within),
            key=lambda item: (_instant(item[1]), _instant(item[0])),
        )
        latest = sorted(
            [point for point in within if point.period_key == latest_period],
            key=_point_sort_key,
        )
        canonical_values = {
            point.value.canonical if point.value is not None else "<missing>"
            for point in latest
        }
        if len(canonical_values) > 1:
            return (
                MetricState(
                    metric_id=definition.metric_id,
                    metric_version=definition.version,
                    status=MetricStateStatus.CONFLICT,
                    as_of=as_of,
                    unit=definition.unit,
                    unit_system=definition.unit_system,
                    evidence_refs=[
                        _evidence(item, role=EvidenceRole.CONTRADICTING)
                        for item in latest
                    ],
                    reason_codes=["conflicting_observations"],
                ),
                latest,
            )
        selected = latest[-1]
        if selected.value is None:
            return (
                MetricState(
                    metric_id=definition.metric_id,
                    metric_version=definition.version,
                    status=MetricStateStatus.UNKNOWN,
                    as_of=as_of,
                    unit=definition.unit,
                    unit_system=definition.unit_system,
                    reason_codes=["observation_value_missing"],
                ),
                latest,
            )
        if not self._unit_matches(selected.value, definition):
            return (
                MetricState(
                    metric_id=definition.metric_id,
                    metric_version=definition.version,
                    status=MetricStateStatus.UNKNOWN,
                    as_of=as_of,
                    unit=definition.unit,
                    unit_system=definition.unit_system,
                    reason_codes=["unit_mismatch"],
                ),
                latest,
            )
        age = (cutoff - _instant(selected.end)).total_seconds() / 3600
        status = (
            MetricStateStatus.STALE
            if age > definition.stale_after_hours
            else MetricStateStatus.CURRENT
        )
        reasons = (
            ["latest_observation_exceeds_freshness_window"]
            if status == MetricStateStatus.STALE
            else []
        )
        return (
            MetricState(
                metric_id=definition.metric_id,
                metric_version=definition.version,
                status=status,
                as_of=as_of,
                latest_value=selected.value.value,
                unit=selected.value.unit,
                unit_system=selected.value.unit_system,
                effective_start=selected.start,
                effective_end=selected.end,
                recorded_at=selected.issued,
                age_hours=round(age, 6),
                latest_observation=selected.reference,
                evidence_refs=[_evidence(selected, role=EvidenceRole.SOURCE)],
                reason_codes=reasons,
            ),
            latest,
        )

    def _numeric_trend(
        self,
        *,
        definition: StateMetricDefinition,
        points: list[_Point],
        as_of: str,
    ) -> tuple[NumericTrend, list[_Point]]:
        cutoff = _instant(as_of)
        start = cutoff - timedelta(hours=definition.trend_window_hours)
        period_start = start.isoformat()
        within = [point for point in points if _instant(point.end) >= start]
        groups: dict[tuple[str, str], list[_Point]] = defaultdict(list)
        for point in within:
            groups[point.period_key].append(point)
        conflict_points = [
            point
            for group in groups.values()
            if len(
                {
                    item.value.canonical if item.value is not None else "<missing>"
                    for item in group
                }
            )
            > 1
            for point in group
        ]
        if conflict_points:
            ordered_conflicts = sorted(conflict_points, key=_point_sort_key)
            return (
                NumericTrend(
                    metric_id=definition.metric_id,
                    metric_version=definition.version,
                    status=TrendCalculationStatus.CONFLICT,
                    unit=definition.unit,
                    unit_system=definition.unit_system,
                    point_count=len(ordered_conflicts),
                    period_start=period_start,
                    period_end=as_of,
                    algorithm_version=definition.algorithm_version,
                    evidence_refs=[
                        _evidence(item, role=EvidenceRole.CONTRADICTING)
                        for item in ordered_conflicts
                    ],
                    reason_codes=["conflicting_observations"],
                ),
                ordered_conflicts,
            )
        unique: list[_Point] = []
        for period_key in sorted(
            groups,
            key=lambda item: (_instant(item[1]), _instant(item[0])),
        ):
            unique.append(sorted(groups[period_key], key=_point_sort_key)[-1])
        valued = [point for point in unique if point.value is not None]
        if any(not self._unit_matches(point.value, definition) for point in valued):
            return (
                NumericTrend(
                    metric_id=definition.metric_id,
                    metric_version=definition.version,
                    status=TrendCalculationStatus.UNIT_MISMATCH,
                    unit=definition.unit,
                    unit_system=definition.unit_system,
                    point_count=len(unique),
                    period_start=period_start,
                    period_end=as_of,
                    algorithm_version=definition.algorithm_version,
                    evidence_refs=[
                        _evidence(item, role=EvidenceRole.SUPPORTING) for item in unique
                    ],
                    reason_codes=["unit_mismatch"],
                ),
                unique,
            )
        unit_keys = {
            (point.value.kind, point.value.unit_system, point.value.unit)
            for point in valued
        }
        if len(unit_keys) > 1:
            return (
                NumericTrend(
                    metric_id=definition.metric_id,
                    metric_version=definition.version,
                    status=TrendCalculationStatus.UNIT_MISMATCH,
                    unit=definition.unit,
                    unit_system=definition.unit_system,
                    point_count=len(unique),
                    period_start=period_start,
                    period_end=as_of,
                    algorithm_version=definition.algorithm_version,
                    evidence_refs=[
                        _evidence(item, role=EvidenceRole.SUPPORTING) for item in unique
                    ],
                    reason_codes=["heterogeneous_value_types_or_units"],
                ),
                unique,
            )
        numeric = [point for point in valued if point.value.numeric is not None]
        if len(numeric) < definition.minimum_trend_points or len(numeric) != len(unique):
            reason = (
                "non_numeric_or_missing_value"
                if unique and len(numeric) != len(unique)
                else "insufficient_points"
            )
            return (
                NumericTrend(
                    metric_id=definition.metric_id,
                    metric_version=definition.version,
                    status=TrendCalculationStatus.INSUFFICIENT_DATA,
                    unit=definition.unit,
                    unit_system=definition.unit_system,
                    point_count=len(unique),
                    period_start=period_start,
                    period_end=as_of,
                    algorithm_version=definition.algorithm_version,
                    evidence_refs=[
                        _evidence(item, role=EvidenceRole.SUPPORTING) for item in unique
                    ],
                    reason_codes=[reason],
                ),
                unique,
            )
        ordered = sorted(numeric, key=_point_sort_key)
        first = cast(Decimal, ordered[0].value.numeric)
        last = cast(Decimal, ordered[-1].value.numeric)
        delta = last - first
        direction = (
            TrendDirection.INCREASING
            if delta > 0
            else TrendDirection.DECREASING
            if delta < 0
            else TrendDirection.UNCHANGED
        )
        value = ordered[-1].value
        return (
            NumericTrend(
                metric_id=definition.metric_id,
                metric_version=definition.version,
                status=TrendCalculationStatus.CALCULATED,
                direction=direction,
                first_value=_decimal_text(first),
                last_value=_decimal_text(last),
                delta=_decimal_text(delta),
                unit=value.unit,
                unit_system=value.unit_system,
                point_count=len(ordered),
                period_start=period_start,
                period_end=as_of,
                algorithm_version=definition.algorithm_version,
                evidence_refs=[
                    _evidence(item, role=EvidenceRole.SUPPORTING) for item in ordered
                ],
            ),
            ordered,
        )

    @staticmethod
    def _unit_matches(value: _Value, definition: StateMetricDefinition) -> bool:
        if definition.unit is None:
            return True
        if value.kind != "quantity" or value.unit != definition.unit:
            return False
        return (
            definition.unit_system is None
            or value.unit_system == definition.unit_system
        )

    def _same_projection(
        self,
        persisted: ClinicalStateSnapshot,
        *,
        states: list[MetricState],
        trends: list[NumericTrend],
        source_refs: list[ResourceReference],
        definition_refs: list[ResourceReference],
        definitions: list[StateMetricDefinition],
    ) -> bool:
        return (
            persisted.states == states
            and persisted.trends == trends
            and persisted.metric_definitions == definitions
            and persisted.source_observation_refs == source_refs
            and persisted.metric_definition_refs == definition_refs
            and persisted.algorithm_version == self.ALGORITHM_VERSION
        )

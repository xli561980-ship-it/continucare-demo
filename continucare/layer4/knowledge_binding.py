"""Deterministic adapter from published L1 metrics to Layer-4 state contracts.

The L1 release defines what a metric means and how it is represented.  Layer 4
still needs an explicit, non-clinical data-recency policy before it can decide
whether a value is current or stale.  Keeping that policy as a required input
prevents this adapter from inventing clinical windows or hard-coding one
particular knowledge release.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from continucare.knowledge.models import KnowledgeRelease
from continucare.layer4.contracts import StateMetricDefinition


@dataclass(frozen=True)
class StateWindowPolicy:
    """Engineering recency windows for one declared L1 ``time_window``."""

    lookback_hours: int
    stale_after_hours: int
    trend_window_hours: int
    minimum_trend_points: int = 2

    def __post_init__(self) -> None:
        if min(
            self.lookback_hours,
            self.stale_after_hours,
            self.trend_window_hours,
        ) <= 0:
            raise ValueError("state window hours must be positive")
        if self.stale_after_hours > self.lookback_hours:
            raise ValueError("stale_after_hours cannot exceed lookback_hours")
        if self.minimum_trend_points < 2:
            raise ValueError("minimum_trend_points must be at least two")


@dataclass(frozen=True)
class L1StateMetricBinding:
    """Adapted definitions plus runtime metrics that fail closed without a code."""

    definitions: tuple[StateMetricDefinition, ...]
    skipped_runtime_metric_ids: tuple[str, ...]


def bind_l1_state_metric_definitions(
    release: KnowledgeRelease,
    *,
    pathway_code: str,
    pathway_version: str,
    window_policies: Mapping[str, StateWindowPolicy],
) -> L1StateMetricBinding:
    """Adapt governed runtime metrics into replayable Layer-4 definitions.

    Only runtime metrics with a published Observation code can be matched to a
    FHIR Observation.  A runtime metric without such a code is reported as
    skipped instead of guessing a code.  Unknown recency policies and ambiguous
    unit sets fail the whole binding operation.
    """

    if not pathway_code.strip() or not pathway_version.strip():
        raise ValueError("pathway code and version are required for L4 binding")

    definitions: list[StateMetricDefinition] = []
    skipped: list[str] = []
    for metric in sorted(release.metrics, key=lambda item: item.metric_id):
        if not metric.runtime_eligible:
            continue
        if metric.clinical_interpretation_allowed:
            raise ValueError(
                f"runtime metric {metric.metric_id} allows clinical interpretation"
            )
        if metric.observation_code is None:
            skipped.append(metric.metric_id)
            continue
        policy = window_policies.get(metric.time_window)
        if policy is None:
            raise ValueError(
                f"runtime metric {metric.metric_id} has no L4 window policy for "
                f"{metric.time_window!r}"
            )
        if len(metric.allowed_units) > 1:
            raise ValueError(
                f"runtime metric {metric.metric_id} has an ambiguous L4 unit set"
            )
        unit = metric.allowed_units[0] if metric.allowed_units else None
        definitions.append(
            StateMetricDefinition(
                metric_id=metric.metric_id,
                version=release.manifest.release_id,
                pathway_code=pathway_code,
                pathway_version=pathway_version,
                code_system=metric.observation_code.system,
                code=metric.observation_code.code,
                display=metric.display_zh,
                unit=unit.code if unit else None,
                unit_system=unit.system if unit else None,
                lookback_hours=policy.lookback_hours,
                stale_after_hours=policy.stale_after_hours,
                trend_window_hours=policy.trend_window_hours,
                minimum_trend_points=policy.minimum_trend_points,
            )
        )

    if not definitions:
        raise ValueError("L1 release has no runtime metric that Layer 4 can bind")
    return L1StateMetricBinding(
        definitions=tuple(definitions),
        skipped_runtime_metric_ids=tuple(skipped),
    )

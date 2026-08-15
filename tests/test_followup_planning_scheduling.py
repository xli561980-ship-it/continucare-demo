from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from continucare.followup_planning import (
    FieldOrigin,
    FieldProvenance,
    PatientFollowupItem,
    ResolvedSchedule,
    ResponseType,
    ScheduleBasis,
    ScheduleCalculationError,
    ScheduleValues,
    calculate_patient_burden,
    expand_scheduled_items,
    preview_schedule,
)
from continucare.followup_planning.models import ScheduleFieldProvenance


NOW = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)


def _schedule(
    *,
    interval_days=2,
    times=(time(9), time(18)),
    duration_days=5,
    timezone_name="Europe/Berlin",
):
    provenance = {
        name: FieldProvenance(
            field_path=f"schedule.{name}",
            origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
            source_reference="Practitioner/synthetic-doctor",
        )
        for name in ("interval_days", "times_of_day", "duration_days", "timezone")
    }
    return ResolvedSchedule(
        values=ScheduleValues(
            interval_days=interval_days,
            times_of_day=times,
            duration_days=duration_days,
            timezone=timezone_name,
        ),
        basis=ScheduleBasis.DOCTOR_MANUAL,
        field_provenance=ScheduleFieldProvenance(**provenance),
    )


def _item(item_id="item-synthetic-1", *, schedule=None, start=date(2026, 8, 16)):
    definition_fields = {
        "monitoring_definition_ref",
        "evidence_snapshot_refs",
        "prompt",
        "response_type",
        "unit",
        "options",
    }
    doctor_fields = {
        "source_candidate_ids",
        "display_order",
        "review_role",
        "include_in_summary",
        "status",
        "series_id",
        "effective_start",
        "effective_end",
        "change_reason",
    }
    return PatientFollowupItem(
        item_id=item_id,
        item_version=1,
        plan_version_id="plan-synthetic-1:v1",
        source_candidate_ids=("candidate-synthetic-1",),
        prompt="请回答合成问题。",
        response_type=ResponseType.BOOLEAN,
        schedule=schedule,
        display_order=0 if item_id.endswith("1") else 1,
        series_id=f"series-{item_id}",
        effective_start=start,
        change_reason="合成测试",
        field_provenance=tuple(
            FieldProvenance(
                field_path=field_name,
                origin=FieldOrigin.DOCTOR_CURRENT_SETTING,
                source_reference="Practitioner/synthetic-doctor",
            )
            for field_name in sorted(definition_fields | doctor_fields)
        ),
        created_by="Practitioner/synthetic-doctor",
        created_at=NOW,
    )


def test_interval_and_duration_use_half_open_local_calendar_semantics():
    occurrences = expand_scheduled_items((_item(schedule=_schedule()),))

    assert [value.local_date for value in occurrences] == [
        date(2026, 8, 16),
        date(2026, 8, 16),
        date(2026, 8, 18),
        date(2026, 8, 18),
        date(2026, 8, 20),
        date(2026, 8, 20),
    ]
    assert all(value.scheduled_at.tzinfo is not None for value in occurrences)


def test_duration_one_includes_only_the_start_date():
    occurrences = expand_scheduled_items(
        (_item(schedule=_schedule(interval_days=4, times=(time(9),), duration_days=1)),)
    )
    assert len(occurrences) == 1
    assert occurrences[0].local_date == date(2026, 8, 16)


def test_burden_keeps_sessions_items_and_busiest_day_separate():
    schedule = _schedule()
    items = (
        _item("item-synthetic-1", schedule=schedule),
        _item("item-synthetic-2", schedule=schedule),
    )

    burden = calculate_patient_burden(items)

    assert burden.scheduled_sessions == 6
    assert burden.total_items == 12
    assert burden.busiest_day_items == 4


def test_fourteen_day_preview_is_deterministic_and_half_open():
    item = _item(
        schedule=_schedule(
            interval_days=3,
            times=(time(9),),
            duration_days=30,
        )
    )
    first = preview_schedule((item,), window_start=date(2026, 8, 16))
    second = preview_schedule((item,), window_start=date(2026, 8, 16))

    assert first == second
    assert first.window_end_exclusive == date(2026, 8, 30)
    assert [session.local_date for session in first.sessions] == [
        date(2026, 8, 16),
        date(2026, 8, 19),
        date(2026, 8, 22),
        date(2026, 8, 25),
        date(2026, 8, 28),
    ]


def test_duration_change_recomputes_total_items_without_changing_sessions_meaning():
    short = _item(
        schedule=_schedule(interval_days=2, times=(time(9),), duration_days=3)
    )
    long = _item(
        schedule=_schedule(interval_days=2, times=(time(9),), duration_days=7)
    )

    assert calculate_patient_burden((short,)).total_items == 2
    assert calculate_patient_burden((long,)).total_items == 4


def test_effective_end_stops_future_occurrences_without_deleting_history():
    item = _item(schedule=_schedule(interval_days=1, times=(time(9),), duration_days=5))
    values = item.model_dump(mode="python")
    values.update(status="stopped", effective_end=date(2026, 8, 18))
    stopped = PatientFollowupItem.model_validate(values)

    assert [value.local_date for value in expand_scheduled_items((stopped,))] == [
        date(2026, 8, 16),
        date(2026, 8, 17),
    ]


def test_dst_ambiguous_time_is_stable_and_nonexistent_time_is_rejected():
    ambiguous = _item(
        schedule=_schedule(
            interval_days=1,
            times=(time(2, 30),),
            duration_days=1,
        ),
        start=date(2026, 10, 25),
    )
    occurrence = expand_scheduled_items((ambiguous,))[0]
    assert occurrence.scheduled_at.utcoffset() == timedelta(hours=2)

    nonexistent = _item(
        schedule=_schedule(
            interval_days=1,
            times=(time(2, 30),),
            duration_days=1,
        ),
        start=date(2026, 3, 29),
    )
    with pytest.raises(ScheduleCalculationError, match="not a valid local time"):
        expand_scheduled_items((nonexistent,))


def test_missing_schedule_produces_no_hidden_occurrences():
    item = _item(schedule=None)
    assert expand_scheduled_items((item,)) == ()
    assert calculate_patient_burden((item,)).model_dump() == {
        "scheduled_sessions": 0,
        "total_items": 0,
        "busiest_day_items": 0,
    }

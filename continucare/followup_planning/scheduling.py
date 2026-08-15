"""Deterministic calendar expansion and patient-burden calculations."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from continucare.followup_planning.models import (
    PatientBurden,
    PatientFollowupItem,
    SchedulePreview,
    ScheduledItem,
    ScheduledSession,
)


class ScheduleCalculationError(ValueError):
    pass


def _scheduled_datetime(local_date: date, local_time, timezone_name: str) -> datetime:
    """Resolve local wall time, choosing fold=0 and rejecting nonexistent times."""

    zone = ZoneInfo(timezone_name)
    candidate = datetime.combine(local_date, local_time, tzinfo=zone).replace(fold=0)
    round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
    if (
        round_trip.date() != local_date
        or round_trip.time().replace(tzinfo=None) != local_time
    ):
        raise ScheduleCalculationError(
            f"{local_date.isoformat()} {local_time.isoformat()} is not a valid local "
            f"time in {timezone_name}"
        )
    return candidate


def expand_scheduled_items(
    items: tuple[PatientFollowupItem, ...] | list[PatientFollowupItem],
) -> tuple[ScheduledItem, ...]:
    """Expand every effective item over its half-open local calendar window.

    Active schedule dates are ``effective_start + n * interval_days`` while the
    offset is strictly less than ``duration_days``. An item's ``effective_end``
    is also exclusive, so stopping an item never deletes earlier occurrences.
    """

    expanded: list[ScheduledItem] = []
    for item in items:
        schedule = item.schedule
        if schedule is None:
            continue
        values = schedule.values
        offset = 0
        while offset < values.duration_days:
            local_date = item.effective_start + timedelta(days=offset)
            if item.effective_end is not None and local_date >= item.effective_end:
                break
            for local_time in values.times_of_day:
                expanded.append(
                    ScheduledItem(
                        item_id=item.item_id,
                        item_version=item.item_version,
                        local_date=local_date,
                        local_time=local_time,
                        timezone=values.timezone,
                        scheduled_at=_scheduled_datetime(
                            local_date, local_time, values.timezone
                        ),
                    )
                )
            offset += values.interval_days
    return tuple(
        sorted(
            expanded,
            key=lambda value: (
                value.scheduled_at.astimezone(timezone.utc),
                value.item_id,
                value.item_version,
            ),
        )
    )


def preview_schedule(
    items: tuple[PatientFollowupItem, ...] | list[PatientFollowupItem],
    *,
    window_start: date,
    days: int = 14,
) -> SchedulePreview:
    if days < 1:
        raise ValueError("preview days must be at least one")
    window_end = window_start + timedelta(days=days)
    grouped: dict[tuple[date, object, str], list[ScheduledItem]] = defaultdict(list)
    for item in expand_scheduled_items(items):
        if window_start <= item.local_date < window_end:
            grouped[(item.local_date, item.local_time, item.timezone)].append(item)
    sessions = tuple(
        ScheduledSession(
            local_date=key[0],
            local_time=key[1],
            timezone=key[2],
            items=tuple(
                sorted(grouped[key], key=lambda value: (value.item_id, value.item_version))
            ),
        )
        for key in sorted(grouped, key=lambda value: (value[0], value[1], value[2]))
    )
    return SchedulePreview(
        window_start=window_start,
        window_end_exclusive=window_end,
        sessions=sessions,
    )


def calculate_patient_burden(
    items: tuple[PatientFollowupItem, ...] | list[PatientFollowupItem],
) -> PatientBurden:
    """Keep sessions, item completions, and busiest-day items separate."""

    occurrences = expand_scheduled_items(items)
    session_keys = {
        (item.local_date, item.local_time, item.timezone) for item in occurrences
    }
    items_by_day: dict[date, int] = defaultdict(int)
    for item in occurrences:
        items_by_day[item.local_date] += 1
    return PatientBurden(
        scheduled_sessions=len(session_keys),
        total_items=len(occurrences),
        busiest_day_items=max(items_by_day.values(), default=0),
    )

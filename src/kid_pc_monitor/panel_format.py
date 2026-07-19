"""Human-friendly display formatting for the parent web panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


def format_minutes_duration(minutes: int | float | None) -> str:
    """Format a minute count as H:MM or 'Not set'."""
    if minutes is None:
        return "Not set"
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours}:{mins:02d}"
    return f"{mins} min"


def format_seconds_duration(seconds: int | float) -> str:
    return format_minutes_duration(seconds / 60)


def _format_compact_clock_time(dt: datetime) -> str:
    """Format a datetime as a compact 12-hour clock string, e.g. 2:16pm."""
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{suffix}"


def format_snapshot_recorded_at(
    recorded_at: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Format a snapshot ISO timestamp for parent-facing UI."""
    if not recorded_at:
        return ""
    try:
        dt = datetime.fromisoformat(recorded_at)
    except ValueError:
        return recorded_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    local_dt = dt.astimezone()
    reference = (now or datetime.now().astimezone()).astimezone()
    snapshot_day = local_dt.date()
    today = reference.date()
    time_str = _format_compact_clock_time(local_dt)
    if snapshot_day == today:
        return time_str
    if snapshot_day == today - timedelta(days=1):
        return f"{time_str} yesterday"
    return f"{time_str} {local_dt.strftime('%b')} {local_dt.day}"


@dataclass(frozen=True)
class UsageBar:
    """A single day's bar in the usage-history chart."""

    label: str  # short day label, e.g. "Mon 14"
    used_minutes: int
    allowance_minutes: int | None
    height_pct: int  # bar height as a percentage of the chart scale (0..100)
    limit_pct: int | None  # allowance marker as a percentage of the chart scale
    over_limit: bool
    no_data: bool


@dataclass(frozen=True)
class UsageChart:
    scale_minutes: int
    bars: list[UsageBar]
    has_data: bool


def _short_day_label(date_iso: str) -> str:
    try:
        parsed = date.fromisoformat(date_iso)
    except ValueError:
        return date_iso
    return f"{parsed.strftime('%a')} {parsed.day}"


def usage_chart(days: list[dict[str, Any]], *, scale_floor_minutes: int = 60) -> UsageChart:
    """Build bar-chart data from usage-history day entries.

    Every bar is scaled to one common maximum — the largest of any day's usage
    or allowance, with a floor so a near-empty week is not drawn full height —
    so the bars are visually comparable. Days flagged ``no_data`` render as an
    empty slot. ``over_limit`` is set when usage exceeded the allowance plus any
    extension granted that day.
    """
    used: list[int | None] = []
    allowances: list[int | None] = []
    for day in days:
        if day.get("no_data"):
            used.append(None)
            allowances.append(None)
            continue
        used.append(round(int(day.get("accumulated_seconds") or 0) / 60))
        limit = day.get("daily_limit")
        allowances.append(int(limit) if limit is not None else None)

    scale = max(
        [scale_floor_minutes]
        + [value for value in used if value is not None]
        + [value for value in allowances if value is not None]
    )

    def pct(value: int) -> int:
        return max(0, min(100, round(value / scale * 100)))

    bars: list[UsageBar] = []
    has_data = False
    for day, used_min, limit_min in zip(days, used, allowances, strict=True):
        label = _short_day_label(str(day.get("date", "")))
        if used_min is None:
            bars.append(UsageBar(label, 0, None, 0, None, False, no_data=True))
            continue
        has_data = True
        ext_min = round(int(day.get("cumulative_extension_seconds") or 0) / 60)
        effective_limit = None if limit_min is None else limit_min + ext_min
        over = effective_limit is not None and used_min > effective_limit
        bars.append(
            UsageBar(
                label=label,
                used_minutes=used_min,
                allowance_minutes=limit_min,
                height_pct=pct(used_min),
                limit_pct=None if limit_min is None else pct(limit_min),
                over_limit=over,
                no_data=False,
            )
        )
    return UsageChart(scale_minutes=scale, bars=bars, has_data=has_data)

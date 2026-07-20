"""Pure lock policy helpers for the Kid PC Monitor agent.

This module intentionally has no Windows, tkinter, socket, or filesystem
dependencies so it can be unit-tested on any development machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime

# Default morning unlock when wake_time is not configured (e.g. legacy state files).
DEFAULT_WAKE_TIME = dtime(7, 0)


@dataclass(frozen=True)
class LockDecision:
    should_lock: bool
    reason: str = ""


def should_monitor_user(
    current_user: str,
    monitored_users: list[str] | tuple[str, ...] | None = None,
    exempt_users: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Return whether restrictions apply to current_user."""
    monitored_users = monitored_users or []
    exempt_users = exempt_users or []

    if monitored_users:
        return current_user in monitored_users

    if exempt_users:
        return current_user not in exempt_users

    return True


def parse_time_hhmm(value: str) -> dtime:
    """Parse HH:MM or H:MM into a time; raises ValueError if invalid."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time '{value}'; use HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time '{value}'; hour/minute out of range")
    return dtime(hour, minute)


def _minutes_since_midnight(when: dtime) -> int:
    return when.hour * 60 + when.minute


def usage_period_date(now: datetime, wake_time: dtime = DEFAULT_WAKE_TIME) -> date:
    """
    Label for the current daily usage period.

    The period starts at wake_time each day (not midnight), so early-morning
    use before wake still counts toward the previous day's allowance.
    """
    if (now.hour, now.minute) < (wake_time.hour, wake_time.minute):
        return now.date() - timedelta(days=1)
    return now.date()


def is_weekend_period(now: datetime, wake_time: dtime = DEFAULT_WAKE_TIME) -> bool:
    """True when the current wake-to-wake usage period is a Saturday or Sunday."""
    return usage_period_date(now, wake_time).weekday() >= 5


def is_in_bedtime_curfew(
    now: datetime,
    bed_time: dtime,
    wake_time: dtime = DEFAULT_WAKE_TIME,
) -> bool:
    """
    True when now is in the overnight curfew from bed_time until wake_time.

    Typical case: bedtime 21:00, wake 07:00 — locked from 21:00 through 06:59.
    """
    now_m = now.hour * 60 + now.minute
    bed_m = _minutes_since_midnight(bed_time)
    wake_m = _minutes_since_midnight(wake_time)
    if bed_m == wake_m:
        return False
    if bed_m > wake_m:
        return now_m >= bed_m or now_m < wake_m
    return bed_m <= now_m < wake_m


def minutes_until_bedtime(now: datetime, bed_time: dtime | None) -> float | None:
    """Minutes from now until the next occurrence of bed_time (None if unset)."""
    if bed_time is None:
        return None
    bed_datetime = now.replace(
        hour=bed_time.hour,
        minute=bed_time.minute,
        second=0,
        microsecond=0,
    )
    if bed_datetime <= now:
        bed_datetime = bed_datetime + timedelta(days=1)
    return (bed_datetime - now).total_seconds() / 60


def lock_decision(
    *,
    now: datetime,
    bed_time: dtime | None,
    effective_usage_allowance_minutes: float | None,
    accumulated_minutes: float,
    monitor_user: bool = True,
    manual_lock_active: bool = False,
    wake_time: dtime = DEFAULT_WAKE_TIME,
) -> LockDecision:
    """
    Decide whether the agent should enforce a lock at now.

    bed_time starts a curfew that lasts until wake_time (not midnight).
    Usage allowances lock once accumulated_minutes for the current wake-to-wake period
    reaches effective_usage_allowance_minutes (daily allowance plus any extensions).
    """
    if not monitor_user:
        return LockDecision(False)

    if manual_lock_active:
        return LockDecision(True, "Manual lock requested")

    if bed_time is not None and is_in_bedtime_curfew(now, bed_time, wake_time):
        if (now.hour, now.minute) < (wake_time.hour, wake_time.minute):
            return LockDecision(
                True,
                f"Before wake-up time {wake_time.hour:02d}:{wake_time.minute:02d}",
            )
        return LockDecision(
            True,
            f"Past bedtime {bed_time.hour:02d}:{bed_time.minute:02d}",
        )

    if (
        effective_usage_allowance_minutes is not None
        and accumulated_minutes >= effective_usage_allowance_minutes
    ):
        allowance_label = int(effective_usage_allowance_minutes)
        if effective_usage_allowance_minutes != allowance_label:
            allowance_label = round(effective_usage_allowance_minutes, 1)
        return LockDecision(
            True,
            f"Daily allowance of {allowance_label} minutes reached",
        )

    return LockDecision(False)


def minutes_until_lock(
    *,
    now: datetime,
    bed_time: dtime | None,
    effective_usage_allowance_minutes: float | None,
    accumulated_minutes: float,
    monitor_user: bool = True,
    manual_lock_active: bool = False,
    wake_time: dtime = DEFAULT_WAKE_TIME,
) -> float | None:
    """Return minutes until the next lock, 0 if already locked, or None."""
    if not monitor_user:
        return None

    if lock_decision(
        now=now,
        bed_time=bed_time,
        effective_usage_allowance_minutes=effective_usage_allowance_minutes,
        accumulated_minutes=accumulated_minutes,
        monitor_user=monitor_user,
        manual_lock_active=manual_lock_active,
        wake_time=wake_time,
    ).should_lock:
        return 0

    min_remaining = None

    if bed_time is not None:
        min_remaining = minutes_until_bedtime(now, bed_time)

    if effective_usage_allowance_minutes is not None:
        minutes_remaining = effective_usage_allowance_minutes - accumulated_minutes
        if min_remaining is None or minutes_remaining < min_remaining:
            min_remaining = minutes_remaining

    return min_remaining


def brief_enforcement_reason(full_reason: str) -> str:
    """Shorten a lock_decision reason for protocol/UI display."""
    lower = full_reason.lower()
    if "before wake-up" in lower:
        return "before wake-up"
    if "past bedtime" in lower:
        return "past bedtime"
    if "daily allowance" in lower:
        return "daily limit reached"
    return full_reason


def enforcement_state(
    *,
    now: datetime,
    bed_time: dtime | None,
    effective_usage_allowance_minutes: float | None,
    accumulated_minutes: float,
    monitor_user: bool = True,
    wake_time: dtime = DEFAULT_WAKE_TIME,
) -> tuple[bool, str | None]:
    """Return schedule/limit enforcement without considering manual lock."""
    decision = lock_decision(
        now=now,
        bed_time=bed_time,
        effective_usage_allowance_minutes=effective_usage_allowance_minutes,
        accumulated_minutes=accumulated_minutes,
        monitor_user=monitor_user,
        manual_lock_active=False,
        wake_time=wake_time,
    )
    if not decision.should_lock:
        return False, None
    return True, brief_enforcement_reason(decision.reason)


def format_access_status(
    *,
    manual_lock: bool,
    enforcement_active: bool,
    enforcement_reason: str | None,
    screen_locked: bool,
) -> str:
    """Compose a brief access status for the parent panel."""
    if manual_lock and not enforcement_active:
        return "Locked — manual lock"
    if enforcement_active:
        reason = enforcement_reason or "enforcement active"
        return f"Locked — {reason}"
    if screen_locked:
        return "Screen locked"
    return "Unlocked"

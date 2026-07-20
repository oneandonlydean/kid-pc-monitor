"""Persistent daily settings and runtime state for the Kid PC Monitor agent."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

from kid_pc_monitor.earn_quiz import normalize_difficulty
from kid_pc_monitor.lock_policy import DEFAULT_WAKE_TIME, parse_time_hhmm, usage_period_date

DEFAULT_VALUES_FILE = "daily_settings.json"
STATE_FILE = "state.json"
LEGACY_STATE_FILE = "pc_control_state.json"
LEGACY_INSTALL_CONFIG = "install_config.json"

logger = logging.getLogger(__name__)


@dataclass
class DailySettings:
    bed_time: dtime | None
    wake_time: dtime
    allowance: int | None  # minutes; None = no screen-time cap
    show_timer: bool = True  # whether the kid's on-screen countdown overlay is shown
    break_interval_minutes: int = 0  # active minutes between forced breaks; 0 = off
    break_duration_minutes: int = 5  # how long each forced break lock lasts
    # Optional separate schedule for Saturday/Sunday; used only when enabled.
    weekend_enabled: bool = False
    weekend_bed_time: dtime | None = None
    weekend_allowance: int | None = None
    # "Earn time" quiz: kid answers questions to earn extra minutes.
    earn_enabled: bool = False
    earn_reward_minutes: int = 5  # minutes granted per correct answer
    earn_questions: int = 5  # questions per quiz session
    earn_daily_cap_minutes: int = 30  # most minutes earnable per day
    earn_spelling_difficulty: str = "medium"  # easy | medium | hard
    earn_maths_difficulty: str = "medium"  # easy | medium | hard
    # Carry-over: unused daily allowance rolls into a bank, capped at N days' worth.
    carryover_enabled: bool = False
    carryover_max_days: int = 3


@dataclass
class RuntimeState:
    timestamp: datetime
    accumulated_seconds: float
    manual_lock_active: bool
    cumulative_extension_seconds: int
    # When the kid last asked for more time (None once granted/dismissed/reset).
    time_request_at: datetime | None = None
    # When the current forced break ends (None when not on a break).
    break_active_until: datetime | None = None
    # accumulated_seconds value at the last break end; usage past it drives the next break.
    break_baseline_seconds: float = 0.0
    # Minutes-worth of time earned via the spelling quiz today (for the daily cap).
    earned_today_seconds: int = 0
    # Banked unused allowance carried over from previous days (persists across days).
    carryover_seconds: int = 0


def _format_time(value: dtime) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _parse_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def effective_daily_allowance_minutes(daily: DailySettings, runtime: RuntimeState) -> float | None:
    """Return the enforced cap in minutes, or None when there is no usage cap."""
    extension_minutes = runtime.cumulative_extension_seconds / 60
    if daily.allowance is None:
        if extension_minutes <= 0:
            return None
        return extension_minutes
    return daily.allowance + extension_minutes


def runtime_state_is_current(
    runtime: RuntimeState,
    wake_time: dtime,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now()
    return usage_period_date(runtime.timestamp, wake_time) == usage_period_date(now, wake_time)


def fresh_runtime_state(now: datetime | None = None) -> RuntimeState:
    now = now or datetime.now()
    return RuntimeState(
        timestamp=now,
        accumulated_seconds=0.0,
        manual_lock_active=False,
        cumulative_extension_seconds=0,
    )


def reset_runtime_for_new_period(runtime: RuntimeState, now: datetime | None = None) -> None:
    now = now or datetime.now()
    runtime.timestamp = now
    runtime.accumulated_seconds = 0.0
    runtime.manual_lock_active = False
    runtime.cumulative_extension_seconds = 0
    runtime.time_request_at = None
    runtime.break_active_until = None
    runtime.break_baseline_seconds = 0.0
    runtime.earned_today_seconds = 0


def reset_runtime_if_needed(
    runtime: RuntimeState,
    wake_time: dtime,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now()
    if runtime_state_is_current(runtime, wake_time, now):
        return False
    reset_runtime_for_new_period(runtime, now)
    return True


def compute_carryover_seconds(daily: DailySettings, runtime: RuntimeState, now: datetime) -> int:
    """The new carry-over bank when the usage period ending at ``now`` rolls over.

    Unused allowance (the base, the existing bank, and any quiz-earned minutes,
    minus what was used) rolls into the bank, capped at ``carryover_max_days``
    days of the base allowance. Earned minutes never extend past bedtime, so
    banking whatever the kid could not spend today is how the reward survives to
    the next day. Fully-idle days (agent off across a day boundary) each credit a
    base allowance. Parent time extensions are one-day grants and never bank.
    When carry-over is off, or there is no daily cap, the bank is left unchanged.
    """
    if not daily.carryover_enabled or daily.allowance is None:
        return runtime.carryover_seconds
    base = daily.allowance
    carry_minutes = runtime.carryover_seconds / 60
    earned_minutes = runtime.earned_today_seconds / 60
    used_minutes = runtime.accumulated_seconds / 60
    leftover = max(0.0, (base + carry_minutes + earned_minutes) - used_minutes)
    previous = usage_period_date(runtime.timestamp, daily.wake_time)
    current = usage_period_date(now, daily.wake_time)
    idle_days = max(0, (current - previous).days - 1)
    leftover += idle_days * base
    cap_minutes = daily.carryover_max_days * base
    return int(min(leftover, cap_minutes) * 60)


def roll_over_if_needed(
    daily: DailySettings,
    runtime: RuntimeState,
    now: datetime | None = None,
) -> bool:
    """Bank carry-over and reset the runtime when a new usage period has started."""
    now = now or datetime.now()
    if runtime_state_is_current(runtime, daily.wake_time, now):
        return False
    new_carryover = compute_carryover_seconds(daily, runtime, now)
    reset_runtime_for_new_period(runtime, now)
    runtime.carryover_seconds = new_carryover
    return True


def daily_to_dict(daily: DailySettings) -> dict:
    payload: dict = {
        "wake_time": _format_time(daily.wake_time),
        "allowance": daily.allowance,
        "show_timer": daily.show_timer,
        "break_interval_minutes": daily.break_interval_minutes,
        "break_duration_minutes": daily.break_duration_minutes,
        "weekend_enabled": daily.weekend_enabled,
        "weekend_bed_time": (
            _format_time(daily.weekend_bed_time) if daily.weekend_bed_time is not None else None
        ),
        "weekend_allowance": daily.weekend_allowance,
        "earn_enabled": daily.earn_enabled,
        "earn_reward_minutes": daily.earn_reward_minutes,
        "earn_questions": daily.earn_questions,
        "earn_daily_cap_minutes": daily.earn_daily_cap_minutes,
        "earn_spelling_difficulty": daily.earn_spelling_difficulty,
        "earn_maths_difficulty": daily.earn_maths_difficulty,
        "carryover_enabled": daily.carryover_enabled,
        "carryover_max_days": daily.carryover_max_days,
    }
    if daily.bed_time is not None:
        payload["bed_time"] = _format_time(daily.bed_time)
    else:
        payload["bed_time"] = None
    return payload


def runtime_to_dict(runtime: RuntimeState) -> dict:
    return {
        "timestamp": runtime.timestamp.isoformat(timespec="seconds"),
        "accumulated_seconds": round(runtime.accumulated_seconds, 3),
        "manual_lock_active": runtime.manual_lock_active,
        "cumulative_extension_seconds": runtime.cumulative_extension_seconds,
        "time_request_at": (
            runtime.time_request_at.isoformat(timespec="seconds")
            if runtime.time_request_at is not None
            else None
        ),
        "break_active_until": (
            runtime.break_active_until.isoformat(timespec="seconds")
            if runtime.break_active_until is not None
            else None
        ),
        "break_baseline_seconds": round(runtime.break_baseline_seconds, 3),
        "earned_today_seconds": int(runtime.earned_today_seconds),
        "carryover_seconds": int(runtime.carryover_seconds),
    }


def load_daily_from_dict(data: dict) -> DailySettings:
    wake_raw = data.get("wake_time", _format_time(DEFAULT_WAKE_TIME))
    wake_time = parse_time_hhmm(str(wake_raw))

    bed_time: dtime | None
    bed_raw = data.get("bed_time")
    if bed_raw is None or bed_raw == "":
        bed_time = None
    else:
        bed_time = parse_time_hhmm(str(bed_raw))

    allowance = data.get("allowance")
    if allowance is not None:
        allowance = int(allowance)

    weekend_bed_raw = data.get("weekend_bed_time")
    if weekend_bed_raw is None or weekend_bed_raw == "":
        weekend_bed_time = None
    else:
        weekend_bed_time = parse_time_hhmm(str(weekend_bed_raw))
    weekend_allowance = data.get("weekend_allowance")
    if weekend_allowance is not None:
        weekend_allowance = int(weekend_allowance)

    return DailySettings(
        bed_time=bed_time,
        wake_time=wake_time,
        allowance=allowance,
        show_timer=bool(data.get("show_timer", True)),
        break_interval_minutes=int(data.get("break_interval_minutes", 0) or 0),
        break_duration_minutes=int(data.get("break_duration_minutes", 5) or 5),
        weekend_enabled=bool(data.get("weekend_enabled", False)),
        weekend_bed_time=weekend_bed_time,
        weekend_allowance=weekend_allowance,
        earn_enabled=bool(data.get("earn_enabled", False)),
        earn_reward_minutes=int(data.get("earn_reward_minutes", 5) or 5),
        earn_questions=int(data.get("earn_questions", 5) or 5),
        earn_daily_cap_minutes=int(data.get("earn_daily_cap_minutes", 30) or 30),
        earn_spelling_difficulty=normalize_difficulty(data.get("earn_spelling_difficulty")),
        earn_maths_difficulty=normalize_difficulty(data.get("earn_maths_difficulty")),
        carryover_enabled=bool(data.get("carryover_enabled", False)),
        carryover_max_days=int(data.get("carryover_max_days", 3) or 3),
    )


def is_complete_daily_dict(data: dict) -> bool:
    """True when wake_time, bed_time, and a positive allowance are all configured."""
    wake_raw = data.get("wake_time")
    bed_raw = data.get("bed_time")
    allowance_raw = data.get("allowance")
    if not wake_raw or not bed_raw or allowance_raw is None:
        return False
    try:
        allowance = int(allowance_raw)
        if allowance <= 0:
            return False
        daily = load_daily_from_dict(data)
    except (ValueError, TypeError):
        return False
    return daily.bed_time is not None and daily.allowance is not None and daily.allowance > 0


def find_complete_daily_settings(
    *,
    profile_path: Path | None,
    program_data_path: Path | None,
) -> tuple[DailySettings, Path] | None:
    """Return daily settings and source path when a complete schedule file exists."""
    for path in (profile_path, program_data_path):
        if path is None:
            continue
        data = _read_json(path)
        if data is None or not is_complete_daily_dict(data):
            continue
        try:
            return load_daily_from_dict(data), path
        except ValueError:
            continue
    return None


def load_runtime_from_dict(data: dict) -> RuntimeState:
    timestamp_raw = data.get("timestamp")
    if not isinstance(timestamp_raw, str):
        raise ValueError("state.json missing timestamp")
    request_raw = data.get("time_request_at")
    time_request_at = (
        _parse_timestamp(request_raw) if isinstance(request_raw, str) and request_raw else None
    )
    break_raw = data.get("break_active_until")
    break_active_until = (
        _parse_timestamp(break_raw) if isinstance(break_raw, str) and break_raw else None
    )
    return RuntimeState(
        timestamp=_parse_timestamp(timestamp_raw),
        accumulated_seconds=float(data.get("accumulated_seconds", 0.0)),
        manual_lock_active=bool(data.get("manual_lock_active", False)),
        cumulative_extension_seconds=int(data.get("cumulative_extension_seconds", 0)),
        time_request_at=time_request_at,
        break_active_until=break_active_until,
        break_baseline_seconds=float(data.get("break_baseline_seconds", 0.0)),
        earned_today_seconds=int(data.get("earned_today_seconds", 0) or 0),
        carryover_seconds=int(data.get("carryover_seconds", 0) or 0),
    )


def program_data_daily_path() -> Path | None:
    if sys.platform != "win32":
        return None
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    return Path(program_data) / "KidPCMonitor" / DEFAULT_VALUES_FILE


def program_data_legacy_install_config_path() -> Path | None:
    if sys.platform != "win32":
        return None
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    return Path(program_data) / "KidPCMonitor" / LEGACY_INSTALL_CONFIG


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None


def program_data_target_user() -> str | None:
    """Return the monitored account from ProgramData daily_settings.json, if set."""
    path = program_data_daily_path()
    if path is None:
        return None
    data = _read_json(path)
    if data is None:
        return None
    target = data.get("target_user")
    if isinstance(target, str) and target.strip():
        return target.strip()
    return None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _legacy_lock_times_to_bed_time(lock_times_raw: list | None) -> dtime | None:
    if not lock_times_raw:
        return None
    first = lock_times_raw[0]
    if isinstance(first, str) and ":" in first:
        return parse_time_hhmm(first)
    return None


def migrate_legacy_state(
    legacy_path: Path,
    *,
    current_user: str,
) -> tuple[DailySettings, RuntimeState] | None:
    legacy = _read_json(legacy_path)
    if legacy is None:
        return None

    wake_time = DEFAULT_WAKE_TIME
    if isinstance(legacy.get("wake_time"), str):
        wake_time = parse_time_hhmm(legacy["wake_time"])

    bed_time = _legacy_lock_times_to_bed_time(legacy.get("lock_times"))
    allowance = legacy.get("usage_allowance")
    if allowance is not None:
        allowance = int(allowance)

    daily = DailySettings(
        bed_time=bed_time,
        wake_time=wake_time,
        allowance=allowance,
    )

    now = datetime.now()
    accumulated_seconds = float(legacy.get("accumulated_seconds", 0.0))
    manual_lock_active = bool(legacy.get("manual_lock_active", False))

    runtime = RuntimeState(
        timestamp=now,
        accumulated_seconds=accumulated_seconds,
        manual_lock_active=manual_lock_active,
        cumulative_extension_seconds=0,
    )

    if not runtime_state_is_current(runtime, wake_time, now):
        reset_runtime_for_new_period(runtime, now)
    elif "accumulated_date" in legacy:
        from datetime import date as ddate

        saved_date = ddate.fromisoformat(legacy["accumulated_date"])
        if saved_date < usage_period_date(now, wake_time):
            reset_runtime_for_new_period(runtime, now)
        else:
            runtime.accumulated_seconds = accumulated_seconds

    logger.info(
        "Migrated legacy state for %s from %s",
        current_user,
        legacy_path,
    )
    return daily, runtime


def _bootstrap_daily_from_program_data(current_user: str) -> DailySettings | None:
    for path in (program_data_daily_path(),):
        if path is None:
            continue
        data = _read_json(path)
        if data is None:
            continue
        target_user = data.get("target_user")
        if isinstance(target_user, str) and target_user:
            if target_user.lower() != current_user.lower():
                continue
        try:
            return load_daily_from_dict(data)
        except ValueError as exc:
            logger.warning("Invalid program-data daily settings at %s: %s", path, exc)

    legacy_install = program_data_legacy_install_config_path()
    if legacy_install is not None:
        data = _read_json(legacy_install)
        if data is not None:
            target_user = data.get("target_user")
            if isinstance(target_user, str) and target_user:
                if target_user.lower() != current_user.lower():
                    return None
            wake_raw = data.get("wake_time")
            if isinstance(wake_raw, str):
                try:
                    wake_time = parse_time_hhmm(wake_raw)
                    return DailySettings(
                        bed_time=None,
                        wake_time=wake_time,
                        allowance=None,
                    )
                except ValueError:
                    pass
    return None


class AgentStateStore:
    """Read/write daily_settings.json and state.json under the agent data directory."""

    def __init__(self, data_directory: Path, *, current_user: str) -> None:
        self.data_directory = data_directory
        self.current_user = current_user
        self.daily_path = data_directory / DEFAULT_VALUES_FILE
        self.state_path = data_directory / STATE_FILE
        self.legacy_state_path = data_directory / LEGACY_STATE_FILE

    def load(self) -> tuple[DailySettings, RuntimeState]:
        daily = self._load_daily()
        runtime = self._load_runtime(daily.wake_time)
        if roll_over_if_needed(daily, runtime):
            logger.info("Runtime state is from a previous usage period; rolled the day over")
        return daily, runtime

    def save(self, daily: DailySettings, runtime: RuntimeState) -> None:
        runtime.timestamp = datetime.now()
        _write_json(self.daily_path, daily_to_dict(daily))
        _write_json(self.state_path, runtime_to_dict(runtime))

    def _load_daily(self) -> DailySettings:
        data = _read_json(self.daily_path)
        if data is not None:
            return load_daily_from_dict(data)

        migrated = migrate_legacy_state(self.legacy_state_path, current_user=self.current_user)
        if migrated is not None:
            daily, runtime = migrated
            self.save(daily, runtime)
            return daily

        bootstrapped = _bootstrap_daily_from_program_data(self.current_user)
        if bootstrapped is not None:
            runtime = fresh_runtime_state()
            self.save(bootstrapped, runtime)
            return bootstrapped

        daily = DailySettings(
            bed_time=None,
            wake_time=DEFAULT_WAKE_TIME,
            allowance=None,
        )
        runtime = fresh_runtime_state()
        self.save(daily, runtime)
        return daily

    def _load_runtime(self, wake_time: dtime) -> RuntimeState:
        data = _read_json(self.state_path)
        if data is not None:
            try:
                return load_runtime_from_dict(data)
            except ValueError as exc:
                logger.warning("Invalid %s: %s; starting fresh", self.state_path, exc)

        migrated = migrate_legacy_state(self.legacy_state_path, current_user=self.current_user)
        if migrated is not None:
            daily, runtime = migrated
            self.save(daily, runtime)
            return runtime

        return fresh_runtime_state()

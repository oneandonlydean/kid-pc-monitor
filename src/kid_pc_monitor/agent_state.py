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


@dataclass
class RuntimeState:
    timestamp: datetime
    accumulated_seconds: float
    manual_lock_active: bool
    cumulative_extension_seconds: int


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


def daily_to_dict(daily: DailySettings) -> dict:
    payload: dict = {
        "wake_time": _format_time(daily.wake_time),
        "allowance": daily.allowance,
        "show_timer": daily.show_timer,
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

    return DailySettings(
        bed_time=bed_time,
        wake_time=wake_time,
        allowance=allowance,
        show_timer=bool(data.get("show_timer", True)),
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
    return RuntimeState(
        timestamp=_parse_timestamp(timestamp_raw),
        accumulated_seconds=float(data.get("accumulated_seconds", 0.0)),
        manual_lock_active=bool(data.get("manual_lock_active", False)),
        cumulative_extension_seconds=int(data.get("cumulative_extension_seconds", 0)),
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
        if reset_runtime_if_needed(runtime, daily.wake_time):
            logger.info("Runtime state is from a previous usage period; resetting daily counters")
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

"""Daily time limits, bedtime enforcement, and parent actions for the kid agent."""

from __future__ import annotations

import getpass
import logging
import math
import os
import threading
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

from kid_pc_monitor.agent_overlay import overlay_state
from kid_pc_monitor.agent_state import (
    AgentStateStore,
    DailySettings,
    RuntimeState,
    effective_daily_allowance_minutes,
    reset_runtime_for_new_period,
    reset_runtime_if_needed,
)
from kid_pc_monitor.host_platform import HostPlatform, get_default_platform
from kid_pc_monitor.lock_policy import (
    DEFAULT_WAKE_TIME,
    enforcement_state,
    format_access_status,
    lock_decision,
    minutes_until_lock,
    should_monitor_user,
    usage_period_date,
)

# ============================================
# CONFIGURATION
# ============================================

# List of Windows usernames to monitor (leave empty to monitor all users)
# Example: MONITORED_USERS = ['Tommy', 'Sarah', 'kid1']
MONITORED_USERS: list[str] = []

# List of Windows usernames to EXEMPT from monitoring (parents/admins)
# Example: EXEMPT_USERS = ['pavel', 'Mom', 'Dad', 'Administrator']
EXEMPT_USERS: list[str] = []

# If both lists are empty, ALL users will be monitored
# If MONITORED_USERS has entries, ONLY those users are monitored
# If EXEMPT_USERS has entries, everyone EXCEPT those users is monitored

# Morning unlock for bedtime curfews and daily usage reset (HH:MM local time).
# Overridden by wake_time in pc_control_state.json (set at install).
DEFAULT_WAKE_UP_TIME = DEFAULT_WAKE_TIME

# Per-user data directory for agent state (and pc_control.log when used by the agent).
data_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KidPCMonitor"
data_dir.mkdir(parents=True, exist_ok=True)


class PCTimeControl:
    def __init__(
        self,
        platform: HostPlatform | None = None,
        *,
        monitored_users: list[str] | None = None,
        exempt_users: list[str] | None = None,
        data_directory: Path | None = None,
        start_background_threads: bool = True,
    ):
        self.platform = platform or get_default_platform()
        self.monitored_users = MONITORED_USERS if monitored_users is None else monitored_users
        self.exempt_users = EXEMPT_USERS if exempt_users is None else exempt_users

        self.daily = DailySettings(
            bed_time=None,
            wake_time=DEFAULT_WAKE_UP_TIME,
            allowance=None,
        )
        self.runtime = RuntimeState(
            timestamp=datetime.now(),
            accumulated_seconds=0.0,
            manual_lock_active=False,
            cumulative_extension_seconds=0,
        )
        # Wall-clock of the most recent observed active+unlocked tick, or None
        # if the previous tick was paused. Used to credit elapsed seconds
        # between consecutive active ticks while ignoring paused gaps.
        self.last_tick_at = None
        # Throttle for periodic save_state() calls inside run_monitor.
        self.last_persist_at = None
        self.is_locked = False
        self.last_activity = datetime.now()
        self.current_user = getpass.getuser()
        base_dir = data_directory or data_dir
        base_dir.mkdir(parents=True, exist_ok=True)
        self.agent_log_path = base_dir / "pc_control.log"
        self.state_store = AgentStateStore(base_dir, current_user=self.current_user)
        self.logger = logging.getLogger("PCTimeControl")
        self.warnings_sent = set()  # Track which warnings have been sent
        self.warning_intervals = [30, 15, 5, 1]  # Warning times in minutes before lock
        self.warnings_date = None

        # Log which user we're running as
        if self.should_monitor_user():
            self.logger.info(f"Monitoring enabled for user: {self.current_user}")
            print(f"[{datetime.now():%H:%M:%S}] Monitoring user: {self.current_user}")
        else:
            self.logger.info(f"User {self.current_user} is EXEMPT from monitoring")
            print(
                f"[{datetime.now():%H:%M:%S}] User {self.current_user} is EXEMPT - no restrictions will apply"
            )

        # Load previous state if exists
        self.load_state()
        self.warnings_date = usage_period_date(datetime.now(), self.daily.wake_time)

        if start_background_threads:
            self.monitor_thread = threading.Thread(target=self.monitor_activity, daemon=True)
            self.monitor_thread.start()
        else:
            self.monitor_thread = None

    def should_monitor_user(self):
        """Check if current user should be monitored based on configuration"""
        return should_monitor_user(self.current_user, self.monitored_users, self.exempt_users)

    def load_state(self):
        """Load saved daily settings and runtime state from JSON files."""
        try:
            self.daily, self.runtime = self.state_store.load()
            bed_label = (
                f"{self.daily.bed_time.hour:02d}:{self.daily.bed_time.minute:02d}"
                if self.daily.bed_time is not None
                else "none"
            )
            effective = effective_daily_allowance_minutes(self.daily, self.runtime)
            self.logger.info(
                "State applied: bed_time=%s wake_time=%02d:%02d daily_allowance=%s "
                "effective_allowance=%s manual_lock=%s accumulated_min=%.1f "
                "extension_sec=%s",
                bed_label,
                self.daily.wake_time.hour,
                self.daily.wake_time.minute,
                self.daily.allowance,
                effective,
                self.runtime.manual_lock_active,
                self.runtime.accumulated_seconds / 60,
                self.runtime.cumulative_extension_seconds,
            )
            print(
                f"[{datetime.now():%H:%M:%S}] Loaded settings from "
                f"{self.state_store.data_directory}"
            )
        except Exception as e:
            self.logger.error("Error loading state: %s", e, exc_info=True)
            print(f"[{datetime.now():%H:%M:%S}] Could not load previous state: {e}")

    def save_state(self):
        """Save daily settings and runtime state to JSON files."""
        try:
            self.state_store.save(self.daily, self.runtime)
            self.logger.debug("State saved")
        except Exception as e:
            self.logger.error("Error saving state: %s", e, exc_info=True)

    def read_log_tail(self, *, tail: int) -> tuple[list[str], bool]:
        """Return up to ``tail`` lines from the end of the agent log and whether more exist."""
        path = self.agent_log_path
        if not path.is_file():
            return [], False
        with path.open("rb") as handle:
            handle.seek(0, 2)
            file_end = handle.tell()
            if file_end == 0:
                return [], False

            block = 8192
            remaining = file_end
            fragment = b""
            raw_lines: list[bytes] = []

            while remaining > 0 and len(raw_lines) <= tail:
                read_size = min(block, remaining)
                remaining -= read_size
                handle.seek(remaining)
                chunk = handle.read(read_size)
                fragment = chunk + fragment
                parts = fragment.split(b"\n")
                if remaining > 0:
                    fragment = parts[0]
                    parts = parts[1:]
                else:
                    fragment = b""
                for part in reversed(parts):
                    if part or raw_lines:
                        raw_lines.append(part)
                    if len(raw_lines) > tail:
                        break

            truncated = remaining > 0 or len(raw_lines) > tail
            if len(raw_lines) > tail:
                raw_lines = raw_lines[:tail]
            raw_lines.reverse()
            lines = [line.decode("utf-8", errors="replace") for line in raw_lines]

        return lines, truncated

    def _effective_usage_allowance_minutes(self) -> float | None:
        return effective_daily_allowance_minutes(self.daily, self.runtime)

    def _reset_runtime_if_needed(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        used_minutes = self.runtime.accumulated_seconds / 60
        extension_seconds = self.runtime.cumulative_extension_seconds
        if not reset_runtime_if_needed(self.runtime, self.daily.wake_time, now):
            return False
        self.logger.info(
            "Wake-time rollover (%02d:%02d): resetting daily runtime state "
            "(was %.1f min used, %s extension sec)",
            self.daily.wake_time.hour,
            self.daily.wake_time.minute,
            used_minutes,
            extension_seconds,
        )
        return True

    def check_if_locked(self) -> bool:
        """True when this session's workstation is locked (OS-specific)."""
        return self.platform.check_session_locked()

    def session_is_active(self) -> bool:
        """True when this session is the active console and not locked."""
        return self.platform.session_is_active()

    def tick_accumulator(self):
        """
        Advance the active-use counter by the seconds elapsed since the last
        active tick. Reset at wake_time each day. Called once per second from
        run_monitor.
        """
        now = datetime.now()

        if self._reset_runtime_if_needed(now=now):
            self.last_tick_at = None

        # Never credit usage while a lock window is active (bedtime curfew or
        # manual lock). Accurate session detection already covers this, but a
        # brief detection flicker during curfew must not inflate usage.
        in_lock_window, _ = self.currently_in_lock_window()

        if self.should_monitor_user() and self.session_is_active() and not in_lock_window:
            if self.last_tick_at is not None:
                delta = (now - self.last_tick_at).total_seconds()
                # Clamp to (0, 60): negative means clock went backwards;
                # >60 means the loop stalled or we resumed from sleep, in
                # which case we don't want to credit the whole gap.
                if 0 < delta < 60:
                    self.runtime.accumulated_seconds += delta
            self.last_tick_at = now
        else:
            self.last_tick_at = None

    def monitor_activity(self):
        """Monitor lock/unlock status"""
        while True:
            actual_locked = self.check_if_locked()

            # Detect unlock
            if self.is_locked and not actual_locked:
                self.is_locked = False
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] PC has been unlocked (detected by activity)"
                )

            # Detect manual lock (not by our script)
            elif not self.is_locked and actual_locked:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] PC has been locked (detected)")

            time.sleep(3)  # Check every 3 seconds

    def set_bed_time(self, hour: int, minute: int) -> None:
        """Set the nightly bedtime curfew start."""
        self.daily.bed_time = dtime(hour, minute)
        self.logger.info("Parent action: bed time set to %02d:%02d", hour, minute)

    def clear_bed_time(self) -> None:
        self.daily.bed_time = None
        self.logger.info("Parent action: bed time cleared")

    def set_wake_time(self, hour: int, minute: int) -> None:
        """Set the daily morning unlock time (end of bedtime curfew)."""
        now = datetime.now()
        previous_wake_time = self.daily.wake_time
        self.daily.wake_time = dtime(hour, minute)
        if usage_period_date(now, previous_wake_time) < usage_period_date(
            now, self.daily.wake_time
        ):
            used_minutes = self.runtime.accumulated_seconds / 60
            extension_seconds = self.runtime.cumulative_extension_seconds
            self.logger.info(
                "Wake-time change (%02d:%02d -> %02d:%02d): resetting daily runtime state "
                "(was %.1f min used, %s extension sec)",
                previous_wake_time.hour,
                previous_wake_time.minute,
                self.daily.wake_time.hour,
                self.daily.wake_time.minute,
                used_minutes,
                extension_seconds,
            )
            reset_runtime_for_new_period(self.runtime, now)
            self.last_tick_at = None
            self.warnings_sent.clear()
        self.warnings_date = usage_period_date(now, self.daily.wake_time)
        self.logger.info("Parent action: wake time set to %02d:%02d", hour, minute)

    def set_daily_allowance(self, minutes: int | None) -> None:
        """Set the default daily screen-time allowance in minutes."""
        self.daily.allowance = minutes
        if minutes is None:
            self.logger.info("Parent action: daily limit cleared")
        else:
            self.logger.info("Parent action: daily limit set to %d minutes", minutes)

    def extend_time(self, minutes: int) -> None:
        """Add temporary extra allowance for the current usage period."""
        self.runtime.cumulative_extension_seconds += minutes * 60
        self.runtime.manual_lock_active = False
        self.warnings_sent.clear()
        self.logger.info("Parent action: extended time by %d minutes", minutes)

    def clear_extensions(self) -> None:
        self.runtime.cumulative_extension_seconds = 0
        self.logger.info("Parent action: time extensions cleared")

    def show_message(self, message, title="PC Time Control"):
        """Display a message to the logged-in user (OS-specific UI)."""
        self.platform.show_message(message, title=title)

    def send_parent_message(self, message: str, title: str = "PC Time Control") -> None:
        """Show a parent-initiated popup and record it in the agent log."""
        preview = message if len(message) <= 120 else f"{message[:120]}..."
        self.logger.info("Parent action: message sent: %s", preview)
        self.show_message(message, title=title)

    def engage_manual_lock(self) -> None:
        """Enable manual lock enforcement and lock the workstation."""
        self.runtime.manual_lock_active = True
        self.lock_pc()
        self.logger.info("Parent action: manual lock engaged")

    def clear_manual_lock(self) -> None:
        """Clear manual lock enforcement."""
        self.runtime.manual_lock_active = False
        self.logger.info("Parent action: manual lock cleared")

    def lock_pc(self):
        """Lock the workstation for this session."""
        try:
            self.is_locked = True
            self.platform.lock_workstation()
        except Exception as e:
            self.logger.error(f"Error locking PC: {e}")
            print(f"[{datetime.now():%H:%M:%S}] Error locking PC: {e}")

    def shutdown_pc(self, seconds=60):
        """Shutdown PC with warning."""
        try:
            self.platform.shutdown(seconds)
            self.logger.info("Parent action: shutdown in %ds", seconds)
        except Exception as e:
            self.logger.error(f"Error initiating shutdown: {e}")
            print(f"[{datetime.now():%H:%M:%S}] Error shutting down: {e}")

    def cancel_shutdown(self):
        """Cancel pending shutdown."""
        self.platform.cancel_shutdown()

    def get_time_remaining(self):
        """Calculate minutes remaining until lock. Returns None if no allowance set."""
        return minutes_until_lock(
            now=datetime.now(),
            bed_time=self.daily.bed_time,
            effective_usage_allowance_minutes=self._effective_usage_allowance_minutes(),
            accumulated_minutes=self.runtime.accumulated_seconds / 60,
            monitor_user=self.should_monitor_user(),
            manual_lock_active=self.runtime.manual_lock_active,
            wake_time=self.daily.wake_time,
        )

    def check_and_send_warnings(self):
        """Check if warnings should be sent and send them"""
        # Clear sent-warning memory at wake_time so the 30/15/5/1-minute warnings
        # fire again for the next usage period if the agent runs continuously.
        today = usage_period_date(datetime.now(), self.daily.wake_time)
        if today != self.warnings_date:
            self.warnings_sent.clear()
            self.warnings_date = today

        time_remaining = self.get_time_remaining()

        if time_remaining is None or time_remaining <= 0:
            return

        # Fire the smallest applicable threshold first so a kid with only a
        # few minutes left doesn't get a misleading "15 minutes" popup. Once
        # a smaller threshold fires, mark the larger thresholds as also-sent
        # — those longer warning windows never applied to this session and
        # would just be noise if they fired later.
        for warning_mins in sorted(self.warning_intervals):
            warning_key = f"{warning_mins}min"
            if warning_key in self.warnings_sent:
                continue
            if time_remaining > warning_mins:
                continue

            self.warnings_sent.add(warning_key)
            for larger in self.warning_intervals:
                if larger > warning_mins:
                    self.warnings_sent.add(f"{larger}min")

            actual_mins = max(1, math.ceil(time_remaining))
            unit = "minute" if actual_mins == 1 else "minutes"
            msg = f"⚠️ Computer will lock in {actual_mins} {unit}!"

            self.show_message(msg, "Warning")
            self.logger.info(
                "Warning sent: %d %s remaining (threshold %d)",
                actual_mins,
                unit,
                warning_mins,
            )
            print(f"[{datetime.now():%H:%M:%S}] Warning: {actual_mins} {unit} until lock")
            break

    def update_time_overlay(self) -> None:
        """Refresh the kid's on-screen countdown.

        Shows minutes remaining for a monitored user with an active limit and
        hides otherwise. No-op on platforms without an on-screen overlay.
        """
        state = overlay_state(self.get_time_remaining()) if self.should_monitor_user() else None
        if state is None:
            self.platform.update_time_overlay(None)
        else:
            self.platform.update_time_overlay(state.text, urgent=state.urgent)

    def currently_in_lock_window(self):
        """
        Return (locked, reason) for whether the agent should currently be
        enforcing a lock. Treats each scheduled lock_time as the start of a
        window that runs until wake_time, so a child who signs in after bedtime
        or before wake is still locked out. Usage-allowance enforcement is a
        simple "minutes-used >= allowance" check for the current wake-to-wake day.
        """
        decision = lock_decision(
            now=datetime.now(),
            bed_time=self.daily.bed_time,
            effective_usage_allowance_minutes=self._effective_usage_allowance_minutes(),
            accumulated_minutes=self.runtime.accumulated_seconds / 60,
            monitor_user=self.should_monitor_user(),
            manual_lock_active=self.runtime.manual_lock_active,
            wake_time=self.daily.wake_time,
        )
        return decision.should_lock, decision.reason

    def enforcement_lock_state(self) -> tuple[bool, str | None]:
        """Return schedule/limit enforcement without considering manual lock."""
        return enforcement_state(
            now=datetime.now(),
            bed_time=self.daily.bed_time,
            effective_usage_allowance_minutes=self._effective_usage_allowance_minutes(),
            accumulated_minutes=self.runtime.accumulated_seconds / 60,
            monitor_user=self.should_monitor_user(),
            wake_time=self.daily.wake_time,
        )

    def get_access_status(self) -> str:
        """Brief access status for the parent panel."""
        enforcement_active, enforcement_reason = self.enforcement_lock_state()
        return format_access_status(
            manual_lock=self.runtime.manual_lock_active,
            enforcement_active=enforcement_active,
            enforcement_reason=enforcement_reason,
            screen_locked=self.check_if_locked(),
        )

    def run_monitor(self):
        """
        Main monitoring loop. Continuously re-issues LockWorkStation while a
        lock window is active so a child who unlocks the screen with their
        password is immediately re-locked.
        """
        print("PC Time Control is running...")
        last_logged_reason = None
        while True:
            self.tick_accumulator()
            self.check_and_send_warnings()
            self.update_time_overlay()

            locked, reason = self.currently_in_lock_window()
            if locked and not self.check_if_locked():
                if reason != last_logged_reason:
                    self.logger.info(f"Locking PC: {reason}")
                    print(f"[{datetime.now():%H:%M:%S}] Locking PC: {reason}")
                    last_logged_reason = reason
                self.lock_pc()
            elif not locked:
                last_logged_reason = None

            # Persist the accumulator periodically so a crash or power loss
            # doesn't hand the kid a free reset of their daily usage.
            now = datetime.now()
            if self.last_persist_at is None or (now - self.last_persist_at).total_seconds() >= 60:
                self.save_state()
                self.last_persist_at = now

            time.sleep(1)

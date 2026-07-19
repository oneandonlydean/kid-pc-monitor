"""Tests for PCTimeControl orchestration with a fake HostPlatform."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from unittest import mock

from kid_pc_monitor.host_platform import HostPlatform
from kid_pc_monitor.pc_time_control import PCTimeControl


class FakeHostPlatform(HostPlatform):
    """In-memory platform stub for unit tests."""

    def __init__(
        self,
        *,
        locked: bool = False,
        session_active: bool = True,
        hostname: str = "test-pc",
    ) -> None:
        self.locked = locked
        self.session_active = session_active
        self.hostname = hostname
        self.lock_calls = 0
        self.shutdown_calls: list[int] = []
        self.messages: list[tuple[str, str]] = []

    def check_session_locked(self) -> bool:
        return self.locked

    def session_is_active(self) -> bool:
        return self.session_active and not self.locked

    def lock_workstation(self) -> None:
        self.lock_calls += 1
        self.locked = True

    def shutdown(self, seconds: int = 60) -> None:
        self.shutdown_calls.append(seconds)

    def cancel_shutdown(self) -> None:
        pass

    def show_message(self, message: str, title: str = "PC Time Control") -> None:
        self.messages.append((title, message))

    def get_hostname(self) -> str:
        return self.hostname


class PCTimeControlTests(unittest.TestCase):
    def test_should_monitor_user_respects_exempt_list(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                exempt_users=["parent"],
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.current_user = "parent"
            self.assertFalse(control.should_monitor_user())
            control.current_user = "kid"
            self.assertTrue(control.should_monitor_user())

    def test_load_and_save_state_round_trip(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            control = PCTimeControl(
                platform=platform,
                data_directory=data_dir,
                start_background_threads=False,
            )
            control.set_bed_time(21, 0)
            control.set_daily_allowance(90)
            control.runtime.manual_lock_active = True
            control.runtime.accumulated_seconds = 120.0
            control.runtime.cumulative_extension_seconds = 900
            control.save_state()

            reloaded = PCTimeControl(
                platform=platform,
                data_directory=data_dir,
                start_background_threads=False,
            )
            self.assertEqual(reloaded.daily.bed_time, dtime(21, 0))
            self.assertEqual(reloaded.daily.allowance, 90)
            self.assertTrue(reloaded.runtime.manual_lock_active)
            self.assertAlmostEqual(reloaded.runtime.accumulated_seconds, 120.0)
            self.assertEqual(reloaded.runtime.cumulative_extension_seconds, 900)

    def test_tick_accumulator_only_while_session_active(self) -> None:
        platform = FakeHostPlatform(session_active=True, locked=False)
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.last_tick_at = datetime.now() - timedelta(seconds=30)
            control.tick_accumulator()
            self.assertGreater(control.runtime.accumulated_seconds, 0.0)

            before = control.runtime.accumulated_seconds
            platform.session_active = False
            control.tick_accumulator()
            self.assertEqual(control.runtime.accumulated_seconds, before)

    def test_tick_accumulator_does_not_count_while_locked(self) -> None:
        platform = FakeHostPlatform(session_active=True, locked=True)
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.last_tick_at = datetime.now() - timedelta(seconds=30)
            control.tick_accumulator()
            self.assertEqual(control.runtime.accumulated_seconds, 0.0)

    def test_tick_accumulator_skips_during_bedtime_curfew(self) -> None:
        platform = FakeHostPlatform(session_active=True, locked=False)
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            # Build a curfew around "now": the only awake minute is [now+1, now+2),
            # so the current instant is always inside the curfew regardless of when
            # the test runs (handles midnight wraparound too).
            now = datetime.now()
            wake = (now + timedelta(minutes=1)).time()
            bed = (now + timedelta(minutes=2)).time()
            control.set_wake_time(wake.hour, wake.minute)
            control.set_bed_time(bed.hour, bed.minute)
            in_window, _ = control.currently_in_lock_window()
            self.assertTrue(in_window)
            control.last_tick_at = now - timedelta(seconds=30)
            control.tick_accumulator()
            self.assertEqual(control.runtime.accumulated_seconds, 0.0)

    def test_set_wake_time_before_now_resets_daily_runtime(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.daily.wake_time = dtime(9, 0)
            control.runtime.timestamp = datetime(2026, 6, 6, 7, 8)
            control.runtime.accumulated_seconds = 14_400
            control.runtime.manual_lock_active = True
            control.runtime.cumulative_extension_seconds = 3600

            with mock.patch(
                "kid_pc_monitor.pc_time_control.datetime", wraps=datetime
            ) as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 6, 7, 9)
                with self.assertLogs("PCTimeControl", level="INFO") as logs:
                    control.set_wake_time(7, 0)

            self.assertEqual(control.daily.wake_time, dtime(7, 0))
            self.assertEqual(control.runtime.timestamp, datetime(2026, 6, 6, 7, 9))
            self.assertEqual(control.runtime.accumulated_seconds, 0.0)
            self.assertFalse(control.runtime.manual_lock_active)
            self.assertEqual(control.runtime.cumulative_extension_seconds, 0)
            self.assertIsNone(control.last_tick_at)
            self.assertIn(
                "Wake-time change (09:00 -> 07:00): resetting daily runtime state",
                "\n".join(logs.output),
            )

    def test_set_wake_time_later_than_now_keeps_daily_runtime(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.daily.wake_time = dtime(7, 0)
            control.runtime.timestamp = datetime(2026, 6, 6, 7, 8)
            control.runtime.accumulated_seconds = 120

            with mock.patch(
                "kid_pc_monitor.pc_time_control.datetime", wraps=datetime
            ) as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 6, 8, 0)
                control.set_wake_time(9, 0)

            self.assertEqual(control.daily.wake_time, dtime(9, 0))
            self.assertAlmostEqual(control.runtime.accumulated_seconds, 120.0)

    def test_extend_time_adds_to_cumulative_extension_only(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.set_daily_allowance(60)
            control.runtime.accumulated_seconds = 100.0
            control.extend_time(30)
            self.assertEqual(control.daily.allowance, 60)
            self.assertAlmostEqual(control.runtime.accumulated_seconds, 100.0)
            self.assertEqual(control.runtime.cumulative_extension_seconds, 1800)

    def test_extend_time_clears_manual_lock(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.runtime.manual_lock_active = True
            control.extend_time(15)
            self.assertFalse(control.runtime.manual_lock_active)
            self.assertEqual(control.runtime.cumulative_extension_seconds, 900)
            locked, _ = control.currently_in_lock_window()
            self.assertFalse(locked)

    def test_extend_time_resets_warning_tracking(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.warnings_sent = {"30min", "15min", "5min", "1min"}

            with mock.patch.object(control, "get_time_remaining", return_value=14.0):
                control.check_and_send_warnings()
            self.assertEqual(platform.messages, [])

            control.extend_time(30)

            self.assertEqual(control.warnings_sent, set())
            with mock.patch.object(control, "get_time_remaining", return_value=14.0):
                control.check_and_send_warnings()
            self.assertEqual(len(platform.messages), 1)
            self.assertIn("14 minutes", platform.messages[0][1])

    def test_currently_in_lock_window_manual_lock(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.runtime.manual_lock_active = True
            locked, reason = control.currently_in_lock_window()
            self.assertTrue(locked)
            self.assertIn("Manual", reason)

    def test_lock_pc_delegates_to_platform(self) -> None:
        platform = FakeHostPlatform(locked=False)
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.lock_pc()
            self.assertEqual(platform.lock_calls, 1)
            self.assertTrue(platform.locked)

    def test_engage_manual_lock_logs_parent_action(self) -> None:
        platform = FakeHostPlatform(locked=False)
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            with self.assertLogs("PCTimeControl", level="INFO") as logs:
                control.engage_manual_lock()
            self.assertIn("Parent action: manual lock engaged", "\n".join(logs.output))
            self.assertTrue(control.runtime.manual_lock_active)

    def test_send_parent_message_logs_truncated_preview(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            long_text = "x" * 150
            with self.assertLogs("PCTimeControl", level="INFO") as logs:
                control.send_parent_message(long_text)
            output = "\n".join(logs.output)
            self.assertIn("Parent action: message sent:", output)
            self.assertIn("...", output)
            self.assertNotIn("x" * 150, output)

    def test_bootstraps_wake_time_from_program_data_daily(self) -> None:
        platform = FakeHostPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            program_data = data_dir / "ProgramData" / "KidPCMonitor"
            program_data.mkdir(parents=True)
            (program_data / "daily_settings.json").write_text(
                '{"target_user": "kid", "wake_time": "08:30"}',
                encoding="utf-8",
            )
            old_win = sys.platform
            try:
                sys.platform = "win32"
                from unittest.mock import patch

                from kid_pc_monitor import agent_state as agent_state_mod

                original = agent_state_mod.program_data_daily_path
                agent_state_mod.program_data_daily_path = lambda: (
                    program_data / "daily_settings.json"
                )
                try:
                    with patch("getpass.getuser", return_value="kid"):
                        control = PCTimeControl(
                            platform=platform,
                            data_directory=data_dir / "profile",
                            start_background_threads=False,
                        )
                finally:
                    agent_state_mod.program_data_daily_path = original
            finally:
                sys.platform = old_win

            self.assertEqual(control.daily.wake_time, dtime(8, 30))
            self.assertTrue((data_dir / "profile" / "daily_settings.json").is_file())

    def test_check_if_locked_delegates_to_platform(self) -> None:
        platform = FakeHostPlatform(locked=True)
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            self.assertTrue(control.check_if_locked())

    def _break_control(self, tmp: str) -> PCTimeControl:
        control = PCTimeControl(
            platform=FakeHostPlatform(),
            data_directory=Path(tmp),
            start_background_threads=False,
        )
        control.daily.break_interval_minutes = 45
        control.daily.break_duration_minutes = 5
        return control

    def test_break_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform = FakeHostPlatform()
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.daily.break_interval_minutes = 45
            control.daily.break_duration_minutes = 5
            now = datetime.now()
            # Not enough active use yet.
            control.runtime.accumulated_seconds = 44 * 60
            control.update_break_state(now)
            self.assertFalse(control.on_break(now))
            # Interval reached -> a break starts and the screen should lock.
            control.runtime.accumulated_seconds = 45 * 60
            control.update_break_state(now)
            self.assertTrue(control.on_break(now))
            locked, reason = control.currently_in_lock_window()
            self.assertTrue(locked)
            self.assertEqual(reason, "Break time")
            self.assertTrue(any(title == "Break time" for title, _ in platform.messages))
            # After the duration the break ends and the counter baseline resets.
            later = now + timedelta(minutes=5)
            control.update_break_state(later)
            self.assertFalse(control.on_break(later))
            self.assertEqual(control.runtime.break_baseline_seconds, 45 * 60)

    def test_breaks_disabled_never_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = self._break_control(tmp)
            control.daily.break_interval_minutes = 0  # off
            control.runtime.accumulated_seconds = 100 * 60
            control.update_break_state(datetime.now())
            self.assertFalse(control.on_break())

    def test_set_break_interval_zero_clears_active_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = self._break_control(tmp)
            control.runtime.break_active_until = datetime.now() + timedelta(minutes=5)
            control.set_break_interval(0)
            self.assertIsNone(control.runtime.break_active_until)

    def _weekend_control(self, tmp: str) -> PCTimeControl:
        control = PCTimeControl(
            platform=FakeHostPlatform(),
            data_directory=Path(tmp),
            start_background_threads=False,
        )
        control.daily.allowance = 60
        control.daily.bed_time = dtime(21, 0)
        control.daily.weekend_allowance = 180
        control.daily.weekend_bed_time = dtime(22, 30)
        return control

    def test_weekend_schedule_used_on_weekend_when_enabled(self) -> None:
        saturday = datetime(2026, 7, 18, 10, 0)  # a Saturday
        wednesday = datetime(2026, 7, 15, 10, 0)
        with tempfile.TemporaryDirectory() as tmp:
            control = self._weekend_control(tmp)
            control.daily.weekend_enabled = True
            self.assertEqual(control._effective_base_allowance(saturday), 180)
            self.assertEqual(control._effective_bed_time(saturday), dtime(22, 30))
            self.assertEqual(control._effective_base_allowance(wednesday), 60)
            self.assertEqual(control._effective_bed_time(wednesday), dtime(21, 0))

    def test_weekend_schedule_ignored_when_disabled(self) -> None:
        saturday = datetime(2026, 7, 18, 10, 0)
        with tempfile.TemporaryDirectory() as tmp:
            control = self._weekend_control(tmp)
            control.daily.weekend_enabled = False
            self.assertEqual(control._effective_base_allowance(saturday), 60)
            self.assertEqual(control._effective_bed_time(saturday), dtime(21, 0))

    def _earn_control(self, tmp: str) -> PCTimeControl:
        control = PCTimeControl(
            platform=FakeHostPlatform(),
            data_directory=Path(tmp),
            start_background_threads=False,
        )
        control.daily.earn_enabled = True
        control.daily.earn_reward_minutes = 3
        control.daily.earn_daily_cap_minutes = 20
        return control

    def test_award_earned_time_respects_daily_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = self._earn_control(tmp)
            self.assertEqual(control.earn_remaining_minutes(), 20)
            self.assertEqual(control.award_earned_time(15), 15)
            self.assertEqual(control.runtime.cumulative_extension_seconds, 15 * 60)
            # The next award is capped to the 5 minutes left under the cap.
            self.assertEqual(control.award_earned_time(15), 5)
            self.assertEqual(control.earn_remaining_minutes(), 0)
            self.assertEqual(control.award_earned_time(10), 0)

    def test_earn_session_none_when_disabled_or_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = self._earn_control(tmp)
            control.daily.earn_enabled = False
            self.assertIsNone(control._earn_session())
            control.daily.earn_enabled = True
            session = control._earn_session()
            assert session is not None
            self.assertEqual(session.reward_minutes, 3)
            self.assertEqual(session.remaining_minutes, 20)
            control.runtime.earned_today_seconds = 20 * 60  # cap reached
            self.assertIsNone(control._earn_session())

    def test_earn_award_grants_reward_per_correct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = self._earn_control(tmp)
            self.assertEqual(control._earn_award(4), 12)  # 4 correct * 3 min


if __name__ == "__main__":
    unittest.main()

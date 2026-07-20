"""Tests for the kid-side on-screen time-remaining overlay."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from unittest import mock

from kid_pc_monitor.agent_overlay import (
    corner_geometry,
    format_duration,
    overlay_detail_lines,
    overlay_label,
    overlay_state,
)
from kid_pc_monitor.host_platform import HostPlatform
from kid_pc_monitor.lock_policy import minutes_until_bedtime
from kid_pc_monitor.pc_time_control import PCTimeControl


class CornerGeometryTests(unittest.TestCase):
    PRIMARY = (0, 0, 1920, 1080)

    def test_four_corners_on_primary(self) -> None:
        self.assertEqual(corner_geometry("top-left", self.PRIMARY, 200, 120), (20, 20))
        self.assertEqual(corner_geometry("top-right", self.PRIMARY, 200, 120), (1700, 20))
        self.assertEqual(corner_geometry("bottom-left", self.PRIMARY, 200, 120), (20, 940))
        self.assertEqual(corner_geometry("bottom-right", self.PRIMARY, 200, 120), (1700, 940))

    def test_second_monitor_offset(self) -> None:
        second = (1920, 0, 3840, 1080)
        self.assertEqual(corner_geometry("top-right", second, 200, 120), (3620, 20))
        self.assertEqual(corner_geometry("top-left", second, 200, 120), (1940, 20))

    def test_unknown_corner_falls_back_to_top_right(self) -> None:
        self.assertEqual(
            corner_geometry("middle", self.PRIMARY, 200, 120),
            corner_geometry("top-right", self.PRIMARY, 200, 120),
        )

    def test_clamped_to_monitor_top_left(self) -> None:
        # A window taller/wider than the monitor stays pinned to the top-left.
        self.assertEqual(corner_geometry("bottom-right", self.PRIMARY, 4000, 2000), (0, 0))


class OverlayLabelTests(unittest.TestCase):
    def test_none_when_no_limit(self) -> None:
        self.assertIsNone(overlay_label(None))

    def test_none_when_at_or_past_limit(self) -> None:
        self.assertIsNone(overlay_label(0))
        self.assertIsNone(overlay_label(-3))

    def test_shows_a_live_seconds_countdown(self) -> None:
        self.assertEqual(overlay_label(45), "Time left: 45:00")
        self.assertEqual(overlay_label(44.1), "Time left: 44:06")
        self.assertEqual(overlay_label(0.2), "Time left: 0:12")

    def test_at_least_one_second_while_positive(self) -> None:
        self.assertEqual(overlay_label(0.001), "Time left: 0:01")

    def test_hours_use_h_mm_ss(self) -> None:
        self.assertEqual(overlay_label(60), "Time left: 1:00:00")
        self.assertEqual(overlay_label(125), "Time left: 2:05:00")


class OverlayStateTests(unittest.TestCase):
    def test_none_when_nothing_to_show(self) -> None:
        self.assertIsNone(overlay_state(None))
        self.assertIsNone(overlay_state(0))

    def test_not_urgent_with_plenty_of_time(self) -> None:
        state = overlay_state(45)
        assert state is not None
        self.assertEqual(state.text, "Time left: 45:00")
        self.assertFalse(state.urgent)

    def test_urgent_within_final_five_minutes(self) -> None:
        state = overlay_state(5)
        assert state is not None
        self.assertTrue(state.urgent)
        self.assertTrue(overlay_state(1.5).urgent)  # type: ignore[union-attr]

    def test_urgent_threshold_is_configurable(self) -> None:
        self.assertFalse(overlay_state(9, urgent_below_minutes=5).urgent)  # type: ignore[union-attr]
        self.assertTrue(overlay_state(9, urgent_below_minutes=10).urgent)  # type: ignore[union-attr]


class FormatDurationTests(unittest.TestCase):
    def test_minutes_and_seconds(self) -> None:
        self.assertEqual(format_duration(45), "45:00")
        self.assertEqual(format_duration(44.1), "44:06")

    def test_hours_use_h_mm_ss(self) -> None:
        self.assertEqual(format_duration(60), "1:00:00")
        self.assertEqual(format_duration(125), "2:05:00")

    def test_at_least_one_second(self) -> None:
        self.assertEqual(format_duration(0.001), "0:01")


class MinutesUntilBedtimeTests(unittest.TestCase):
    def test_none_when_no_bedtime(self) -> None:
        self.assertIsNone(minutes_until_bedtime(datetime(2026, 7, 20, 19, 18), None))

    def test_counts_down_to_tonight(self) -> None:
        now = datetime(2026, 7, 20, 19, 18)
        self.assertEqual(minutes_until_bedtime(now, dtime(20, 30)), 72)

    def test_rolls_to_tomorrow_once_past(self) -> None:
        now = datetime(2026, 7, 20, 21, 0)
        self.assertEqual(minutes_until_bedtime(now, dtime(20, 30)), 23 * 60 + 30)


class OverlayDetailLinesTests(unittest.TestCase):
    def test_empty_without_context(self) -> None:
        self.assertEqual(overlay_detail_lines(), [])

    def test_bedtime_line_shows_clock_time_and_countdown(self) -> None:
        lines = overlay_detail_lines(bed_time=dtime(20, 30), minutes_until_bedtime=72)
        self.assertEqual(lines, ["Bedtime 20:30 — in 1:12:00"])

    def test_bedtime_line_without_countdown(self) -> None:
        self.assertEqual(overlay_detail_lines(bed_time=dtime(20, 30)), ["Bedtime 20:30"])

    def test_allowance_line(self) -> None:
        self.assertEqual(
            overlay_detail_lines(allowance_minutes_left=35.5),
            ["Allowance left: 35:30"],
        )

    def test_saved_line_shown_when_carryover_enabled(self) -> None:
        self.assertEqual(
            overlay_detail_lines(carryover_enabled=True, carryover_minutes=20),
            ["Saved: 20 min"],
        )

    def test_saved_line_shows_zero_when_enabled_but_empty(self) -> None:
        # A stable readout the kid can watch grow, even before anything is banked.
        self.assertEqual(overlay_detail_lines(carryover_enabled=True), ["Saved: 0 min"])

    def test_saved_line_hidden_when_carryover_disabled(self) -> None:
        self.assertEqual(overlay_detail_lines(carryover_minutes=20), [])

    def test_negative_allowance_floors_at_zero(self) -> None:
        self.assertEqual(
            overlay_detail_lines(allowance_minutes_left=-5),
            ["Allowance left: 0:01"],
        )

    def test_all_lines_together(self) -> None:
        lines = overlay_detail_lines(
            bed_time=dtime(20, 30),
            minutes_until_bedtime=72,
            allowance_minutes_left=35,
            carryover_enabled=True,
            carryover_minutes=10,
        )
        self.assertEqual(
            lines,
            ["Bedtime 20:30 — in 1:12:00", "Allowance left: 35:00", "Saved: 10 min"],
        )


class OverlayStateDetailTests(unittest.TestCase):
    def test_detail_is_empty_without_context(self) -> None:
        state = overlay_state(45)
        assert state is not None
        self.assertEqual(state.detail, "")

    def test_detail_joins_lines_with_newlines(self) -> None:
        state = overlay_state(
            72,
            bed_time=dtime(20, 30),
            minutes_until_bedtime=72,
            allowance_minutes_left=35,
        )
        assert state is not None
        self.assertEqual(state.text, "Time left: 1:12:00")
        self.assertEqual(state.detail, "Bedtime 20:30 — in 1:12:00\nAllowance left: 35:00")


class _RecordingPlatform(HostPlatform):
    """Minimal platform stub that records on-screen overlay updates."""

    def __init__(self) -> None:
        self.overlay_calls: list[tuple[str | None, bool]] = []
        self.detail_calls: list[str] = []
        self.request_handler: Callable[[], None] | None = None
        self.earn_start: Callable[[], object] | None = None
        self.earn_award: Callable[[int], int] | None = None

    def check_session_locked(self) -> bool:
        return False

    def session_is_active(self) -> bool:
        return True

    def lock_workstation(self) -> None:
        pass

    def shutdown(self, seconds: int = 60) -> None:
        pass

    def cancel_shutdown(self) -> None:
        pass

    def show_message(self, message: str, title: str = "PC Time Control") -> None:
        pass

    def get_hostname(self) -> str:
        return "test-pc"

    def update_time_overlay(
        self, text: str | None, *, urgent: bool = False, detail: str = ""
    ) -> None:
        self.overlay_calls.append((text, urgent))
        self.detail_calls.append(detail)

    def set_overlay_request_handler(self, handler: Callable[[], None] | None) -> None:
        self.request_handler = handler

    def set_overlay_earn_handler(
        self,
        start_session: Callable[[], object] | None,
        award: Callable[[int], int] | None,
    ) -> None:
        self.earn_start = start_session
        self.earn_award = award


class UpdateTimeOverlayTests(unittest.TestCase):
    def test_forwards_remaining_label_for_monitored_user(self) -> None:
        platform = _RecordingPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.set_daily_allowance(90)
            control.runtime.accumulated_seconds = 30 * 60  # 30 min used -> 60 left
            control.update_time_overlay()
        self.assertEqual(platform.overlay_calls, [("Time left: 1:00:00", False)])

    def test_forwards_urgent_when_few_minutes_left(self) -> None:
        platform = _RecordingPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.set_daily_allowance(90)
            control.runtime.accumulated_seconds = 87 * 60  # 3 min left -> urgent
            control.update_time_overlay()
        self.assertEqual(platform.overlay_calls, [("Time left: 3:00", True)])

    def test_detail_shows_bedtime_countdown_and_allowance(self) -> None:
        platform = _RecordingPlatform()
        now = datetime(2026, 7, 20, 19, 18)
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.set_daily_allowance(120)
            control.set_bed_time(20, 30)
            control.runtime.accumulated_seconds = 60 * 60  # 60 of 120 min used
            with mock.patch("kid_pc_monitor.pc_time_control.datetime") as dt:
                dt.now.return_value = now
                control.update_time_overlay()
        # Bedtime (72 min away) binds before the 60 min of allowance left.
        self.assertEqual(platform.overlay_calls, [("Time left: 1:00:00", False)])
        self.assertEqual(
            platform.detail_calls,
            ["Bedtime 20:30 — in 1:12:00\nAllowance left: 1:00:00"],
        )

    def test_detail_reports_carryover_when_enabled(self) -> None:
        platform = _RecordingPlatform()
        now = datetime(2026, 7, 20, 12, 0)
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.set_daily_allowance(60)
            control.set_carryover_enabled(True)
            control.runtime.carryover_seconds = 20 * 60
            with mock.patch("kid_pc_monitor.pc_time_control.datetime") as dt:
                dt.now.return_value = now
                control.update_time_overlay()
        # 60 base + 20 banked, nothing used yet; the bank shows on its own line.
        self.assertEqual(platform.overlay_calls, [("Time left: 1:20:00", False)])
        self.assertEqual(platform.detail_calls, ["Allowance left: 1:20:00\nSaved: 20 min"])

    def test_hidden_for_exempt_user(self) -> None:
        platform = _RecordingPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                exempt_users=["kid"],
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.current_user = "kid"
            control.set_daily_allowance(90)
            control.update_time_overlay()
        self.assertEqual(platform.overlay_calls, [(None, False)])

    def test_hidden_when_show_timer_disabled(self) -> None:
        platform = _RecordingPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.set_daily_allowance(90)
            control.set_show_timer(False)  # parent hid the overlay
            control.update_time_overlay()
        self.assertEqual(platform.overlay_calls, [(None, False)])

    def test_hidden_when_no_limit_set(self) -> None:
        platform = _RecordingPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.update_time_overlay()
        self.assertEqual(platform.overlay_calls, [(None, False)])

    def test_overlay_request_button_records_a_time_request(self) -> None:
        platform = _RecordingPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            # The agent registers its handler so the overlay button can reach it.
            self.assertIsNotNone(platform.request_handler)
            self.assertIsNone(control.runtime.time_request_at)
            assert platform.request_handler is not None
            platform.request_handler()  # simulate the kid clicking "Ask for more time"
            self.assertIsNotNone(control.runtime.time_request_at)

    def test_overlay_earn_handlers_registered_and_award(self) -> None:
        platform = _RecordingPlatform()
        with tempfile.TemporaryDirectory() as tmp:
            control = PCTimeControl(
                platform=platform,
                data_directory=Path(tmp),
                start_background_threads=False,
            )
            control.daily.earn_enabled = True
            control.daily.earn_reward_minutes = 5
            control.daily.earn_daily_cap_minutes = 30
            assert platform.earn_start is not None
            assert platform.earn_award is not None
            self.assertIsNotNone(platform.earn_start())
            self.assertEqual(platform.earn_award(2), 10)  # 2 correct * 5 min


if __name__ == "__main__":
    unittest.main()

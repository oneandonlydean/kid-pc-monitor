"""Tests for the kid-side on-screen time-remaining overlay."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from kid_pc_monitor.agent_overlay import corner_geometry, overlay_label, overlay_state
from kid_pc_monitor.host_platform import HostPlatform
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


class _RecordingPlatform(HostPlatform):
    """Minimal platform stub that records on-screen overlay updates."""

    def __init__(self) -> None:
        self.overlay_calls: list[tuple[str | None, bool]] = []
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

    def update_time_overlay(self, text: str | None, *, urgent: bool = False) -> None:
        self.overlay_calls.append((text, urgent))

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

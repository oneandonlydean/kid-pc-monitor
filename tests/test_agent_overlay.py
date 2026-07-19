"""Tests for the kid-side on-screen time-remaining overlay."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kid_pc_monitor.agent_overlay import overlay_label, overlay_state
from kid_pc_monitor.host_platform import HostPlatform
from kid_pc_monitor.pc_time_control import PCTimeControl


class OverlayLabelTests(unittest.TestCase):
    def test_none_when_no_limit(self) -> None:
        self.assertIsNone(overlay_label(None))

    def test_none_when_at_or_past_limit(self) -> None:
        self.assertIsNone(overlay_label(0))
        self.assertIsNone(overlay_label(-3))

    def test_rounds_up_to_whole_minutes(self) -> None:
        self.assertEqual(overlay_label(0.2), "Time left: 1 min")
        self.assertEqual(overlay_label(44.1), "Time left: 45 min")

    def test_exact_minute(self) -> None:
        self.assertEqual(overlay_label(45), "Time left: 45 min")

    def test_hours_and_minutes(self) -> None:
        self.assertEqual(overlay_label(60), "Time left: 1h 00m")
        self.assertEqual(overlay_label(125), "Time left: 2h 05m")


class OverlayStateTests(unittest.TestCase):
    def test_none_when_nothing_to_show(self) -> None:
        self.assertIsNone(overlay_state(None))
        self.assertIsNone(overlay_state(0))

    def test_not_urgent_with_plenty_of_time(self) -> None:
        state = overlay_state(45)
        assert state is not None
        self.assertEqual(state.text, "Time left: 45 min")
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
        self.assertEqual(platform.overlay_calls, [("Time left: 1h 00m", False)])

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
        self.assertEqual(platform.overlay_calls, [("Time left: 3 min", True)])

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


if __name__ == "__main__":
    unittest.main()

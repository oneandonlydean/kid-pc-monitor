"""Tests for agent_state persistence and migration."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

from kid_pc_monitor.agent_state import (
    AgentStateStore,
    DailySettings,
    RuntimeState,
    effective_daily_allowance_minutes,
    fresh_runtime_state,
    migrate_legacy_state,
    reset_runtime_for_new_period,
    reset_runtime_if_needed,
    runtime_state_is_current,
)


class AgentStateTests(unittest.TestCase):
    def test_effective_daily_allowance_includes_extensions(self) -> None:
        daily = DailySettings(bed_time=None, wake_time=dtime(7, 0), allowance=120)
        runtime = RuntimeState(
            timestamp=datetime.now(),
            accumulated_seconds=0,
            manual_lock_active=False,
            cumulative_extension_seconds=1800,
        )
        self.assertEqual(effective_daily_allowance_minutes(daily, runtime), 150)

    def test_effective_daily_allowance_none_without_base_or_extension(self) -> None:
        daily = DailySettings(bed_time=None, wake_time=dtime(7, 0), allowance=None)
        runtime = RuntimeState(
            timestamp=datetime.now(),
            accumulated_seconds=0,
            manual_lock_active=False,
            cumulative_extension_seconds=0,
        )
        self.assertIsNone(effective_daily_allowance_minutes(daily, runtime))

    def test_runtime_state_is_current_uses_wake_time_period(self) -> None:
        wake = dtime(7, 0)
        now = datetime(2026, 5, 18, 8, 0)
        runtime = RuntimeState(
            timestamp=datetime(2026, 5, 18, 7, 30),
            accumulated_seconds=0,
            manual_lock_active=False,
            cumulative_extension_seconds=0,
        )
        self.assertTrue(runtime_state_is_current(runtime, wake, now))

        old = RuntimeState(
            timestamp=datetime(2026, 5, 17, 20, 0),
            accumulated_seconds=100,
            manual_lock_active=True,
            cumulative_extension_seconds=600,
        )
        self.assertFalse(runtime_state_is_current(old, wake, now))

    def test_migrate_legacy_state_maps_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "pc_control_state.json"
            legacy.write_text(
                json.dumps(
                    {
                        "lock_times": ["21:00"],
                        "wake_time": "08:30",
                        "usage_allowance": 90,
                        "manual_lock_active": True,
                        "accumulated_seconds": 120,
                        "accumulated_date": datetime.now().date().isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            migrated = migrate_legacy_state(legacy, current_user="kid")
            assert migrated is not None
            daily, runtime = migrated
            self.assertEqual(daily.bed_time, dtime(21, 0))
            self.assertEqual(daily.wake_time, dtime(8, 30))
            self.assertEqual(daily.allowance, 90)
            self.assertTrue(runtime.manual_lock_active)
            self.assertAlmostEqual(runtime.accumulated_seconds, 120.0)

    def test_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStateStore(Path(tmp), current_user="kid")
            daily = DailySettings(
                bed_time=dtime(21, 0),
                wake_time=dtime(7, 0),
                allowance=120,
            )
            runtime = RuntimeState(
                timestamp=datetime.now(),
                accumulated_seconds=300,
                manual_lock_active=False,
                cumulative_extension_seconds=900,
            )
            store.save(daily, runtime)
            loaded_daily, loaded_runtime = store.load()
            self.assertEqual(loaded_daily.bed_time, dtime(21, 0))
            self.assertEqual(loaded_daily.allowance, 120)
            self.assertAlmostEqual(loaded_runtime.accumulated_seconds, 300.0)
            self.assertEqual(loaded_runtime.cumulative_extension_seconds, 900)

    def test_store_round_trip_persists_show_timer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStateStore(Path(tmp), current_user="kid")
            daily = DailySettings(
                bed_time=None, wake_time=dtime(7, 0), allowance=60, show_timer=False
            )
            runtime = RuntimeState(
                timestamp=datetime.now(),
                accumulated_seconds=0,
                manual_lock_active=False,
                cumulative_extension_seconds=0,
            )
            store.save(daily, runtime)
            loaded_daily, _ = store.load()
            self.assertIs(loaded_daily.show_timer, False)

    def test_earn_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStateStore(Path(tmp), current_user="kid")
            daily = DailySettings(
                bed_time=None,
                wake_time=dtime(7, 0),
                allowance=60,
                earn_enabled=True,
                earn_reward_minutes=10,
                earn_questions=8,
                earn_daily_cap_minutes=45,
            )
            runtime = RuntimeState(
                timestamp=datetime.now(),
                accumulated_seconds=0,
                manual_lock_active=False,
                cumulative_extension_seconds=0,
                earned_today_seconds=600,
            )
            store.save(daily, runtime)
            loaded_daily, loaded_runtime = store.load()
            self.assertTrue(loaded_daily.earn_enabled)
            self.assertEqual(loaded_daily.earn_reward_minutes, 10)
            self.assertEqual(loaded_daily.earn_questions, 8)
            self.assertEqual(loaded_daily.earn_daily_cap_minutes, 45)
            self.assertEqual(loaded_runtime.earned_today_seconds, 600)

    def test_weekend_schedule_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStateStore(Path(tmp), current_user="kid")
            daily = DailySettings(
                bed_time=dtime(21, 0),
                wake_time=dtime(7, 0),
                allowance=60,
                weekend_enabled=True,
                weekend_bed_time=dtime(22, 30),
                weekend_allowance=180,
            )
            store.save(daily, fresh_runtime_state())
            loaded, _ = store.load()
            self.assertTrue(loaded.weekend_enabled)
            self.assertEqual(loaded.weekend_bed_time, dtime(22, 30))
            self.assertEqual(loaded.weekend_allowance, 180)

    def test_break_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStateStore(Path(tmp), current_user="kid")
            daily = DailySettings(
                bed_time=None,
                wake_time=dtime(7, 0),
                allowance=60,
                break_interval_minutes=45,
                break_duration_minutes=10,
            )
            runtime = RuntimeState(
                timestamp=datetime.now(),
                accumulated_seconds=0,
                manual_lock_active=False,
                cumulative_extension_seconds=0,
                break_baseline_seconds=1800.0,
            )
            store.save(daily, runtime)
            loaded_daily, loaded_runtime = store.load()
            self.assertEqual(loaded_daily.break_interval_minutes, 45)
            self.assertEqual(loaded_daily.break_duration_minutes, 10)
            self.assertEqual(loaded_runtime.break_baseline_seconds, 1800.0)

    def test_time_request_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStateStore(Path(tmp), current_user="kid")
            daily = DailySettings(bed_time=None, wake_time=dtime(7, 0), allowance=60)
            requested_at = datetime.now().replace(microsecond=0)
            runtime = RuntimeState(
                timestamp=datetime.now(),
                accumulated_seconds=0,
                manual_lock_active=False,
                cumulative_extension_seconds=0,
                time_request_at=requested_at,
            )
            store.save(daily, runtime)
            _daily, loaded = store.load()
            self.assertEqual(loaded.time_request_at, requested_at)

    def test_show_timer_defaults_true_for_legacy_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "daily_settings.json").write_text(
                json.dumps({"wake_time": "07:00", "bed_time": None, "allowance": 60}),
                encoding="utf-8",
            )
            store = AgentStateStore(data_dir, current_user="kid")
            loaded_daily, _ = store.load()
            self.assertIs(loaded_daily.show_timer, True)

    def test_store_resets_stale_runtime_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            daily_path = data_dir / "daily_settings.json"
            state_path = data_dir / "state.json"
            daily_path.write_text(
                json.dumps({"wake_time": "07:00", "bed_time": None, "allowance": 60}),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-01-01T12:00:00",
                        "accumulated_seconds": 999,
                        "manual_lock_active": True,
                        "cumulative_extension_seconds": 3600,
                    }
                ),
                encoding="utf-8",
            )
            store = AgentStateStore(data_dir, current_user="kid")
            _daily, runtime = store.load()
            self.assertFalse(runtime.manual_lock_active)
            self.assertEqual(runtime.accumulated_seconds, 0.0)
            self.assertEqual(runtime.cumulative_extension_seconds, 0)

    def test_reset_runtime_for_new_period_clears_daily_fields(self) -> None:
        runtime = RuntimeState(
            timestamp=datetime(2026, 1, 1, 12, 0),
            accumulated_seconds=500,
            manual_lock_active=True,
            cumulative_extension_seconds=1800,
        )
        reset_runtime_for_new_period(runtime, datetime(2026, 1, 2, 8, 0))
        self.assertEqual(runtime.accumulated_seconds, 0.0)
        self.assertFalse(runtime.manual_lock_active)
        self.assertEqual(runtime.cumulative_extension_seconds, 0)

    def test_reset_runtime_if_needed_resets_stale_period(self) -> None:
        runtime = RuntimeState(
            timestamp=datetime(2026, 6, 5, 10, 0),
            accumulated_seconds=14_400,
            manual_lock_active=True,
            cumulative_extension_seconds=3600,
        )
        did_reset = reset_runtime_if_needed(
            runtime,
            dtime(7, 0),
            datetime(2026, 6, 6, 7, 9),
        )
        self.assertTrue(did_reset)
        self.assertEqual(runtime.timestamp, datetime(2026, 6, 6, 7, 9))
        self.assertEqual(runtime.accumulated_seconds, 0.0)
        self.assertFalse(runtime.manual_lock_active)
        self.assertEqual(runtime.cumulative_extension_seconds, 0)

    def test_reset_runtime_if_needed_leaves_current_period_unchanged(self) -> None:
        runtime = RuntimeState(
            timestamp=datetime(2026, 6, 6, 7, 8),
            accumulated_seconds=120,
            manual_lock_active=False,
            cumulative_extension_seconds=0,
        )
        did_reset = reset_runtime_if_needed(
            runtime,
            dtime(7, 0),
            datetime(2026, 6, 6, 8, 0),
        )
        self.assertFalse(did_reset)
        self.assertAlmostEqual(runtime.accumulated_seconds, 120.0)


if __name__ == "__main__":
    unittest.main()

"""Tests for panel display formatting helpers."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from kid_pc_monitor.panel_format import format_snapshot_recorded_at, usage_chart


def _day(date_iso: str, used_min: int, limit: int | None, ext_min: int = 0) -> dict:
    return {
        "date": date_iso,
        "accumulated_seconds": used_min * 60,
        "daily_limit": limit,
        "cumulative_extension_seconds": ext_min * 60,
    }


class UsageChartTests(unittest.TestCase):
    def test_empty_and_no_data_days(self) -> None:
        chart = usage_chart([{"date": "2026-07-14", "no_data": True}])
        self.assertFalse(chart.has_data)
        self.assertEqual(chart.scale_minutes, 60)  # floor
        self.assertTrue(chart.bars[0].no_data)
        self.assertEqual(chart.bars[0].height_pct, 0)

    def test_scale_and_height_use_common_maximum(self) -> None:
        chart = usage_chart([_day("2026-07-14", 30, 90), _day("2026-07-15", 45, 60)])
        self.assertEqual(chart.scale_minutes, 90)  # max(60 floor, 30, 45, 90, 60)
        self.assertTrue(chart.has_data)
        self.assertEqual(chart.bars[0].height_pct, 33)  # 30/90
        self.assertEqual(chart.bars[0].limit_pct, 100)  # 90/90
        self.assertFalse(chart.bars[0].over_limit)

    def test_over_limit_flag_counts_extension(self) -> None:
        over = usage_chart([_day("2026-07-14", 120, 90)]).bars[0]
        self.assertTrue(over.over_limit)
        # Same usage but a 30-min extension raises the effective cap to 120.
        not_over = usage_chart([_day("2026-07-14", 100, 90, ext_min=30)]).bars[0]
        self.assertFalse(not_over.over_limit)

    def test_no_allowance_leaves_limit_marker_absent(self) -> None:
        bar = usage_chart([_day("2026-07-14", 30, None)]).bars[0]
        self.assertIsNone(bar.limit_pct)
        self.assertIsNone(bar.allowance_minutes)
        self.assertFalse(bar.over_limit)

    def test_day_label(self) -> None:
        bar = usage_chart([_day("2026-07-14", 10, 60)]).bars[0]
        self.assertEqual(bar.label, f"{date(2026, 7, 14).strftime('%a')} 14")


class FormatSnapshotRecordedAtTests(unittest.TestCase):
    _TZ = timezone(timedelta(hours=-5))
    _NOW = datetime(2026, 6, 5, 18, 0, 0, tzinfo=_TZ)

    def test_today_shows_compact_local_time(self) -> None:
        self.assertEqual(
            format_snapshot_recorded_at(
                "2026-06-05T15:30:00-05:00",
                now=self._NOW,
            ),
            "3:30pm",
        )

    def test_today_morning_shows_am(self) -> None:
        self.assertEqual(
            format_snapshot_recorded_at(
                "2026-06-05T09:05:00-05:00",
                now=self._NOW,
            ),
            "9:05am",
        )

    def test_yesterday(self) -> None:
        self.assertEqual(
            format_snapshot_recorded_at(
                "2026-06-04T15:30:00-05:00",
                now=self._NOW,
            ),
            "3:30pm yesterday",
        )

    def test_older_date_shows_short_month_and_day(self) -> None:
        self.assertEqual(
            format_snapshot_recorded_at(
                "2026-06-01T15:30:00-05:00",
                now=self._NOW,
            ),
            "3:30pm Jun 1",
        )

    def test_invalid_timestamp_is_returned_unchanged(self) -> None:
        self.assertEqual(format_snapshot_recorded_at("not-a-date"), "not-a-date")


if __name__ == "__main__":
    unittest.main()

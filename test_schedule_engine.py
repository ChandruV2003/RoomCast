import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from schedule_engine import format_schedule_rows, is_schedule_active, is_schedule_hold_active, next_schedule_change


class ScheduleEngineTests(unittest.TestCase):
    def test_active_during_same_day_window(self):
        rows = [{"day": "SAT", "start": "17:55", "end": "20:05"}]
        when = datetime(2026, 3, 14, 18, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(is_schedule_active(rows, when=when))

    def test_inactive_outside_same_day_window(self):
        rows = [{"day": "SUN", "start": "10:25", "end": "14:05"}]
        when = datetime(2026, 3, 15, 9, 45, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(is_schedule_active(rows, when=when))

    def test_overnight_window(self):
        rows = [{"day": "SAT", "start": "23:00", "end": "01:00"}]
        when = datetime(2026, 3, 15, 0, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(is_schedule_active(rows, when=when))

    def test_next_schedule_change(self):
        rows = [{"day": "SUN", "start": "10:25", "end": "14:05"}]
        when = datetime(2026, 3, 15, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        next_change = next_schedule_change(rows, when=when)
        self.assertEqual(next_change.isoformat(), "2026-03-15T10:25:00-04:00")

    def test_schedule_formatting(self):
        rows = [{"day": "WED", "start": "18:55", "end": "21:05"}]
        self.assertEqual(format_schedule_rows(rows), "WED 18:55-21:05")

    def test_disabled_schedule_rows_do_not_activate(self):
        rows = [{"day": "SAT", "start": "18:00", "end": "21:00", "enabled": False}]
        when = datetime(2026, 3, 14, 18, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(is_schedule_active(rows, when=when))
        self.assertEqual(format_schedule_rows(rows), "[off] SAT 18:00-21:00")

    def test_schedule_hold_is_active_shortly_after_end(self):
        rows = [{"day": "WED", "start": "18:00", "end": "21:00"}]
        when = datetime(2026, 3, 11, 22, 15, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(is_schedule_hold_active(rows, when=when, grace_minutes=120))

    def test_schedule_hold_expires_after_grace_window(self):
        rows = [{"day": "WED", "start": "18:00", "end": "21:00"}]
        when = datetime(2026, 3, 12, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertFalse(is_schedule_hold_active(rows, when=when, grace_minutes=120))


if __name__ == "__main__":
    unittest.main()

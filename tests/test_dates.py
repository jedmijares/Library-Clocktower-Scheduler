import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from libcal_scheduler.config import Slot
from libcal_scheduler.dates import (
    CHICAGO,
    SATURDAY,
    SUNDAY,
    Window,
    add_months,
    bookability,
    horizon,
    horizon_windows,
    latest_on_or_before,
    parse_slot,
    weekend_windows,
    window_for,
)

SLOTS = {
    "saturday": Slot(start="11:30", end="16:30"),
    "sunday": Slot(start="13:30", end="16:30"),
}


class AddMonths(unittest.TestCase):
    def test_ordinary_addition(self):
        self.assertEqual(add_months(date(2026, 8, 9), 3), date(2026, 11, 9))

    def test_month_end_overflow_is_clamped(self):
        # Aug 31 + 3 months would be Nov 31, which does not exist.
        self.assertEqual(add_months(date(2026, 8, 31), 3), date(2026, 11, 30))
        self.assertEqual(add_months(date(2026, 11, 30), 3), date(2027, 2, 28))
        self.assertEqual(add_months(date(2024, 11, 30), 3), date(2025, 2, 28))

    def test_leap_year_february(self):
        self.assertEqual(add_months(date(2027, 11, 30), 3), date(2028, 2, 29))

    def test_crosses_year_boundary(self):
        self.assertEqual(add_months(date(2026, 11, 15), 3), date(2027, 2, 15))


class LatestOnOrBefore(unittest.TestCase):
    def test_monday_horizon_finds_that_weekend(self):
        monday = date(2026, 11, 9)
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(latest_on_or_before(monday, SATURDAY), date(2026, 11, 7))
        self.assertEqual(latest_on_or_before(monday, SUNDAY), date(2026, 11, 8))

    def test_a_date_that_is_already_the_weekday_returns_itself(self):
        saturday = date(2026, 11, 7)
        self.assertEqual(latest_on_or_before(saturday, SATURDAY), saturday)

    def test_saturday_horizon_pushes_sunday_back_six_days(self):
        saturday = date(2026, 11, 7)
        self.assertEqual(latest_on_or_before(saturday, SUNDAY), date(2026, 11, 1))


class HorizonWindows(unittest.TestCase):
    def test_monday_horizon_gives_one_weekend(self):
        # Chicago today = Sun Aug 9 2026 -> horizon Mon Nov 9
        windows = horizon_windows(date(2026, 8, 9), SLOTS, 92)
        self.assertEqual([w.day for w in windows], [date(2026, 11, 7), date(2026, 11, 8)])
        self.assertEqual(windows[0].start, "11:30")
        self.assertEqual(windows[1].start, "13:30")

    def test_saturday_horizon_gives_two_different_weekends(self):
        # today = Fri Aug 7 2026 -> horizon Sat Nov 7. The Saturday is the horizon
        # itself; the Sunday is six days earlier, a DIFFERENT weekend.
        self.assertEqual(horizon(date(2026, 8, 7), 92), date(2026, 11, 7))
        windows = horizon_windows(date(2026, 8, 7), SLOTS, 92)
        self.assertEqual([w.day for w in windows], [date(2026, 11, 1), date(2026, 11, 7)])
        self.assertEqual((windows[1].day - windows[0].day).days, 6)

    def test_each_window_carries_its_own_weekday_and_times(self):
        windows = horizon_windows(date(2026, 8, 9), SLOTS, 92)
        by_day = {w.weekday_name: w for w in windows}
        self.assertEqual(by_day["Saturday"].end, "16:30")
        self.assertEqual(by_day["Sunday"].start, "13:30")


class SlotLattice(unittest.TestCase):
    def test_saturday_window_covers_ten_half_hour_slots(self):
        window = Window(day=date(2026, 11, 7), start="11:30", end="16:30")
        starts = window.slot_starts()
        self.assertEqual(len(starts), 10)
        self.assertEqual(starts[0], "11:30")
        self.assertEqual(starts[-1], "16:00")

    def test_sunday_window_is_an_exact_fit_with_six_slots(self):
        # CPL Sunday hours are 13:30-16:30, so the fallback window has zero slack.
        window = Window(day=date(2026, 11, 8), start="13:30", end="16:30")
        self.assertEqual(window.slot_starts(), ["13:30", "14:00", "14:30", "15:00", "15:30", "16:00"])

    def test_window_label_is_human_readable(self):
        window = Window(day=date(2026, 11, 7), start="11:30", end="16:30")
        self.assertEqual(window.label(), "Sat Nov 7, 2026  11:30am – 4:30pm")


class Timezone(unittest.TestCase):
    def test_slot_timestamps_are_parsed_as_chicago(self):
        parsed = parse_slot("2026-08-24 10:30:00")
        self.assertEqual(parsed.tzinfo, CHICAGO)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=-5))  # CDT

    def test_window_across_the_dst_boundary_keeps_wall_clock_times(self):
        # DST 2026 ends Sun Nov 1. An August window is CDT (-05:00); a November one
        # is CST (-06:00). The wall-clock window must stay 11:30-16:30 either way.
        august = Window(day=date(2026, 8, 8), start="11:30", end="16:30")
        november = Window(day=date(2026, 11, 7), start="11:30", end="16:30")
        self.assertEqual(august.start_at.utcoffset(), timedelta(hours=-5))
        self.assertEqual(november.start_at.utcoffset(), timedelta(hours=-6))
        for window in (august, november):
            self.assertEqual(window.start_at.strftime("%H:%M"), "11:30")
            self.assertEqual(len(window.slot_starts()), 10)

    def test_resolution_is_identical_whatever_the_machine_zone(self):
        """One instant, late on a Chicago Thursday.

        Machines in UTC, London or Tokyo already read the next day, and a naive
        `date.today()` there resolves a target a full week later. Anchoring to
        Chicago gives one answer regardless.
        """
        instant = datetime(2026, 8, 6, 23, 30, tzinfo=CHICAGO)
        expected = horizon_windows(instant.astimezone(CHICAGO).date(), SLOTS, 92)
        for zone in ("America/Chicago", "Etc/UTC", "Europe/London", "Asia/Tokyo"):
            chicago_date = instant.astimezone(ZoneInfo(zone)).astimezone(CHICAGO).date()
            self.assertEqual(
                [w.day for w in horizon_windows(chicago_date, SLOTS, 92)],
                [w.day for w in expected],
                f"machine in {zone} must resolve the same dates",
            )

    def test_naive_machine_date_would_have_been_wrong(self):
        """Proves the failure mode is real rather than theoretical."""
        instant = datetime(2026, 8, 6, 23, 30, tzinfo=CHICAGO)
        correct = horizon_windows(instant.date(), SLOTS, 92)
        naive = horizon_windows(instant.astimezone(ZoneInfo("Asia/Tokyo")).date(), SLOTS, 92)
        saturdays = ([w.day for w in correct if w.day.weekday() == SATURDAY][0],
                     [w.day for w in naive if w.day.weekday() == SATURDAY][0])
        self.assertNotEqual(saturdays[0], saturdays[1])
        self.assertEqual((saturdays[1] - saturdays[0]).days, 7)


class WindowFor(unittest.TestCase):
    def test_weekdays_have_no_configured_window(self):
        self.assertIsNone(window_for(date(2026, 11, 4), SLOTS))  # Wednesday

    def test_saturday_and_sunday_pick_their_own_slots(self):
        self.assertEqual(window_for(date(2026, 11, 7), SLOTS).start, "11:30")
        self.assertEqual(window_for(date(2026, 11, 8), SLOTS).start, "13:30")


class Weekends(unittest.TestCase):
    def test_three_weekends_back_from_the_horizon(self):
        windows = weekend_windows(date(2026, 8, 9), SLOTS, 92, 3)
        days = [w.day for w in windows]
        self.assertEqual(days[0], date(2026, 10, 24))
        self.assertEqual(days[-1], date(2026, 11, 8))
        self.assertEqual(len(days), 6)

    def test_dates_beyond_the_horizon_are_excluded(self):
        # horizon Sat Nov 7 -> the Sunday after it (Nov 8) is not yet bookable
        windows = weekend_windows(date(2026, 8, 7), SLOTS, 92, 1)
        self.assertEqual([w.day for w in windows], [date(2026, 11, 7)])


class BookabilityRules(unittest.TestCase):
    REF = date(2026, 8, 9)

    def test_inside_the_window_is_bookable(self):
        self.assertTrue(bookability(date(2026, 11, 7), self.REF, 92, 7).ok)

    def test_beyond_three_months_is_refused(self):
        verdict = bookability(date(2026, 11, 14), self.REF, 92, 7)
        self.assertFalse(verdict.ok)
        self.assertIn("beyond the 92-day booking window", verdict.reason)

    def test_inside_the_seven_day_cutoff_is_refused(self):
        verdict = bookability(date(2026, 8, 13), self.REF, 92, 7)
        self.assertFalse(verdict.ok)
        self.assertIn("at least 7", verdict.reason)

    def test_exactly_seven_days_out_is_allowed(self):
        self.assertTrue(bookability(date(2026, 8, 16), self.REF, 92, 7).ok)

    def test_the_past_is_refused(self):
        verdict = bookability(date(2026, 8, 8), self.REF, 92, 7)
        self.assertFalse(verdict.ok)
        self.assertIn("in the past", verdict.reason)


if __name__ == "__main__":
    unittest.main()

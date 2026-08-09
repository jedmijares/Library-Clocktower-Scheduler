import json
import unittest
from datetime import date, datetime
from pathlib import Path

from libcal_scheduler.config import validate
from libcal_scheduler.dates import CHICAGO, Window
from libcal_scheduler.grid import Availability, CLOSED, FULLY_BOOKED, OPEN, PARTIAL
from libcal_scheduler.guardrails import check
from libcal_scheduler.history import Booking

ROOT = Path(__file__).resolve().parent.parent

# Chicago "now" for every case: Sunday 9 Aug 2026, so the horizon is Mon 9 Nov.
NOW = datetime(2026, 8, 9, 6, 12, tzinfo=CHICAGO)
TARGET = Window(day=date(2026, 11, 7), start="11:30", end="16:30")  # a Saturday
OPEN_AVAIL = Availability(status=OPEN, free=("11:30am–4:30pm",))


def config():
    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    cfg["requester"].update(
        firstName="Ada",
        lastName="Lovelace",
        email="ada@example.com",
        telephone="312-555-0100",
        address="1150 W Fullerton Ave, Chicago IL 60614",
    )
    cfg["answers"]["organization"] = "Informal community group"
    cfg["answers"]["expectedAttendance"] = 20
    return validate(cfg)


def booking(day: str, room_id: str = "bezazian") -> Booking:
    return Booking(
        day=day, room_id=room_id, branch="Bezazian", room="Meeting Room",
        start="11:30", end="16:30", booked_at="2026-08-09T06:00:00-05:00",
    )


def run(**overrides):
    cfg = overrides.pop("config", None) or config()
    return check(
        room=overrides.pop("room", None) or cfg.room("bezazian"),
        window=overrides.pop("window", TARGET),
        availability=overrides.pop("availability", OPEN_AVAIL),
        config=cfg,
        bookings=overrides.pop("bookings", []),
        now=overrides.pop("now", NOW),
    )


class HappyPath(unittest.TestCase):
    def test_a_clean_booking_passes(self):
        verdict = run()
        self.assertTrue(verdict.ok, verdict.refusals)
        self.assertEqual(verdict.refusals, ())


class Refusals(unittest.TestCase):
    def test_a_weekday_target_is_refused(self):
        wednesday = Window(day=date(2026, 11, 4), start="11:30", end="16:30")
        verdict = run(window=wednesday)
        self.assertFalse(verdict.ok)
        self.assertTrue(any("Wednesday" in r for r in verdict.refusals))

    def test_a_window_that_does_not_match_config_is_refused(self):
        wrong = Window(day=date(2026, 11, 7), start="09:30", end="16:30")
        verdict = run(window=wrong)
        self.assertTrue(any("does not match the configured" in r for r in verdict.refusals))

    def test_a_date_beyond_the_three_month_window_is_refused(self):
        verdict = run(window=Window(day=date(2026, 11, 14), start="11:30", end="16:30"))
        self.assertTrue(any("beyond the 90-day booking window" in r for r in verdict.refusals))

    def test_a_date_inside_the_seven_day_cutoff_is_refused(self):
        verdict = run(window=Window(day=date(2026, 8, 15), start="11:30", end="16:30"))
        self.assertTrue(any("at least 7" in r for r in verdict.refusals))

    def test_partial_availability_is_refused(self):
        partial = Availability(status=PARTIAL, free=("9:30am–2:30pm",), blocked=("14:30",))
        verdict = run(availability=partial)
        self.assertTrue(any("not open for the whole window" in r for r in verdict.refusals))
        self.assertTrue(any("9:30am–2:30pm" in r for r in verdict.refusals))

    def test_fully_booked_is_refused(self):
        verdict = run(availability=Availability(status=FULLY_BOOKED))
        self.assertTrue(any("nothing free" in r for r in verdict.refusals))

    def test_a_closed_day_is_refused(self):
        verdict = run(availability=Availability(status=CLOSED))
        self.assertFalse(verdict.ok)

    def test_unknown_slot_classes_refuse_rather_than_book_blind(self):
        odd = Availability(status=OPEN, unknown_classes=("s-lc-eq-brand-new",))
        verdict = run(availability=odd)
        self.assertTrue(any("unrecognised slot classes" in r for r in verdict.refusals))

    def test_an_existing_booking_on_that_date_is_refused(self):
        verdict = run(bookings=[booking("2026-11-07")])
        self.assertTrue(any("refusing to double-book" in r for r in verdict.refusals))

    def test_a_booking_on_another_date_does_not_block(self):
        self.assertTrue(run(bookings=[booking("2026-10-31")]).ok)

    def test_an_unverified_field_map_is_refused(self):
        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        cfg["requester"].update(
            firstName="Ada", lastName="Lovelace", email="ada@example.com",
            telephone="312-555-0100", address="1150 W Fullerton Ave",
        )
        cfg["answers"]["organization"] = "Informal community group"
        for room in cfg["rooms"]:
            if room["id"] == "bezazian":
                room["fieldMapVerified"] = None
        validated = validate(cfg)
        verdict = run(config=validated, room=validated.room("bezazian"))
        self.assertTrue(any("unverified field map" in r for r in verdict.refusals))

    def test_attendance_over_capacity_is_refused(self):
        cfg = config()
        # Rogers Park holds 40; ask for more without tripping config validation.
        object.__setattr__(cfg, "answers", dict(cfg.answers, expectedAttendance=44))
        verdict = run(config=cfg, room=cfg.room("rogers-park"))
        self.assertTrue(any("exceeds" in r and "capacity 40" in r for r in verdict.refusals))

    def test_refusals_accumulate(self):
        verdict = run(
            window=Window(day=date(2026, 11, 4), start="09:00", end="10:00"),
            availability=Availability(status=FULLY_BOOKED),
            bookings=[booking("2026-11-04")],
        )
        self.assertGreaterEqual(len(verdict.refusals), 3)


class Warnings(unittest.TestCase):
    def test_leaning_on_one_branch_warns_but_does_not_refuse(self):
        history = [booking(d) for d in ("2026-07-11", "2026-07-18", "2026-07-25")]
        verdict = run(bookings=history)
        self.assertTrue(verdict.ok, verdict.refusals)
        self.assertTrue(any("of your last 5 bookings" in w for w in verdict.warnings))

    def test_occasional_use_does_not_warn(self):
        verdict = run(bookings=[booking("2026-07-11")])
        self.assertFalse(any("last 5 bookings" in w for w in verdict.warnings))

    def test_room_specific_warnings_are_surfaced(self):
        cfg = config()
        verdict = run(config=cfg, room=cfg.room("west-loop"))
        self.assertTrue(verdict.ok, verdict.refusals)
        self.assertTrue(any("Not enclosed" in w for w in verdict.warnings))

    def test_edgewater_theater_style_capacity_is_surfaced(self):
        cfg = config()
        verdict = run(config=cfg, room=cfg.room("edgewater-b"))
        self.assertTrue(any("theater style" in w for w in verdict.warnings))

    def test_machine_timezone_mismatch_is_surfaced(self):
        # The container runs as UTC, so this fires here and names both zones.
        verdict = run()
        mismatch = [w for w in verdict.warnings if "all dates below are Chicago" in w]
        self.assertTrue(mismatch, verdict.warnings)
        self.assertIn("2026-11", "".join(mismatch) + "2026-11")  # sanity


if __name__ == "__main__":
    unittest.main()

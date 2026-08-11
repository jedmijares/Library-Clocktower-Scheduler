import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from libcal_scheduler.dates import Window
from libcal_scheduler.grid import (
    AVAILABLE,
    BOOKED,
    CLOSED,
    FULLY_BOOKED,
    OPEN,
    PADDING,
    PARTIAL,
    UNKNOWN,
    checksums,
    classify,
    evaluate,
    free_ranges,
    parse,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grids"

SAT = ("11:30", "16:30")
SUN = ("13:30", "16:30")


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def window(day: str, times: tuple[str, str]) -> Window:
    return Window(day=date.fromisoformat(day), start=times[0], end=times[1])


class Classify(unittest.TestCase):
    def test_known_classes(self):
        self.assertEqual(classify(None), AVAILABLE)
        self.assertEqual(classify(""), AVAILABLE)
        self.assertEqual(classify("s-lc-eq-checkout"), BOOKED)
        self.assertEqual(classify("s-lc-eq-r-padding"), PADDING)

    def test_extra_classes_still_match(self):
        self.assertEqual(classify("foo s-lc-eq-checkout bar"), BOOKED)
        self.assertEqual(classify("s-lc-eq-r-padding extra"), PADDING)

    def test_unrecognised_class_is_not_treated_as_available(self):
        """A LibCal change must surface, not silently look bookable."""
        self.assertEqual(classify("s-lc-eq-something-new"), UNKNOWN)
        self.assertNotEqual(classify("s-lc-eq-something-new"), AVAILABLE)


class PaddingSemantics(unittest.TestCase):
    """Regression guard on the empirical basis for the className mapping.

    Padding was interpreted as buffer *around bookings* because every padding slot
    observed was adjacent to a booked one. If that ever stops holding, the reading
    of the grid is wrong and this test should fail rather than the tool booking
    over someone.
    """

    def test_every_padding_slot_is_adjacent_to_a_booked_slot(self):
        checked = 0
        for path in sorted(FIXTURES.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for eid in {s["itemId"] for s in payload["slots"]}:
                for day, slots in parse(payload, eid).items():
                    for start, state in slots.items():
                        if state != PADDING:
                            continue
                        checked += 1
                        before = (
                            datetime.strptime(start, "%H:%M") - timedelta(minutes=30)
                        ).strftime("%H:%M")
                        after = (
                            datetime.strptime(start, "%H:%M") + timedelta(minutes=30)
                        ).strftime("%H:%M")
                        neighbours = {slots.get(before), slots.get(after)}
                        self.assertIn(
                            BOOKED,
                            neighbours,
                            f"{path.name} {day} {start}: padding with no adjacent booking",
                        )
        self.assertGreater(checked, 50, "fixtures should contain plenty of padding slots")

    def test_fixtures_contain_no_unknown_classes(self):
        for path in sorted(FIXTURES.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for slot in payload["slots"]:
                self.assertNotEqual(
                    classify(slot.get("className")),
                    UNKNOWN,
                    f"{path.name}: unexpected className {slot.get('className')!r}",
                )


class ParseAndFilter(unittest.TestCase):
    def test_eid_is_ignored_by_the_api_so_filtering_happens_here(self):
        """Lincoln Park's gid holds two rooms; the grid returns both regardless."""
        payload = load("lincoln_park_aug")
        item_ids = {s["itemId"] for s in payload["slots"]}
        self.assertEqual(item_ids, {65898, 65899}, "response covers the whole gid")
        large = parse(payload, 65898)
        small = parse(payload, 65899)
        self.assertTrue(large and small)
        self.assertNotEqual(large, small, "the two rooms have different availability")

    def test_edgewater_grid_carries_both_rooms(self):
        payload = load("edgewater_oct")
        self.assertEqual({s["itemId"] for s in payload["slots"]}, {65906, 65907})

    def test_checksums_are_keyed_by_full_timestamp(self):
        payload = load("bezazian_oct")
        found = checksums(payload, 65884)
        self.assertTrue(found)
        key = next(iter(found))
        self.assertRegex(key, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertRegex(found[key], r"^[0-9a-f]{32}$")


class WindowEvaluation(unittest.TestCase):
    """Ground truth taken from the availability sweep run against the live site."""

    def test_bezazian_sunday_oct_18_is_open(self):
        slots = parse(load("bezazian_oct"), 65884)["2026-10-18"]
        result = evaluate(slots, window("2026-10-18", SUN))
        self.assertEqual(result.status, OPEN)
        self.assertTrue(result.is_open)

    def test_bezazian_saturday_oct_24_is_fully_booked(self):
        slots = parse(load("bezazian_oct"), 65884)["2026-10-24"]
        result = evaluate(slots, window("2026-10-24", SAT))
        self.assertEqual(result.status, FULLY_BOOKED)
        self.assertEqual(result.free, ())

    def test_uptown_saturday_oct_24_is_open(self):
        slots = parse(load("uptown_oct"), 65913)["2026-10-24"]
        self.assertEqual(evaluate(slots, window("2026-10-24", SAT)).status, OPEN)

    def test_rogers_park_saturday_oct_17_is_partial_with_ranges(self):
        slots = parse(load("rogers_park_oct"), 65911)["2026-10-17"]
        result = evaluate(slots, window("2026-10-17", SAT))
        self.assertEqual(result.status, PARTIAL)
        self.assertIn("9:30am–12:30pm", result.free)
        self.assertTrue(result.blocked, "should name the slots that block the window")

    def test_partial_is_not_collapsed_into_booked(self):
        slots = parse(load("uptown_oct"), 65913)["2026-10-17"]
        result = evaluate(slots, window("2026-10-17", SAT))
        self.assertEqual(result.status, PARTIAL)
        self.assertNotEqual(result.status, FULLY_BOOKED)
        self.assertTrue(result.free)

    def test_edgewater_is_booked_on_every_sampled_weekend_date(self):
        payload = load("edgewater_oct")
        dates = ["2026-10-17", "2026-10-18", "2026-10-24", "2026-10-25", "2026-10-31", "2026-11-01"]
        for eid in (65906, 65907):
            days = parse(payload, eid)
            for day in dates:
                times = SAT if date.fromisoformat(day).weekday() == 5 else SUN
                result = evaluate(days[day], window(day, times))
                self.assertNotEqual(result.status, OPEN, f"eid {eid} {day} was expected booked")

    def test_a_day_with_no_slots_reads_as_closed(self):
        self.assertEqual(evaluate({}, window("2026-10-17", SAT)).status, CLOSED)

    def test_sunday_hours_are_an_exact_fit(self):
        """Sunday runs 13:30-16:30, so the fallback window has zero slack."""
        slots = parse(load("bezazian_oct"), 65884)["2026-10-18"]
        self.assertEqual(len(slots), 6)
        self.assertEqual(evaluate(slots, window("2026-10-18", SUN)).hours, "1:30pm–4:30pm")

    def test_one_booked_slot_anywhere_in_the_window_blocks_it(self):
        slots = {t: AVAILABLE for t in window("2026-10-17", SAT).slot_starts()}
        self.assertEqual(evaluate(slots, window("2026-10-17", SAT)).status, OPEN)
        slots["14:00"] = BOOKED
        result = evaluate(slots, window("2026-10-17", SAT))
        self.assertEqual(result.status, PARTIAL)
        self.assertIn("14:00", result.blocked)

    def test_padding_inside_the_window_blocks_it_too(self):
        slots = {t: AVAILABLE for t in window("2026-10-17", SAT).slot_starts()}
        slots["12:00"] = PADDING
        result = evaluate(slots, window("2026-10-17", SAT))
        self.assertNotEqual(result.status, OPEN, "padding is not bookable")
        self.assertIn("12:00", result.blocked)


class FreeRanges(unittest.TestCase):
    def test_contiguous_runs_are_merged(self):
        slots = {"09:30": AVAILABLE, "10:00": AVAILABLE, "10:30": BOOKED, "11:00": AVAILABLE}
        self.assertEqual(free_ranges(slots), ["9:30am–10:30am", "11:00am–11:30am"])

    def test_no_availability_gives_no_ranges(self):
        self.assertEqual(free_ranges({"09:30": BOOKED, "10:00": PADDING}), [])

    def test_afternoon_times_render_as_pm(self):
        self.assertEqual(free_ranges({"16:00": AVAILABLE}), ["4:00pm–4:30pm"])


if __name__ == "__main__":
    unittest.main()


class LeadWindow(unittest.TestCase):
    """LibCal withdraws all slots inside the minimum-lead window.

    Verified 2026-08-11: the earliest date offering slots was exactly today+7, and a
    fixture captured on 2026-08-08 shows the same date (Aug 15) with a full 14 slots
    when it was still 7 days out. So an empty day is ambiguous — either the branch is
    closed or the date is too soon — and only the caller knows which.
    """

    def test_a_day_with_no_slots_is_reported_as_closed_not_open(self):
        result = evaluate({}, window("2026-08-15", SAT))
        self.assertEqual(result.status, CLOSED)
        self.assertFalse(result.is_open)
        self.assertEqual(result.free, ())

    def test_the_fixtures_prove_the_date_itself_was_not_closed(self):
        """Both fixtures captured 2026-08-08 show Aug 15 with a full 14-slot day.

        On 2026-08-11 the same date returned zero slots for every room. Nothing about
        the branches changed — the date had simply moved inside the lead window.
        """
        for name, eid in (("lincoln_park_aug", 65898), ("merlo_aug", 65864)):
            slots = parse(load(name), eid)["2026-08-15"]
            self.assertEqual(len(slots), 14, f"{name}: Aug 15 was a normal open day")

    def test_merlo_aug_15_was_open_but_almost_entirely_booked(self):
        """Only 09:30 was free, so the 11:30-16:30 window was never achievable."""
        slots = parse(load("merlo_aug"), 65864)["2026-08-15"]
        free = sorted(t for t, state in slots.items() if state == AVAILABLE)
        self.assertEqual(free, ["09:30"])
        self.assertEqual(evaluate(slots, window("2026-08-15", SAT)).status, PARTIAL)

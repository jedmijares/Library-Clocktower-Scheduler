import json
import unittest
from datetime import date
from pathlib import Path

from libcal_scheduler.client import Reserver
from libcal_scheduler.config import FieldMap, Room

MAP = FieldMap(name="standard", fields={"purpose": "q10096"}, required=("purpose",))
ROOM = Room(
    id="bezazian", branch="Bezazian", room="Meeting Room", capacity=65, enabled=True,
    lid=9208, gid=16884, eid=65884, field_map=MAP, field_map_verified="2026-08-09",
)


class FakeHttp:
    """Records payloads instead of sending them."""

    def __init__(self, response=None):
        self.sent = []
        self.response = response or {"slots": []}
        self.exchanges = []

    def post_json(self, path, payload, *, referer_lid):
        self.sent.append((path, payload, referer_lid))
        return self.response


class GridRange(unittest.TestCase):
    def test_end_is_sent_one_day_past_the_inclusive_last_day(self):
        """LibCal's `end` is exclusive: asking for end=Nov 2 returns through Nov 1.

        Verified against a captured response, and the reason a single-day request used
        to come back empty.
        """
        http = FakeHttp()
        Reserver(http).grid(9208, 16884, date(2026, 11, 7), date(2026, 11, 8))
        _, payload, lid = http.sent[0]
        self.assertEqual(payload["start"], "2026-11-07")
        self.assertEqual(payload["end"], "2026-11-09", "end must be exclusive-adjusted")
        self.assertEqual(lid, 9208, "Referer lid is mandatory")

    def test_a_single_day_request_still_covers_that_day(self):
        http = FakeHttp()
        Reserver(http).availability(ROOM, date(2026, 11, 7), date(2026, 11, 7))
        _, payload, _ = http.sent[0]
        self.assertEqual((payload["start"], payload["end"]), ("2026-11-07", "2026-11-08"))

    def test_eid_is_sent_as_minus_one_because_the_api_ignores_it(self):
        http = FakeHttp()
        Reserver(http).grid(9208, 16884, date(2026, 11, 7), date(2026, 11, 7))
        self.assertEqual(http.sent[0][1]["eid"], -1)

    def test_the_captured_fixture_confirms_exclusivity(self):
        path = Path(__file__).resolve().parent / "fixtures" / "grids" / "bezazian_oct.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        days = {s["start"][:10] for s in payload["slots"]}
        # fetched with start=2026-10-17 end=2026-11-02
        self.assertIn("2026-11-01", days)
        self.assertNotIn("2026-11-02", days)


if __name__ == "__main__":
    unittest.main()

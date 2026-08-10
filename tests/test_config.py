import copy
import json
import unittest
from pathlib import Path

from libcal_scheduler.config import (
    KNOWN_HONEYPOT_NAMES,
    ConfigError,
    validate,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"


def good_config() -> dict:
    """The shipped example with the placeholder personal fields filled in."""
    cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    cfg["requester"].update(
        firstName="Ada",
        lastName="Lovelace",
        email="ada@example.com",
        telephone="312-555-0100",
        address="1150 W Fullerton Ave, Chicago IL 60614",
    )
    # Every room caps at >= 35; keep attendance inside all of them.
    cfg["answers"]["expectedAttendance"] = 20
    # West Loop requires an organization answer even for an Individual request.
    cfg["answers"]["organization"] = "Informal community group — no website"
    return cfg


def problems_of(cfg) -> list[str]:
    try:
        validate(cfg)
    except ConfigError as err:
        return err.problems
    return []


class ExampleConfig(unittest.TestCase):
    def test_example_validates_once_filled_in(self):
        cfg = validate(good_config())
        self.assertEqual(len(cfg.rooms), 8)
        self.assertEqual(cfg.slots["saturday"].start, "11:30")
        self.assertEqual(cfg.slots["sunday"].end, "16:30")
        # LibCal enforces a flat 90 days, not three calendar months — probed
        # 2026-08-09, where the last day offering slots was exactly today+90.
        self.assertEqual(cfg.advance_days, 90)
        self.assertEqual(cfg.min_lead_days, 7)

    def test_documentation_keys_are_stripped(self):
        cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertIn("_comment", cfg, "the example does carry doc keys")
        validated = validate(good_config())
        # _branches lives inside fieldMaps; if stripping missed it, the unknown-key
        # check in _validate_field_maps would have complained.
        self.assertEqual(validated.rooms[0].field_map.name, "standard")

    def test_all_missing_requester_fields_reported_at_once(self):
        cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        found = problems_of(cfg)
        for missing in ("firstName", "lastName", "email", "telephone", "address"):
            self.assertTrue(
                any(p.startswith(f"requester.{missing}:") for p in found),
                f"expected a problem for requester.{missing}, got:\n" + "\n".join(found),
            )


class Honeypots(unittest.TestCase):
    def test_example_never_maps_a_honeypot_name(self):
        cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        blob = json.dumps(cfg["fieldMaps"])
        for name in KNOWN_HONEYPOT_NAMES:
            self.assertNotIn(
                f'"{name}"', blob, f"{name} is a honeypot and must not appear in a field map"
            )

    def test_mapping_a_honeypot_name_is_rejected(self):
        for name in ("Zip", "Feedback", "Name", "Question", "URL"):
            cfg = good_config()
            cfg["fieldMaps"]["standard"]["fields"]["address"] = name
            found = problems_of(cfg)
            self.assertTrue(
                any("honeypot" in p for p in found),
                f"mapping address to {name!r} should be refused, got:\n" + "\n".join(found),
            )

    def test_requester_zip_is_rejected_with_an_explanation(self):
        cfg = good_config()
        cfg["requester"]["zip"] = "60614"
        found = problems_of(cfg)
        self.assertTrue(any(p.startswith("requester.zip:") for p in found))
        self.assertTrue(any("honeypot" in p for p in found))


class FieldMaps(unittest.TestCase):
    def test_six_branches_share_standard_and_west_loop_differs(self):
        cfg = validate(good_config())
        standard = [r for r in cfg.rooms if r.field_map.name == "standard"]
        west_loop = [r for r in cfg.rooms if r.field_map.name == "west_loop"]
        self.assertEqual(len(west_loop), 1)
        self.assertEqual(len(standard), 7)  # 8 rooms, one of which is West Loop

    def test_west_loop_requires_what_others_leave_optional(self):
        cfg = validate(good_config())
        standard = cfg.room("lincoln-park").field_map
        wl = cfg.room("west-loop").field_map
        for key in ("organization", "speakers", "pressMedia"):
            self.assertNotIn(key, standard.required, f"{key} optional on standard")
            self.assertIn(key, wl.required, f"{key} required at West Loop")
        self.assertEqual(standard.field_names("speakers"), ["q10097"])
        self.assertEqual(wl.field_names("speakers"), ["q32154"])

    def test_a_key_may_map_to_several_inputs(self):
        cfg = validate(good_config())
        wl = cfg.room("west-loop").field_map
        # West Loop asks for the organization twice: optional q10095 and required q32153
        self.assertEqual(wl.field_names("organization"), ["q10095", "q32153"])
        self.assertEqual(wl.field_names("nothingHere"), [])

    def test_unknown_field_map_reference_is_reported(self):
        cfg = good_config()
        cfg["rooms"][0]["fieldMap"] = "nope"
        found = problems_of(cfg)
        self.assertTrue(any("'nope' is not defined in fieldMaps" in p for p in found))

    def test_required_key_absent_from_fields_is_reported(self):
        cfg = good_config()
        cfg["fieldMaps"]["standard"]["required"].append("nonsense")
        found = problems_of(cfg)
        self.assertTrue(any("'nonsense'" in p for p in found))

    def test_identity_fields_may_not_be_mapped(self):
        cfg = good_config()
        cfg["fieldMaps"]["standard"]["fields"]["email"] = "email"
        found = problems_of(cfg)
        self.assertTrue(any("identity fields are handled automatically" in p for p in found))


class Rooms(unittest.TestCase):
    def test_attendance_over_a_room_capacity_is_rejected(self):
        cfg = good_config()
        cfg["answers"]["expectedAttendance"] = 60  # fine for the 80s, too many for Merlo (46)
        found = problems_of(cfg)
        self.assertTrue(
            any("merlo" in p and "exceeds capacity 46" in p for p in found),
            "\n".join(found),
        )

    def test_attendance_within_every_enabled_room_passes(self):
        cfg = good_config()
        cfg["answers"]["expectedAttendance"] = 35  # smallest capacity is Edgewater A at 35
        self.assertEqual(problems_of(cfg), [])

    def test_disabling_a_small_room_permits_larger_attendance(self):
        cfg = good_config()
        cfg["answers"]["expectedAttendance"] = 60
        for room in cfg["rooms"]:
            if room["capacity"] < 60:
                room["enabled"] = False
        self.assertEqual(problems_of(cfg), [])

    def test_unverified_field_map_blocks_booking_but_not_availability(self):
        cfg = good_config()
        cfg["rooms"][0]["fieldMapVerified"] = None
        validated = validate(cfg)  # still a valid config
        room = validated.rooms[0]
        self.assertTrue(room.enabled, "still readable for availability")
        self.assertFalse(room.bookable, "but not bookable")

    def test_duplicate_room_ids_are_caught(self):
        cfg = good_config()
        cfg["rooms"][1]["id"] = cfg["rooms"][0]["id"]
        self.assertTrue(any("duplicate id" in p for p in problems_of(cfg)))

    def test_room_label_and_lookup(self):
        cfg = validate(good_config())
        self.assertEqual(cfg.room("bezazian").label, "Bezazian · Meeting Room")
        self.assertIsNone(cfg.room("does-not-exist"))


class EventWindow(unittest.TestCase):
    def test_slot_times_must_be_hhmm(self):
        cfg = good_config()
        cfg["event"]["slots"]["saturday"] = {"start": "4:30pm", "end": "16:30"}
        self.assertTrue(any("slots.saturday.start" in p for p in problems_of(cfg)))

    def test_slot_end_must_follow_start(self):
        cfg = good_config()
        cfg["event"]["slots"]["sunday"] = {"start": "16:30", "end": "13:30"}
        self.assertTrue(
            any("end (13:30) must be after start (16:30)" in p for p in problems_of(cfg))
        )

    def test_organizational_request_demands_an_organization(self):
        cfg = good_config()
        cfg["answers"]["requestType"] = "Organizational"
        cfg["answers"]["organization"] = ""
        self.assertTrue(any(p.startswith("answers.organization:") for p in problems_of(cfg)))

    def test_a_branch_specific_answer_gap_is_not_a_config_error(self):
        """West Loop needs `speakers`; the others do not.

        Which room is being booked is not known at load time, so this is enforced in
        guardrails instead — see test_guardrails.RequiredAnswers.
        """
        cfg = good_config()
        cfg["answers"]["speakers"] = ""
        self.assertEqual(problems_of(cfg), [])


if __name__ == "__main__":
    unittest.main()

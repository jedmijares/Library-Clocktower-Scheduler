import json
import unittest
from pathlib import Path

from libcal_scheduler.config import FieldMap, validate
from libcal_scheduler.form import PayloadError, build_payload, parse_form, pick_bucket

ROOT = Path(__file__).resolve().parent
FORMS = ROOT / "fixtures" / "forms"

# Honeypot field names observed per branch. Five distinct names across seven forms.
EXPECTED_HONEYPOTS = {
    "lincoln_park": "Zip",
    "west_loop": "Zip",
    "bezazian": "Zip",
    "merlo": "URL",
    "rogers_park": "Feedback",
    "uptown": "Name",
    "edgewater": "Question",
}

STANDARD = FieldMap(
    name="standard",
    fields={
        "telephone": "q10094",
        "address": "q10093",
        "requestType": "q10092",
        "organization": "q10095",
        "purpose": "q10096",
        "speakers": "q10097",
        "pressMedia": "q10098",
        "expectedAttendance": "q10099",
        "involvesAnimalsCookingMedicalPhysical": "q10100",
        "acknowledgements": ["q29731[]", "q29746[]"],
    },
    required=(
        "telephone",
        "address",
        "requestType",
        "purpose",
        "expectedAttendance",
        "involvesAnimalsCookingMedicalPhysical",
    ),
)

WEST_LOOP = FieldMap(
    name="west_loop",
    fields={
        **STANDARD.fields,
        "organization": ["q10095", "q32153"],
        "speakers": "q32154",
        "pressMedia": "q32155",
        "acknowledgements": ["q29731[]", "q29746[]", "q18806[]"],
    },
    required=STANDARD.required + ("organization", "speakers", "pressMedia"),
)

REQUESTER = {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "email": "ada@example.com",
    "telephone": "312-555-0100",
    "address": "1150 W Fullerton Ave, Chicago IL 60614",
}

VALUES = {
    "telephone": REQUESTER["telephone"],
    "address": REQUESTER["address"],
    "requestType": "Individual",
    "organization": "Informal community group — no website",
    "purpose": "Recurring community meetup.",
    "speakers": "None",
    "pressMedia": "None",
    "expectedAttendance": 20,
    "involvesAnimalsCookingMedicalPhysical": "No",
}


def load(branch: str):
    return parse_form((FORMS / f"{branch}.html").read_text(encoding="utf-8"))


class HoneypotDetection(unittest.TestCase):
    def test_every_branch_has_exactly_one_honeypot_and_we_find_it(self):
        for branch, expected in EXPECTED_HONEYPOTS.items():
            form = load(branch)
            names = [f.name for f in form.honeypots]
            self.assertEqual(names, [expected], f"{branch}: honeypots were {names}")

    def test_the_behavioural_signal_fires_for_all_seven(self):
        """removeChild is preferred over the class, since classes are likelier to churn."""
        for branch in EXPECTED_HONEYPOTS:
            form = load(branch)
            self.assertIn("removed-by-script", form.honeypots[0].signals, branch)

    def test_both_signals_currently_agree(self):
        for branch in EXPECTED_HONEYPOTS:
            form = load(branch)
            self.assertEqual(
                set(form.honeypots[0].signals),
                {"removed-by-script", "honeypot-class"},
                f"{branch}: signals disagree, which is worth surfacing",
            )

    def test_honeypots_are_marked_required_as_bait(self):
        for branch in EXPECTED_HONEYPOTS:
            self.assertTrue(load(branch).honeypots[0].required, branch)

    def test_honeypot_names_rotate_so_a_denylist_would_be_useless(self):
        distinct = set(EXPECTED_HONEYPOTS.values())
        self.assertGreater(len(distinct), 3, "names vary across branches")
        self.assertEqual(distinct, {"Zip", "URL", "Feedback", "Name", "Question"})

    def test_honeypots_never_collide_with_a_real_field_name(self):
        for branch in EXPECTED_HONEYPOTS:
            form = load(branch)
            real = {f.name for f in form.real_fields}
            trapped = {f.name for f in form.honeypots}
            self.assertEqual(real & trapped, set(), branch)

    def test_by_name_excludes_honeypots(self):
        form = load("lincoln_park")
        self.assertEqual(form.by_name("Zip"), [], "Zip must be invisible to lookups")


class FormStructure(unittest.TestCase):
    def test_session_token_is_captured(self):
        form = load("lincoln_park")
        self.assertTrue(form.session)
        self.assertRegex(form.session, r"^\d+$")

    def test_submit_action_is_the_book_endpoint(self):
        self.assertEqual(load("lincoln_park").action, "/ajax/space/book")

    def test_identity_fields_are_present_everywhere(self):
        for branch in EXPECTED_HONEYPOTS:
            form = load(branch)
            for name in ("fname", "lname", "email"):
                self.assertTrue(form.by_name(name), f"{branch} missing {name}")

    def test_west_loop_requires_ids_the_others_leave_optional(self):
        wl = load("west_loop").required_names()
        lp = load("lincoln_park").required_names()
        for name in ("q32153", "q32154", "q32155"):
            self.assertIn(name, wl)
            self.assertNotIn(name, lp)
        self.assertNotIn("q10097", lp - {"q10097"})

    def test_six_branches_share_the_standard_question_set(self):
        baseline = load("lincoln_park").required_names() - {"Zip"}
        for branch in ("bezazian", "merlo", "rogers_park", "uptown", "edgewater"):
            other = load(branch).required_names() - {EXPECTED_HONEYPOTS[branch]}
            self.assertEqual(other, baseline, f"{branch} diverges from the standard map")


class AttendanceBuckets(unittest.TestCase):
    def test_options_are_ranges_not_numbers(self):
        options = load("lincoln_park").options_for("q10099")
        self.assertIn("11-20", options)
        self.assertIn("126-178", options)
        self.assertNotIn("20", options)

    def test_bucket_selection(self):
        options = load("lincoln_park").options_for("q10099")
        self.assertEqual(pick_bucket(options, 20), "11-20")
        self.assertEqual(pick_bucket(options, 1), "1-5")
        self.assertEqual(pick_bucket(options, 65), "61-70")
        self.assertIsNone(pick_bucket(options, 500))

    def test_options_are_not_capped_at_room_capacity(self):
        """Lincoln Park's room holds 80 yet the dropdown still offers more."""
        options = load("lincoln_park").options_for("q10099")
        self.assertIn("81-90", options)


class PayloadBuilding(unittest.TestCase):
    def test_standard_branch_payload(self):
        form = load("bezazian")
        payload = build_payload(form, STANDARD, VALUES, REQUESTER)
        self.assertEqual(payload["fname"], "Ada")
        self.assertEqual(payload["q10094"], "312-555-0100")
        self.assertEqual(payload["q10099"], "11-20")
        self.assertEqual(payload["q10092"], "Individual")
        self.assertEqual(payload["q29731[]"], "I understand.")

    def test_payload_never_contains_the_honeypot(self):
        for branch, trap in EXPECTED_HONEYPOTS.items():
            field_map = WEST_LOOP if branch == "west_loop" else STANDARD
            payload = build_payload(load(branch), field_map, VALUES, REQUESTER)
            self.assertNotIn(trap, payload, f"{branch}: honeypot {trap} was filled")

    def test_payload_contains_nothing_outside_the_allowlist(self):
        form = load("uptown")
        payload = build_payload(form, STANDARD, VALUES, REQUESTER)
        allowed = {"fname", "lname", "email"}
        for target in STANDARD.fields.values():
            allowed.update(target if isinstance(target, list) else [target])
        self.assertEqual(set(payload) - allowed, set())

    def test_west_loop_fills_the_duplicated_organization_fields(self):
        payload = build_payload(load("west_loop"), WEST_LOOP, VALUES, REQUESTER)
        self.assertEqual(payload["q10095"], payload["q32153"])
        self.assertEqual(payload["q32154"], "None")
        self.assertEqual(payload["q18806[]"], "I understand.")

    def test_missing_required_answer_refuses_rather_than_partially_submitting(self):
        values = dict(VALUES, purpose="")
        with self.assertRaises(PayloadError) as caught:
            build_payload(load("bezazian"), STANDARD, values, REQUESTER)
        self.assertTrue(any("purpose" in p for p in caught.exception.problems))

    def test_attendance_outside_every_bucket_refuses(self):
        values = dict(VALUES, expectedAttendance=5000)
        with self.assertRaises(PayloadError) as caught:
            build_payload(load("bezazian"), STANDARD, values, REQUESTER)
        self.assertTrue(any("expectedAttendance" in p for p in caught.exception.problems))

    def test_value_outside_a_selects_options_refuses(self):
        values = dict(VALUES, requestType="Sideways")
        with self.assertRaises(PayloadError) as caught:
            build_payload(load("bezazian"), STANDARD, values, REQUESTER)
        self.assertTrue(any("Sideways" in p for p in caught.exception.problems))

    def test_west_loop_map_on_a_standard_branch_refuses(self):
        """q32154 does not exist outside West Loop, and it is required there."""
        with self.assertRaises(PayloadError) as caught:
            build_payload(load("bezazian"), WEST_LOOP, VALUES, REQUESTER)
        self.assertTrue(any("q3215" in p for p in caught.exception.problems))

    def test_optional_blank_answers_are_simply_omitted(self):
        values = dict(VALUES, speakers="", pressMedia="", organization="")
        payload = build_payload(load("bezazian"), STANDARD, values, REQUESTER)
        for name in ("q10097", "q10098", "q10095"):
            self.assertNotIn(name, payload)

    def test_acknowledgements_can_be_left_unchecked(self):
        payload = build_payload(
            load("bezazian"), STANDARD, VALUES, REQUESTER, auto_check_acknowledgements=False
        )
        self.assertNotIn("q29731[]", payload)

    def test_payload_matches_the_example_config_field_maps(self):
        """Guards against the config and the parser drifting apart."""
        cfg = json.loads((ROOT.parent / "config.example.json").read_text(encoding="utf-8"))
        cfg["requester"].update(REQUESTER)
        cfg["answers"]["organization"] = VALUES["organization"]
        validated = validate(cfg)
        for room in validated.enabled_rooms:
            branch = {
                "lincoln-park": "lincoln_park",
                "bezazian": "bezazian",
                "uptown": "uptown",
                "merlo": "merlo",
                "rogers-park": "rogers_park",
                "west-loop": "west_loop",
                "edgewater-a": "edgewater",
                "edgewater-b": "edgewater",
            }[room.id]
            payload = build_payload(
                load(branch), room.field_map, validated.values, validated.requester
            )
            self.assertNotIn(EXPECTED_HONEYPOTS[branch], payload, room.id)
            self.assertIn("q10096", payload, room.id)


if __name__ == "__main__":
    unittest.main()

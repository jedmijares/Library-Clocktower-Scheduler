"""Configuration loading and validation.

Validation accumulates every problem it finds rather than raising on the first,
because a half-filled config should report all its gaps in one run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Answer keys the tool understands. Each field map routes these to the question
# ids used by that branch's form.
SEMANTIC_KEYS = (
    "telephone",
    "address",
    "requestType",
    "organization",
    "purpose",
    "speakers",
    "pressMedia",
    "expectedAttendance",
    "involvesAnimalsCookingMedicalPhysical",
)

# Identity fields, present under these names on every branch's form.
IDENTITY_FIELDS = ("fname", "lname", "email")

# Semantic keys whose values live under `requester` rather than `answers`.
SEMANTIC_FROM_REQUESTER = ("telephone", "address")

# Field names observed as honeypots across all seven CPL forms (2026-08-09).
# The real protection is positional exclusion in form.py plus the allowlist;
# this list only exists so a config that mentions one fails validation loudly.
# It is NOT a denylist the submitter relies on — the names rotate per branch.
KNOWN_HONEYPOT_NAMES = frozenset({"Zip", "Feedback", "Name", "Question", "URL"})

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("config is invalid:\n  - " + "\n  - ".join(problems))


@dataclass(frozen=True)
class FieldMap:
    name: str
    fields: dict[str, str | list[str]]
    required: tuple[str, ...]

    def field_names(self, key: str) -> list[str]:
        """Form field names for a semantic key. A key may map to several inputs —
        West Loop asks for the organization twice."""
        value = self.fields.get(key)
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]


@dataclass(frozen=True)
class Room:
    id: str
    branch: str
    room: str
    capacity: int
    enabled: bool
    lid: int
    gid: int
    eid: int
    field_map: FieldMap
    field_map_verified: str | None
    notes: str = ""
    warn: str = ""

    @property
    def label(self) -> str:
        return f"{self.branch} · {self.room}"

    @property
    def bookable(self) -> bool:
        """Availability is readable without a verified field map; booking is not."""
        return self.enabled and self.field_map_verified is not None


@dataclass(frozen=True)
class Slot:
    start: str  # "HH:MM"
    end: str  # "HH:MM"


@dataclass(frozen=True)
class Config:
    event_name: str
    slots: dict[str, Slot]  # "saturday" | "sunday"
    advance_days: int
    min_lead_days: int
    requester: dict[str, str]
    answers: dict[str, object]
    auto_check_acknowledgements: bool
    rooms: tuple[Room, ...]

    @property
    def values(self) -> dict[str, object]:
        """Semantic answer values, merged from `requester` and `answers`.

        The form does not care which config section a value came from — `telephone`
        and `address` happen to live under requester, the rest under answers — so
        everything downstream works from this single mapping.
        """
        return {**self.answers, **{k: self.requester.get(k, "") for k in SEMANTIC_FROM_REQUESTER}}

    @property
    def enabled_rooms(self) -> tuple[Room, ...]:
        return tuple(r for r in self.rooms if r.enabled)

    def room(self, room_id: str) -> Room | None:
        return next((r for r in self.rooms if r.id == room_id), None)


def _strip_doc_keys(value):
    """Drop the _comment / _note / _branches documentation keys used in the example."""
    if isinstance(value, list):
        return [_strip_doc_keys(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_doc_keys(v) for k, v in value.items() if not k.startswith("_")}
    return value


def _minutes(hhmm: str) -> int:
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


def _validate_field_maps(raw: dict, problems: list[str]) -> dict[str, FieldMap]:
    maps: dict[str, FieldMap] = {}
    if not isinstance(raw, dict) or not raw:
        problems.append("fieldMaps: must be a non-empty object")
        return maps

    for name, spec in raw.items():
        at = f"fieldMaps.{name}"
        if not isinstance(spec, dict):
            problems.append(f"{at}: must be an object")
            continue
        fields = spec.get("fields")
        if not isinstance(fields, dict) or not fields:
            problems.append(f"{at}.fields: must be a non-empty object")
            continue

        clean: dict[str, str | list[str]] = {}
        for key, value in fields.items():
            if key in IDENTITY_FIELDS:
                problems.append(f"{at}.fields.{key}: identity fields are handled automatically")
                continue
            if key != "acknowledgements" and key not in SEMANTIC_KEYS:
                problems.append(f"{at}.fields: unknown key {key!r}")
                continue
            names = value if isinstance(value, list) else [value]
            if not names or any(not isinstance(n, str) or not n for n in names):
                problems.append(f"{at}.fields.{key}: must be a field name or list of field names")
                continue
            # Belt and braces on top of positional exclusion and the allowlist.
            trapped = KNOWN_HONEYPOT_NAMES.intersection(names)
            if trapped:
                problems.append(
                    f"{at}.fields.{key}: {sorted(trapped)} is a known honeypot field name — "
                    "it must never appear in a field map"
                )
                continue
            clean[key] = value

        required = spec.get("required", [])
        if not isinstance(required, list):
            problems.append(f"{at}.required: must be a list")
            required = []
        for key in required:
            if key not in clean:
                problems.append(f"{at}.required lists {key!r} but fields has no entry for it")

        maps[name] = FieldMap(name=name, fields=clean, required=tuple(required))
    return maps


def _validate_rooms(raw, maps: dict[str, FieldMap], attendance, problems: list[str]) -> list[Room]:
    rooms: list[Room] = []
    if not isinstance(raw, list) or not raw:
        problems.append("rooms: must be a non-empty list")
        return rooms

    seen: set[str] = set()
    for index, entry in enumerate(raw):
        at = f"rooms[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{at}: must be an object")
            continue
        room_id = entry.get("id")
        at = f"rooms[{index}]" + (f" ({room_id})" if room_id else "")
        if not isinstance(room_id, str) or not room_id.strip():
            problems.append(f"{at}.id: must be a non-empty string")
            room_id = f"__invalid_{index}"
        elif room_id in seen:
            problems.append(f"{at}.id: duplicate id {room_id!r}")
        seen.add(room_id)

        ids: dict[str, int] = {}
        for key in ("lid", "gid", "eid", "capacity"):
            value = entry.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                problems.append(f"{at}.{key}: must be a positive integer")
                ids[key] = 0
            else:
                ids[key] = value

        map_name = entry.get("fieldMap")
        field_map = maps.get(map_name) if isinstance(map_name, str) else None
        if field_map is None:
            problems.append(
                f"{at}.fieldMap: {map_name!r} is not defined in fieldMaps "
                f"(have: {sorted(maps) or 'none'})"
            )

        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            problems.append(f"{at}.enabled: must be true or false")
            enabled = False

        # CPL policy: "The number attending a meeting may not exceed the established
        # capacity of the room." LibCal's dropdown does NOT enforce this, so we do.
        if enabled and isinstance(attendance, int) and ids["capacity"] and attendance > ids["capacity"]:
            problems.append(
                f"{at}: answers.expectedAttendance {attendance} exceeds capacity {ids['capacity']}"
            )

        verified = entry.get("fieldMapVerified")
        if verified is not None and not isinstance(verified, str):
            problems.append(f"{at}.fieldMapVerified: must be a date string or null")
            verified = None

        if field_map is None:
            continue
        rooms.append(
            Room(
                id=room_id,
                branch=str(entry.get("branch", "")).strip() or room_id,
                room=str(entry.get("room", "")).strip() or "Meeting Room",
                capacity=ids["capacity"],
                enabled=enabled,
                lid=ids["lid"],
                gid=ids["gid"],
                eid=ids["eid"],
                field_map=field_map,
                field_map_verified=verified,
                notes=str(entry.get("notes", "")),
                warn=str(entry.get("warn", "")),
            )
        )

    if rooms and not any(r.enabled for r in rooms):
        problems.append("rooms: at least one room must be enabled")
    return rooms


def validate(raw: object, *, strict: bool = True) -> Config:
    """Validate a config.

    `strict=False` tolerates unfilled personal details and answers, which `check`
    never reads — structural problems are still fatal. `book` uses strict=True.
    """
    problems: list[str] = []
    gaps: list[str] = []  # completeness, only fatal when strict
    data = _strip_doc_keys(raw)
    if not isinstance(data, dict):
        raise ConfigError(["config must be a JSON object"])

    # --- event ---------------------------------------------------------------
    event = data.get("event")
    slots: dict[str, Slot] = {}
    event_name, advance_days, min_lead_days = "", 90, 7
    if not isinstance(event, dict):
        problems.append("event: missing")
    else:
        event_name = str(event.get("name", "")).strip()
        if not event_name:
            problems.append("event.name: must not be empty")
        raw_slots = event.get("slots")
        if not isinstance(raw_slots, dict):
            problems.append("event.slots: missing")
            raw_slots = {}
        for day in ("saturday", "sunday"):
            slot = raw_slots.get(day)
            if not isinstance(slot, dict):
                problems.append(f"event.slots.{day}: missing")
                continue
            start, end = slot.get("start"), slot.get("end")
            bad = False
            for label, value in (("start", start), ("end", end)):
                if not isinstance(value, str) or not TIME_RE.match(value):
                    problems.append(f"event.slots.{day}.{label}: must be HH:MM, got {value!r}")
                    bad = True
            if not bad and _minutes(end) <= _minutes(start):
                problems.append(f"event.slots.{day}: end ({end}) must be after start ({start})")
                bad = True
            if not bad:
                slots[day] = Slot(start=start, end=end)

        advance_days = event.get("advanceDays", 90)
        if not isinstance(advance_days, int) or isinstance(advance_days, bool) or advance_days < 1:
            problems.append("event.advanceDays: must be a positive integer")
            advance_days = 90
        min_lead_days = event.get("minLeadDays", 7)
        if not isinstance(min_lead_days, int) or isinstance(min_lead_days, bool) or min_lead_days < 0:
            problems.append("event.minLeadDays: must be a non-negative integer")
            min_lead_days = 7

    # --- requester ------------------------------------------------------------
    requester: dict[str, str] = {}
    raw_requester = data.get("requester")
    if not isinstance(raw_requester, dict):
        problems.append("requester: missing")
    else:
        if "zip" in raw_requester:
            problems.append(
                "requester.zip: remove it — no branch has a real zip field. The input named "
                "'Zip' on some branches is a honeypot; put the zip inside requester.address."
            )
        for key in ("firstName", "lastName", "email", "telephone", "address"):
            value = raw_requester.get(key)
            if not isinstance(value, str) or not value.strip():
                gaps.append(f"requester.{key}: must be filled in — the booking form requires it")
                continue
            requester[key] = value.strip()
        email = requester.get("email", "")
        if email and not EMAIL_RE.match(email):
            problems.append(f"requester.email: does not look like an email ({email})")

    # --- answers --------------------------------------------------------------
    answers: dict[str, object] = {}
    raw_answers = data.get("answers")
    attendance: object = None
    if not isinstance(raw_answers, dict):
        problems.append("answers: missing")
    else:
        answers = dict(raw_answers)
        if not str(answers.get("purpose", "")).strip():
            gaps.append("answers.purpose: must be filled in — required at every branch")
        if answers.get("requestType") not in ("Individual", "Organizational"):
            problems.append(
                f"answers.requestType: must be 'Individual' or 'Organizational', "
                f"got {answers.get('requestType')!r}"
            )
        if answers.get("involvesAnimalsCookingMedicalPhysical") not in ("No", "Yes"):
            problems.append("answers.involvesAnimalsCookingMedicalPhysical: must be 'No' or 'Yes'")
        attendance = answers.get("expectedAttendance")
        if not isinstance(attendance, int) or isinstance(attendance, bool) or attendance < 1:
            problems.append("answers.expectedAttendance: must be a positive integer")
            attendance = None
        if answers.get("requestType") == "Organizational" and not str(answers.get("organization", "")).strip():
            gaps.append("answers.organization: required when requestType is 'Organizational'")

    acks = data.get("acknowledgements", {})
    auto_check = True
    if isinstance(acks, dict):
        auto_check = bool(acks.get("autoCheck", True))
    else:
        problems.append("acknowledgements: must be an object")

    maps = _validate_field_maps(data.get("fieldMaps"), problems)
    rooms = _validate_rooms(data.get("rooms"), maps, attendance, problems)

    # Deliberately NOT checked here: whether each enabled room's required answers have
    # values. That depends on which room is being booked — West Loop needs an
    # organization the others do not — so it belongs in guardrails, which knows the
    # choice and can refuse before any hold is created.

    if strict:
        problems.extend(gaps)
    if problems:
        raise ConfigError(problems)

    return Config(
        event_name=event_name,
        slots=slots,
        advance_days=advance_days,
        min_lead_days=min_lead_days,
        requester=requester,
        answers=answers,
        auto_check_acknowledgements=auto_check,
        rooms=tuple(rooms),
    )


def load(path: str | Path = "config.json", *, strict: bool = True) -> Config:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ConfigError(
            [f"cannot read {path} — copy config.example.json to config.json and fill it in"]
        ) from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as err:
        raise ConfigError([f"{path.name} is not valid JSON: {err}"]) from None
    return validate(raw, strict=strict)

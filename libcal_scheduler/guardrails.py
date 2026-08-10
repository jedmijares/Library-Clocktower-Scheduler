"""Checks run before a booking may be confirmed.

Split deliberately in two:

* **Refusals** stop the run. These are conditions under which submitting would be
  wrong — the wrong weekday, a date CPL will not accept, a slot that is not actually
  free, a duplicate booking, an unverified field map.
* **Warnings** are shown on the review screen and can be accepted. These are things
  worth knowing before confirming, not errors.

A human confirms every submission, so this is a second pair of eyes rather than the
only line of defence. The payload allowlist assertion lives in `form.build_payload`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .config import Config, Room
from .dates import CHICAGO, Window, bookability
from .grid import Availability, OPEN
from .history import Booking, booking_on, usage_counts


@dataclass(frozen=True)
class Verdict:
    refusals: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.refusals


def check(
    *,
    room: Room,
    window: Window,
    availability: Availability,
    config: Config,
    bookings: list[Booking],
    now: datetime,
    window_overridden: bool = False,
) -> Verdict:
    refusals: list[str] = []
    warnings: list[str] = []
    today: date = now.astimezone(CHICAGO).date()

    # --- the date is one we actually want -----------------------------------
    slot = config.slots.get(window.day.strftime("%A").lower())
    if slot is None:
        refusals.append(
            f"{window.day} is a {window.weekday_name}; only "
            f"{' and '.join(sorted(k.title() for k in config.slots))} are configured"
        )
    elif (slot.start, slot.end) != (window.start, window.end):
        # An explicit --window is a deliberate one-off, so it is surfaced rather than
        # blocked. Without the flag a mismatch means something computed the wrong
        # window, which is worth refusing over.
        message = (
            f"window {window.start}–{window.end} differs from the configured "
            f"{window.weekday_name} slot {slot.start}–{slot.end}"
        )
        (warnings if window_overridden else refusals).append(message)

    # --- CPL would accept it ------------------------------------------------
    verdict = bookability(window.day, today, config.advance_days, config.min_lead_days)
    if not verdict.ok:
        refusals.append(verdict.reason)

    # --- the room is usable -------------------------------------------------
    if not room.enabled:
        refusals.append(f"{room.label} is disabled in config")
    if room.field_map_verified is None:
        refusals.append(
            f"{room.label} has an unverified field map — run `discover` before booking it"
        )

    # Answers this branch requires must actually have values. Checked here rather than
    # at config load because it is room-specific: West Loop requires an organization,
    # speakers and press answers that every other branch leaves optional.
    values = config.values
    for key in room.field_map.required:
        if key == "acknowledgements":
            continue
        value = values.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            section = "requester" if key in ("telephone", "address") else "answers"
            refusals.append(
                f"{section}.{key} is empty, but {room.branch} requires it"
            )

    attendance = config.answers.get("expectedAttendance")
    if isinstance(attendance, int) and attendance > room.capacity:
        refusals.append(
            f"expected attendance {attendance} exceeds {room.label}'s capacity {room.capacity}; "
            "CPL requires attendance not exceed the room's established capacity"
        )

    # --- the slot is genuinely free -----------------------------------------
    if availability.status != OPEN:
        detail = ", ".join(availability.free) if availability.free else "nothing free"
        refusals.append(
            f"{room.label} is not open for the whole window on {window.day} ({detail})"
        )
    if availability.unknown_classes:
        refusals.append(
            "the availability grid contained unrecognised slot classes "
            f"{list(availability.unknown_classes)} — LibCal may have changed; not booking blind"
        )

    # --- no double booking --------------------------------------------------
    existing = booking_on(bookings, window.day)
    if existing is not None:
        refusals.append(
            f"history already records a booking on {window.day} "
            f"({existing.branch} · {existing.room}); refusing to double-book"
        )

    # --- warnings -----------------------------------------------------------
    counts = usage_counts(bookings, window=5)
    used = counts.get(room.id, 0)
    if used >= 3:
        warnings.append(
            f"{room.label} accounts for {used} of your last 5 bookings — CPL may limit how "
            "often one applicant books a branch"
        )

    if room.warn:
        warnings.append(f"{room.label}: {room.warn}")

    machine_zone = datetime.now().astimezone().tzname()
    chicago_zone = now.astimezone(CHICAGO).tzname()
    if machine_zone != chicago_zone:
        warnings.append(
            f"this machine is on {machine_zone}; all dates below are Chicago "
            f"({chicago_zone}) — {now.astimezone(CHICAGO):%a %Y-%m-%d %H:%M %Z}"
        )

    return Verdict(refusals=tuple(refusals), warnings=tuple(warnings))

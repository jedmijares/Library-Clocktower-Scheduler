"""Reading LibCal's availability grid.

`POST /spaces/availability/grid` returns a flat list of 30-minute slots, each with
an `itemId` (the room's `eid`) and an optional `className`. Two quirks matter:

* The `eid` request parameter is **ignored** — the response covers every room in the
  `gid`, so results must be filtered by `itemId` client-side.
* Availability is encoded in `className`, and the mapping was established
  empirically rather than from documentation: across a three-week sample all 98
  padding slots sat immediately adjacent to a booked slot, with zero exceptions,
  and 29 day/room combinations were entirely class-free against 11 entirely booked.

An unrecognised `className` is deliberately treated as *not available* and surfaced,
so a LibCal change shows up as a loud refusal rather than a booking on top of
someone else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .dates import Window

AVAILABLE = "available"
BOOKED = "booked"
PADDING = "padding"
UNKNOWN = "unknown"

_CLASS_STATES = {
    "": AVAILABLE,
    "s-lc-eq-checkout": BOOKED,
    "s-lc-eq-r-padding": PADDING,
}

# Statuses a window can have as a whole.
OPEN = "open"
PARTIAL = "partial"
FULLY_BOOKED = "booked"
CLOSED = "closed"


def classify(class_name: str | None) -> str:
    """Map a slot's `className` to an availability state."""
    key = (class_name or "").strip()
    if key in _CLASS_STATES:
        return _CLASS_STATES[key]
    # Substring match covers LibCal appending extra classes to the same cell.
    if "checkout" in key:
        return BOOKED
    if "padding" in key:
        return PADDING
    return UNKNOWN


def parse(payload: dict, eid: int) -> dict[str, dict[str, str]]:
    """Group one room's slots by Chicago date.

    Returns `{"YYYY-MM-DD": {"HH:MM": state}}`, filtered to `eid`.
    """
    days: dict[str, dict[str, str]] = {}
    for slot in payload.get("slots", []):
        if slot.get("itemId") != eid:
            continue
        stamp = slot.get("start", "")
        if len(stamp) < 16:
            continue
        days.setdefault(stamp[:10], {})[stamp[11:16]] = classify(slot.get("className"))
    return days


def checksums(payload: dict, eid: int) -> dict[str, str]:
    """Per-slot checksums for one room, keyed by full naive timestamp.

    Needed to start a booking; they are not short-lived (checksums ~20 minutes old
    were still accepted), so a grid read and a booking need not be back-to-back.
    """
    return {
        slot["start"]: slot["checksum"]
        for slot in payload.get("slots", [])
        if slot.get("itemId") == eid and "checksum" in slot
    }


def _pretty(hhmm: str) -> str:
    hours, mins = (int(part) for part in hhmm.split(":"))
    return f"{hours % 12 or 12}:{mins:02d}{'am' if hours < 12 else 'pm'}"


def _plus_30(hhmm: str) -> str:
    moment = datetime.strptime(hhmm, "%H:%M") + timedelta(minutes=30)
    return moment.strftime("%H:%M")


def free_ranges(day_slots: dict[str, str]) -> list[str]:
    """Contiguous runs of available time, as human-readable ranges."""
    runs: list[list[str]] = []
    current: list[str] = []
    for start in sorted(day_slots):
        if day_slots[start] == AVAILABLE:
            current.append(start)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return [f"{_pretty(run[0])}–{_pretty(_plus_30(run[-1]))}" for run in runs]


def open_hours(day_slots: dict[str, str]) -> str:
    """The branch's opening span for the day, inferred from which slots exist."""
    if not day_slots:
        return ""
    return f"{_pretty(min(day_slots))}–{_pretty(_plus_30(max(day_slots)))}"


@dataclass(frozen=True)
class Availability:
    status: str
    free: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    unknown_classes: tuple[str, ...] = ()
    hours: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == OPEN

    def summary(self) -> str:
        if self.status == OPEN:
            return "open"
        if self.status == CLOSED:
            return "closed"
        if self.status == FULLY_BOOKED:
            return "booked"
        return ", ".join(self.free) if self.free else "booked"


def evaluate(day_slots: dict[str, str], window: Window) -> Availability:
    """Is the *entire* requested window free?

    Partial availability is reported explicitly rather than collapsed into a miss —
    seeing "only 9:30–2:30 free" is far more useful than a bare "unavailable".
    """
    if not day_slots:
        return Availability(status=CLOSED)

    hours = open_hours(day_slots)
    unknown = tuple(sorted({s for s in day_slots.values() if s == UNKNOWN}))
    required = window.slot_starts()
    blocked = tuple(start for start in required if day_slots.get(start) != AVAILABLE)

    if not blocked:
        return Availability(status=OPEN, free=tuple(free_ranges(day_slots)), hours=hours)

    any_free = any(state == AVAILABLE for state in day_slots.values())
    status = PARTIAL if any_free else FULLY_BOOKED
    return Availability(
        status=status,
        free=tuple(free_ranges(day_slots)),
        blocked=blocked,
        unknown_classes=unknown,
        hours=hours,
    )

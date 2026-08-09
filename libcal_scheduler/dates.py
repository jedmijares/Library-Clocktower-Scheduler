"""Date resolution, anchored to America/Chicago.

Dates and times here are Chicago local; machine-local time is not consulted. The dev
container runs as UTC and the user travels, so `date.today()` would be wrong for
several hours a day. There are 52 dates in 2026 where a one-day slip in "what is
today" moves the resolved target Saturday by a full week.

LibCal's availability grid returns naive timestamps ("2026-08-24 10:30:00") which are
implicitly Chicago local; `parse_slot` attaches the zone.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")

SATURDAY = 5  # date.weekday(): Monday is 0
SUNDAY = 6

_DAY_KEY = {SATURDAY: "saturday", SUNDAY: "sunday"}


def now() -> datetime:
    """The current moment in Chicago. The only sanctioned source of 'now'."""
    return datetime.now(CHICAGO)


def today() -> date:
    """Today's date in Chicago — not the machine's date."""
    return now().date()


def parse_slot(stamp: str) -> datetime:
    """Parse a naive grid timestamp as Chicago local time."""
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHICAGO)


def add_months(start: date, months: int) -> date:
    """Add whole months, clamping to the last valid day of the target month.

    Aug 31 + 3 months would be Nov 31, which does not exist; it becomes Nov 30.
    """
    zero_based = start.month - 1 + months
    year = start.year + zero_based // 12
    month = zero_based % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def latest_on_or_before(day: date, weekday: int) -> date:
    """The most recent `weekday` falling on or before `day`."""
    return day - timedelta(days=(day.weekday() - weekday) % 7)


@dataclass(frozen=True)
class Window:
    """A concrete bookable window on a specific Chicago date."""

    day: date
    start: str  # "HH:MM"
    end: str  # "HH:MM"

    @property
    def weekday_name(self) -> str:
        return self.day.strftime("%A")

    @property
    def start_at(self) -> datetime:
        return self._at(self.start)

    @property
    def end_at(self) -> datetime:
        return self._at(self.end)

    def _at(self, hhmm: str) -> datetime:
        hours, mins = (int(part) for part in hhmm.split(":"))
        return datetime(self.day.year, self.day.month, self.day.day, hours, mins, tzinfo=CHICAGO)

    def slot_starts(self) -> list[str]:
        """The 30-minute slot start times covering this window, as "HH:MM".

        LibCal's grid is a half-hour lattice, so a window is available only if every
        one of these slots is free.
        """
        out: list[str] = []
        cursor = self.start_at
        while cursor < self.end_at:
            out.append(cursor.strftime("%H:%M"))
            cursor += timedelta(minutes=30)
        return out

    def label(self) -> str:
        def pretty(hhmm: str) -> str:
            hours, mins = (int(part) for part in hhmm.split(":"))
            suffix = "am" if hours < 12 else "pm"
            return f"{hours % 12 or 12}:{mins:02d}{suffix}"

        return f"{self.day.strftime('%a %b %-d, %Y')}  {pretty(self.start)} – {pretty(self.end)}"


def horizon(reference: date, advance_days: int) -> date:
    """The furthest date currently bookable.

    CPL's guidelines say "up to three months in advance", but LibCal enforces a fixed
    **90 days** — probed 2026-08-09, where the last day offering slots was exactly
    `today + 90`, two days short of three calendar months. Kept configurable because it
    is a tenant setting Springshare lets libraries change.
    """
    return reference + timedelta(days=advance_days)


def horizon_windows(reference: date, slots: dict, advance_days: int) -> list[Window]:
    """The furthest-out bookable Saturday and Sunday.

    These are **not always the same weekend**. With a Monday horizon they are that
    weekend's Saturday and Sunday; with a Saturday horizon the Saturday is the
    horizon itself and the Sunday is six days earlier. Callers must therefore treat
    them as two independent dates, never as a pair.
    """
    edge = horizon(reference, advance_days)
    windows = []
    for weekday in (SATURDAY, SUNDAY):
        slot = slots.get(_DAY_KEY[weekday])
        if slot is None:
            continue
        windows.append(
            Window(day=latest_on_or_before(edge, weekday), start=slot.start, end=slot.end)
        )
    return sorted(windows, key=lambda w: w.day)


def window_for(day: date, slots: dict) -> Window | None:
    """The configured window for a given date, or None if it is not a Sat/Sun."""
    slot = slots.get(_DAY_KEY.get(day.weekday(), ""))
    return None if slot is None else Window(day=day, start=slot.start, end=slot.end)


def weekend_windows(reference: date, slots: dict, advance_days: int, count: int) -> list[Window]:
    """The last `count` bookable weekends, oldest first.

    Walks back a week at a time from the horizon Saturday, pairing each Saturday with
    the Sunday that follows it, and keeping only dates still inside the window.
    """
    edge = horizon(reference, advance_days)
    saturday = latest_on_or_before(edge, SATURDAY)
    out: list[Window] = []
    for index in range(count):
        base = saturday - timedelta(weeks=index)
        for offset, weekday in ((0, SATURDAY), (1, SUNDAY)):
            day = base + timedelta(days=offset)
            if day > edge or day < reference:
                continue
            window = window_for(day, slots)
            if window is not None:
                out.append(window)
    return sorted(out, key=lambda w: w.day)


@dataclass(frozen=True)
class Bookability:
    ok: bool
    reason: str = ""


def bookability(day: date, reference: date, advance_days: int, min_lead_days: int) -> Bookability:
    """Whether CPL would accept a request for `day`, judged in Chicago dates.

    Two limits from the Meeting Room Use Guidelines: an advance cap (90 days as
    LibCal enforces it) and no later than seven days before use.
    """
    if day < reference:
        return Bookability(False, f"{day} is in the past (Chicago today is {reference})")
    edge = horizon(reference, advance_days)
    if day > edge:
        return Bookability(
            False,
            f"{day} is beyond the {advance_days}-day booking window, which currently ends {edge}",
        )
    lead = (day - reference).days
    if lead < min_lead_days:
        return Bookability(
            False,
            f"{day} is only {lead} day(s) away; CPL requires at least {min_lead_days}",
        )
    return Bookability(True)

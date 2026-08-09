"""Booking history, stored as JSON lines.

Exists mainly to answer "have I been leaning on one branch?" — CPL's Meeting Room Use
Guidelines reserve the right to *"limit the number or length of meetings during any
time period for any applicant"*, so spreading bookings across branches is the point of
showing recent use at selection time.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

DEFAULT_PATH = Path("history.jsonl")


@dataclass(frozen=True)
class Booking:
    day: str  # "YYYY-MM-DD", the event date
    room_id: str
    branch: str
    room: str
    start: str  # "HH:MM"
    end: str
    booked_at: str  # ISO timestamp, Chicago
    confirmation: str = ""

    @property
    def date(self) -> date:
        return date.fromisoformat(self.day)


def load(path: str | Path = DEFAULT_PATH) -> list[Booking]:
    """Read the history file, skipping any malformed line rather than failing."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[Booking] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            out.append(
                Booking(
                    day=raw["day"],
                    room_id=raw["room_id"],
                    branch=raw.get("branch", ""),
                    room=raw.get("room", ""),
                    start=raw.get("start", ""),
                    end=raw.get("end", ""),
                    booked_at=raw.get("booked_at", ""),
                    confirmation=raw.get("confirmation", ""),
                )
            )
        except (json.JSONDecodeError, KeyError):
            continue
    return sorted(out, key=lambda b: b.day)


def append(booking: Booking, path: str | Path = DEFAULT_PATH) -> None:
    """Append one booking. Only ever called after a confirmed submission."""
    path = Path(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(booking), ensure_ascii=False) + "\n")


def recent(bookings: list[Booking], count: int = 5) -> list[Booking]:
    """The most recent `count` bookings, oldest first."""
    return bookings[-count:]


def usage_counts(bookings: list[Booking], window: int = 5) -> dict[str, int]:
    """How many of the last `window` bookings went to each room."""
    counts: dict[str, int] = {}
    for booking in recent(bookings, window):
        counts[booking.room_id] = counts.get(booking.room_id, 0) + 1
    return counts


def booking_on(bookings: list[Booking], day: date) -> Booking | None:
    """An existing booking for that date, if any — guards against double-booking."""
    target = day.isoformat()
    return next((b for b in bookings if b.day == target), None)


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")

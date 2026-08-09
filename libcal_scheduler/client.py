"""The LibCal booking sequence.

```
1. POST /spaces/availability/grid          slots + per-slot checksum
2. POST /spaces/availability/booking/add   add[...]    -> pending booking, 1h default end
3. POST /spaces/availability/booking/add   update[...] -> stretch to the full window
4. POST /ajax/space/times                  -> form HTML + session token
                                              *** creates a 30-minute hold ***
5. POST /ajax/space/book                   allowlisted fields -> reservation
```

No login is involved: `patron` and `patronHash` are sent empty and accepted.

Step 4 is the first step with a side effect, so `check` stops before it. A crash
between 4 and 5 leaves a slot held for 30 minutes; `release` undoes that when it can.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Room
from .dates import Window
from .form import BookingForm, parse_form
from .grid import Availability, checksums, evaluate, parse
from .http import Client, HttpError

GRID = "/spaces/availability/grid"
ADD = "/spaces/availability/booking/add"
TIMES = "/ajax/space/times"
BOOK = "/ajax/space/book"

BOOKING_METHOD = 11  # springyPage.bookingMethod on every CPL reserve page


class BookingRefused(Exception):
    """LibCal declined a step. Carries its message verbatim."""


@dataclass
class Pending:
    id: int
    eid: int
    gid: int
    lid: int
    start: str
    end: str
    checksum: str
    options: tuple[str, ...] = ()
    option_checksums: tuple[str, ...] = ()

    def as_payload(self, index: int = 0) -> dict:
        prefix = f"bookings[{index}]"
        return {
            f"{prefix}[id]": self.id,
            f"{prefix}[eid]": self.eid,
            f"{prefix}[seat_id]": 0,
            f"{prefix}[gid]": self.gid,
            f"{prefix}[lid]": self.lid,
            f"{prefix}[start]": self.start,
            f"{prefix}[end]": self.end,
            f"{prefix}[checksum]": self.checksum,
        }

    def as_json(self) -> str:
        return json.dumps(
            [
                {
                    "id": self.id,
                    "eid": self.eid,
                    "seat_id": 0,
                    "gid": self.gid,
                    "lid": self.lid,
                    "start": self.start,
                    "end": self.end,
                    "checksum": self.checksum,
                }
            ]
        )


def _raise_on_error(payload: dict) -> dict:
    if payload.get("error"):
        raise BookingRefused(str(payload["error"]))
    issues = payload.get("limitIssues")
    if issues:
        # CPL's booking-frequency limits, enforced server-side. Reported verbatim and
        # never retried around.
        raise BookingRefused(f"LibCal reported booking limits: {json.dumps(issues)}")
    return payload


def _pending_from(payload: dict) -> Pending:
    bookings = payload.get("bookings") or []
    if not bookings:
        raise BookingRefused("LibCal returned no pending booking")
    raw = bookings[0]
    return Pending(
        id=raw["id"],
        eid=raw["eid"],
        gid=raw["gid"],
        lid=raw["lid"],
        start=raw["start"],
        end=raw["end"],
        checksum=raw["checksum"],
        options=tuple(raw.get("options") or ()),
        option_checksums=tuple(raw.get("optionChecksums") or ()),
    )


class Reserver:
    def __init__(self, http: Client) -> None:
        self.http = http

    # -- step 1 ------------------------------------------------------------
    def grid(self, lid: int, gid: int, first: date, last: date) -> dict:
        """Raw grid response for a whole space group, over an **inclusive** date range.

        Two API quirks are absorbed here so callers cannot get them wrong:

        * The `end` parameter is **exclusive** — asking for `end=2026-11-02` returns
          data through Nov 1 — so a day is added before sending.
        * `eid` is ignored and the response covers every room in the `gid`, so rooms
          sharing a group (Edgewater has two) need only one request between them.
        """
        return self.http.post_json(
            GRID,
            {
                "lid": lid,
                "gid": gid,
                "eid": -1,
                "seat": 0,
                "seatId": 0,
                "zone": 0,
                "start": first.isoformat(),
                "end": (last + timedelta(days=1)).isoformat(),
                "pageIndex": 0,
                "pageSize": 18,
            },
            referer_lid=lid,
        )

    def availability(self, room: Room, first: date, last: date) -> tuple[dict, dict]:
        """Slot states and checksums for one room over an inclusive date range."""
        payload = self.grid(room.lid, room.gid, first, last)
        return parse(payload, room.eid), checksums(payload, room.eid)

    def window_availability(self, room: Room, window: Window) -> tuple[Availability, dict]:
        days, sums = self.availability(room, window.day, window.day)
        return evaluate(days.get(window.day.isoformat(), {}), window), sums

    # -- steps 2 and 3 -----------------------------------------------------
    def start_booking(self, room: Room, window: Window, slot_checksums: dict) -> Pending:
        """Select the window's first slot, then stretch the end to cover it."""
        first_slot = f"{window.day.isoformat()} {window.slot_starts()[0]}:00"
        checksum = slot_checksums.get(first_slot)
        if checksum is None:
            raise BookingRefused(f"no checksum for {first_slot}; the grid read is stale")

        added = _raise_on_error(
            self.http.post_json(
                ADD,
                {
                    "add[eid]": room.eid,
                    "add[seat_id]": 0,
                    "add[gid]": room.gid,
                    "add[lid]": room.lid,
                    "add[start]": first_slot,
                    "add[checksum]": checksum,
                    "lid": room.lid,
                    "gid": room.gid,
                    "start": window.day.isoformat(),
                    "end": window.day.isoformat(),
                },
                referer_lid=room.lid,
            )
        )
        pending = _pending_from(added)

        wanted_end = f"{window.day.isoformat()} {window.end}:00"
        if pending.end == wanted_end:
            return pending
        return self.extend(room, window, pending, wanted_end)

    def extend(self, room: Room, window: Window, pending: Pending, wanted_end: str) -> Pending:
        """Set the end time via the duration dropdown's option checksums."""
        try:
            index = pending.options.index(wanted_end)
        except ValueError:
            raise BookingRefused(
                f"{wanted_end} is not offered as an end time "
                f"(options: {', '.join(pending.options) or 'none'})"
            ) from None
        if index >= len(pending.option_checksums):
            raise BookingRefused("LibCal returned options without matching checksums")

        updated = _raise_on_error(
            self.http.post_json(
                ADD,
                {
                    "update[id]": pending.id,
                    "update[end]": wanted_end,
                    "update[checksum]": pending.option_checksums[index],
                    "lid": room.lid,
                    "gid": room.gid,
                    "start": window.day.isoformat(),
                    "end": window.day.isoformat(),
                    **pending.as_payload(),
                },
                referer_lid=room.lid,
            )
        )
        stretched = _pending_from(updated)
        if stretched.end != wanted_end:
            raise BookingRefused(
                f"asked for an end of {wanted_end} but LibCal set {stretched.end}"
            )
        return stretched

    # -- step 4 (creates a hold) -------------------------------------------
    def open_form(self, room: Room, pending: Pending) -> BookingForm:
        """Fetch the booking form. **This creates a 30-minute hold on the slot.**"""
        payload = self.http.post_json(
            TIMES,
            {
                "patron": "",
                "patronHash": "",
                "returnUrl": "",
                "method": BOOKING_METHOD,
                **pending.as_payload(),
            },
            referer_lid=room.lid,
        )
        if payload.get("redirect"):
            raise BookingRefused(
                f"LibCal wants authentication first: {payload['redirect']}"
            )
        markup = payload.get("html")
        if not markup:
            raise BookingRefused("LibCal returned no booking form")
        form = parse_form(markup)
        if not form.session:
            raise BookingRefused("booking form carried no session token")
        return form

    # -- step 5 ------------------------------------------------------------
    def submit(self, room: Room, pending: Pending, form: BookingForm, fields: dict) -> str:
        """Submit the reservation. Returns LibCal's confirmation HTML."""
        payload = {
            **fields,
            "session": form.session,
            "bookings": pending.as_json(),
            "returnUrl": "",
            "pickupHolds": "",
            "method": BOOKING_METHOD,
        }
        result = self.http.post_json(BOOK, payload, referer_lid=room.lid)
        if result.get("error"):
            raise BookingRefused(str(result["error"]))
        return str(result.get("html", ""))

    # -- cleanup -----------------------------------------------------------
    def release(self, room: Room, window: Window, pending: Pending) -> bool:
        """Try to release a hold after abandoning. Best effort.

        The pending booking is keyed to the session LibCal issued, so this can fail
        with `Invalid Booking ID`; callers should tell the user the slot stays held for
        30 minutes rather than pretend it was freed.
        """
        try:
            self.http.post_json(
                ADD,
                {
                    "removeId": pending.id,
                    "lid": room.lid,
                    "gid": room.gid,
                    "start": window.day.isoformat(),
                    "end": window.day.isoformat(),
                },
                referer_lid=room.lid,
            )
            return True
        except (HttpError, BookingRefused):
            return False

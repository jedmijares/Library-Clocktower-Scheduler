"""Command line entry point.

    check     read-only availability (default). Creates no holds.
    book      interactive: check -> pick -> review -> confirm -> submit
    history   print booking history
    discover  refresh the roster from LibCal's own location list

`book` is the only command that writes anything, and it always stops for an explicit
`[y/N]` before submitting.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

from . import history as history_mod
from . import review as review_mod
from .client import BookingRefused, Reserver
from .config import Config, ConfigError, Room, load
from .dates import CHICAGO, Window, bookability, horizon, horizon_windows, now, weekend_windows, window_for
from .form import PayloadError, build_payload
from .grid import Availability, OPEN, checksums as grid_checksums, evaluate, parse as grid_parse
from .guardrails import check as run_guardrails
from .http import Client, HttpError

LOG_DIR = Path("logs")


WINDOW_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_window_override(raw: str | None) -> tuple[str, str] | None:
    """Parse `--window 12:00-16:30` into normalised HH:MM bounds."""
    if not raw:
        return None
    match = WINDOW_RE.match(raw.strip())
    if match is None:
        raise SystemExit(f"--window {raw!r} is not HH:MM-HH:MM (e.g. 12:00-16:30)")
    start = f"{int(match.group(1)):02d}:{match.group(2)}"
    end = f"{int(match.group(3)):02d}:{match.group(4)}"
    if end <= start:
        raise SystemExit(f"--window {raw!r}: end must be after start")
    return start, end


def _resolve_windows(config: Config, args) -> list[Window]:
    today = now().astimezone(CHICAGO).date()
    override = _parse_window_override(getattr(args, "window", None))
    if args.date:
        windows = []
        for raw in args.date:
            try:
                day = date.fromisoformat(raw)
            except ValueError:
                raise SystemExit(f"--date {raw!r} is not YYYY-MM-DD")
            window = window_for(day, config.slots)
            if window is None:
                raise SystemExit(
                    f"{day} is a {day:%A}; only "
                    f"{' and '.join(sorted(k.title() for k in config.slots))} are configured"
                )
            windows.append(window)
        return _apply_override(windows, override)
    if args.weekends:
        found = weekend_windows(today, config.slots, config.advance_days, args.weekends)
    else:
        found = horizon_windows(today, config.slots, config.advance_days)
    return _apply_override(found, override)


def _apply_override(windows: list[Window], override: tuple[str, str] | None) -> list[Window]:
    if override is None:
        return windows
    start, end = override
    return [Window(day=w.day, start=start, end=end) for w in windows]


def _rooms_for(config: Config, only: str | None) -> list[Room]:
    rooms = list(config.enabled_rooms)
    if only:
        rooms = [r for r in rooms if r.id == only]
        if not rooms:
            raise SystemExit(
                f"no enabled room with id {only!r}; have: "
                + ", ".join(r.id for r in config.enabled_rooms)
            )
    return rooms


def _sweep(
    reserver: Reserver, rooms: list[Room], windows: list[Window], *, quiet: bool = False
) -> tuple[list[tuple[Room, dict[str, Availability]]], dict[tuple[str, str], dict]]:
    """Read availability for every room across every target date.

    One grid request per room covering the whole span, since the API accepts a range.
    """
    # Inclusive range; Reserver.grid handles the API's exclusive `end`.
    first = min(w.day for w in windows)
    last = max(w.day for w in windows)
    rows: list[tuple[Room, dict[str, Availability]]] = []
    sums: dict[tuple[str, str], dict] = {}

    # One request per space group, not per room: the grid returns every room in a gid,
    # so Edgewater's two rooms share a single fetch.
    grids: dict[tuple[int, int], dict | None] = {}
    for room in rooms:
        key = (room.lid, room.gid)
        if key in grids:
            continue
        try:
            grids[key] = reserver.grid(room.lid, room.gid, first, last)
        except (HttpError, BookingRefused) as err:
            grids[key] = None
            if not quiet:
                print(f"  ✗ {room.branch}: {err}")

    for room in rooms:
        payload = grids.get((room.lid, room.gid))
        if payload is None:
            rows.append((room, {}))
            continue
        days, checksums = grid_parse(payload, room.eid), grid_checksums(payload, room.eid)
        sums[(room.id, "checksums")] = checksums
        by_day: dict[str, Availability] = {}
        for window in windows:
            key = window.day.isoformat()
            by_day[key] = evaluate(days.get(key, {}), window)
        rows.append((room, by_day))
        if not quiet:
            states = "  ".join(
                f"{w.day:%b %-d}: {by_day[w.day.isoformat()].summary()}" for w in windows
            )
            flag = "  ⚠ field map unverified" if room.field_map_verified is None else ""
            print(f"  ✓ {room.label:<30} {states}{flag}")
    return rows, sums


def cmd_check(config: Config, args) -> int:
    today = now().astimezone(CHICAGO).date()
    windows = _resolve_windows(config, args)
    rooms = _rooms_for(config, args.room)
    bookings = history_mod.load(args.history)

    print(review_mod.header(config, now(), horizon(today, config.advance_days)))
    print()
    unbookable = set()
    for window in windows:
        verdict = bookability(window.day, today, config.advance_days, config.min_lead_days)
        if not verdict.ok:
            unbookable.add(window.day)
        note = "" if verdict.ok else f"   (not bookable: {verdict.reason})"
        print(f"Target   {window.label()}{note}")
    print()
    print(review_mod.history_block(bookings))
    print()
    print(f"Reading availability — {len(rooms)} room(s), 10s apart…")
    reserver = Reserver(Client())
    rows, _ = _sweep(reserver, rooms, windows)
    print()
    print(review_mod.availability_table(rows, windows, bookings, unbookable))
    if unbookable:
        print()
        print(
            "  – too soon = inside CPL's "
            f"{config.min_lead_days}-day minimum; LibCal withdraws every slot, so"
        )
        print("             availability cannot be read for those dates either.")
    return 0


def _prompt(question: str) -> str:
    try:
        return input(question).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def cmd_book(config: Config, args) -> int:
    today = now().astimezone(CHICAGO).date()
    windows = _resolve_windows(config, args)
    rooms = _rooms_for(config, args.room)
    bookings = history_mod.load(args.history)

    print(review_mod.header(config, now(), horizon(today, config.advance_days)))
    print()
    for window in windows:
        print(f"Target   {window.label()}")
    print()
    print(review_mod.history_block(bookings))
    print()
    print(f"Reading availability — {len(rooms)} room(s), 10s apart…")
    reserver = Reserver(Client())
    rows, sums = _sweep(reserver, rooms, windows)
    print()
    print(review_mod.availability_table(rows, windows, bookings))
    print()

    # Only offer combinations that are actually open.
    choices: list[tuple[Room, Window, Availability]] = []
    for room, by_day in rows:
        for window in windows:
            availability = by_day.get(window.day.isoformat())
            if availability and availability.status == OPEN:
                choices.append((room, window, availability))
    if not choices:
        print("Nothing on the roster is open for the whole window on any target date.")
        return 1

    print("Open combinations:")
    for index, (room, window, _) in enumerate(choices, start=1):
        flag = "  ⚠ unverified field map" if room.field_map_verified is None else ""
        print(f"  {index:<3}{room.label:<32}{window.label()}{flag}")
    answer = _prompt(f"Select [1-{len(choices)}, q to quit] → ")
    if not answer.isdigit() or not 1 <= int(answer) <= len(choices):
        print("Nothing selected.")
        return 1
    room, window, availability = choices[int(answer) - 1]

    verdict = run_guardrails(
        room=room,
        window=window,
        availability=availability,
        config=config,
        bookings=bookings,
        now=now(),
        window_overridden=_parse_window_override(getattr(args, "window", None)) is not None,
    )
    if not verdict.ok:
        print()
        print(f"Refusing to book {room.label} on {window.day}:")
        for refusal in verdict.refusals:
            print(f"  ✗ {refusal}")
        return 1

    checksums = sums.get((room.id, "checksums"), {})
    print()
    print(f"Reserving the window at {room.label}…")
    try:
        pending = reserver.start_booking(room, window, checksums)
    except (HttpError, BookingRefused) as err:
        print(f"  ✗ {err}")
        return 1

    if args.dry_run:
        print("  --dry-run: stopping before the form step, so no hold was created.")
        return 0

    print("  Opening the booking form (this holds the slot for 30 minutes)…")
    try:
        form = reserver.open_form(room, pending)
    except (HttpError, BookingRefused) as err:
        print(f"  ✗ {err}")
        return 1

    try:
        payload = build_payload(
            form,
            room.field_map,
            config.values,
            config.requester,
            auto_check_acknowledgements=config.auto_check_acknowledgements,
        )
    except PayloadError as err:
        print()
        print("Cannot build a submission payload:")
        for problem in err.problems:
            print(f"  ✗ {problem}")
        _abandon(reserver, room, window, pending)
        return 1

    print()
    print(review_mod.review(
        room=room, window=window, form=form, payload=payload, config=config, verdict=verdict
    ))
    print()
    answer = _prompt("Submit this request?  [y/N] → ").lower()
    if answer not in ("y", "yes"):
        print("Not submitted.")
        _abandon(reserver, room, window, pending)
        return 1

    try:
        book_id, confirmation = reserver.submit(room, pending, form, payload)
    except (HttpError, BookingRefused) as err:
        print(f"  ✗ {err}")
        _write_log(reserver)
        return 1

    history_mod.append(
        history_mod.Booking(
            day=window.day.isoformat(),
            room_id=room.id,
            branch=room.branch,
            room=room.room,
            start=window.start,
            end=window.end,
            booked_at=history_mod.stamp(now()),
            book_id=book_id,
            confirmation=" ".join(confirmation.split())[:200],
        ),
        args.history,
    )
    print()
    print(f"Submitted. {room.label} on {window.label()}")
    if book_id:
        print(f"LibCal booking id: {book_id}")
    print("CPL approves meeting room requests within three days; watch for their email.")
    _write_log(reserver)
    return 0


def _abandon(reserver: Reserver, room: Room, window: Window, pending) -> None:
    if reserver.release(room, window, pending):
        print("  Hold released.")
    else:
        print("  Could not release the hold — the slot stays reserved for up to 30 minutes.")
    _write_log(reserver)


def _write_log(reserver: Reserver) -> None:
    if not reserver.http.exchanges:
        return
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{now():%Y%m%d-%H%M%S}.jsonl"
    path.write_text(reserver.http.audit_log() + "\n", encoding="utf-8")
    print(f"  Audit log: {path}")


def cmd_history(config: Config, args) -> int:
    bookings = history_mod.load(args.history)
    if not bookings:
        print("No bookings recorded yet.")
        return 0
    print(f"{'Date':<14}{'Branch':<18}{'Room':<24}{'Window':<16}LibCal id")
    print("-" * 92)
    for booking in bookings:
        print(
            f"{booking.day:<14}{booking.branch[:17]:<18}{booking.room[:23]:<24}"
            f"{booking.start}–{booking.end:<10}{booking.book_id or '-'}"
        )
    counts = history_mod.usage_counts(bookings, window=5)
    print()
    print("Last 5 bookings by room: " + ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items())))
    return 0


def cmd_discover(config: Config, args) -> int:
    """Print the roster LibCal advertises, for copying into config.

    Reads reserve pages only, so it creates no holds. Verifying a *field map* needs the
    booking form, which does create a hold — that stays a deliberate, separate step.
    """
    print("Roster discovery reads reserve pages only — no holds are created.")
    print("Note: verifying a field map requires opening a booking form, which holds a slot.")
    print()
    print("Configured rooms:")
    for room in config.rooms:
        state = "enabled " if room.enabled else "disabled"
        verified = room.field_map_verified or "UNVERIFIED"
        print(
            f"  {room.id:<14}{state}  lid={room.lid} gid={room.gid} eid={room.eid} "
            f"cap={room.capacity:<4} map={room.field_map.name:<10}{verified}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="libcal-scheduler",
        description="Interactive assistant for booking Chicago Public Library meeting rooms.",
    )
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--history", default="history.jsonl", help="path to history.jsonl")
    sub = parser.add_subparsers(dest="command")

    def add_target_args(target):
        target.add_argument(
            "--date", action="append", metavar="YYYY-MM-DD",
            help="a specific date; repeatable. Defaults to the furthest bookable Sat and Sun.",
        )
        target.add_argument(
            "--weekends", type=int, metavar="N",
            help="the last N bookable weekends instead of just the horizon",
        )
        target.add_argument("--room", metavar="ID", help="restrict to one roster room id")
        target.add_argument(
            "--window", metavar="HH:MM-HH:MM",
            help="override the configured slot times for this run, e.g. 12:00-16:30",
        )

    check = sub.add_parser("check", help="read-only availability; creates no holds")
    add_target_args(check)

    book = sub.add_parser("book", help="interactive booking with a confirm prompt")
    add_target_args(book)
    book.add_argument(
        "--dry-run", action="store_true",
        help="stop before opening the form, so no 30-minute hold is created",
    )

    sub.add_parser("history", help="print booking history")
    sub.add_parser("discover", help="show the roster and field-map verification state")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args((argv or []) + ["check"])

    try:
        config = load(args.config, strict=args.command == "book")
    except ConfigError as err:
        print(str(err), file=sys.stderr)
        return 2

    handlers = {
        "check": cmd_check,
        "book": cmd_book,
        "history": cmd_history,
        "discover": cmd_discover,
    }
    return handlers[args.command](config, args)


if __name__ == "__main__":
    raise SystemExit(main())

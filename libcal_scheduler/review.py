"""Terminal rendering: the availability table and the pre-submit review screen.

The review screen deliberately lists the honeypot field alongside the real ones,
labelled, so it is visible at a glance that the tool found the trap and is choosing
not to fill it.
"""

from __future__ import annotations

from .config import Config, Room
from .dates import Window
from .form import BookingForm
from .grid import Availability, OPEN, PARTIAL
from .guardrails import Verdict
from .history import Booking, recent, usage_counts

MARK = {OPEN: "✓ open", PARTIAL: "~", "booked": "✗ booked", "closed": "closed"}


def _status_cell(availability: Availability, width: int = 15, *, too_soon: bool = False) -> str:
    if availability.status == OPEN:
        return "✓ open".ljust(width)
    if availability.status == PARTIAL:
        return f"~ {availability.free[0] if availability.free else 'partial'}".ljust(width)
    if availability.status == "closed":
        # LibCal withdraws every slot for a date inside the minimum-lead window, which is
        # indistinguishable from a branch being shut unless the caller tells us which it is.
        return ("– too soon" if too_soon else "closed").ljust(width)
    return "✗ booked".ljust(width)


def header(config: Config, now, horizon_date) -> str:
    lines = [
        f"LibCal Scheduler   —   {config.event_name}",
        f"Chicago time  {now:%a %Y-%m-%d %H:%M %Z}",
        f"Bookable through  {horizon_date:%a %Y-%m-%d}  (today + {config.advance_days} days)",
    ]
    return "\n".join(lines)


def history_block(bookings: list[Booking], count: int = 5) -> str:
    if not bookings:
        return "Recent bookings   (none recorded yet)"
    entries = [f"{b.date:%b %-d} {b.branch}" for b in recent(bookings, count)]
    return "Recent bookings   " + "  ·  ".join(entries)


def availability_table(
    rows: list[tuple[Room, dict[str, Availability]]],
    windows: list[Window],
    bookings: list[Booking],
    unbookable: set | None = None,
) -> str:
    """One row per room, one column per target date."""
    counts = usage_counts(bookings, window=5)
    head = f"  {'#':<3}{'Room':<30}{'Cap':<6}"
    for window in windows:
        head += f"{window.day:%a %b %-d}".ljust(17)
    head += "Recent use"
    out = [head, "  " + "-" * (len(head) - 2)]

    too_soon_days = unbookable or set()
    for index, (room, by_day) in enumerate(rows, start=1):
        line = f"  {index:<3}{room.label[:29]:<30}{room.capacity:<6}"
        for window in windows:
            line += _status_cell(
                by_day.get(window.day.isoformat(), Availability("closed")),
                17,
                too_soon=window.day in too_soon_days,
            )
        used = counts.get(room.id, 0)
        line += f"{used} of last 5"
        if room.field_map_verified is None:
            line += "   ⚠ unverified"
        out.append(line)
    return "\n".join(out)


def _label_for(form: BookingForm, name: str) -> str:
    for candidate in form.by_name(name):
        if candidate.label:
            return candidate.label
    return ""


def review(
    *,
    room: Room,
    window: Window,
    form: BookingForm,
    payload: dict,
    config: Config,
    verdict: Verdict,
) -> str:
    """The screen shown immediately before the confirm prompt."""
    out = [
        f"Review — {room.label}",
        f"         {window.label()}",
        "",
    ]

    required = form.required_names()
    ordered = [f.name for f in form.real_fields if f.name in payload]
    seen: set[str] = set()
    for name in ordered:
        if name in seen:
            continue
        seen.add(name)
        star = "*" if name in required else " "
        label = _label_for(form, name)
        value = str(payload[name])
        if len(value) > 58:
            value = value[:55] + "…"
        out.append(f"  {name:<11}{star} {label[:26]:<28}{value}")

    # Fields the branch offers but we deliberately left empty.
    skipped = [
        f.name
        for f in form.real_fields
        if f.name not in payload
        and f.name not in ("session", "bookings", "returnUrl", "method", "pickupHolds")
        and f.kind not in ("hidden", "submit", "button")
    ]
    if skipped:
        out.append("")
        out.append("  Left blank (optional at this branch):")
        for name in dict.fromkeys(skipped):
            out.append(f"    {name:<11}  {_label_for(form, name)[:52]}")

    if form.honeypots:
        out.append("")
        out.append("  HONEYPOT — present in the form, deliberately NOT submitted:")
        for trap in form.honeypots:
            out.append(
                f"    {trap.name:<11}  container {trap.container_id} · class {trap.container_class}"
            )
            out.append(f"    {'':<11}  detected by: {', '.join(trap.signals)}")

    out.append("")
    out.append("  * = required by this branch")

    if verdict.warnings:
        out.append("")
        for warning in verdict.warnings:
            out.append(f"  ⚠  {warning}")

    if verdict.refusals:
        out.append("")
        out.append("  REFUSED — not submitting:")
        for refusal in verdict.refusals:
            out.append(f"    ✗ {refusal}")

    return "\n".join(out)

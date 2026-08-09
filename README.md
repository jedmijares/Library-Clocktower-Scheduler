# LibCal Scheduler

Books a Chicago Public Library meeting room for a recurring event.

Reads availability across a roster of rooms, lets you pick one, shows you the exact form
payload, and submits only after you confirm. Manually run — there is no scheduling.

Python 3.11+, **no dependencies**.

## Setup

```bash
cp config.example.json config.json
# fill in requester.{firstName,lastName,email,telephone,address}
```

There is no zip field — put the zip inside `requester.address`. (The input named `Zip` on
some branches is a honeypot; see `docs/portal-notes.md`.)

If you plan to book **West Loop**, also fill `answers.organization` — it requires an
organization name, speakers and press answers even for an `Individual` request. Every other
branch leaves those optional.

## Use

```bash
python3 -m libcal_scheduler.cli                      # check: the furthest bookable Sat and Sun
python3 -m libcal_scheduler.cli check --weekends 3   # the next 3 bookable weekends
python3 -m libcal_scheduler.cli check --date 2026-11-07
python3 -m libcal_scheduler.cli check --room bezazian

python3 -m libcal_scheduler.cli book                 # pick -> review -> confirm -> submit
python3 -m libcal_scheduler.cli book --dry-run       # stop before anything is held

python3 -m libcal_scheduler.cli history
python3 -m libcal_scheduler.cli discover             # roster + field-map verification state
```

`check` is read-only and creates no holds, so run it as often as you like. A sweep paces
requests 10 seconds apart, so eight rooms takes roughly a minute.

The two default targets are **not always the same weekend**. The horizon can land on any
weekday, so the furthest bookable Saturday and the furthest bookable Sunday are sometimes
six days apart — each is labelled with its own date.

## Booking

`book` walks the same sweep, then offers only room/date combinations where the *entire*
window is free. After you pick, it reserves the window and shows a review screen listing
every field it will submit, plus the branch's honeypot field labelled as deliberately not
submitted. Nothing is sent until you answer `y`.

Opening the form places a **30-minute hold** on the slot. Declining at the prompt releases
it where possible, and says so plainly when it cannot.

CPL approves meeting room requests within three days, so expect an email rather than an
instant confirmation.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

119 tests, no network required — they run against seven captured booking forms and six
captured availability responses in `tests/fixtures/`.

## Notes

- `config.json`, `logs/` and `history.jsonl` contents are personal; only `config.json` and
  `logs/` are gitignored, so `history.jsonl` is committed deliberately as a record.
- `CLAUDE.md` covers conventions and the rules that are easy to get wrong.
- `docs/portal-notes.md` records everything learned about the portal, with dates.

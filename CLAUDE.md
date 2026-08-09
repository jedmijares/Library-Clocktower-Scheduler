# LibCal Scheduler — working notes

Interactive assistant for booking a Chicago Public Library meeting room for a recurring
event. Reads availability across a roster of rooms, lets you pick, shows the
exact form payload, and submits only after an explicit `[y/N]`.

## Conventions

- **Python 3.11+, standard library only.** No dependencies, deliberately. `urllib.request`,
  `html.parser`, `json`, `datetime`, `zoneinfo`.
- Tests are `unittest`: `python3 -m unittest discover -s tests -t .`
- Runs entirely in the dev container. There is no browser and no Windows dependency.
- `config.json` is gitignored (personal details); `config.example.json` is the template.

## Rules that are easy to get wrong

**All dates are `America/Chicago`.** Never call `date.today()` or bare `datetime.now()`.
Use `dates.now()` / `dates.today()`. The container runs as UTC and the user travels; there
are 52 dates in 2026 where a one-day slip moves the resolved target Saturday by a week.
Grid timestamps are naive and implicitly Chicago — `dates.parse_slot` attaches the zone.

**The booking horizon is 90 days, not 3 months.** CPL's published guidelines say "up to
three months in advance"; LibCal enforces a flat 90 days. Probed 2026-08-09: the last day
offering slots was exactly `today + 90`, two days short of three calendar months. Lives in
config as `event.advanceDays`.

**The form filler is an allowlist, never an enumeration.** Send only `fname`, `lname`,
`email` and the `q####` ids a field map names. Never iterate parsed fields filling
whatever looks required — see the honeypot section below.

**`Referer` is mandatory** on every request, or the grid returns `403 Invalid Referrer.`
in a 17-byte body. `http.Client` requires it as a keyword argument so it cannot be
forgotten.

**The grid's `end` parameter is exclusive.** `Reserver.grid` takes an inclusive range and
adds the day itself. Getting this wrong silently drops the last date — it made the horizon
Sunday read as "closed" for every room.

**Unrecognised slot classes are not "available".** `grid.classify` returns `UNKNOWN`, and
guardrails refuse to book. A LibCal change should surface loudly rather than book on top
of someone else's reservation.

## The honeypot

Every branch's booking form carries one extra `required` text input whose container is
deleted by an inline `<script>` before a human sees it. **The name rotates per branch** —
`Zip`, `Feedback`, `Name`, `Question`, `URL` observed across seven forms — so nothing may
depend on knowing it.

Three independent layers keep it unfilled:

1. **Positional exclusion** — `form.parse_form` flags honeypots by container, and
   `BookingForm.by_name` excludes them. A honeypot sharing a real field's name would still
   resolve to the real control.
2. **Allowlist** — `build_payload` only ever emits known names.
3. **Assertion** — the payload is checked against the allowlist and the detected honeypot
   set before returning; a stray key raises.

Detection prefers the `removeChild` call (behavioural) over the container class
(cosmetic). `config.KNOWN_HONEYPOT_NAMES` exists only so a config mentioning one fails
validation; it is not a denylist anything relies on.

The review screen *displays* the honeypot, labelled, so it is visible that the tool found
it and chose not to fill it.

## Layout

```
libcal_scheduler/
  config.py      validation; accumulates every problem rather than failing on the first
  dates.py       Chicago-anchored resolution, horizon, windows, 30-minute slot lattice
  http.py        urllib wrapper: mandatory Referer, timeout, body-on-error, 10s pacing
  grid.py        className -> availability; whole-window evaluation; free ranges
  form.py        html.parser; honeypot detection; allowlisted payload building
  client.py      the five-step booking sequence
  guardrails.py  hard refusals vs. warnings
  history.py     history.jsonl
  review.py      terminal rendering
  cli.py         check (default) / book / history / discover
tests/fixtures/  7 captured booking forms, 6 captured grid responses
```

## Etiquette baked in

- Honest User-Agent identifying the tool; no spoofed headers or decoy requests.
- 10 seconds between requests, honouring `Crawl-delay: 10`.
- `check` never calls `/ajax/space/times`, so it creates no holds and is free to run.
- `limitIssues` from LibCal is reported verbatim and never retried around.
- One grid request per `gid`, not per room — Edgewater's two rooms share one.

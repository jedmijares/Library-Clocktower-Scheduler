# CPL / LibCal portal notes

Everything here was established by observation against the live site, not from
documentation. Dates given are when each fact was verified.

## Platform

Chicago Public Library's room booking runs on **Springshare LibCal** at
`chipl.libcal.com` (institution `iid 3116`). An earlier assumption that it ran on
Communico was wrong — it was inferred from a DNS record rather than the booking page.

Reserve page: `https://chipl.libcal.com/reserve?lid=<lid>&gid=<gid>`

- `gid` preselects the room group.
- `&date=` is **ignored** — there is no way to deep-link to a date.
- The `<select name="lid">` on any reserve page lists all ~80 CPL branches.

## Roster (verified 2026-08-09)

| Branch | lid | gid | eid | Cap | Room |
| --- | --- | --- | --- | --- | --- |
| Lincoln Park | 9234 | 16895 | 65898 | 80 | Large Meeting Room |
| Lincoln Park | 9234 | 16895 | 65899 | 10 | Small Meeting Room (excluded, too small) |
| Bezazian | 9208 | 16884 | 65884 | 65 | Meeting Room |
| Uptown | 9264 | 16904 | 65913 | 50 | Community Room |
| Merlo | 9242 | 16874 | 65864 | 46 | Meeting Room |
| Rogers Park | 9255 | 16903 | 65911 | 40 | Community Room |
| West Loop | 9271 | 16970 | 66010 | 80 | Meeting Room |
| Edgewater | 9220 | 16900 | 65906 | 35 | Meeting Room A |
| Edgewater | 9220 | 16900 | 65907 | 45 | Meeting Room B |

### Library-provided descriptions

Headline capacity can mislead: it is often quoted theater style, so check the table and
chair counts if your event needs people seated at tables.

- **Bezazian** — "10 tables and 65 chairs. A lectern, screen, and sink." Best table provision.
- **Uptown** — "eight tables, 50 chairs and a lectern."
- **Rogers Park** — "20' x 35' … eight tables, 40 chairs, a screen and sink."
- **Lincoln Park** — "tables, chairs, a lectern, screen, and ceiling-mounted projector."
- **West Loop** — "not enclosed but an open space surrounded by study rooms. There will be
  foot traffic." The only room CPL warns about.
- **Edgewater** — "Room A holds up to 35 people, theater style, **fewer if tables are
  used**." So its capacities overstate seated-at-tables capacity.

No noise or enclosure disclosure exists for Bezazian, Rogers Park or Uptown — confirmed by
reading all three booking forms. Combined with Rogers Park's stated dimensions and the
sinks at Bezazian and Rogers Park, those three are very likely conventional enclosed
rooms. Not proof.

## Booking window

CPL's guidelines say "up to three months in advance, but no later than seven days before
requested use". The **advance limit is enforced as a flat 90 days**: probed 2026-08-09,
the last day offering slots was 2026-11-07, exactly `today + 90` and two days short of
three calendar months.

Open hours, inferred from which slots the grid returns: Sat 09:30–16:30,
Sun 13:30–16:30, Mon/Wed 10:30–17:00, Tue/Thu 12:30–19:00, Fri 09:30–16:30. The Sunday
window is a six-slot exact fit, so any single existing booking eliminates the whole day.

## Availability grid

`POST /spaces/availability/grid`

```
className                meaning
(absent)                 AVAILABLE
s-lc-eq-checkout         BOOKED
s-lc-eq-r-padding        buffer beside a booking; not bookable
```

Basis: across a three-week sample all 98 padding slots sat immediately adjacent to a
booked slot, zero exceptions; 29 day/room combinations were entirely class-free against 11
entirely booked. `tests/test_grid.py` re-asserts the adjacency property against the
fixtures, so a change in meaning shows up as a test failure.

Quirks:

- **`eid` is ignored.** The response covers every room in the `gid`; filter by `itemId`.
- **`end` is exclusive.** `end=2026-11-02` returns data through Nov 1.
- **`Referer` is mandatory** — otherwise `403 Invalid Referrer.` with a 17-byte body.
- **Slot checksums are not short-lived** — checksums ~20 minutes old were still accepted.

## Booking sequence

```
1. POST /spaces/availability/grid          slots + per-slot checksum
2. POST /spaces/availability/booking/add   add[...]     -> pending booking, 1h default end
3. POST /spaces/availability/booking/add   update[...]  -> stretch to the full window
4. POST /ajax/space/times                  -> form HTML + hidden session token
                                              *** creates a 30-minute hold ***
5. POST /ajax/space/book                   allowlisted fields -> reservation
```

- **No login.** `patron` and `patronHash` sent empty and accepted; no LibAuth redirect.
- `springyPage.bookingMethod = 11` on every CPL reserve page.
- Duration is a dropdown, not repeated clicks: step 2 returns `options[]` with a parallel
  `optionChecksums[]`, so the full window is one `update` call.
- `/ajax/space/createcart` is **not** on this path — that is the equipment-cart feature.
- Step 4's hold is keyed to the `session` id in the form. `removeId` releases it only with
  that session context; without it the endpoint answers `404 Invalid Booking ID`.
- `limitIssues` in a response carries CPL's booking-frequency limits, enforced server-side.

## Form field maps

All seven forms read live 2026-08-09. **Six branches are identical; West Loop is the sole
outlier**, defining new question ids that duplicate the optional ones as required.

| Prompt | LP / Bezazian / Merlo / Rogers Park / Uptown / Edgewater | West Loop |
| --- | --- | --- |
| Name / email | `fname` `lname` `email` ✱ | same ✱ |
| Telephone / Address | `q10094` `q10093` ✱ | same ✱ |
| Type of request | `q10092` ✱ Individual\|Organizational | same ✱ |
| Organization | `q10095` optional | `q10095` optional **+ `q32153` ✱** |
| Purpose of meeting | `q10096` ✱ | same ✱ |
| Names of speakers | `q10097` optional | **`q32154` ✱** |
| Press / news media | `q10098` optional | **`q32155` ✱** |
| Expected attendance | `q10099` ✱ | same ✱ |
| Animals/cooking/medical/physical | `q10100` ✱ No\|Yes | same ✱ |
| "No entrance fee…" | `q29731[]` ✱ | same ✱ |
| "Responsible for set up…" | `q29746[]` optional | same optional |
| Open-space disclosure | — | `q18806[]` optional |

**West Loop requires an organization name even for an `Individual` request**, along with
speakers and press answers.

**There is no real zip field.** Address (`q10093`) is free text; put the zip in it.

**`q10099` is a range bucket**, and its `<option>` tags carry no `value`, so the submitted
value is the option text: `1-5 · 6-10 · 11-20 · 21-30 · 31-40 · 41-50 · 51-60 · 61-70 ·
71-80 · 81-90 · 91-100 · 101-125 · 126-178`. The list is **not** capped at room capacity,
so the capacity check is ours to enforce — CPL policy states "The number attending a
meeting may not exceed the established capacity of the room."

## Honeypot

Every form carries one extra `required` text input whose container an inline `<script>`
deletes before render. Name, container id and class all rotate per branch:

| Branch | field name | container id | class |
| --- | --- | --- | --- |
| Lincoln Park | `Zip` | `slchp-Zip-cntr` | `s-input-hp` |
| West Loop | `Zip` | `s-lcxp-Zip-slc` | `s-input-hp` |
| Bezazian | `Zip` | `slchp-Zip_cntr` | `s-inph-ox` |
| Merlo | `URL` | `s-lcxp-URL-cntr` | `s-inph-ox` |
| Rogers Park | `Feedback` | `slch_Feedback-cntr` | `slc-hi` |
| Uptown | `Name` | `slch_Name-slc` | `s-hpinp-ox` |
| Edgewater | `Question` | `s-lcxp-Question-slc` | (hp-family) |

Each also carries `autocomplete="off"` so browser autofill cannot trip it, and
`required="required"` as bait. Across all seven forms **no honeypot name collides with any
real field name** — real fields are lowercase (`fname`, `lname`, `email`) or `q`-prefixed.

**No public documentation found.** Checked 22 release posts in the current sitemap, 24
further 2020–21 release posts, the one Wayback snapshot of the `new-features` category, and
third-party sources. `blog.springshare.com`'s own search is broken — it returns the same
ten newest posts for every query — so it proves nothing either way, and Springshare's real
documentation is behind a LibAuth staff login. Absence of public docs is the expected state
for a maintained anti-bot measure; publishing the field names would defeat it.

## robots.txt and terms

`chipl.libcal.com/robots.txt` has `User-agent: *` / `Crawl-delay: 10` / `Disallow: /`.
CPL **added** the blanket disallow: Carnegie, Clinton-Macomb and Poplar Bluff all ship the
vendor default with only `Disallow: /process_`. It is a crawler convention rather than a
contract, but it is a deliberate choice on CPL's own site — hence the 10-second pacing,
honest User-Agent, and human confirmation before every submission.

Springshare publishes **no Terms of Service** (`/terms`, `/terms-of-service`, `/legal`,
`/tos`, `/terms-of-use`, `/acceptable-use` all 404; only `/privacy` exists). The only terms
a patron agrees to are CPL's, at the booking form checkbox: "I confirm that I have read and
will comply with the Chicago Public Library's Guidelines for Meeting Room Use." Those cover
conduct, fees, damages and compliance — nothing about automation.

The material constraint is CPL's "the Library may limit the number or length of meetings
during any time period for any applicant" — a **frequency** limit, which is why the tool
shows recent-use counts per room at selection time.

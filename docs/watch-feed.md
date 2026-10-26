# One Inbox → watch feed contract (v1)

Agreed 2026-10-26 between One Inbox (the Pi side) and the Pokémon buddy watch face (mayob123/pokeguyjogalot).

## Endpoint

- `GET https://<pi-tailscale-name>.ts.net/feed/watch.json`
- Header: `X-OneInbox-Token: <token>` (missing or wrong token → `401`, empty body)
- Served by the Pi over Tailscale HTTPS (`tailscale cert`), so Garmin Connect accepts it. Not exposed outside the tailnet.
- `Content-Type: application/json`, body under 1 KB.

## Body

```json
{
  "v": 1,
  "at": "2026-10-26T07:30:00+01:00",
  "left": 3,
  "done": 2,
  "xp": 148,
  "next": "A-kasse hours",
  "kind": "admin",
  "bill": "Thu",
  "mood": "hungry",
  "event": "MSc lecture: Resear…",
  "event_at": "14:00",
  "event_ts": 1793019600,
  "now_id": "8Ck3kB1nT7Wq2xYzAbCdEf"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `v` | int | Contract version. Additive changes keep `1`. |
| `at` | string | ISO 8601 with offset, when the Pi built the feed. Watch shows "confused" itself if older than 2 h. |
| `left` | int | Items still open in Today. |
| `done` | int | Items cleared today (resets at local midnight). |
| `xp` | int | Running total kept on the Pi, never resets. +1 per item done. |
| `next` | string or null | Next item, max 20 characters, cut with "…". null when clear. |
| `kind` | string or null | `bill`, `school`, `admin`, `msc`, `job`, `danish`, `friend`, `travel`, `other`. For discreet mode. |
| `bill` | string or null | Next bill due as a short day ("Thu", "26 Oct"), null if none in 7 days. |
| `mood` | string | `happy` (left = 0), `normal`, `hungry` (anything overdue), `lonely` (friend overdue, nothing else overdue), `sleepy` (after 22:00 with left > 0). First match in order: hungry, lonely, sleepy, happy, normal. |
| `event` | string or null | Next calendar event that hasn't started, within 24 h. Max 20 characters, cut with "…". null if none. Added 2026-10-26. |
| `event_at` | string or null | Its local start time: `"14:00"` if today, `"Tue 09:00"` if tomorrow. null with `event`. |
| `event_ts` | int or null | `event`'s start as Unix epoch seconds, for a countdown. Added 2026-10-26. |
| `now_id` | string or null | Things id of the item in `next` (the NOW item, after any done or skip below). null when `next` is null or the Mac agent is too old to send ids. Added 2026-10-26. |

The Mac agent sends the next few timed events (48 h ahead) on each sync, and the Pi picks the first one still to come each time the feed is read, so `event` moves on even between syncs. All-day events and the Birthdays and Holidays calendars are left out.

Unknown fields must be ignored by the watch, so new fields can be added without breaking it.

## Phone feed: `/feed/today.json`

Added 2026-10-26 for the Pocket Pal iPhone app, which shows the whole Today list. Same header and token as the watch feed. The body is the watch feed plus `items`, every open item in Things Today in Today order (at most 40):

```json
{ "...": "the watch fields above", "items": [
  { "title": "A-kasse hours", "kind": "admin", "due": null },
  { "title": "Pay the electricity bill", "kind": "bill", "due": "2026-10-30" }
] }
```

| Field | Type | Notes |
| --- | --- | --- |
| `items[].title` | string | Up to 80 characters, cut with "…". |
| `items[].kind` | string | As `kind` above. |
| `items[].due` | string or null | The Things deadline as `YYYY-MM-DD`. |

The watch keeps using `/feed/watch.json`, which stays under 1 KB for the Garmin's memory. The list needs the Mac agent from this change too (it reports `today`); until it runs, `items` is empty.

## Today list for the phone

`GET /feed/today.json`, same token: every field above plus `items`, the open Things Today list in order, each `{"id", "title", "kind", "due"}`. `id` is the Things id, so the phone can open it with `things:///show?id=<id>`. `title` is cut to 80 characters and `due` is an ISO date or null.

### Pillars

`today.json` also has `pillars`: one entry for every kind (`bill`, `school`, `admin`, `msc`, `job`, `danish`, `friend`, `travel`, `other`), each

```json
{"done_week": 2, "open": 3, "waiting_mail": 1, "last_done": "2026-10-26", "next_due": "2026-10-28"}
```

- `done_week`: Things to-dos of that kind completed since Monday 00:00 local time, plus any ticked on the watch or phone that Things hasn't caught up with.
- `open`: open to-dos of that kind anywhere in Things except Someday.
- `waiting_mail`: emails of that kind still waiting on a decision on the triage page.
- `last_done`: date of the most recent completion in the last 60 days, or null.
- `next_due`: earliest deadline among the open to-dos, or null.

A to-do's kind comes from keywords in its title, area and project (see `KIND_WORDS` in `agent.py`), so naming Things areas after the pillars ("Danish", "MSc", "Job") makes them line up.

## Done and skip from the watch or phone

`POST /api/todo/done` and `POST /api/todo/skip` with body `{"id": "<Things id>"}`. Send the `X-OneInbox-Token` header (the watch), or `Authorization: Bearer <ONEBOX_CAPTURE_TOKEN>` (the phone). The reply is `{"ok": true, ...watch feed...}`, so the caller can redraw straight away. `404` means the id isn't in today's list.

- **done**: the item drops out of both feeds immediately, `left` goes down, `done` and `xp` go up. On its next sync the Mac agent ticks it off in Things (needs `things_auth_token` in `agent.json`), and it is counted once.
- **skip**: the item moves to the end of today's order until local midnight. Things is not changed. Skipping several keeps them in the order they were skipped.

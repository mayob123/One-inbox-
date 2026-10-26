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
  "mood": "hungry"
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

Unknown fields must be ignored by the watch, so new fields can be added without breaking it.

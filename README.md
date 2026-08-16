# Eurostar Snap watcher

Checks Eurostar Snap for the lowest fare on specific routes and dates, and sends a
WhatsApp message when the price drops to or below a threshold. Runs on a GitHub
Actions cron so it keeps working while your laptop is closed.

## Setup

Two repository secrets are required (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `CALLMEBOT_PHONE` | Your WhatsApp number with country code, e.g. `+447700900123` |
| `CALLMEBOT_APIKEY` | The key CallMeBot sends you after activation |

To get the key: add CallMeBot as a WhatsApp contact and send it
`I allow callmebot to send me messages`. The current bot number is listed at
<https://www.callmebot.com/blog/free-api-whatsapp-messages/> (it rotates).

## What to edit

Everything lives in `config.json`:

| Key | Meaning |
|---|---|
| `threshold_gbp` | Alert when the day's lowest fare is at or below this |
| `realert_after_min` | Wait this long before repeating an alert for an unchanged price |
| `routes[]` | One entry per route to watch |

A route takes either a single `outbound` date or an `outbound_from` / `outbound_to`
span. A span is not one page load per day: the Snap page renders a strip of
neighbouring dates with their prices, so the watcher re-anchors the calendar at the
first date it has not seen yet and covers a month in a handful of loads. When a load
turns up no new in-range date, Snap is not selling that far ahead yet and the route
stops there.

Station codes: London St Pancras `7015400`, Paris Gare du Nord `8727100`,
Brussels Midi `8814001`, Amsterdam Centraal `8400058`.

## Running it

Two workflows:

| Workflow | Schedule | What it sends |
|---|---|---|
| Snap watcher | every 30 min | WhatsApp alert only when a fare is at or below the threshold |
| Daily digest | off, manual only | One summary of all routes |

Alerts are batched: one message per run listing every qualifying date, not one
message per date. A month-long span can qualify on many dates at once, and
per-date messages would flood WhatsApp and trip CallMeBot's rate limit.

The daily digest used to double as a heartbeat and is off by request, so nothing
arrives when there is no deal. To confirm the chain is still alive, run Daily
digest by hand from the Actions tab, or uncomment its cron in
`.github/workflows/digest.yml`.

Two kill switches, in increasing order of bluntness: set
`notify.callmebot.enabled` to `false` to keep scraping but stop the messages, or
`gh workflow disable "Snap watcher"` to stop the scheduled runs entirely
(`gh workflow enable` to undo). Commenting out the cron works too but only takes
effect once pushed.

Trigger either by hand from the Actions tab → Run workflow. Watcher runs upload
`monitor.log` as an artifact.

Locally: `SNAP_FORCE_CHECK=1 CALLMEBOT_PHONE=... CALLMEBOT_APIKEY=... python monitor.py`
Test the notification path only: `python monitor.py --test-notify`
Send a digest on demand: `python monitor.py --digest`

## Limitations

- Snap only sells between 14 days and 48 hours before departure. Outside that
  window, or when a date is sold out, the price shows as `-`.
- Snap's results page only exposes the lowest fare per day, so this cannot filter
  by departure time. `window_pref` is a label, not a filter.
- Scheduled GitHub Actions runs are best-effort and can be delayed under load.

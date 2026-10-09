# OctoDave

Dave's own dashboard for Octopus Energy's **Agile Octopus** tariff, in two versions that share the same features and look:

| | Mac | iPhone |
|---|---|---|
| Lives in | `mac/OctoDave.app` | `docs/` (served by GitHub Pages) |
| Runs as | A small Python server on 127.0.0.1:8765, opened in the browser | A single web page added to the Home Screen |
| API key kept in | macOS Keychain | The page's local storage on the phone |
| Talks to Octopus | From Python | Directly from the page |

## What it shows

- **Right now**: live watts from the Octopus Home Mini, the current Agile price, cost per hour, and **today so far** (today's Home Mini half-hours priced at each half-hour's Agile rate, plus today's standing charge; refreshed every 5 minutes).
- **Prices ahead**: the published Agile prices from now on, plus the cheapest 1, 2 or 3-hour window.
- **This billing month** (from the billing day, 15th by default): cost so far against the same point last month, a forecast for the month (actual so far + 28-day average daily energy cost + standing charge for each day left), a cumulative chart and the workings, plus an optional monthly **budget** showing spent so far, the even pace and the forecast against it.
- **History** (7, 30 or 90 days): usage, cost, average price, the 4–7pm peak, a "shift your peak use" savings slider, the most expensive evenings, a half-hourly heatmap, and what the same usage would have cost on Flexible, Tracker and a 12-month fix.
- CSV export of the half-hourly history.

## Setup details needed

From the Octopus account, under Personal details › API access: the API key (`sk_live_…`), the electricity MPAN, the meter serial number and the account number (`A-1234ABCD`). The Home Mini device ID is optional; it's found from the account automatically.

**None of these go in this repository.** They're entered in the app and stored on the device.

## Mac app

Build the zip and install:

```sh
./mac/build.sh            # writes dist/OctoDave-mac.zip
```

Unzip, drag **OctoDave** to Applications, quit the old copy first (Quit button at the bottom of its page). macOS asks for approval once per new build: System Settings › Privacy & Security › Open Anyway. Needs Python 3.8+ (python.org or Homebrew). Logs: `~/.agile-dashboard/app.log`.

Run without building: `python3 mac/OctoDave.app/Contents/Resources/agile_dashboard.py`

## iPhone app

`docs/` is a static site. With GitHub Pages set to deploy from the `main` branch, `/docs` folder, it's served at `https://privster.github.io/octodave/`. On the iPhone: open that address in Safari › Share › **Add to Home Screen**. Open OctoDave from the Home Screen icon, then enter the details. (A Home Screen web app has its own storage, separate from Safari, so set it up from the icon, not in a Safari tab.)

Updates: push to `main`; the phone picks up the new page next time it's opened (sometimes after a second open).

## Security notes

- The repository is public (free GitHub Pages needs that). It contains code only, no keys or meter details.
- The page has a strict Content-Security-Policy: it can only talk to `api.octopus.energy` (and the `api.oegb-kraken.energy` fallback) and loads no third-party scripts.
- On the phone the key is in local storage for the `privster.github.io` origin. Every GitHub Pages site under the PrivSter account shares that origin, so any other Pages site published from this account could read it. Keep this the only Pages site on the account, or move it to its own domain later.
- An Octopus API key reads account and usage data. If it's ever exposed, regenerate it on the Octopus API access page and re-enter it in both apps.

## Octopus API notes

- REST: `https://api.octopus.energy/v1/` with the key as the HTTP Basic username. Half-hourly consumption arrives about a day late.
- Live data: GraphQL at `/v1/graphql/`. Get a token with `obtainKrakenToken(input: {APIKey})`, send it as the `Authorization` header. `smartMeterTelemetry(deviceId, start, end, grouping)` returns `readAt`, `consumption` (running total, Wh), `consumptionDelta` (Wh), `demand` (W), `costDelta`, `costDeltaWithTax`. Groupings: `TEN_SECONDS`, `ONE_MINUTE`, `FIVE_MINUTES`, `HALF_HOURLY`, `HOURLY`.
- Octopus rate-limits live data (error codes `KT-CT-1199`, `KT-CT-1188`). The apps poll live readings once a minute and today's total every 5 minutes, and pause live updates if limited.
- The API allows calls from web pages on other sites (CORS), which is what lets the iPhone version work without a server.

# OctoDave: notes for Claude Code

Read README.md first. This file adds how to work on the project.

## Owner
Dave, UK, on Agile Octopus with an Octopus Home Mini. Infosec analyst, so keep security decisions explicit and explained. British English in all UI text. Plain, direct copy.

## Layout
- `mac/OctoDave.app/Contents/Resources/agile_dashboard.py`: the whole Mac app. Python 3.8+ standard library only (no pip packages). Contains the server, the Octopus client and the page (the `PAGE` string).
- `mac/OctoDave.app/Contents/MacOS/OctoDave`: bash launcher. Runs `agile_dashboard.py` from Dave's clone at `~/Code/octodave` when it exists and parses, otherwise the copy in the bundle. Starts the server on port 8765 with `--app` (stops itself an hour after the last sign of an open tab; an open tab reconnects by itself when the app is reopened).
- `mac/build.sh`: zips the app into `dist/` (git-ignored).
- `docs/index.html`: the whole iPhone app, a browser port of the Mac app. Served by GitHub Pages from `/docs` on `main`.

## Rules
- Keep the two versions in step. A feature added to one should be added to the other unless it only makes sense on one device. The pricing logic is deliberately the same in both: `price_today()` in Python and `priceToday()` in JS.
- Never commit keys, MPANs, meter serials, account numbers or device IDs. Not in code, tests, fixtures or commit messages. Test fixtures use made-up values.
- No third-party scripts, fonts or CDNs in `docs/index.html`; keep its Content-Security-Policy meta tag as strict as it is now. If a new Octopus host is needed, add it to `connect-src` only.
- Respect Octopus rate limits: live readings at most once a minute, today's total at most every 5 minutes.
- Bump the version in all places when releasing: `VERSION` in agile_dashboard.py, `CFBundleShortVersionString` and `CFBundleVersion` in Info.plist, `VERSION` and the footer text in docs/index.html.

## Releasing (Dave doesn't use git or GitHub himself; Claude does all of it)
1. Commit and push to `main`. GitHub Pages updates the iPhone app within a couple of minutes.
2. On Dave's Mac, pull into `~/Code/octodave` (`git pull --ff-only origin main`). The Mac app runs the code from there, so Dave only needs to Quit and reopen it. No new zip is needed unless the launcher or Info.plist changes.
3. Check the live page shows the new version in its footer.

## Testing
- Mac: run `python3 mac/OctoDave.app/Contents/Resources/agile_dashboard.py --no-browser --port 8899` and call the JSON routes with the session token from the page, or just open http://127.0.0.1:8899/. The saved key in the Keychain is used, so this hits Dave's real account: read-only calls only, and keep the number of live (GraphQL) calls low.
- iPhone page: serve `docs/` locally (`python3 -m http.server -d docs 8123`) and open it in a browser. Entering real details there stores them in that browser's local storage; use "Forget my details" afterwards.
- Check both apps in light and dark mode, and the iPhone page at 390px wide with no sideways scrolling.

## History
- 1.5: Mac app (Python server, Keychain, history analysis, live Home Mini, tariff comparison).
- 1.6: "Today so far" in the Right now panel (both). First iPhone version, adding Prices ahead and the cheapest window.
- 1.7: Roadmap section 1. Real device ID removed from help text; device ID format check; out-of-date live readings flagged and priced at the rate when taken; Mac app no longer quits under an open tab and reconnects after a restart; Prices ahead on the Mac; iPhone warns when set up in a Safari tab.
- 1.8: Mac app runs the code from `~/Code/octodave`, so updates need only Quit and reopen. Footer shows the version and where the code came from.
- 1.9: Billing month (roadmap 2.1 and 2.2): billing day setting (default 15) and a This billing month panel with cost so far vs last month, an end-of-month forecast, a cumulative chart and the workings. Same `computeForecast()` in both apps.
- 1.9.2: Removed the Billing month period button (it mostly duplicated the This billing month panel). A heading above the history panels says which days they cover.
- 1.10: Monthly budget (roadmap 2.3) inside This billing month: spent vs budget, even pace, forecast vs budget. Mac saves it via POST /api/prefs into config.json; iPhone in local storage. Same `renderBudget()` in both.
- 1.11: History fills the gap since Octopus's last settled reading (up to 3 days) with Home Mini half-hours (roadmap 2.6), flagged as estimates (4th element of each `hh` row, `estimated_from`, a `source` column in the CSV, faded bars and squares). The forecast leaves out today. Same `halfhour_energy()` / `halfhourEnergy()` in both apps.
- 1.11.1: Budget box moved up, straight under the billing month figures.
- 1.12: Price bands (roadmap 2.4). Each half-hour is Low, Mid or High by unit price; thresholds default to 10p and 25p, editable, saved as `band_low_p` / `band_high_p` (Mac config via `POST /api/prefs`, iPhone `App.setBands`). New panel above the heatmap: kWh, £ and share per band, a High-rate note, daily kWh stacked by band. Prices ahead is now coloured by band with a key. `/api/prefs` only changes the keys it is sent; setup keeps budget and bands (`PREF_KEYS`).
- 1.13: Today's prices at a glance (roadmap 2.5) under the live reading: price now with its band, when it changes and the next price, and today's lowest, highest and spread. `/api/ahead` and `App.ahead()` now return slots from midnight today (`from` field); the Prices ahead chart still starts at the current half-hour. The live pill shows the band (Low / Mid / High rate, plus "4–7pm peak" in the peak), or "Reading out of date". `CHEAP_P` removed.
- 1.13.1: The price glance sits on one line on wide screens (900px and up, so the Mac); it stays as two lines on the iPhone.
- 1.14: Day drill-down (roadmap 2.7). Clicking or tapping a day in the 4–7pm chart opens `#dayPanel` (above Price bands): used, cost incl. standing, average price, 4–7pm share, band mix, and half-hourly kWh bars by band with the unit price as a stepped line on a right-hand scale; Previous / Next / Close; Home Mini estimates noted and faded. Shared `openDay` / `renderDay` in both apps; the Mac daily chart now uses clickable groups like the iPhone.
- 1.15: Insights (roadmap 2.8), panel above the heatmap, complete days only (today left out). Always on: 10th-percentile half-hour × 2 = kW, cost a month priced at the plain average of half-hourly prices (it runs round the clock). Busiest hour: the two neighbouring half-hours with the most use, its share and average price against the overall average, with a note if it costs over 15% more or is overnight. Average-day chart with the busiest hour highlighted and the always-on level dashed; workings in a details box. Shared `computeInsights` / `renderInsights`.
- 1.16: Weather-adjusted forecast (roadmap 2.9). Shared `fitWeather` / `weatherAdjust` / `weatherRows`; `computeForecast(data, bill, wx)`. The billing-month fetch now starts at least 92 days back for the fit. Mac `GET /api/weather` (Python `fetch_json`, cached 3 h, 15 min after a failure); iPhone `App.weather()`. Location: outward postcode from the Octopus account → postcodes.io → coordinates rounded to 0.1°, saved as `wx_outcode` / `wx_lat` / `wx_lon` (dropped on setup so they're re-derived). Only the outward code leaves the app. iPhone CSP `connect-src` adds `api.open-meteo.com` and `api.postcodes.io`. Not applied unless the fit shows a real link; the workings say why.
- 1.17: Equipment tariffs in the comparison (roadmap 3). `EQUIPMENT` list (code prefix → label, what it needs) and `pick_equipment_products` / `pickEquipmentProducts`, preferring variable versions and skipping Loyal / EV Saver fixes. Only fetched when the `#cmpEquip` box is ticked: `/api/analysis?equip=1` on the Mac (cache key includes it), `App.analysis(..., equip)` on the iPhone (key suffix `|equip`). Setting saved as `show_equipment` (kept on setup). Rows carry `needs` and show a "Needs …" label. `/api/prefs` now also takes `show_equipment`.

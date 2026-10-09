# OctoDave roadmap

Brief for Claude Code. Read CLAUDE.md first; its rules apply to everything here (keep Mac and iPhone in step, no personal details in the repo, strict CSP, respect Octopus rate limits).

Reference app: **Octo-Aid** (Dave uses it on his iPhone). Its screens were reviewed on 9 Oct 2026 and the useful ideas are listed below. Build OctoDave's own versions, in OctoDave's look; don't copy Octo-Aid's design.

**Validated against Octo-Aid on 9 Oct 2026:** "Today so far" (cost, kWh, standing charge) and the 7-day totals (kWh and £) matched exactly. Treat the current pricing logic as correct and don't change it without a reason.

---

## 1. Fixes (do these first)

1.1 **Remove the real Home Mini device ID from both apps.** The "it looks like 00-1C-55-…" example in the error text and the setup hint is Dave's real device ID. Replace it with a made-up one of the same format (for example `00-00-00-00-00-00-00-00`) in `agile_dashboard.py` and `docs/index.html`.

1.2 **Device ID validation (both apps).** A Home Mini device ID is 8 hex pairs separated by hyphens. Reject anything else in setup with a clear message. Dave entered an 8-digit number from Octo-Aid, which gave "Unauthorized". Note in the hint that it is *not* the Wi-Fi MAC address shown in the router (the Home Mini shows up there as `OCTO-CADLITE` with a different address). Also: for Dave's account, automatic lookup returns no device ("Octopus didn't list a device"), so the manually entered ID is required. Keep the manual field.

1.3 **Out-of-date live readings.** Octopus sometimes doesn't receive Home Mini readings for an hour or more (seen 9 Oct: reading 69 min old in both apps). When the reading is over 10 minutes old:
- show it clearly as out of date (a pill or muted state, not just the small "Reading from 69 min ago" text);
- price "Costing" at the rate for the half-hour when the reading was taken, and label it as such. Octo-Aid does this; OctoDave currently uses the current half-hour's price, which mixes an old reading with a new price.

1.4 **Mac app stops itself while its tab is still open.** The server shuts down 10 minutes after the last heartbeat. Safari pauses background tabs, so the heartbeat stops and the app quits while the tab still looks open. Options: a longer idle limit (for example 60 minutes), sending a heartbeat on `visibilitychange`, and a clear "OctoDave has stopped, open it again from Applications" state in the page. Pick the simplest that works.

1.5 **Add `.gitignore` to the repo** (it was missed on upload): `dist/`, `.DS_Store`, `__pycache__/`, `*.pyc`, `*.log`, `.env`, `config.json`, `api-key`.

1.6 **Parity:** "Prices ahead" and the cheapest 1/2/3-hour window exist only on the iPhone. Add them to the Mac app.

---

## 2. Features from Octo-Aid worth adding

Ordered by usefulness to Dave. Each needs to work on both apps.

### 2.1 Billing month
Octo-Aid works in billing months; Dave's runs from the **15th to the 14th**. Add a "billing day" setting (default 15) and a "This billing month" period alongside 7d/30d/90d.
- Cost so far this billing month, with kWh.
- Compared with the same point in the last billing month ("£0.06 higher than this point last billing month").

### 2.2 Forecast to the end of the billing month
Octo-Aid's method, which is transparent and reasonable:
- actual cost so far + (average daily energy cost over the last 28 days × days remaining) + standing charge for the remaining days;
- compare with last billing month's total ("£5.98 below last month");
- chart: cumulative cost by day for this month (actual, then projected) against last month;
- an expandable "How this is worked out" section listing the inputs (window used, days, averages, standing charge). Dave values seeing the workings.

### 2.3 Budget
- A monthly budget amount per billing month (for example £62), stored on the device.
- Remaining, % used, days elapsed, and a gauge.
- Actual vs pace (budget × days elapsed ÷ days in month) vs predicted (from 2.2), with an over/under message.

### 2.4 Price bands
Octo-Aid splits usage into High, Mid and Low rate. Add bands with editable thresholds (suggested defaults: Low under 10p, High 25p and over, Mid in between; negative prices count as Low).
- Daily usage bars stacked by band.
- Band summary for the period: share of kWh, kWh and £ for each band.
- "High-rate usage" insight: % of kWh at High rate and what it cost.

### 2.5 Today's prices at a glance
In or next to the Right now panel:
- "Until 20:30 · next 8.4p at 20:30";
- today's lowest, highest and spread;
- the band for the current half-hour.

### 2.6 Day drill-down
Tap a day in the daily chart to open that day: half-hourly bars coloured by band, kWh, £, average p/kWh and band mix. Include **today** from the Home Mini half-hours that "Today so far" already fetches, labelled as provisional (Home Mini estimate, settled data arrives the next day).

### 2.7 Insights
- **Base load**: the always-on power in W, plus what it costs per month. Suggested method: a low percentile (for example the 10th) of half-hourly kWh over the period × 2 to get kW, priced at the period's average rate.
- **Typical use window**: the hour with the largest share of usage ("04:30–05:30, 22% of recent usage"). That's a large share for a single early-morning hour. Showing it helps spot something on a timer that could move to a cheaper slot.

---

## 3. Later

- **Price alerts.** Octo-Aid has notifications. A page on GitHub Pages can't send push notifications by itself. A simpler route is a Claude scheduled task that runs in the cloud after 16:00 each day, reads tomorrow's Agile prices (public API, no key needed) and messages Dave about negative or very cheap slots. Dave wants scheduled tasks to run in the cloud only, never tied to his laptop.
- **Tariff comparison.** OctoDave leaves out tariffs that need extra equipment. Octo-Aid includes them: it ranked Cosy (needs a heat pump), Snug (needs electric storage heaters) and Intelligent Flux (needs a battery or solar) as cheaper, which isn't useful unless Dave has that equipment. Keep them hidden by default, with an option to show them flagged by what they need.
- Share or export a period summary (Octo-Aid has a share button on Usage).

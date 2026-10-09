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

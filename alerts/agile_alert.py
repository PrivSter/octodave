#!/usr/bin/env python3
"""Agile price alert for tomorrow, run by .github/workflows/agile-alert.yml.

Fetches tomorrow's Agile Octopus unit rates (public API, no key) and sends a
push notification through ntfy if any half-hour is negative or under the
threshold. Silent otherwise. Standard library only.

Environment:
  NTFY_TOPIC   ntfy topic to publish to (a GitHub secret; never commit it)
  TARIFF       tariff code (default Dave's: Agile import, region A)
  THRESHOLD_P  alert below this many p/kWh (default 5)
  FINAL        "1" on the last run of the day: report unpublished prices or
               failures instead of waiting for the next run
  FOR_DATE     YYYY-MM-DD to check instead of tomorrow (testing)
  TEST         "1" to label the message as a test and skip the duplicate check
  DRY_RUN      "1" to print the message instead of sending it
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
TARIFF = os.environ.get("TARIFF") or "E-1R-AGILE-24-10-01-A"
PRODUCT = TARIFF[5:-2]  # E-1R-<product>-A
THRESHOLD = float(os.environ.get("THRESHOLD_P") or 5)
FINAL = os.environ.get("FINAL") == "1"
TEST = os.environ.get("TEST") == "1"
DRY = os.environ.get("DRY_RUN") == "1"
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY = "https://ntfy.sh/"
UA = {"User-Agent": "octodave-agile-alert (github.com/PrivSter/octodave)"}


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def day_bounds(day):
    start = dt.datetime.combine(day, dt.time(0), UK)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), UK)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def fetch_slots(day):
    start, end = day_bounds(day)
    q = urllib.parse.urlencode({
        "period_from": start.strftime("%Y-%m-%dT%H:%MZ"),
        "period_to": end.strftime("%Y-%m-%dT%H:%MZ"),
        "page_size": 100,
    })
    url = (f"https://api.octopus.energy/v1/products/{PRODUCT}/electricity-tariffs/"
           f"{TARIFF}/standard-unit-rates/?{q}")
    slots = []
    for r in get_json(url).get("results", []):
        t = dt.datetime.fromisoformat(r["valid_from"].replace("Z", "+00:00"))
        if start <= t < end:
            slots.append((t.astimezone(UK), float(r["value_inc_vat"])))
    slots.sort()
    return slots, start, end


def hhmm(t):
    return t.strftime("%H:%M")


def price(p):
    s = f"{abs(p):.1f}p"
    return ("−" + s) if p < 0 else s


def runs(slots, test):
    """Group neighbouring half-hours that pass test(price) into runs."""
    out, cur = [], []
    for t, p in slots:
        if test(p) and (not cur or t - cur[-1][0] == dt.timedelta(minutes=30)):
            cur.append((t, p))
            continue
        if cur:
            out.append(cur)
        cur = [(t, p)] if test(p) else []
    if cur:
        out.append(cur)
    return out


def describe(run):
    lo_t, lo_p = min(run, key=lambda s: s[1])
    end = run[-1][0] + dt.timedelta(minutes=30)
    return f"{hhmm(run[0][0])}–{hhmm(end)}", lo_p, hhmm(lo_t)


def already_sent(tag):
    if TEST or DRY:
        return False
    try:
        url = f"{NTFY}{urllib.parse.quote(TOPIC)}/json?poll=1&since=20h"
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            for line in r.read().decode().splitlines():
                if line.strip() and tag in (json.loads(line).get("tags") or []):
                    return True
    except Exception as e:  # a failed check shouldn't block the alert
        print(f"Couldn't check earlier messages: {e}")
    return False


def send(title, body, tags, priority="default"):
    if TEST:
        title = "TEST: " + title
    print(f"--- {title}\n{body}\n(tags {tags})")
    if DRY:
        return
    if not TOPIC:
        sys.exit("NTFY_TOPIC isn't set, so nothing was sent.")
    # Titles go in an HTTP header, so keep them plain ASCII.
    title = title.replace("−", "-").replace("–", "-")
    req = urllib.request.Request(
        NTFY + urllib.parse.quote(TOPIC), data=body.encode("utf-8"), method="POST",
        headers={**UA, "Title": title, "Tags": ",".join(tags), "Priority": priority,
                 "Content-Type": "text/plain; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def main():
    day = (dt.date.fromisoformat(os.environ["FOR_DATE"]) if os.environ.get("FOR_DATE")
           else dt.datetime.now(UK).date() + dt.timedelta(days=1))
    label = day.strftime("%a %-d %b")
    # Tests get their own tag so they never stop the real alert for that day.
    tag = ("test" if TEST else "d") + day.strftime("%Y%m%d")

    if already_sent(tag):
        print(f"Already notified for {day}; nothing to do.")
        return

    slots = None
    for attempt in (1, 2):
        try:
            slots, _, _ = fetch_slots(day)
            break
        except Exception as e:
            err = e
            print(f"Fetch attempt {attempt} failed: {e}")
            if attempt == 1:
                time.sleep(60)
    if slots is None:
        if FINAL:
            send("Agile price check failed",
                 f"Couldn't fetch the Agile prices for {label} from Octopus: {err}. "
                 "No price check today.", ["warning", tag], "low")
        return

    print(f"{day}: {len(slots)} half-hours")
    if len(slots) < 40:
        if FINAL:
            send("Agile prices not out",
                 f"Prices for {label} weren't published by the time of the last check "
                 f"({len(slots)} of 48 half-hours), so there's no price check today.",
                 ["hourglass", tag], "low")
        return

    neg = runs(slots, lambda p: p < 0)
    cheap = runs(slots, lambda p: 0 <= p < THRESHOLD)
    lo_t, lo_p = min(slots, key=lambda s: s[1])
    hi_t, hi_p = max(slots, key=lambda s: s[1])
    print(f"lowest {price(lo_p)} at {hhmm(lo_t)}, highest {price(hi_p)} at {hhmm(hi_t)}")
    if not neg and not cheap:
        print(f"No alert - nothing under {THRESHOLD:g}p on {label}.")
        return

    lines = []
    if neg:
        span, p, at = describe(neg[0])
        title = f"Negative prices {label} {span}, down to {price(p)}"
        for r in neg:
            span, p, at = describe(r)
            lines.append(f"Negative {span}: lowest {price(p)} at {at}")
    else:
        span, p, at = describe(min(cheap, key=lambda r: min(x for _, x in r)))
        title = f"Under {THRESHOLD:g}p {label} {span}, down to {price(p)}"
    for r in cheap:
        span, p, at = describe(r)
        lines.append(f"Under {THRESHOLD:g}p {span}: lowest {price(p)} at {at}")
    lines.append(f"Day's range: {price(lo_p)} at {hhmm(lo_t)} to {price(hi_p)} at {hhmm(hi_t)}")
    lines.append("Good times for the washing machine, dishwasher, tumble dryer, "
                 "hot water or charging things.")
    send(title, "\n".join(lines), ["zap", tag], "high" if neg else "default")


if __name__ == "__main__":
    main()

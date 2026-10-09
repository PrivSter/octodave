#!/usr/bin/env python3
"""
OctoDave
========

A local dashboard for an Octopus Energy (UK) customer on Agile Octopus.

It runs a small web server on your own computer (127.0.0.1 only, so nothing
else on your network can reach it) and opens the dashboard in your browser.

  * Your API key is stored in the macOS Keychain (on other systems, in a file
    only your user account can read). It is only ever sent to Octopus Energy.
  * The page never sees the key. The server makes every Octopus request.
  * No third-party packages and no external scripts: Python 3.8+ only.

Run it:
    python3 agile_dashboard.py               # start and open the dashboard
    python3 agile_dashboard.py --no-browser  # start without opening a tab
    python3 agile_dashboard.py --port 8899   # use a different port

Stop it with Ctrl+C in the Terminal window.
"""

import argparse
import base64
import bisect
import json
import os
import platform
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "OctoDave"
VERSION = "1.6"

# Octopus documents api.octopus.energy as the API host. The account page's own
# examples use api.oegb-kraken.energy, so that is kept as a fallback if the
# first host can't be reached at all.
API_HOSTS = ["https://api.octopus.energy", "https://api.oegb-kraken.energy"]

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".agile-dashboard")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
KEY_FILE = os.path.join(CONFIG_DIR, "api-key")  # only used when Keychain isn't available
KEYCHAIN_SERVICE = "agile-dashboard-octopus-api-key"
KEYCHAIN_ACCOUNT = "agile-dashboard"

# A Home Mini device ID: 8 pairs of hex digits joined by dashes, e.g. 00-11-22-33-44-55-66-77.
# It is not the Wi-Fi MAC address the router shows for the Home Mini.
DEVICE_ID_RE = re.compile(r"(?:[0-9A-F]{2}-){7}[0-9A-F]{2}")
DEVICE_ID_HELP = ("The Home Mini device ID should be 8 pairs of letters and numbers joined by dashes, "
                  "like 00-11-22-33-44-55-66-77. Octo-Aid shows it under Settings > Home Mini Device. "
                  "It isn't the Wi-Fi address your router lists for the Home Mini.")

# Distributor ID (first two digits of the MPAN) to Octopus region letter.
DIST_TO_GSP = {
    "10": "_A", "11": "_B", "12": "_C", "13": "_D", "14": "_E", "15": "_F",
    "16": "_G", "17": "_P", "18": "_N", "19": "_J", "20": "_H", "21": "_K",
    "22": "_L", "23": "_M",
}

ANALYSIS_TTL = 15 * 60   # seconds to reuse an analysis before asking Octopus again
LIVE_MIN_GAP = 30        # never ask Octopus for a live reading more often than this
TODAY_MIN_GAP = 5 * 60   # today's running cost: ask Octopus at most this often


# --------------------------------------------------------------------------
# Key and settings storage
# --------------------------------------------------------------------------

class KeyStore:
    """Keeps the API key in the macOS Keychain, or a 0600 file elsewhere."""

    def __init__(self):
        self.use_keychain = platform.system() == "Darwin" and shutil.which("security") is not None

    def where(self):
        return "macOS Keychain" if self.use_keychain else KEY_FILE

    def get(self):
        if self.use_keychain:
            r = subprocess.run(
                ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT,
                 "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True)
            return r.stdout.strip() or None if r.returncode == 0 else None
        try:
            with open(KEY_FILE, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    def set(self, key):
        if self.use_keychain:
            r = subprocess.run(
                ["security", "add-generic-password", "-a", KEYCHAIN_ACCOUNT,
                 "-s", KEYCHAIN_SERVICE, "-l", APP_NAME, "-w", key, "-U"],
                capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("Couldn't save the key to the Keychain: " + (r.stderr.strip() or "unknown error"))
            return
        ensure_config_dir()
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)

    def delete(self):
        if self.use_keychain:
            subprocess.run(
                ["security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE],
                capture_output=True, text=True)
        try:
            os.remove(KEY_FILE)
        except FileNotFoundError:
            pass


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_config(cfg):
    ensure_config_dir()
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def delete_config():
    try:
        os.remove(CONFIG_FILE)
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

_OFFSET_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_ts(s):
    """Octopus timestamps -> epoch seconds. Handles 'Z', '+01:00' and '+0100'."""
    s = s.strip().replace("Z", "+00:00")
    s = re.sub(r"\.\d+", "", s)  # drop fractional seconds; older Pythons can't parse 3-digit ones
    s = _OFFSET_NO_COLON.sub(r"\1:\2", s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iso_utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def local_day_start(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())


# --------------------------------------------------------------------------
# Octopus API client
# --------------------------------------------------------------------------

def make_ssl_context():
    """Default certificates, plus the Mac's own trusted roots.

    Python installed from python.org ships its own OpenSSL with no root
    certificates until 'Install Certificates.command' is run. Loading the
    certificates macOS already trusts avoids that step.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi  # optional
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    if platform.system() != "Darwin":
        return ctx
    if os.path.exists("/etc/ssl/cert.pem"):
        try:
            ctx.load_verify_locations("/etc/ssl/cert.pem")
        except Exception:
            pass
    for keychain in ("/System/Library/Keychains/SystemRootCertificates.keychain",
                     "/Library/Keychains/System.keychain"):
        try:
            r = subprocess.run(["security", "find-certificate", "-a", "-p", keychain],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and "BEGIN CERTIFICATE" in r.stdout:
                ctx.load_verify_locations(cadata=r.stdout)
        except Exception:
            pass
    return ctx


SSL_CTX = make_ssl_context()

SSL_HELP = ("Python couldn't verify Octopus's secure certificate. If you installed Python from "
            "python.org, open Finder > Applications > Python 3.x and run 'Install Certificates.command', "
            "then start the dashboard again.")


class ApiError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class Octopus:
    def __init__(self, key):
        self.key = key
        self.host = None
        self._token = None
        self._token_exp = 0.0
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------

    def request(self, path_or_url, params=None, auth=True, method="GET", body=None, headers=None):
        absolute = path_or_url.startswith("http")
        hosts = [None] if absolute else ([self.host] if self.host else API_HOSTS)
        last_err = None
        for host in hosts:
            url = path_or_url if absolute else host + path_or_url
            if params:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
            h = {"User-Agent": "agile-usage-dashboard/" + VERSION, "Accept": "application/json"}
            if headers:
                h.update(headers)
            if auth and self.key:
                h["Authorization"] = "Basic " + base64.b64encode((self.key + ":").encode()).decode()
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                h["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            try:
                with urllib.request.urlopen(req, timeout=45, context=SSL_CTX) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    if host:
                        self.host = host
                    return payload
            except urllib.error.HTTPError as e:
                if host:
                    self.host = host
                if e.code in (401, 403):
                    raise ApiError("Octopus didn't accept the API key (HTTP %d). Check it on your Octopus "
                                   "account's API access page." % e.code, e.code, "auth")
                if e.code == 404:
                    raise ApiError("Octopus couldn't find that. Check the MPAN, meter serial number and "
                                   "account number.", 404, "not_found")
                if e.code == 429:
                    raise ApiError("Octopus is limiting how often you can ask for data. Wait a few minutes "
                                   "and try again.", 429, "rate_limited")
                detail = e.read().decode("utf-8", "replace")[:200]
                raise ApiError("Octopus returned an error (HTTP %d). %s" % (e.code, detail), e.code)
            except urllib.error.URLError as e:
                if isinstance(e.reason, ssl.SSLError) or "CERTIFICATE" in str(e.reason).upper():
                    raise ApiError(SSL_HELP, None, "ssl")
                last_err = e
                continue
            except (TimeoutError, OSError) as e:
                last_err = e
                continue
        raise ApiError("Couldn't reach Octopus (%s). Check your internet connection." % last_err, None, "network")

    def get_all(self, path, params=None):
        data = self.request(path, params)
        results = list(data.get("results", []))
        nxt = data.get("next")
        pages = 1
        while nxt and pages < 200:
            data = self.request(nxt)
            results.extend(data.get("results", []))
            nxt = data.get("next")
            pages += 1
        return results

    # -- GraphQL (live readings) ------------------------------------------

    def _token_value(self):
        with self._lock:
            if self._token and time.time() < self._token_exp:
                return self._token
        d = self.graphql(
            "mutation GetToken($key: String!) { obtainKrakenToken(input: {APIKey: $key}) { token } }",
            {"key": self.key}, with_token=False)
        tok = (d.get("obtainKrakenToken") or {}).get("token")
        if not tok:
            raise ApiError("Octopus didn't issue a token for live readings.")
        with self._lock:
            self._token = tok
            self._token_exp = time.time() + 50 * 60
        return tok

    def graphql(self, query, variables=None, with_token=True, _retried=False):
        headers = {"Authorization": self._token_value()} if with_token else {}
        data = self.request("/v1/graphql/", auth=False, method="POST",
                            body={"query": query, "variables": variables or {}}, headers=headers)
        errors = data.get("errors") or []
        if errors:
            codes = [((e.get("extensions") or {}).get("errorCode") or "") for e in errors]
            text = " ".join(e.get("message", "") for e in errors)
            if "KT-CT-1199" in codes or "KT-CT-1188" in codes:
                raise ApiError("Octopus is limiting live readings for now. Live updates are paused; "
                               "try again later.", 429, "rate_limited")
            if with_token and not _retried and ("KT-CT-1124" in codes or "expired" in text.lower()):
                with self._lock:
                    self._token = None
                return self.graphql(query, variables, with_token, _retried=True)
            raise ApiError("Octopus returned an error for live data: " + (text or ", ".join(codes)))
        return data.get("data") or {}


# --------------------------------------------------------------------------
# Rates
# --------------------------------------------------------------------------

class RateTable:
    """Price periods (start, end, pence) with lookup by timestamp."""

    def __init__(self, rows):
        dd = [r for r in rows if r.get("payment_method") in (None, "DIRECT_DEBIT")]
        rows = dd or rows
        items = []
        seen = set()
        for r in rows:
            if r.get("value_inc_vat") is None or not r.get("valid_from"):
                continue
            start = parse_ts(r["valid_from"])
            if start in seen:
                continue
            seen.add(start)
            end = parse_ts(r["valid_to"]) if r.get("valid_to") else float("inf")
            items.append((start, end, float(r["value_inc_vat"])))
        items.sort()
        self.starts = [i[0] for i in items]
        self.ends = [i[1] for i in items]
        self.values = [i[2] for i in items]

    def __bool__(self):
        return bool(self.values)

    def at(self, t):
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0 and t < self.ends[i]:
            return self.values[i]
        return None

    def nearest(self, t):
        if not self.values:
            return None
        i = bisect.bisect_right(self.starts, t) - 1
        return self.values[max(i, 0)]


def tariff_to_product(tariff_code):
    # E-1R-AGILE-24-10-01-A -> AGILE-24-10-01
    parts = tariff_code.split("-")
    return "-".join(parts[2:-1])


def fetch_rates(octo, tariff_code, kind, t0, t1):
    product = tariff_to_product(tariff_code)
    path = "/v1/products/%s/electricity-tariffs/%s/%s/" % (product, tariff_code, kind)
    rows = octo.get_all(path, {"period_from": iso_utc(t0), "period_to": iso_utc(t1), "page_size": 1500})
    if not rows:
        rows = octo.get_all(path, {"page_size": 100})
    return RateTable(rows)


# --------------------------------------------------------------------------
# Tariffs to compare against
# --------------------------------------------------------------------------

# Tariffs that need extra kit (EV, heat pump, storage heaters, solar or battery)
# or that are export/prepay/business products are left out of the comparison.
EXCLUDE = re.compile(r"\b(go|ev|cosy|flux|snug|intelligent|outgoing|export|prepay|business|"
                     r"power ?pack|economy|drive|zero bills?)\b", re.I)


def _name(p):
    return ((p.get("full_name") or "") + " " + (p.get("display_name") or "")).strip()


def _newest(cands):
    return max(cands, key=lambda p: p.get("available_from") or "") if cands else None


def pick_comparison_products(products):
    base = [p for p in products
            if (p.get("direction") or "IMPORT").upper() == "IMPORT"
            and not p.get("is_business") and not p.get("is_prepay")]
    open_ = [p for p in base if not p.get("is_restricted") and not EXCLUDE.search(_name(p))]

    flexible = _newest([p for p in open_ if "flexible" in _name(p).lower()])
    tracker = _newest([p for p in open_ if p.get("is_tracker") or "tracker" in _name(p).lower()])
    fixed_all = [p for p in open_
                 if not p.get("is_variable") and not p.get("is_tracker")
                 and ("fix" in _name(p).lower() or "FIX" in p.get("code", "").upper())]
    fixed = _newest([p for p in fixed_all if p.get("term") == 12]) or _newest(fixed_all)

    picks = []
    if flexible:
        picks.append(("Flexible Octopus", flexible))
    if tracker:
        picks.append(("Octopus Tracker", tracker))
    if fixed:
        picks.append(("12-month fixed", fixed))
    agile = _newest([p for p in base if "AGILE" in p.get("code", "").upper()
                     and "OUTGOING" not in p.get("code", "").upper()])
    return picks, agile


def region_tariff_code(octo, product_code, region):
    detail = octo.request("/v1/products/%s/" % product_code)
    by_region = (detail.get("single_register_electricity_tariffs") or {}).get(region) or {}
    t = by_region.get("direct_debit_monthly") or (next(iter(by_region.values())) if by_region else None)
    if not t or not t.get("code"):
        raise ApiError("No %s price is published for region %s." % (product_code, region.strip("_")))
    return t["code"], detail.get("full_name") or detail.get("display_name") or product_code


def price_today(rows, units, day_start):
    """Price today's Home Mini readings.

    rows: sorted [(slot_start_epoch, cumulative_wh or None, delta_wh or None)], ideally
    starting with the half-hour before midnight so the day's first slot has a baseline.
    Energy in a slot is the rise in the meter's running total since the previous slot.
    If Octopus sends no running totals, each slot's own delta is used instead.
    Returns (kwh, energy_pence, kwh_with_no_published_price).
    """
    kwh = energy = unpriced = 0.0
    use_cumulative = any(cum is not None for _, cum, _ in rows)
    prev = None
    for t, cum, delta in rows:
        wh = None
        if use_cumulative:
            if cum is None:
                continue
            if prev is not None and cum >= prev:
                wh = cum - prev
            prev = cum
        else:
            wh = delta
        if wh is None or t < day_start:
            continue
        k = wh / 1000.0
        rate = units.at(t) if units else None
        kwh += k
        if rate is None:
            unpriced += k
        else:
            energy += k * rate
    return kwh, energy, unpriced


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------

class App:
    def __init__(self, port):
        self.port = port
        self.session_token = secrets.token_urlsafe(32)
        self.keystore = KeyStore()
        self.cfg = load_config()
        key = self.keystore.get()
        self.octo = Octopus(key) if key else None
        self.lock = threading.Lock()
        self.analysis_cache = {}
        self.products_cache = None
        self.live_cache = None
        self.live_at = 0.0
        self.today_cache = None
        self.today_at = 0.0
        self.current_rate_cache = {}
        self.agreement_extended = None
        self.last_seen = time.monotonic()

    # -- status and setup -------------------------------------------------

    def configured(self):
        return bool(self.octo and self.cfg.get("mpan") and self.cfg.get("serial"))

    def status(self):
        key = self.octo.key if self.octo else ""
        hint = (key[:8] + "…" + key[-4:]) if len(key) > 12 else ("saved" if key else "")
        return {
            "configured": self.configured(),
            "key_hint": hint,
            "key_store": self.keystore.where(),
            "mpan": self.cfg.get("mpan", ""),
            "serial": self.cfg.get("serial", ""),
            "account": self.cfg.get("account", ""),
            "device_id": self.cfg.get("device_id", ""),
            "region": (self.cfg.get("region") or "").strip("_"),
            "host": self.octo.host if self.octo else None,
            "live_available": bool(self.cfg.get("account") or self.cfg.get("device_id")),
        }

    def setup(self, body):
        key = (body.get("key") or "").strip()
        if not key and self.octo:
            key = self.octo.key
        mpan = re.sub(r"\s", "", body.get("mpan") or "")
        serial = re.sub(r"\s", "", body.get("serial") or "").upper()
        account = re.sub(r"\s", "", body.get("account") or "").upper()
        device = re.sub(r"[\s:]", "-", (body.get("device_id") or "").strip()).upper()

        if not key:
            raise ApiError("Enter your API key.", 400)
        if not re.fullmatch(r"sk_live_[A-Za-z0-9]+", key):
            raise ApiError("That doesn't look like an Octopus API key. It should start with sk_live_.", 400)
        if not re.fullmatch(r"\d{13}", mpan):
            raise ApiError("The MPAN should be 13 digits.", 400)
        if not serial:
            raise ApiError("Enter your electricity meter's serial number.", 400)
        if account and not re.fullmatch(r"A-[0-9A-F]{8}", account):
            raise ApiError("The account number should look like A-1234ABCD.", 400)
        if device and not DEVICE_ID_RE.fullmatch(device):
            raise ApiError(DEVICE_ID_HELP, 400)

        octo = Octopus(key)
        region = None
        try:
            mp = octo.request("/v1/electricity-meter-points/%s/" % mpan)
            region = mp.get("gsp")
        except ApiError as e:
            if e.status in (401, 403):
                raise
        region = region or DIST_TO_GSP.get(mpan[:2])
        if not region:
            raise ApiError("Couldn't work out your region from that MPAN.", 400)

        # Proves the key, MPAN and serial work together.
        octo.request("/v1/electricity-meter-points/%s/meters/%s/consumption/" % (mpan, serial),
                     {"page_size": 1})

        if account:
            acc = octo.request("/v1/accounts/%s/" % account)
            mpans = [emp.get("mpan") for prop in acc.get("properties", [])
                     for emp in prop.get("electricity_meter_points", [])]
            if mpan not in mpans:
                raise ApiError("That account number doesn't include this MPAN.", 400)

        self.keystore.set(key)
        cfg = {"mpan": mpan, "serial": serial, "account": account, "device_id": device, "region": region}
        save_config(cfg)
        with self.lock:
            self.cfg = cfg
            self.octo = octo
            self.analysis_cache.clear()
            self.live_cache = None
            self.today_cache = None
            self.current_rate_cache.clear()
        return self.status()

    def forget(self):
        self.keystore.delete()
        delete_config()
        with self.lock:
            self.cfg = {}
            self.octo = None
            self.analysis_cache.clear()
            self.live_cache = None
            self.today_cache = None
        return self.status()

    # -- tariff history -----------------------------------------------------

    def products(self):
        if self.products_cache and time.time() - self.products_cache[0] < 6 * 3600:
            return self.products_cache[1]
        rows = self.octo.get_all("/v1/products/", {"brand": "OCTOPUS_ENERGY", "page_size": 100})
        self.products_cache = (time.time(), rows)
        return rows

    def agreements(self):
        """Tariff periods for this MPAN: [(tariff_code, start_epoch, end_epoch)]."""
        account = self.cfg.get("account")
        if account:
            acc = self.octo.request("/v1/accounts/%s/" % account)
            out = []
            for prop in acc.get("properties", []):
                for emp in prop.get("electricity_meter_points", []):
                    if emp.get("mpan") != self.cfg["mpan"] or emp.get("is_export"):
                        continue
                    for a in emp.get("agreements", []):
                        if not a.get("tariff_code"):
                            continue
                        start = parse_ts(a["valid_from"]) if a.get("valid_from") else 0
                        end = parse_ts(a["valid_to"]) if a.get("valid_to") else float("inf")
                        out.append((a["tariff_code"], start, end))
            if out:
                out = sorted(set(out), key=lambda x: x[1])
                # Octopus's account records can stop at a past end date even though the
                # customer is still on that tariff. Treat the latest one as still running.
                code, start, end = out[-1]
                self.agreement_extended = None
                if end != float("inf") and end < time.time():
                    out[-1] = (code, start, float("inf"))
                    self.agreement_extended = (code, end)
                return out, True
        _, agile = pick_comparison_products(self.products())
        if not agile:
            raise ApiError("Couldn't find the Agile Octopus product. Add your account number in settings "
                           "so your exact tariff can be read.")
        code, _ = region_tariff_code(self.octo, agile["code"], self.cfg["region"])
        return [(code, 0, float("inf"))], False

    # -- analysis -------------------------------------------------------------

    def analysis(self, days, refresh=False):
        days = max(1, min(int(days), 366))
        with self.lock:
            hit = self.analysis_cache.get(days)
        if hit and not refresh and time.time() - hit[0] < ANALYSIS_TTL:
            return hit[1]
        data = self._build_analysis(days)
        with self.lock:
            self.analysis_cache[days] = (time.time(), data)
        return data

    def _build_analysis(self, days):
        octo, cfg = self.octo, self.cfg
        now = time.time()
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
        t_from = int(start.timestamp())

        cons = octo.get_all(
            "/v1/electricity-meter-points/%s/meters/%s/consumption/" % (cfg["mpan"], cfg["serial"]),
            {"period_from": iso_utc(t_from), "period_to": iso_utc(int(now)),
             "page_size": 25000, "order_by": "period"})
        readings = {}
        for r in cons:
            try:
                readings[parse_ts(r["interval_start"])] = float(r["consumption"])
            except (KeyError, ValueError, TypeError):
                continue
        if not readings:
            raise ApiError("Octopus has no half-hourly readings for this period yet. Smart meter data "
                           "usually arrives the next day.", 404, "no_data")
        times = sorted(readings)
        t0, t1 = times[0], times[-1] + 1800

        # Your actual tariff(s) over the period.
        agreements, from_account = self.agreements()
        notes = []
        if from_account and self.agreement_extended and t1 > self.agreement_extended[1]:
            code, ended = self.agreement_extended
            notes.append("Octopus's account records show %s ending on %s with nothing after it, so "
                         "readings since then are priced as if you're still on it." % (
                             tariff_to_product(code), datetime.fromtimestamp(ended).strftime("%d %b %Y")))
        periods = []
        history = []
        for code, a_start, a_end in agreements:
            lo, hi = max(a_start, t0), min(a_end, t1)
            if lo >= hi:
                continue
            periods.append((code, a_start, a_end,
                            fetch_rates(octo, code, "standard-unit-rates", lo, hi),
                            fetch_rates(octo, code, "standing-charges", lo, hi)))
            history.append({"code": code, "from": iso_utc(lo), "to": iso_utc(hi)})
        if not periods:
            # The account's tariff dates don't overlap the readings. Log what we saw so it
            # can be diagnosed, then price everything at the most recent tariff instead.
            sys.stderr.write("Tariff dates didn't overlap readings %s to %s. Agreements: %s\n" % (
                iso_utc(t0), iso_utc(t1),
                [(c, iso_utc(s) if s else "open", "open" if e == float("inf") else iso_utc(int(e)))
                 for c, s, e in agreements]))
            code = sorted(agreements, key=lambda x: x[1])[-1][0]
            periods.append((code, 0, float("inf"),
                            fetch_rates(octo, code, "standard-unit-rates", t0, t1),
                            fetch_rates(octo, code, "standing-charges", t0, t1)))
            history.append({"code": code, "from": iso_utc(t0), "to": iso_utc(t1)})
            notes.append("The tariff dates on your account didn't line up with your readings, so the "
                         "whole period is priced at %s. The Terminal window shows the dates Octopus "
                         "returned." % code)

        def period_for(t):
            for p in periods:
                if p[1] <= t < p[2]:
                    return p
            return periods[-1]

        hh = []
        missing = 0
        for t in times:
            rate = period_for(t)[3].at(t)
            if rate is None:
                missing += 1
            hh.append([t * 1000, round(readings[t], 4), None if rate is None else round(rate, 4)])

        dates = sorted({local_date(t) for t in times})
        standing_by_day = {}
        for d in dates:
            ds = local_day_start(d) + 3600  # an hour in, clear of any midnight boundary
            st = period_for(ds)[4]
            v = st.at(ds) if st else None
            standing_by_day[d] = round(v if v is not None else (st.nearest(ds) if st else 0) or 0, 4)

        actual_energy = sum(k * (p or 0) for _, k, p in hh)
        actual_standing = sum(standing_by_day.values())
        kwh_total = sum(readings.values())

        comparisons = [{
            "label": "Your tariff",
            "name": ", ".join(sorted({h["code"] for h in history})),
            "energy_p": round(actual_energy, 2),
            "standing_p": round(actual_standing, 2),
            "total_p": round(actual_energy + actual_standing, 2),
            "coverage": round(1 - missing / len(hh), 4),
            "actual": True,
        }]

        picks, _ = pick_comparison_products(self.products())
        for label, product in picks:
            try:
                code, full_name = region_tariff_code(octo, product["code"], cfg["region"])
                units = fetch_rates(octo, code, "standard-unit-rates", t0, t1)
                standing = fetch_rates(octo, code, "standing-charges", t0, t1)
                energy = 0.0
                miss = 0
                for t in times:
                    rate = units.at(t)
                    if rate is None:
                        miss += 1
                        rate = units.nearest(t) or 0
                    energy += readings[t] * rate
                st_total = 0.0
                for d in dates:
                    ds = local_day_start(d) + 3600
                    v = standing.at(ds)
                    st_total += v if v is not None else (standing.nearest(ds) or 0)
                comparisons.append({
                    "label": label, "name": full_name, "code": code,
                    "energy_p": round(energy, 2), "standing_p": round(st_total, 2),
                    "total_p": round(energy + st_total, 2),
                    "coverage": round(1 - miss / len(times), 4), "actual": False,
                })
            except ApiError as e:
                comparisons.append({"label": label, "name": _name(product), "error": str(e), "actual": False})

        return {
            "generated": int(now * 1000),
            "days": days,
            "data_from": t0 * 1000,
            "data_to": t1 * 1000,
            "region": cfg["region"].strip("_"),
            "tariff_history": history,
            "tariff_from_account": from_account,
            "kwh_total": round(kwh_total, 3),
            "missing_prices": missing,
            "notes": notes,
            "hh": hh,
            "standing_by_day": standing_by_day,
            "comparisons": comparisons,
            "host": octo.host,
        }

    # -- live -----------------------------------------------------------------

    def _device_ids(self):
        if self.cfg.get("device_id"):
            return [self.cfg["device_id"]]
        account = self.cfg.get("account")
        if not account:
            raise ApiError("Add your account number in settings to see live readings.", 400, "no_account")
        q = ("query Devices($account: String!) { account(accountNumber: $account) { "
             "electricityAgreements(active: true) { meterPoint { meters(includeInactive: true) { "
             "smartDevices { deviceId } } } } } }")
        d = self.octo.graphql(q, {"account": account})
        ids = []
        for ag in ((d.get("account") or {}).get("electricityAgreements") or []):
            for m in ((ag.get("meterPoint") or {}).get("meters") or []):
                for sd in (m.get("smartDevices") or []):
                    if sd.get("deviceId") and sd["deviceId"] not in ids:
                        ids.append(sd["deviceId"])
        if not ids:
            raise ApiError("Octopus didn't list a device for live readings on your account. Click Change "
                           "details and enter the device ID Octo-Aid shows under Settings > Home Mini "
                           "Device (it looks like 00-11-22-33-44-55-66-77).", 404, "no_device")
        return ids

    def _device_error(self, e):
        """Octopus answers 'Unauthorized' when the device isn't linked to this key's account."""
        if self.cfg.get("device_id") and "unauthori" in str(e).lower():
            return ApiError("Octopus won't give readings for Home Mini %s with this API key, so it probably "
                            "isn't the right device ID. Check it under Change details. %s"
                            % (self.cfg["device_id"], DEVICE_ID_HELP), 403, "device_refused")
        return e

    def _current_rate(self):
        now = int(time.time())
        slot = now - now % 1800
        if slot in self.current_rate_cache:
            return self.current_rate_cache[slot]
        agreements, _ = self.agreements()
        code = agreements[-1][0]
        for c, s, e in agreements:
            if s <= now < e:
                code = c
        table = fetch_rates(self.octo, code, "standard-unit-rates", slot - 1800, slot + 3600)
        rate = table.at(now)
        self.current_rate_cache = {slot: rate}
        return rate

    def live(self):
        if self.live_cache and time.time() - self.live_at < LIVE_MIN_GAP:
            return self.live_cache
        q = "query Live($device: String!) { smartMeterTelemetry(deviceId: $device) { readAt consumption demand } }"
        last_error = None
        reading = None
        for dev in self._device_ids():
            try:
                rows = (self.octo.graphql(q, {"device": dev}).get("smartMeterTelemetry")) or []
            except ApiError as e:
                if e.code == "rate_limited":
                    raise
                last_error = e
                continue
            if rows:
                reading = rows[-1]
                if not self.cfg.get("device_id"):
                    self.cfg["device_id"] = dev
                    save_config(self.cfg)
                break
        if reading is None:
            raise (self._device_error(last_error) if last_error else
                   ApiError("No live reading from your Home Mini right now.", 404, "no_reading"))
        demand = float(reading.get("demand") or 0)
        try:
            rate = self._current_rate()
        except ApiError:
            rate = None
        out = {
            "demand_w": demand,
            "read_at": reading.get("readAt"),
            "rate_p": rate,
            "cost_per_hour_p": round(demand / 1000.0 * rate, 2) if rate is not None else None,
            "fetched": int(time.time() * 1000),
        }
        self.live_cache, self.live_at = out, time.time()
        return out

    # -- today so far ------------------------------------------------------------

    def _tariff_now(self):
        now = time.time()
        agreements, _ = self.agreements()
        code = agreements[-1][0]
        for c, s, e in agreements:
            if s <= now < e:
                code = c
        return code

    def today(self):
        if self.today_cache and time.time() - self.today_at < TODAY_MIN_GAP:
            return self.today_cache
        now = int(time.time())
        day_start = local_day_start(local_date(now))
        q = ("query Today($device: String!, $start: DateTime, $end: DateTime) { "
             "smartMeterTelemetry(deviceId: $device, start: $start, end: $end, grouping: HALF_HOURLY) "
             "{ readAt consumption consumptionDelta } }")
        rows = None
        last_error = None
        for dev in self._device_ids():
            try:
                rows = (self.octo.graphql(q, {"device": dev, "start": iso_utc(day_start - 1800),
                                              "end": iso_utc(now)}).get("smartMeterTelemetry")) or []
            except ApiError as e:
                if e.code == "rate_limited":
                    raise
                last_error = e
                continue
            if rows:
                break
        if not rows:
            raise (self._device_error(last_error) if last_error else
                   ApiError("Your Home Mini hasn't sent any readings for today yet.", 404, "no_reading"))
        parsed = []
        for r in rows:
            if not r.get("readAt"):
                continue
            t = parse_ts(r["readAt"])
            t -= t % 1800
            cum, delta = r.get("consumption"), r.get("consumptionDelta")
            parsed.append((t, None if cum is None else float(cum), None if delta is None else float(delta)))
        parsed.sort()
        code = self._tariff_now()
        units = fetch_rates(self.octo, code, "standard-unit-rates", day_start, now + 1800)
        standing = fetch_rates(self.octo, code, "standing-charges", day_start, now + 1)
        st = standing.at(day_start + 3600) if standing else None
        if st is None and standing:
            st = standing.nearest(day_start + 3600)
        st = st or 0.0
        kwh, energy, unpriced = price_today(parsed, units, day_start)
        out = {
            "kwh": round(kwh, 3),
            "energy_p": round(energy, 2),
            "standing_p": round(st, 2),
            "total_p": round(energy + st, 2),
            "unpriced_kwh": round(unpriced, 3),
            "fetched": now * 1000,
        }
        self.today_cache, self.today_at = out, time.time()
        return out

    # -- CSV ----------------------------------------------------------------------

    def csv(self, days):
        data = self.analysis(days)
        lines = ["interval_start_local,kwh,unit_rate_p_per_kwh,cost_p"]
        for ms, kwh, p in data["hh"]:
            ts = datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
            cost = "" if p is None else "%.4f" % (kwh * p)
            lines.append("%s,%s,%s,%s" % (ts, kwh, "" if p is None else p, cost))
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

APP = None
SERVER = None

PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "AgileDashboard"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # keep the Terminal quiet; errors are reported separately

    # -- guards ------------------------------------------------------------

    def _host_ok(self):
        host = self.headers.get("Host", "")
        return host in ("127.0.0.1:%d" % APP.port, "localhost:%d" % APP.port)

    def _token_ok(self, query):
        tok = self.headers.get("X-Dash-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(tok, APP.session_token)

    # -- responses -----------------------------------------------------------

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj))

    def _error(self, e):
        if isinstance(e, ApiError):
            status = e.status if e.status in (400, 401, 403, 404, 429) else 502
            self._json(status, {"error": str(e), "code": e.code})
        else:
            sys.stderr.write("Unexpected error: %r\n" % (e,))
            self._json(500, {"error": "Something went wrong in the dashboard: %s" % e})

    # -- routes ----------------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "Forbidden", "text/plain")
        APP.last_seen = time.monotonic()
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/":
            html = PAGE.replace("__SESSION_TOKEN__", APP.session_token)
            return self._send(200, html, "text/html; charset=utf-8",
                              {"Content-Security-Policy": PAGE_CSP, "X-Frame-Options": "DENY"})
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not path.startswith("/api/"):
            return self._send(404, "Not found", "text/plain")
        if not self._token_ok(query):
            return self._json(403, {"error": "This dashboard tab is out of date. Reload the page."})

        try:
            if path == "/api/status":
                return self._json(200, APP.status())
            if path == "/api/ping":
                return self._json(200, {"ok": True})
            if not APP.configured():
                return self._json(400, {"error": "Set up your details first.", "code": "not_configured"})
            days = int((query.get("days") or ["30"])[0])
            if path == "/api/analysis":
                refresh = (query.get("refresh") or ["0"])[0] == "1"
                return self._json(200, APP.analysis(days, refresh))
            if path == "/api/live":
                return self._json(200, APP.live())
            if path == "/api/today":
                return self._json(200, APP.today())
            if path == "/api/export.csv":
                body = APP.csv(days)
                name = "agile-usage-%s-%dd.csv" % (datetime.now().strftime("%Y%m%d"), days)
                return self._send(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="%s"' % name})
            return self._json(404, {"error": "Unknown request."})
        except Exception as e:  # noqa: BLE001
            return self._error(e)

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "Forbidden", "text/plain")
        APP.last_seen = time.monotonic()
        parsed = urllib.parse.urlparse(self.path)
        if not self._token_ok({}):
            return self._json(403, {"error": "This dashboard tab is out of date. Reload the page."})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": "Expected JSON."})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10000)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if parsed.path == "/api/setup":
                return self._json(200, APP.setup(body))
            if parsed.path == "/api/forget":
                return self._json(200, APP.forget())
            if parsed.path == "/api/quit":
                self._json(200, {"ok": True})
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
                return None
            return self._json(404, {"error": "Unknown request."})
        except Exception as e:  # noqa: BLE001
            return self._error(e)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OctoDave</title>
<style>
/* Layout: a single working column of instrument panels, summary first, detail below. */
:root {
  --bg: #eef1f4; --surface: #ffffff; --surface-2: #f5f7f9; --ink: #15202b; --muted: #5a6777;
  --line: #d9dfe6; --accent: #2c5a94; --peak: #c96a12; --off: #23807d; --good: #1b7a3e; --bad: #b4441a;
  --font-ui: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif;
  --font-num: "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --font-mono: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0e1319; --surface: #161d26; --surface-2: #1c2530; --ink: #e6ecf2; --muted: #93a2b2;
    --line: #29343f; --accent: #7fa8e0; --peak: #f0a14e; --off: #4cb7b2; --good: #5cc287; --bad: #f07f52;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0e1319; --surface: #161d26; --surface-2: #1c2530; --ink: #e6ecf2; --muted: #93a2b2;
  --line: #29343f; --accent: #7fa8e0; --peak: #f0a14e; --off: #4cb7b2; --good: #5cc287; --bad: #f07f52;
  color-scheme: dark;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 var(--font-ui); }
.wrap { max-width: 1080px; margin: 0 auto; padding-inline: 20px; padding-block: 24px 48px; display: grid; gap: 20px; }
h1, h2, h3 { margin: 0; text-wrap: balance; }
h1 { font: 650 1.6rem/1.2 var(--font-num); letter-spacing: -0.01em; }
h2 { font: 620 1.08rem/1.3 var(--font-ui); }
p { margin: 0; }
.muted { color: var(--muted); }
.small { font-size: 0.85rem; }
.num { font-family: var(--font-num); font-variant-numeric: tabular-nums; }
.mono { font-family: var(--font-mono); font-size: 0.85em; }
header.top { display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: end; justify-content: space-between; }
header.top .sub { margin-top: 4px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--surface); }
.seg button { border: 0; background: transparent; color: var(--ink); font: inherit; padding: 6px 12px; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"] { background: var(--accent); color: var(--surface); }
button.btn { font: inherit; border: 1px solid var(--line); background: var(--surface); color: var(--ink);
  border-radius: 8px; padding: 6px 12px; cursor: pointer; }
button.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--surface); font-weight: 600; }
button.btn.danger { color: var(--bad); border-color: var(--bad); }
button:focus-visible, input:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
button:disabled { opacity: 0.6; cursor: default; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 20px; display: grid; gap: 14px; min-width: 0; }
.panel-head { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: baseline; justify-content: space-between; }
.notice { padding: 10px 12px; border-radius: 8px; background: var(--surface-2); border: 1px solid var(--line); font-size: 0.9rem; }
.notice.error { border-color: var(--bad); color: var(--bad); }

/* Live strip */
.live { display: grid; grid-template-columns: auto 1fr auto; gap: 8px 24px; align-items: center; }
.live .watts { font: 650 2.6rem/1 var(--font-num); font-variant-numeric: tabular-nums; }
.live .watts small { font-size: 1rem; color: var(--muted); font-weight: 500; margin-left: 4px; }
.live .facts { display: flex; flex-wrap: wrap; gap: 4px 20px; }
.live .facts b { font-family: var(--font-num); font-variant-numeric: tabular-nums; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; letter-spacing: 0.02em; }
.pill.peak { background: color-mix(in srgb, var(--peak) 18%, transparent); color: var(--peak); }
.pill.off { background: color-mix(in srgb, var(--off) 18%, transparent); color: var(--off); }
.pill.cheap { background: color-mix(in srgb, var(--good) 18%, transparent); color: var(--good); }
@media (max-width: 640px) { .live { grid-template-columns: 1fr; } }

/* Summary tiles */
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 820px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; display: grid; gap: 2px; min-width: 0; }
.tile .label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.tile .value { font: 650 1.6rem/1.2 var(--font-num); font-variant-numeric: tabular-nums; }
.tile.peak .value { color: var(--peak); }

/* Charts */
.chart { width: 100%; overflow-x: auto; }
.chart svg { display: block; width: 100%; height: auto; min-width: 640px; }
.chart text { fill: var(--muted); font: 11px var(--font-ui); }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 0.85rem; color: var(--muted); }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }

/* Shift simulator */
.sim { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 24px; align-items: center;
  padding: 14px 16px; border-radius: 10px; background: var(--surface-2); }
.sim input[type=range] { width: 100%; accent-color: var(--peak); }
.sim .result { font: 650 1.4rem/1.2 var(--font-num); font-variant-numeric: tabular-nums; color: var(--good); text-align: right; }
@media (max-width: 640px) { .sim { grid-template-columns: 1fr; } .sim .result { text-align: left; } }

table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { text-align: left; padding: 7px 10px 7px 0; border-bottom: 1px solid var(--line); }
th { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
td.r, th.r { text-align: right; font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }

/* Comparison */
.cmp { display: grid; gap: 12px; }
.cmp-row { display: grid; grid-template-columns: minmax(150px, 220px) minmax(0, 1fr) auto; gap: 6px 16px; align-items: center; }
.cmp-row .name b { display: block; }
.cmp-row .name span { display: block; font-size: 0.8rem; color: var(--muted); overflow-wrap: anywhere; }
.bar { height: 14px; border-radius: 4px; background: var(--muted); opacity: 0.55; }
.cmp-row.actual .bar { background: var(--accent); opacity: 1; }
.cmp-row .amt { text-align: right; font-family: var(--font-num); font-variant-numeric: tabular-nums; min-width: 8.5em; }
.cmp-row .amt small { display: block; font-size: 0.8rem; }
.delta.up { color: var(--bad); } .delta.down { color: var(--good); }
@media (max-width: 640px) { .cmp-row { grid-template-columns: 1fr auto; } .cmp-row .bar-cell { grid-column: 1 / -1; order: 3; } }

/* Setup */
form.setup { display: grid; gap: 14px; max-width: 560px; }
.field { display: grid; gap: 4px; }
.field label { font-weight: 600; font-size: 0.9rem; }
.field input { font: inherit; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--surface-2); color: var(--ink); }
.field .hint { font-size: 0.8rem; color: var(--muted); }
.row-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
footer.settings { display: flex; flex-wrap: wrap; gap: 8px 24px; align-items: center; justify-content: space-between; font-size: 0.85rem; color: var(--muted); }
.loading { color: var(--muted); font-style: italic; }
@media (prefers-reduced-motion: no-preference) { .bar { transition: width 0.3s ease; } }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>OctoDave</h1>
      <p class="sub muted small" id="subline">Your half-hourly Octopus data, analysed on this computer.</p>
    </div>
    <div class="controls" id="periodControls" hidden>
      <div class="seg" role="group" aria-label="Period">
        <button type="button" data-days="7" aria-pressed="false">7 days</button>
        <button type="button" data-days="30" aria-pressed="true">30 days</button>
        <button type="button" data-days="90" aria-pressed="false">90 days</button>
      </div>
      <button type="button" class="btn" id="refreshBtn">Refresh</button>
    </div>
  </header>

  <section class="panel" id="setupPanel" hidden>
    <div class="panel-head"><h2 id="setupTitle">Connect to Octopus</h2></div>
    <p class="muted small">Your API key is saved in the <span id="keyStoreName">macOS Keychain</span> and only sent to Octopus Energy. Find the key, MPAN and serial number on your Octopus account under Personal details &rsaquo; API access.</p>
    <form class="setup" id="setupForm" autocomplete="off" novalidate>
      <div class="field">
        <label for="f-key">API key</label>
        <input id="f-key" name="key" type="password" spellcheck="false" placeholder="sk_live_…">
        <span class="hint" id="keyHint"></span>
      </div>
      <div class="field">
        <label for="f-mpan">Electricity MPAN</label>
        <input id="f-mpan" name="mpan" inputmode="numeric" spellcheck="false" placeholder="13 digits">
      </div>
      <div class="field">
        <label for="f-serial">Meter serial number</label>
        <input id="f-serial" name="serial" spellcheck="false">
      </div>
      <div class="field">
        <label for="f-account">Account number <span class="muted">(recommended)</span></label>
        <input id="f-account" name="account" spellcheck="false" placeholder="A-1234ABCD">
        <span class="hint">Needed for live Home Mini readings, and to price your history at the exact Agile version you were on.</span>
      </div>
      <div class="field">
        <label for="f-device">Home Mini device ID <span class="muted">(optional)</span></label>
        <input id="f-device" name="device_id" spellcheck="false" placeholder="00-11-22-33-44-55-66-77">
        <span class="hint">Needed if live readings don't appear. Octo-Aid shows it under Settings &rsaquo; Home Mini Device: 8 pairs like 00-11-22-33-44-55-66-77. It isn't the Wi-Fi address your router lists.</span>
      </div>
      <div class="notice error" id="setupError" hidden></div>
      <div class="row-actions">
        <button type="submit" class="btn primary" id="setupSave">Check and save</button>
        <button type="button" class="btn" id="setupCancel" hidden>Cancel</button>
      </div>
    </form>
  </section>

  <div id="dash" hidden style="display:grid; gap:20px;">
    <section class="panel" aria-labelledby="liveTitle">
      <div class="panel-head">
        <h2 id="liveTitle">Right now</h2>
        <div class="controls">
          <span class="muted small" id="liveAge"></span>
          <button type="button" class="btn" id="liveToggle" aria-pressed="true">Pause live updates</button>
        </div>
      </div>
      <div class="live" id="liveBody">
        <div class="watts num" id="liveWatts">–<small>W</small></div>
        <div class="facts" id="liveFacts"><span class="loading">Waiting for your Home Mini…</span></div>
        <div id="liveBand"></div>
      </div>
      <p class="notice" id="liveNote" hidden></p>
    </section>

    <div class="tiles" id="tiles"></div>

    <section class="panel" aria-labelledby="peakTitle">
      <div class="panel-head">
        <h2 id="peakTitle">The 4–7pm peak</h2>
        <span class="muted small">Agile prices are highest between 16:00 and 19:00</span>
      </div>
      <div class="legend"><span><i style="background:var(--peak)"></i>4–7pm</span><span><i style="background:var(--off)"></i>Rest of the day</span><span>Daily energy cost, excluding standing charge</span></div>
      <div class="chart" id="dailyChart"></div>
      <div class="sim">
        <div>
          <label for="shift"><b>If you moved <span id="shiftPct" class="num">50%</span> of your 4–7pm use</b> to the cheapest half-hours of the same day</label>
          <input id="shift" type="range" min="0" max="100" step="5" value="50">
          <p class="muted small" id="shiftNote"></p>
        </div>
        <div class="result" id="shiftResult">–</div>
      </div>
      <div>
        <h3 class="small muted" style="margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">Most expensive evenings</h3>
        <div class="table-wrap"><table id="worstTable"></table></div>
      </div>
    </section>

    <section class="panel" aria-labelledby="heatTitle">
      <div class="panel-head">
        <h2 id="heatTitle">When you use electricity</h2>
        <span class="muted small">Each square is half an hour. Darker means more kWh. The bracket marks 4–7pm.</span>
      </div>
      <div class="chart" id="heatmap"></div>
    </section>

    <section class="panel" aria-labelledby="cmpTitle">
      <div class="panel-head">
        <h2 id="cmpTitle">What the same usage would cost on other tariffs</h2>
        <span class="muted small" id="cmpPeriod"></span>
      </div>
      <div class="cmp" id="cmp"></div>
      <p class="muted small">Includes standing charges. Leaves out tariffs that need an EV, heat pump, storage heaters, solar or a battery. Where Octopus hasn't published a price for part of the period, the nearest published price is used and the row says so.</p>
    </section>

    <div class="notice" id="dashNote" hidden></div>
    <div class="notice error" id="dashError" hidden></div>

    <footer class="settings">
      <span id="settingsLine"></span>
      <span class="row-actions">
        <button type="button" class="btn" id="csvBtn">Download CSV</button>
        <button type="button" class="btn" id="editBtn">Change details</button>
        <button type="button" class="btn danger" id="forgetBtn">Forget my details</button>
        <button type="button" class="btn" id="quitBtn">Quit</button>
      </span>
    </footer>
  </div>
</div>

<script>
"use strict";
const TOKEN = "__SESSION_TOKEN__";
const PEAK_START = 16, PEAK_END = 19, CHEAP_SLOTS = 6, LIVE_EVERY_MS = 60000;
const $ = (s) => document.querySelector(s);
const state = { days: 30, data: null, status: null, live: true, liveTimer: null, forgetArmed: false,
  today: null, todayAt: 0, todayErr: "" };
const TODAY_EVERY_MS = 5 * 60000;

async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign({ "X-Dash-Token": TOKEN }, opts.body ? { "Content-Type": "application/json" } : {});
  let r;
  try {
    r = await fetch(path, Object.assign({}, opts, { headers }));
  } catch (e) {
    const err = new Error("The dashboard has stopped. Open OctoDave again to restart it.");
    err.code = "stopped";
    throw err;
  }
  let body = null;
  try { body = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) {
    const err = new Error((body && body.error) || ("Request failed (" + r.status + ")"));
    err.status = r.status; err.code = body && body.code;
    throw err;
  }
  return body;
}

const pounds = (p) => (p < 0 ? "−£" : "£") + Math.abs(p / 100).toFixed(2);
const pence = (p) => (p == null ? "–" : p.toFixed(1) + "p");
const kwh = (k) => k.toFixed(k >= 100 ? 0 : 1) + " kWh";
const dayKey = (d) => d.toLocaleDateString("en-CA");
const dayLabel = (key) => new Date(key + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
const isPeak = (d) => d.getHours() >= PEAK_START && d.getHours() < PEAK_END;
const svgEl = (tag, attrs) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
};
function show(el, on) { el.hidden = !on; }
function setError(el, msg) { el.textContent = msg || ""; show(el, !!msg); }

// ---------- setup ----------
function openSetup(editing) {
  const s = state.status || {};
  $("#setupTitle").textContent = editing ? "Change your details" : "Connect to Octopus";
  $("#keyStoreName").textContent = s.key_store || "macOS Keychain";
  $("#f-key").value = "";
  $("#f-key").placeholder = editing && s.key_hint ? "Leave blank to keep " + s.key_hint : "sk_live_…";
  $("#keyHint").textContent = editing && s.key_hint ? "A key is saved. Enter a new one only if you've regenerated it." : "";
  $("#f-mpan").value = s.mpan || "";
  $("#f-serial").value = s.serial || "";
  $("#f-account").value = s.account || "";
  $("#f-device").value = s.device_id || "";
  show($("#setupCancel"), !!editing);
  setError($("#setupError"), "");
  show($("#setupPanel"), true);
  show($("#dash"), !editing ? false : true);
  $("#setupPanel").scrollIntoView({ block: "start" });
}

$("#setupForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#setupSave");
  btn.disabled = true; btn.textContent = "Checking with Octopus…";
  setError($("#setupError"), "");
  try {
    const body = {
      key: $("#f-key").value.trim(), mpan: $("#f-mpan").value, serial: $("#f-serial").value,
      account: $("#f-account").value, device_id: $("#f-device").value,
    };
    state.status = await api("/api/setup", { method: "POST", body: JSON.stringify(body) });
    $("#f-key").value = "";
    show($("#setupPanel"), false);
    startDashboard(true);
  } catch (e) {
    setError($("#setupError"), e.message);
  } finally {
    btn.disabled = false; btn.textContent = "Check and save";
  }
});
$("#setupCancel").addEventListener("click", () => show($("#setupPanel"), false));
$("#editBtn").addEventListener("click", () => openSetup(true));
$("#forgetBtn").addEventListener("click", async () => {
  const btn = $("#forgetBtn");
  if (!state.forgetArmed) {
    state.forgetArmed = true;
    btn.textContent = "Click again to delete the saved key and details";
    setTimeout(() => { state.forgetArmed = false; btn.textContent = "Forget my details"; }, 6000);
    return;
  }
  state.forgetArmed = false;
  try {
    state.status = await api("/api/forget", { method: "POST", body: "{}" });
    stopLive();
    show($("#dash"), false); show($("#periodControls"), false);
    openSetup(false);
  } catch (e) { setError($("#dashError"), e.message); }
});
$("#quitBtn").addEventListener("click", async () => {
  stopLive();
  if (state.heartbeat) clearInterval(state.heartbeat);
  try { await api("/api/quit", { method: "POST", body: "{}" }); } catch (e) { /* already stopped */ }
  const wrap = document.querySelector(".wrap");
  wrap.innerHTML = '<header class="top"><div><h1>OctoDave</h1><p class="sub muted small">Stopped. You can close this tab, and open OctoDave again whenever you want it.</p></div></header>';
});
$("#csvBtn").addEventListener("click", () => {
  window.location.href = "/api/export.csv?days=" + state.days + "&t=" + encodeURIComponent(TOKEN);
});

// ---------- period ----------
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  state.days = Number(b.dataset.days);
  document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  loadAnalysis(false);
}));
$("#refreshBtn").addEventListener("click", () => loadAnalysis(true));

// ---------- analysis ----------
async function loadAnalysis(refresh) {
  setError($("#dashError"), "");
  show($("#dashNote"), false);
  $("#tiles").innerHTML = '<div class="tile"><span class="loading">Fetching your half-hourly data from Octopus…</span></div>';
  $("#refreshBtn").disabled = true;
  try {
    state.data = await api("/api/analysis?days=" + state.days + (refresh ? "&refresh=1" : ""));
    renderAll();
  } catch (e) {
    $("#tiles").innerHTML = "";
    setError($("#dashError"), e.message);
  } finally {
    $("#refreshBtn").disabled = false;
  }
}

function prepare(data) {
  const rows = data.hh.map(([ms, k, p]) => { const d = new Date(ms); return { d, k, p, peak: isPeak(d), day: dayKey(d), slot: d.getHours() * 2 + (d.getMinutes() >= 30 ? 1 : 0) }; });
  const days = new Map();
  for (const r of rows) {
    if (!days.has(r.day)) days.set(r.day, { day: r.day, rows: [], kwh: 0, cost: 0, peakKwh: 0, peakCost: 0 });
    const g = days.get(r.day);
    g.rows.push(r); g.kwh += r.k;
    const c = r.p == null ? 0 : r.k * r.p;
    g.cost += c;
    if (r.peak) { g.peakKwh += r.k; g.peakCost += c; }
  }
  return { rows, days: [...days.values()] };
}

function renderAll() {
  const data = state.data;
  const prep = prepare(data);
  state.prep = prep;
  const totalKwh = prep.rows.reduce((a, r) => a + r.k, 0);
  const energy = prep.days.reduce((a, g) => a + g.cost, 0);
  const standing = prep.days.reduce((a, g) => a + (data.standing_by_day[g.day] || 0), 0);
  const peakCost = prep.days.reduce((a, g) => a + g.peakCost, 0);
  const peakKwh = prep.days.reduce((a, g) => a + g.peakKwh, 0);
  const from = new Date(data.data_from), to = new Date(data.data_to - 1);
  const fmtD = (d) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });

  $("#subline").textContent = "Half-hourly readings from " + fmtD(from) + " to " + fmtD(to) +
    " (" + prep.days.length + " days), region " + data.region + ". Readings usually arrive a day late.";

  $("#tiles").innerHTML = "";
  const tile = (label, value, sub, cls) => {
    const t = document.createElement("div"); t.className = "tile" + (cls ? " " + cls : "");
    t.innerHTML = '<span class="label"></span><span class="value"></span><span class="muted small"></span>';
    t.children[0].textContent = label; t.children[1].textContent = value; t.children[2].textContent = sub;
    $("#tiles").appendChild(t);
  };
  tile("Electricity used", kwh(totalKwh), (totalKwh / prep.days.length).toFixed(1) + " kWh a day");
  tile("Total cost", pounds(energy + standing), pounds(standing) + " of it standing charge");
  tile("Average price", pence(totalKwh ? energy / totalKwh : null) + "/kWh", "Energy only, before standing charge");
  tile("Spent 4–7pm", pounds(peakCost), Math.round(energy ? (peakCost / energy) * 100 : 0) + "% of energy cost for " + Math.round(totalKwh ? (peakKwh / totalKwh) * 100 : 0) + "% of use", "peak");

  renderDaily(prep);
  renderShift();
  renderWorst(prep);
  renderHeatmap(prep);
  renderComparison(data, prep);

  const s = state.status;
  $("#settingsLine").textContent = "Key " + (s.key_hint || "saved") + " in " + s.key_store + " · MPAN " + s.mpan +
    (s.account ? " · " + s.account : "") + (data.tariff_history.length ? " · " + data.tariff_history.map((h) => h.code).join(", ") : "");
  const notes = (data.notes || []).slice();
  if (data.missing_prices) notes.push(data.missing_prices + " half-hours had no published price and were left out of costs.");
  $("#dashNote").textContent = notes.join(" ");
  show($("#dashNote"), notes.length > 0);
}

function niceMax(v) {
  if (v <= 0) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pow >= v) return m * pow;
  return 10 * pow;
}

function renderDaily(prep) {
  const W = 960, H = 240, L = 46, R = 8, T = 10, B = 28;
  const n = prep.days.length;
  const step = niceMax(Math.max(...prep.days.map((g) => g.cost)) / 400) * 100;
  const max = step * 4;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Daily energy cost split into 4–7pm and the rest of the day" });
  const y = (p) => T + (H - T - B) * (1 - p / max);
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    svg.appendChild(svgEl("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--line)", "stroke-width": 1 }));
    const t = svgEl("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }); t.textContent = "£" + (v / 100).toFixed(Number.isInteger(step / 100) ? 0 : 2);
    svg.appendChild(t);
  }
  const slot = (W - L - R) / n, bw = Math.max(2, slot * 0.72);
  const labelEvery = Math.ceil(n / 12);
  prep.days.forEach((g, i) => {
    const x = L + i * slot + (slot - bw) / 2;
    const rest = g.cost - g.peakCost;
    const r1 = svgEl("rect", { x, width: bw, y: y(rest), height: Math.max(0, y(0) - y(rest)), fill: "var(--off)", rx: 1.5 });
    const r2 = svgEl("rect", { x, width: bw, y: y(g.cost), height: Math.max(0, y(rest) - y(g.cost)), fill: "var(--peak)", rx: 1.5 });
    const tip = svgEl("title", {}); tip.textContent = dayLabel(g.day) + ": " + pounds(g.cost) + " (" + pounds(g.peakCost) + " at 4–7pm)";
    r2.appendChild(tip); r1.appendChild(tip.cloneNode(true));
    svg.appendChild(r1); svg.appendChild(r2);
    if (i % labelEvery === 0) {
      const t = svgEl("text", { x: x + bw / 2, y: H - 8, "text-anchor": "middle" });
      t.textContent = new Date(g.day + "T12:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short" });
      svg.appendChild(t);
    }
  });
  $("#dailyChart").replaceChildren(svg);
}

function shiftSaving(prep, share) {
  let saved = 0, moved = 0;
  for (const g of prep.days) {
    if (g.peakKwh <= 0) continue;
    const priced = g.rows.filter((r) => !r.peak && r.p != null).map((r) => r.p).sort((a, b) => a - b);
    if (!priced.length) continue;
    const cheap = priced.slice(0, CHEAP_SLOTS);
    const cheapAvg = cheap.reduce((a, b) => a + b, 0) / cheap.length;
    const peakAvg = g.peakCost / g.peakKwh;
    const m = g.peakKwh * share;
    if (peakAvg > cheapAvg) { saved += m * (peakAvg - cheapAvg); moved += m; }
  }
  return { saved, moved };
}

function renderShift() {
  if (!state.prep) return;
  const share = Number($("#shift").value) / 100;
  $("#shiftPct").textContent = Math.round(share * 100) + "%";
  const { saved, moved } = shiftSaving(state.prep, share);
  const days = state.prep.days.length;
  $("#shiftResult").textContent = pounds(saved) + " saved";
  $("#shiftNote").textContent = "That's " + moved.toFixed(1) + " kWh moved over " + days + " days, about " +
    pounds((saved / days) * 30.4) + " a month. Uses each day's real Agile prices and its " + CHEAP_SLOTS + " cheapest half-hours outside 4–7pm.";
}
$("#shift").addEventListener("input", renderShift);

function renderWorst(prep) {
  const worst = [...prep.days].filter((g) => g.peakKwh > 0).sort((a, b) => b.peakCost - a.peakCost).slice(0, 5);
  const t = $("#worstTable");
  t.innerHTML = '<thead><tr><th>Day</th><th class="r">4–7pm use</th><th class="r">4–7pm cost</th><th class="r">Avg 4–7pm price</th><th class="r">Share of day\'s cost</th></tr></thead>';
  const tb = document.createElement("tbody");
  for (const g of worst) {
    const tr = document.createElement("tr");
    const cells = [dayLabel(g.day), kwh(g.peakKwh), pounds(g.peakCost), pence(g.peakCost / g.peakKwh), (g.cost ? Math.round((g.peakCost / g.cost) * 100) : 0) + "%"];
    cells.forEach((c, i) => { const td = document.createElement("td"); td.textContent = c; if (i) td.className = "r"; tr.appendChild(td); });
    tb.appendChild(tr);
  }
  t.appendChild(tb);
}

function renderHeatmap(prep) {
  const n = prep.days.length;
  const L = 74, T = 18, cw = 18, ch = Math.max(8, Math.min(16, Math.floor(520 / Math.max(n, 1))));
  const W = L + 48 * cw + 4, H = T + n * ch + 6;
  const max = Math.max(...prep.rows.map((r) => r.k), 0.001);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Half-hourly usage heatmap by day", style: "min-width:640px" });
  for (let h = 0; h < 24; h += 3) {
    const t = svgEl("text", { x: L + h * 2 * cw, y: 12 }); t.textContent = String(h).padStart(2, "0") + ":00"; svg.appendChild(t);
  }
  const labelEvery = Math.ceil(n / 30);
  prep.days.forEach((g, i) => {
    const yy = T + i * ch;
    if (i % labelEvery === 0) {
      const t = svgEl("text", { x: L - 6, y: yy + ch - 2, "text-anchor": "end" }); t.textContent = dayLabel(g.day); svg.appendChild(t);
    }
    for (const r of g.rows) {
      const rect = svgEl("rect", { x: L + r.slot * cw + 1, y: yy + 1, width: cw - 2, height: ch - 2, rx: 1.5,
        fill: "var(--accent)", "fill-opacity": (0.06 + 0.94 * Math.sqrt(r.k / max)).toFixed(3) });
      const tip = svgEl("title", {});
      tip.textContent = dayLabel(g.day) + " " + r.d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) + ": " + r.k.toFixed(2) + " kWh at " + pence(r.p);
      rect.appendChild(tip); svg.appendChild(rect);
    }
  });
  svg.appendChild(svgEl("rect", { x: L + PEAK_START * 2 * cw - 1, y: T - 2, width: (PEAK_END - PEAK_START) * 2 * cw + 2, height: n * ch + 4,
    fill: "none", stroke: "var(--peak)", "stroke-width": 2, rx: 3 }));
  $("#heatmap").replaceChildren(svg);
}

function renderComparison(data, prep) {
  const rows = data.comparisons;
  const actual = rows.find((r) => r.actual);
  const valid = rows.filter((r) => r.total_p != null);
  const max = Math.max(...valid.map((r) => r.total_p), 1);
  $("#cmpPeriod").textContent = "Over the same " + prep.days.length + " days, " + kwh(prep.rows.reduce((a, r) => a + r.k, 0));
  const box = $("#cmp"); box.replaceChildren();
  const sorted = [actual, ...valid.filter((r) => !r.actual).sort((a, b) => a.total_p - b.total_p), ...rows.filter((r) => r.error)];
  for (const r of sorted) {
    if (!r) continue;
    const row = document.createElement("div"); row.className = "cmp-row" + (r.actual ? " actual" : "");
    const name = document.createElement("div"); name.className = "name";
    const b = document.createElement("b"); b.textContent = r.label; name.appendChild(b);
    const sp = document.createElement("span"); sp.textContent = r.name || ""; name.appendChild(sp);
    const barCell = document.createElement("div"); barCell.className = "bar-cell";
    const amt = document.createElement("div"); amt.className = "amt";
    if (r.error) {
      barCell.innerHTML = '<span class="muted small"></span>'; barCell.firstChild.textContent = r.error;
      amt.textContent = "–";
    } else {
      const bar = document.createElement("div"); bar.className = "bar"; bar.style.width = ((r.total_p / max) * 100).toFixed(1) + "%";
      barCell.appendChild(bar);
      amt.textContent = pounds(r.total_p);
      const sm = document.createElement("small");
      if (r.actual) { sm.textContent = "what you paid"; sm.className = "muted"; }
      else {
        const d = r.total_p - actual.total_p;
        sm.textContent = (d > 0 ? pounds(d) + " more" : pounds(-d) + " less");
        sm.className = "delta " + (d > 0 ? "up" : "down");
      }
      amt.appendChild(sm);
      if (r.coverage < 0.99) {
        const n = document.createElement("span"); n.className = "muted small";
        n.textContent = "Prices published for " + Math.round(r.coverage * 100) + "% of the period; nearest price used for the rest.";
        barCell.appendChild(n);
      }
    }
    row.append(name, barCell, amt); box.appendChild(row);
  }
}

// ---------- live ----------
function stopLive() { if (state.liveTimer) clearInterval(state.liveTimer); state.liveTimer = null; }
function startLive() {
  stopLive();
  if (!state.live) return;
  pollLive();
  state.liveTimer = setInterval(pollLive, LIVE_EVERY_MS);
}
function liveMessage(msg) { $("#liveNote").textContent = msg || ""; show($("#liveNote"), !!msg); }

async function pollLive() {
  if (document.hidden || !state.live) return;
  if (state.status && !state.status.live_available) {
    $("#liveFacts").textContent = "";
    liveMessage("Add your account number under Change details to see live Home Mini readings here.");
    return;
  }
  try {
    const l = await api("/api/live");
    liveMessage("");
    $("#liveWatts").innerHTML = Math.round(l.demand_w).toLocaleString("en-GB") + "<small>W</small>";
    const facts = $("#liveFacts"); facts.replaceChildren();
    const fact = (label, value) => { const s = document.createElement("span"); s.innerHTML = '<span class="muted"></span> <b></b>'; s.children[0].textContent = label; s.children[1].textContent = value; facts.appendChild(s); };
    fact("Price now", l.rate_p == null ? "not published" : pence(l.rate_p) + "/kWh");
    fact("Costing", l.cost_per_hour_p == null ? "–" : pence(l.cost_per_hour_p) + " an hour");
    const band = $("#liveBand"); band.replaceChildren();
    const pill = document.createElement("span");
    const now = new Date();
    if (isPeak(now)) { pill.className = "pill peak"; pill.textContent = "4–7pm peak"; }
    else if (l.rate_p != null && l.rate_p < 15) { pill.className = "pill cheap"; pill.textContent = "Cheap slot"; }
    else { pill.className = "pill off"; pill.textContent = "Off-peak"; }
    band.appendChild(pill);
    renderToday();
    refreshToday();
    if (l.read_at) {
      const age = Math.max(0, Math.round((Date.now() - new Date(l.read_at).getTime()) / 1000));
      $("#liveAge").textContent = "Reading from " + (age < 90 ? age + "s" : Math.round(age / 60) + " min") + " ago";
    }
  } catch (e) {
    if (e.status === 429 || e.code === "rate_limited") {
      state.live = false; stopLive(); updateLiveToggle();
      liveMessage(e.message);
    } else {
      liveMessage(e.message);
    }
  }
}
// Today so far: Home Mini half-hours priced at that day's Agile rates, plus today's standing charge.
async function refreshToday(force) {
  if (!force && Date.now() - state.todayAt < TODAY_EVERY_MS) return;
  state.todayAt = Date.now();
  try {
    state.today = await api("/api/today");
    state.todayErr = "";
  } catch (e) {
    state.todayErr = e.message;
  }
  renderToday();
}
function renderToday() {
  const facts = $("#liveFacts");
  let s = $("#todayFact");
  if (!s) {
    s = document.createElement("span"); s.id = "todayFact";
    s.innerHTML = '<span class="muted">Today so far</span> <b></b> <span class="muted small"></span>';
  }
  if (s.parentNode !== facts) facts.appendChild(s);
  const t = state.today;
  if (t) {
    s.children[1].textContent = pounds(t.total_p);
    let sub = "(" + t.kwh.toFixed(1) + " kWh, incl. " + pence(t.standing_p) + " standing charge";
    if (t.unpriced_kwh > 0.05) sub += ", " + t.unpriced_kwh.toFixed(1) + " kWh not yet priced";
    s.children[2].textContent = sub + ")";
    s.title = "Energy " + pounds(t.energy_p) + " + standing charge " + pounds(t.standing_p) +
      ". Updated " + new Date(t.fetched).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) +
      (state.todayErr ? ". Latest update failed: " + state.todayErr : "");
  } else {
    s.children[1].textContent = state.todayErr ? "unavailable" : "…";
    s.children[2].textContent = "";
    s.title = state.todayErr || "Working out today's cost…";
  }
}
function updateLiveToggle() {
  const b = $("#liveToggle");
  b.setAttribute("aria-pressed", String(state.live));
  b.textContent = state.live ? "Pause live updates" : "Resume live updates";
}
$("#liveToggle").addEventListener("click", () => { state.live = !state.live; updateLiveToggle(); state.live ? startLive() : stopLive(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.live) pollLive(); });

// ---------- start ----------
function startDashboard(fresh) {
  show($("#dash"), true); show($("#periodControls"), true);
  updateLiveToggle();
  loadAnalysis(!!fresh);
  startLive();
}

(async function init() {
  try {
    state.status = await api("/api/status");
  } catch (e) {
    document.querySelector(".wrap").insertAdjacentHTML("beforeend", '<div class="notice error">Couldn\'t talk to the dashboard. Is it still running in Terminal?</div>');
    return;
  }
  // Tells the app a tab is still open, so it only stops itself once every tab is closed.
  state.heartbeat = setInterval(() => { api("/api/ping").catch(() => {}); }, 60000);
  if (state.status.configured) startDashboard(false);
  else openSetup(false);
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

IDLE_LIMIT = 10 * 60  # app mode: stop after this long with no dashboard tab open


def idle_watchdog(server, limit):
    # time.monotonic() pauses while the Mac sleeps, so sleep doesn't count as idle.
    while True:
        time.sleep(15)
        if time.monotonic() - APP.last_seen > limit:
            print("No dashboard tab open for %d minutes; stopping." % (limit // 60), flush=True)
            server.shutdown()
            return


def main():
    global APP, SERVER
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--app", action="store_true",
                    help="run as the Mac app: stop automatically once no dashboard tab is open")
    args = ap.parse_args()

    if sys.version_info < (3, 8):
        sys.exit("Python 3.8 or newer is needed.")

    APP = App(args.port)
    url = "http://127.0.0.1:%d/" % args.port
    try:
        SERVER = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        if args.app:
            webbrowser.open(url)  # most likely already running: just show it
            return
        sys.exit("Port %d is already in use. Is the dashboard already running? "
                 "Otherwise try --port 8899." % args.port)

    print("%s %s is running at %s" % (APP_NAME, VERSION, url), flush=True)
    print("API key storage: %s" % APP.keystore.where(), flush=True)
    if args.app:
        threading.Thread(target=idle_watchdog, args=(SERVER, IDLE_LIMIT), daemon=True).start()
    else:
        print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        SERVER.server_close()
        print("Stopped.", flush=True)


if __name__ == "__main__":
    main()

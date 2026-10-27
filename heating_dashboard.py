"""
Moby's Heat: a heating dashboard for an ESi Centro thermostat.

A small web page showing the room temperature, outside temperature and
mode, with buttons for boost, manual temperature, back to schedule and off.
It also keeps a history of temperatures, a log of changes, and can be
protected with a PIN.

Setup (once):
    pip install pyesicentro aiohttp

Run:
    python heating_dashboard.py
Then open http://localhost:8080 in a browser.

Settings file:
    Put heating_config.ini next to this script with your ESi login, and
    optionally a PIN, port and whether to share on the network. Then it
    starts without asking anything. Keep that file private: it holds your
    ESi password in plain text.

Options (these override the settings file):
    --share        make the page reachable from other devices on your network
                   (open http://<this computer's IP>:8080 on your phone)
    --pin 1234     require a PIN to use the page (strongly advised with --share)
    --port 8080    use a different port

With no settings file, your ESi login is asked for at startup, or read from
the environment variables ESI_EMAIL, ESI_PASSWORD and HEATING_PIN.

History and the activity log are kept in heating.db, next to this script.
"""

import argparse
import asyncio
import configparser
import getpass
import hashlib
import hmac
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web
from pyesicentro.service import Service

from smart_heating import Planner

# ---------- settings you might want to change ----------

# Where to get the outside temperature for, if heating_config.ini has no
# postcode under [location]. Set your postcode there rather than changing these.
LATITUDE = 52.5
LONGITUDE = -1.5

# How often to record a history point, in minutes
HISTORY_EVERY_MINUTES = 5

# How long to keep history, in days
KEEP_HISTORY_DAYS = 90

# ---------- end of settings ----------

DB_PATH = Path(__file__).with_name("heating.db")
CONFIG_PATH = Path(__file__).with_name("heating_config.ini")
PAGE_PATH = Path(__file__).with_name("heating_page.html")
STATIC_PATH = Path(__file__).with_name("static")

MODE_SCHEDULE = "0"
MODE_BOOST = "3"
MODE_OFF = "4"
MODE_MANUAL = "5"
MODE_NAMES = {
    MODE_SCHEDULE: "Schedule",
    MODE_BOOST: "Boost",
    MODE_OFF: "Off",
    MODE_MANUAL: "Manual",
}

RELOGIN_AFTER_SECONDS = 25 * 60   # ESi tokens last 30 minutes
WEATHER_CACHE_SECONDS = 15 * 60
MIN_TEMP = 5.0
MAX_TEMP = 30.0
COOKIE_NAME = "heating_auth"
MAX_PIN_TRIES = 5
PIN_LOCKOUT_SECONDS = 5 * 60


def to_tenths(celsius):
    """20.5 -> '205', the format ESi expects."""
    return str(int(round(float(celsius) * 10)))


def from_tenths(value):
    """'205' -> 20.5. Returns None if the value isn't a number."""
    try:
        return int(value) / 10
    except (TypeError, ValueError):
        return None


def fmt_temp(value):
    return f"{value:.1f}°" if value is not None else "?"


# ---------- storage ----------

class Store:
    """History, activity log and boost timings, kept in a SQLite file."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS history (
                ts INTEGER, device_id TEXT, room REAL, target REAL,
                mode TEXT, outside REAL);
            CREATE INDEX IF NOT EXISTS history_ts ON history (ts);
            CREATE TABLE IF NOT EXISTS activity (
                ts INTEGER, device_id TEXT, text TEXT, ok INTEGER);
            CREATE TABLE IF NOT EXISTS boosts (
                device_id TEXT PRIMARY KEY, started INTEGER, hours INTEGER, temp REAL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(history)")]
        if "calling" not in columns:
            self.db.execute("ALTER TABLE history ADD COLUMN calling TEXT")
        self.db.commit()

    def setting(self, key, value=None):
        """Read a saved setting, or save one when a value is given."""
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
            self.db.commit()
            return value
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def secret(self):
        """A random key, kept between restarts, used to sign the PIN cookie."""
        row = self.db.execute("SELECT value FROM settings WHERE key='secret'").fetchone()
        if row:
            return row[0]
        value = secrets.token_hex(32)
        self.db.execute("INSERT INTO settings VALUES ('secret', ?)", (value,))
        self.db.commit()
        return value

    def add_history(self, device, outside):
        self.db.execute(
            "INSERT INTO history (ts, device_id, room, target, mode, outside, calling) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), device["id"], device["room"], device["target"],
             device["mode"], outside, device.get("calling")))
        cutoff = int(time.time()) - KEEP_HISTORY_DAYS * 86400
        self.db.execute("DELETE FROM history WHERE ts < ?", (cutoff,))
        self.db.commit()

    def history(self, device_id, hours):
        since = int(time.time()) - hours * 3600
        rows = self.db.execute(
            "SELECT ts, room, target, mode, outside, calling FROM history "
            "WHERE device_id = ? AND ts >= ? ORDER BY ts", (device_id, since))
        return [dict(zip(("ts", "room", "target", "mode", "outside", "calling"), r)) for r in rows]

    def history_span_days(self, device_id):
        row = self.db.execute("SELECT MIN(ts), MAX(ts) FROM history WHERE device_id = ?",
                              (device_id,)).fetchone()
        return (row[1] - row[0]) / 86400 if row and row[0] else 0

    def log(self, device_id, text, ok=True):
        self.db.execute("INSERT INTO activity VALUES (?, ?, ?, ?)",
                        (int(time.time()), device_id, text, 1 if ok else 0))
        self.db.execute("DELETE FROM activity WHERE ts < ?", (int(time.time()) - 90 * 86400,))
        self.db.commit()

    def recent_activity(self, limit=12):
        rows = self.db.execute(
            "SELECT ts, text, ok FROM activity ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"ts": ts, "text": text, "ok": bool(ok)} for ts, text, ok in rows]

    def start_boost(self, device_id, hours, temp):
        self.db.execute("INSERT OR REPLACE INTO boosts VALUES (?, ?, ?, ?)",
                        (device_id, int(time.time()), hours, temp))
        self.db.commit()

    def boost_for(self, device_id):
        row = self.db.execute(
            "SELECT started, hours FROM boosts WHERE device_id = ?", (device_id,)).fetchone()
        return row

    def clear_boost(self, device_id):
        self.db.execute("DELETE FROM boosts WHERE device_id = ?", (device_id,))
        self.db.commit()


# ---------- talking to ESi and the weather ----------

class Heating:
    """Keeps one logged-in connection to ESi and re-logs in when needed."""

    def __init__(self, email, password, store):
        self.email = email
        self.password = password
        self.store = store
        self.service = None
        self.web = None
        self.logged_in_at = 0
        self.lock = asyncio.Lock()
        self.weather = (None, 0)   # (temperature, fetched at)
        self.latitude, self.longitude = LATITUDE, LONGITUDE
        self.postcode = ""

    async def find_location(self):
        """Turn the postcode into map coordinates (looked up once, then remembered)."""
        if not self.postcode:
            return
        key = "location:" + self.postcode.upper().replace(" ", "")
        saved = self.store.setting(key)
        if not saved:
            try:
                url = "https://api.postcodes.io/postcodes/" + self.postcode.replace(" ", "")
                async with self.web.get(url) as r:
                    data = await r.json(content_type=None)
                saved = self.store.setting(
                    key, f"{data['result']['latitude']},{data['result']['longitude']}")
            except Exception as err:
                print(f"Couldn't look up the postcode ({err}), using the default location")
                return
        lat, lon = saved.split(",")
        self.latitude, self.longitude = float(lat), float(lon)

    async def start(self):
        self.service = Service(self.email, self.password)
        self.web = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        await self.find_location()
        await self._login()

    async def stop(self):
        if self.service is not None:
            await self.service._session.close()
        if self.web is not None:
            await self.web.close()

    async def _login(self):
        await self.service.authorise()
        self.logged_in_at = time.monotonic()

    async def _run(self, action):
        """Run an ESi call, logging in again and retrying once if it fails."""
        async with self.lock:
            if time.monotonic() - self.logged_in_at > RELOGIN_AFTER_SECONDS:
                await self._login()
            try:
                return await action()
            except Exception:
                await self._login()
                return await action()

    async def outside_temp(self):
        """Current outside temperature from Open-Meteo, cached for 15 minutes."""
        temp, fetched = self.weather
        if time.monotonic() - fetched < WEATHER_CACHE_SECONDS:
            return temp
        try:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {"latitude": self.latitude, "longitude": self.longitude,
                      "current": "temperature_2m"}
            async with self.web.get(url, params=params) as r:
                data = await r.json(content_type=None)
            temp = round(float(data["current"]["temperature_2m"]), 1)
        except Exception:
            temp = self.weather[0]   # keep the last good reading
        self.weather = (temp, time.monotonic())
        return temp

    async def status(self):
        async def fetch():
            devices = await self.service.get_devices()
            if devices is False:
                raise RuntimeError("Not logged in")
            return devices

        devices = await self._run(fetch)
        result = []
        for device_id, stat in devices.items():
            mode = str(stat.work_mode)
            boost_ends = None
            if mode == MODE_BOOST:
                boost_ends = await self._esi_boost_end(device_id)
                if boost_ends is None:
                    boost = self.store.boost_for(device_id)
                    if boost:
                        boost_ends = boost[0] + boost[1] * 3600
            else:
                self.store.clear_boost(device_id)
            presets = str(stat.device_data.get("presetsTemp") or "").split("|")
            presets = [from_tenths(v) for v in presets] if len(presets) == 3 else [None] * 3
            result.append({
                "id": device_id,
                "name": stat.device_name,
                "online": bool(stat.online),
                "room": from_tenths(stat.inside_temparature),
                "target": from_tenths(stat.current_temperature),
                "mode": mode,
                "mode_name": MODE_NAMES.get(mode, f"Mode {mode}"),
                "boost_ends": boost_ends,
                "battery_low": bool(stat.device_data.get("batteryLow")),
                # ESi's th_work flag, recorded as-is: probably "1" while it's calling for heat
                "calling": str(stat.device_data.get("th_work", "")),
                "presets": {"home": presets[0], "sleep": presets[1], "away": presets[2]},
            })
        return result

    async def _esi_boost_end(self, device_id):
        """When the current boost ends, from ESi's own record (works for app boosts too)."""
        try:
            info = await self._run(lambda: self.service.get_device(device_id))
            started = datetime.strptime(info["boost_start"], "%Y/%m/%d %H:%M:%S")
            return int(started.timestamp() + float(info["boost_duration"]) * 3600)
        except Exception:
            return None

    async def set_presets(self, device_id, home, sleep, away):
        await self._run(lambda: self.service.update_preset_temps(
            device_id, homeTemp=to_tenths(home), sleepTemp=to_tenths(sleep),
            awayTemp=to_tenths(away)))

    async def boost(self, device_id, celsius, hours):
        await self._run(lambda: self.service.enable_boost_mode(
            device_id, to_tenths(celsius), str(hours)))
        self.store.start_boost(device_id, hours, celsius)

    async def manual(self, device_id, celsius):
        await self._run(lambda: self.service.update_current_temp(
            device_id, to_tenths(celsius)))

    async def set_mode(self, device_id, mode):
        # The library's disable_boost_mode() actually turns the heating OFF,
        # so "cancel boost" is done by going back to the schedule instead.
        await self._run(lambda: self.service.update_work_mode(device_id, mode))


async def record_history(app):
    """Background job: save a history point every few minutes."""
    heating = app[HEATING]
    while True:
        try:
            outside = await heating.outside_temp()
            for device in await heating.status():
                heating.store.add_history(device, outside)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            print(f"History: couldn't read the thermostat ({err})")
        await asyncio.sleep(HISTORY_EVERY_MINUTES * 60)


# Keys for the things the web app keeps hold of
HEATING = web.AppKey("heating", object)
STORE = web.AppKey("store", object)
PLANNER = web.AppKey("planner", object)
PIN = web.AppKey("pin", str)
SECRET = web.AppKey("secret", str)
PIN_LOCK = web.AppKey("pin_lock", dict)
JOBS = web.AppKey("jobs", dict)


# ---------- PIN ----------

def pin_token(app):
    """What the cookie should contain if the PIN was entered correctly."""
    return hmac.new(app[SECRET].encode(), app[PIN].encode(), hashlib.sha256).hexdigest()


@web.middleware
async def pin_check(request, handler):
    app = request.app
    if not app[PIN] or not request.path.startswith("/api/") or request.path == "/api/login":
        return await handler(request)
    cookie = request.cookies.get(COOKIE_NAME, "")
    if hmac.compare_digest(cookie, pin_token(app)):
        return await handler(request)
    return web.json_response({"ok": False, "error": "PIN needed", "need_pin": True}, status=401)


async def api_login(request):
    app = request.app
    if not app[PIN]:
        return web.json_response({"ok": True})

    lock = app[PIN_LOCK]
    locked_until = lock["until"]
    if time.time() < locked_until:
        wait = int(locked_until - time.time()) // 60 + 1
        return web.json_response(
            {"ok": False, "error": f"Too many wrong tries. Try again in {wait} min."}, status=429)

    body = await request.json()
    if hmac.compare_digest(str(body.get("pin", "")), app[PIN]):
        lock["fails"] = 0
        lock["lockouts"] = 0
        response = web.json_response({"ok": True})
        secure = request.headers.get("X-Forwarded-Proto") == "https" or request.secure
        response.set_cookie(COOKIE_NAME, pin_token(app), max_age=180 * 86400,
                            httponly=True, samesite="Strict", secure=secure)
        return response

    lock["fails"] += 1
    if lock["fails"] >= MAX_PIN_TRIES:
        lock["fails"] = 0
        lock["lockouts"] += 1
        # 5 min, then 10, 20, 40... up to a day, so guessing from outside is hopeless
        wait = min(24 * 3600, PIN_LOCKOUT_SECONDS * 2 ** (lock["lockouts"] - 1))
        lock["until"] = time.time() + wait
        who = request.headers.get("CF-Connecting-IP") or request.remote or "unknown"
        app[STORE].log("", f"Page locked for {wait // 60} min after wrong PINs (from {who})", ok=False)
    return web.json_response({"ok": False, "error": "Wrong PIN"}, status=401)


# ---------- web routes ----------

def check_temp(value):
    temp = float(value)
    if not MIN_TEMP <= temp <= MAX_TEMP:
        raise ValueError(f"Temperature must be between {MIN_TEMP:g} and {MAX_TEMP:g}")
    return temp


async def page(request):
    # Read fresh each time, so changes to the page show up without a restart
    return web.Response(text=PAGE_PATH.read_text(encoding="utf-8"), content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


async def full_status(app):
    heating = app[HEATING]
    status = {
        "ok": True,
        "devices": await heating.status(),
        "outside": await heating.outside_temp(),
        "activity": app[STORE].recent_activity(),
        "now": int(time.time()),
    }
    if status["devices"]:
        status["smart"] = app[PLANNER].summary(status["devices"][0]["id"])
    return status


async def api_status(request):
    try:
        return web.json_response(await full_status(request.app))
    except Exception as err:
        return web.json_response({"ok": False, "error": str(err)}, status=502)


async def api_history(request):
    device_id = request.query.get("device_id", "")
    hours = min(int(request.query.get("hours", "24")), 24 * KEEP_HISTORY_DAYS)
    return web.json_response({"ok": True, "points": request.app[STORE].history(device_id, hours)})


async def api_insights(request):
    """Everything the graphs need beyond the raw history."""
    app = request.app
    store, planner = app[STORE], app[PLANNER]
    device_id = request.query.get("device_id", "")
    points = store.history(device_id, 15 * 24)
    step_hours = HISTORY_EVERY_MINUTES / 60

    def heating(p):
        return (p["room"] is not None and p["target"] is not None
                and p["target"] - p["room"] >= 0.5)

    def r1(v):
        return round(v, 1) if v is not None else None

    by_day = {}
    for p in points:
        by_day.setdefault(datetime.fromtimestamp(p["ts"]).date(), []).append(p)

    today = datetime.now().date()
    days, heatmap = [], []
    for back in range(13, -1, -1):
        d = today.fromordinal(today.toordinal() - back)
        pts = by_day.get(d, [])
        rooms = [p["room"] for p in pts if p["room"] is not None]
        outs = [p["outside"] for p in pts if p["outside"] is not None]
        days.append({
            "date": d.isoformat(),
            "label": d.strftime("%a %d"),
            "hours_recorded": r1(len(pts) * step_hours),
            "heating_hours": r1(sum(1 for p in pts if heating(p)) * step_hours) if pts else None,
            "room_min": r1(min(rooms)) if rooms else None,
            "room_max": r1(max(rooms)) if rooms else None,
            "room_avg": r1(sum(rooms) / len(rooms)) if rooms else None,
            "outside_min": r1(min(outs)) if outs else None,
            "outside_max": r1(max(outs)) if outs else None,
        })
        hours = [[] for _ in range(24)]
        for p in pts:
            if p["room"] is not None:
                hours[datetime.fromtimestamp(p["ts"]).hour].append(p["room"])
        heatmap.append({"label": d.strftime("%a %d"),
                        "hours": [r1(sum(h) / len(h)) if h else None for h in hours]})

    model = planner.warmup_model(device_id)
    try:
        forecast = [{"ts": int(h["ts"]), "outside": h["outside"]}
                    for h in await planner.get_forecast() if h.get("ts")]
    except Exception:
        forecast = []
    outside = await app[HEATING].outside_temp()

    return web.json_response({
        "ok": True,
        "days": days,
        "heatmap": heatmap,
        "forecast": forecast,
        "warmup": {
            "samples": [{"outside": r1(o), "rate": round(r, 2)} for o, r in model.samples if o is not None],
            "line": [{"outside": t, "rate": round(model.rate(t), 2)} for t in range(-5, 21)],
            "learned": model.learned,
            "rate_now": round(model.rate(outside), 2),
        },
    })


async def api_action(request):
    app = request.app
    heating, store = app[HEATING], app[STORE]
    device_id, description = "", "Change"
    try:
        body = await request.json()
        device_id = body["device_id"]
        action = body["action"]

        if action == "boost":
            hours = int(body.get("hours", 1))
            if hours not in (1, 2, 3):
                raise ValueError("Boost can be 1, 2 or 3 hours")
            temp = check_temp(body["temp"])
            description = f"Boost to {fmt_temp(temp)} for {hours} hour{'s' if hours > 1 else ''}"
            await heating.boost(device_id, temp, hours)
        elif action == "manual":
            temp = check_temp(body["temp"])
            description = f"Hold at {fmt_temp(temp)}"
            await heating.manual(device_id, temp)
        elif action == "schedule":
            description = "Back to schedule"
            await heating.set_mode(device_id, MODE_SCHEDULE)
        elif action == "off":
            description = "Heating off"
            await heating.set_mode(device_id, MODE_OFF)
        elif action == "presets":
            home, sleep, away = (check_temp(body[k]) for k in ("home", "sleep", "away"))
            description = f"Presets set to home {fmt_temp(home)}, sleep {fmt_temp(sleep)}, away {fmt_temp(away)}"
            await heating.set_presets(device_id, home, sleep, away)
        else:
            raise ValueError(f"Unknown action: {action}")
    except (ValueError, KeyError) as err:
        return web.json_response({"ok": False, "error": str(err)}, status=400)
    except Exception:
        # The library raises a TypeError when ESi rejects a change
        store.log(device_id, f"{description} (failed)", ok=False)
        return web.json_response({"ok": False, "error": "ESi didn't accept that change"}, status=502)

    store.log(device_id, description)
    if action != "presets":
        app[PLANNER].pause_after_manual(action)
    await asyncio.sleep(2)   # give the thermostat a moment
    try:
        result = await full_status(app)
    except Exception as err:
        result = {"ok": True, "warning": str(err)}
    result["done"] = description
    return web.json_response(result)


async def api_smart(request):
    planner = request.app[PLANNER]
    try:
        body = await request.json()
        action = body.get("action")
        if action == "enable":
            planner.set_enabled(True)
        elif action == "disable":
            planner.set_enabled(False)
        elif action == "resume":
            planner.resume()
        elif action == "add":
            planner.add_period(body.get("days"), body.get("ready"), body.get("until"), body.get("temp"))
        elif action == "delete":
            planner.delete_period(body.get("id"))
        elif action == "add_rule":
            planner.add_rule(body.get("when"), body.get("outside"), body.get("rule_action"),
                             body.get("amount", 0))
        elif action == "delete_rule":
            planner.delete_rule(body.get("id"))
        elif action == "suggest":
            device_id = request.query.get("device_id", "")
            await planner.suggest_plan(device_id, body.get("extra", ""))
        elif action == "takeover":
            planner.set_takeover(bool(body.get("on")), body.get("background"))
        elif action == "use_suggestion":
            planner.use_suggestion()
        elif action == "dismiss_suggestion":
            planner.dismiss_suggestion()
        else:
            raise ValueError(f"Unknown action: {action}")
    except (ValueError, TypeError) as err:
        return web.json_response({"ok": False, "error": str(err)}, status=400)
    if action in ("enable", "disable", "resume", "add", "delete", "add_rule", "delete_rule",
                  "use_suggestion", "takeover"):
        asyncio.create_task(safe_tick(planner))   # react straight away
    return web.json_response({"ok": True, "smart": planner.summary(request.query.get("device_id"))})


async def safe_tick(planner):
    try:
        await planner.tick()
    except Exception as err:
        print(f"Smart heating: {err}")


def static_file(name, content_type=None):
    """A handler that serves one file from the static folder."""
    async def handler(request):
        headers = {"Content-Type": content_type} if content_type else None
        return web.FileResponse(STATIC_PATH / name, headers=headers)
    return handler


def build_app(heating, store, pin, ai_config=None):
    app = web.Application(middlewares=[pin_check])
    app[HEATING] = heating
    app[STORE] = store
    app[PIN] = pin or ""
    app[SECRET] = store.secret()
    # A plain dict, so the counts can change while the dashboard is running
    app[PIN_LOCK] = {"fails": 0, "lockouts": 0, "until": 0}
    app[JOBS] = {}
    app[PLANNER] = Planner(heating, store, ai_config or {})

    async def on_startup(app):
        await heating.start()
        app[JOBS]["history"] = asyncio.create_task(record_history(app))
        app[JOBS]["smart"] = asyncio.create_task(app[PLANNER].run_forever())

    async def on_cleanup(app):
        app[JOBS]["history"].cancel()
        app[JOBS]["smart"].cancel()
        await app[PLANNER].shutdown()
        await heating.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", page)
    app.router.add_static("/static/", STATIC_PATH)
    app.router.add_get("/manifest.webmanifest", static_file("manifest.webmanifest", "application/manifest+json"))
    app.router.add_get("/favicon.ico", static_file("favicon.ico"))
    app.router.add_get("/apple-touch-icon.png", static_file("apple-touch-icon.png"))
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/insights", api_insights)
    app.router.add_post("/api/action", api_action)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/smart", api_smart)
    return app


# ---------- your own web address through Cloudflare ----------

def find_cloudflared():
    found = shutil.which("cloudflared")
    if found:
        return found
    for guess in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                  r"C:\Program Files\cloudflared\cloudflared.exe",
                  os.path.expanduser(r"~\cloudflared.exe")):
        if os.path.exists(guess):
            return guess
    return None


def start_tunnel(hostname, tunnel, port):
    """Run cloudflared in the background and restart it if it stops."""
    exe = find_cloudflared()
    if not exe:
        print("Couldn't find cloudflared, so the web address isn't running. "
              "The dashboard still works at home.")
        return
    cmd = [exe, "tunnel", "--no-autoupdate", "run", "--url", f"http://localhost:{port}", tunnel]

    def run():
        announced = False
        while True:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    low = line.lower()
                    if "registered tunnel connection" in low and not announced:
                        print(f"Online at https://{hostname}")
                        announced = True
                    elif "origin cert" in low or ("tunnel" in low and "not found" in low):
                        print(f"Cloudflare says: {line.strip()}")
                        print(f"The '{tunnel}' tunnel isn't set up on this computer yet.")
                proc.wait()
            except Exception as err:
                print(f"Web address: {err}")
            print("The web address stopped, starting it again in 30 seconds")
            announced = False
            time.sleep(30)

    threading.Thread(target=run, daemon=True).start()


def load_config():
    """Read heating_config.ini if it exists. Missing values come back empty."""
    config = configparser.ConfigParser(interpolation=None)
    if CONFIG_PATH.exists():
        config.read(CONFIG_PATH, encoding="utf-8")
        print(f"Using settings from {CONFIG_PATH.name}")

    def get(section, key):
        return config.get(section, key, fallback="").strip()

    return {
        "email": get("esi", "email"),
        "password": get("esi", "password"),
        "pin": get("dashboard", "pin"),
        "share": get("dashboard", "share").lower() in ("yes", "true", "on", "1"),
        "port": get("dashboard", "port"),
        "postcode": get("location", "postcode"),
        "hostname": get("cloudflare", "hostname"),
        "tunnel": get("cloudflare", "tunnel") or "heating-dashboard",
        "ai": {key: get("ai", key) for key in (
            "order", "gemini_api_key", "gemini_model", "claude_api_key", "claude_model",
            "max_calls_per_day")},
    }


def main():
    config = load_config()

    parser = argparse.ArgumentParser(description="Moby's Heat: a dashboard for an ESi Centro thermostat")
    parser.add_argument("--share", action="store_true",
                        help="make the page reachable from other devices on your network")
    parser.add_argument("--pin", help="PIN needed to use the page")
    parser.add_argument("--port", type=int, help="port to use (default 8080)")
    args = parser.parse_args()

    # Command line first, then the settings file, then environment variables
    pin = args.pin or config["pin"] or os.environ.get("HEATING_PIN", "")
    share = args.share or config["share"]
    port = args.port or int(config["port"] or 8080)

    if pin and not pin.isdigit():
        parser.error("The PIN should be numbers only")
    if config["hostname"] and len(pin) < 6:
        sys.exit("To use your web address, set a PIN of at least 6 numbers in heating_config.ini "
                 "first. Anyone on the internet could reach the page, and it controls your heating.")

    email = config["email"] or os.environ.get("ESI_EMAIL") or input("ESi Centro email: ").strip()
    password = (config["password"] or os.environ.get("ESI_PASSWORD")
                or getpass.getpass("ESi Centro password: "))

    host = "0.0.0.0" if share else "127.0.0.1"
    print(f"\nMoby's Heat on http://localhost:{port}  (Ctrl+C to stop)")
    if share:
        print(f"Shared on your network: open http://<this computer's IP>:{port}")
        if not pin:
            print("Warning: no PIN set, so anyone on your network can change the heating.")
    if pin:
        print("PIN is switched on")
    store = Store(DB_PATH)
    ai = config["ai"]
    print("Smart heating thinks with: " + (
        ", ".join(p for p in ("gemini", "claude") if ai.get(f"{p}_api_key")) + ", then rules"
        if ai.get("gemini_api_key") or ai.get("claude_api_key") else "rules only (no AI keys set)"))
    heating = Heating(email, password, store)
    heating.postcode = config["postcode"]
    if config["hostname"]:
        print(f"Starting your web address, https://{config['hostname']}")
        start_tunnel(config["hostname"], config["tunnel"], port)
    web.run_app(build_app(heating, store, pin, ai),
                host=host, port=port, print=None)



if __name__ == "__main__":
    main()

"""
Smart warm-up for the heating dashboard.

You set the times you want the house warm ("20° by 06:30 until 08:30").
This works out when to start the heating so it's warm on time:

  1. The code learns how fast the house warms up from the dashboard's own
     history, and how that changes with the outside temperature.
  2. Gemini (or Claude if Gemini doesn't answer) looks at those facts plus
     the weather forecast and decides whether to start now or wait.
  3. If neither AI answers, plain rules decide instead.

The code sets the limits, the AI only chooses the timing within them:
it can never start more than 3 hours early, never heats above the
temperature you set, and if it keeps saying "wait" past the last sensible
moment, the rules start the heating anyway.

The heating is driven with 1-hour boosts that are topped up while needed.
If the Pi or internet stops, the boost runs out and the thermostat goes
back to its own ESi schedule within the hour.

If you change the heating by hand (on the dashboard or in the ESi app),
smart heating steps back until the end of that warm period.

Full control (optional): instead of handing back to the ESi schedule between
warm periods, the dashboard holds a low "background" temperature itself, so
the ESi app's schedule isn't needed at all. If the dashboard stops, the
thermostat simply stays at that background temperature. Closing the dashboard
properly (Ctrl+C) hands the heating back to the ESi schedule.
"""

import asyncio
import json
import re
import statistics
import time
from datetime import datetime, timedelta

MODE_SCHEDULE = "0"
MODE_BOOST = "3"
MODE_OFF = "4"
MODE_MANUAL = "5"
DEFAULT_BACKGROUND = 16.0
MIN_BACKGROUND, MAX_BACKGROUND = 10.0, 20.0

CHECK_EVERY_MINUTES = 5        # how often the planner looks at things
EARLIEST_START_HOURS = 3       # never start warming more than this early
ASK_AI_EVERY_MINUTES = 30      # how often to ask the AI during the start window
TOP_UP_WHEN_MINUTES_LEFT = 15  # renew our boost when it has this long left
DEFAULT_RATE = 1.0             # °C per hour, used until there's enough history
SAFETY_FACTOR = 1.15           # allow 15% extra time on top of the estimate
EXTRA_MINUTES = 10             # plus a few minutes for the boiler to get going
MAX_PLAN_TEMP = 24.0

HISTORY_MINUTES = 5            # matches the dashboard's history interval

DAY_SETS = {
    "every": ("Every day", (0, 1, 2, 3, 4, 5, 6)),
    "weekdays": ("Weekdays", (0, 1, 2, 3, 4)),
    "weekends": ("Weekends", (5, 6)),
}


def hhmm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def friendly_when(ts, now):
    """'06:30', 'tomorrow 06:30' or 'Mon 06:30'."""
    d, today = datetime.fromtimestamp(ts).date(), datetime.fromtimestamp(now).date()
    if d == today:
        return hhmm(ts)
    if d == today + timedelta(days=1):
        return f"tomorrow {hhmm(ts)}"
    return datetime.fromtimestamp(ts).strftime("%a %H:%M")


# ---------- learning how fast the house warms up ----------

def learn_warmup(points):
    """
    Look through history for stretches where the heating was calling
    (set temperature well above the room) and measure how fast the room
    rose. Returns a list of (outside temp, °C per hour).
    """
    samples = []
    run = []

    def finish(run):
        if len(run) < 2:
            return
        # Only the first two hours of a warm-up, while it's rising steadily
        start = run[0]
        run = [p for p in run if p["ts"] - start["ts"] <= 2 * 3600]
        end = run[-1]
        hours = (end["ts"] - start["ts"]) / 3600
        if hours < 0.5:
            return
        rise = end["room"] - start["room"]
        if rise < 0.2:
            return
        outs = [p["outside"] for p in run if p["outside"] is not None]
        outside = statistics.mean(outs) if outs else None
        samples.append((outside, rise / hours))

    for p in points:
        heating = (p["room"] is not None and p["target"] is not None
                   and p["target"] - p["room"] >= 0.7)
        if heating and (not run or p["ts"] - run[-1]["ts"] <= 15 * 60):
            run.append(p)
        else:
            finish(run)
            run = [p] if heating else []
    finish(run)
    return samples


class WarmupModel:
    """Turns the samples into 'how many °C per hour at this outside temperature'."""

    def __init__(self, samples):
        self.samples = [(o, r) for o, r in samples if 0.1 <= r <= 6]
        self.slope = 0.0
        self.base = DEFAULT_RATE
        with_outside = [(o, r) for o, r in self.samples if o is not None]

        if len(self.samples) >= 3:
            self.base = statistics.median(r for _, r in self.samples)
        if len(with_outside) >= 6:
            xs = [o for o, _ in with_outside]
            ys = [r for _, r in with_outside]
            if max(xs) - min(xs) >= 3:
                mx, my = statistics.mean(xs), statistics.mean(ys)
                var = sum((x - mx) ** 2 for x in xs)
                slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
                # Colder outside should never make it warm up faster
                self.slope = max(0.0, slope)
                self.base = my - self.slope * mx   # rate at 0° outside

    @property
    def learned(self):
        return len(self.samples) >= 3

    def rate(self, outside):
        if self.slope and outside is not None:
            r = self.base + self.slope * outside
        else:
            r = self.base
        return min(4.0, max(0.3, r))

    def describe(self, outside):
        if not self.learned:
            return (f"Still learning how fast the house warms up "
                    f"({len(self.samples)} of 3 warm-ups seen), assuming {DEFAULT_RATE:.1f}° an hour")
        text = f"Warms up about {self.rate(outside):.1f}° an hour"
        if outside is not None:
            text += f" at {outside:.0f}° outside"
        if self.slope:
            text += f", {self.rate(0):.1f}° when it's freezing"
        return text + f" (from {len(self.samples)} warm-ups)"


# ---------- asking the AI ----------

class AiAdvisor:
    """Gemini first, Claude if Gemini fails. Returns None if neither answers."""

    def __init__(self, config):
        self.config = config
        self.calls_today = 0
        self.calls_day = None

    @property
    def available(self):
        return bool(self.config.get("gemini_api_key") or self.config.get("claude_api_key"))

    def providers(self):
        order = [p.strip().lower() for p in (self.config.get("order") or "gemini, claude").split(",")]
        return [p for p in order if self.config.get(f"{p}_api_key")]

    def _under_cap(self):
        today = datetime.now().date()
        if self.calls_day != today:
            self.calls_day, self.calls_today = today, 0
        return self.calls_today < int(self.config.get("max_calls_per_day") or 40)

    async def ask(self, session, prompt, check, max_tokens=200, problems=None):
        """
        Ask each AI in turn until one gives an answer that check() can read.
        Returns (answer, who) or None.
        """
        problems = [] if problems is None else problems
        if not self._under_cap():
            print("Smart heating: daily AI limit reached, using the rules")
            problems.append("the daily limit of AI questions has been reached")
            return None
        if not self.providers():
            problems.append("no AI keys found in heating_config.ini")
        for provider in self.providers():
            try:
                self.calls_today += 1
                if provider == "gemini":
                    text = await self._gemini(session, prompt)
                elif provider == "claude":
                    text = await self._claude(session, prompt, max_tokens)
                else:
                    continue
                answer = check(text)
                if answer:
                    return answer, provider.capitalize()
                print(f"Smart heating: {provider} gave an answer I couldn't read: {text[:200]}")
                problems.append(f"{provider.capitalize()} replied but not in the shape needed")
            except Exception as err:
                print(f"Smart heating: {provider} didn't answer ({err})")
                problems.append(f"{provider.capitalize()} didn't answer ({friendly_error(err)})")
        return None

    async def decide(self, session, facts):
        """Start now or wait? Returns (start_now, reason, who) or None."""
        prompt = PROMPT.format(facts=json.dumps(facts, indent=2))
        result = await self.ask(session, prompt, parse_answer)
        if result:
            (start_now, reason), who = result
            return start_now, reason, who
        return None

    async def _gemini(self, session, prompt):
        model = self.config.get("gemini_model") or "gemini-3.5-flash-lite"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                 "maxOutputTokens": 4000},
        }
        headers = {"x-goog-api-key": self.config["gemini_api_key"]}
        async with session.post(url, json=body, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} {str(data)[:150]}")
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def _claude(self, session, prompt, max_tokens=200):
        model = self.config.get("claude_model") or "claude-haiku-5-5"
        headers = {
            "x-api-key": self.config["claude_api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        async with session.post("https://api.anthropic.com/v1/messages",
                                json=body, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} {str(data)[:150]}")
        return "".join(part.get("text", "") for part in data["content"])


def friendly_error(err):
    """Turn an API error into a few plain words."""
    text = str(err)
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return "too many requests, or the free allowance is used up for today"
    if "401" in text or "403" in text or "API_KEY" in text or "authentication" in text.lower():
        return "the key wasn't accepted"
    if "404" in text or "not_found" in text.lower():
        return "the model name wasn't recognised"
    if "credit" in text.lower() or "billing" in text.lower():
        return "out of credit"
    if "529" in text or "503" in text or "overloaded" in text.lower():
        return "it's busy, try again shortly"
    return err.__class__.__name__ if not text else text[:80]


def parse_answer(text):
    """Pull {"start_now": ..., "reason": ...} out of the AI's reply."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data.get("start_now"), bool):
        return None
    reason = str(data.get("reason", "")).strip().replace("\n", " ")[:200]
    return data["start_now"], reason


PROMPT = """You help a home heating system decide when to start warming the house.

The house needs to reach the target temperature by the "ready by" time. The
heating is either on (heating towards the target) or not yet started. Starting
too early wastes gas. Starting too late means a cold house. The system checks
again in {{check_again_minutes}} minutes if you say wait, and it will start
anyway at the "latest start" time, so you are choosing whether starting now
is better than waiting.

Things to weigh: how far the room is below the target, how fast it warmed up
before (and that it's slower when colder outside), whether it's getting colder
or warmer outside over the next few hours, and how the room temperature has
been moving in the last hour.

Facts (temperatures in °C, times are local UK time):
{facts}

Reply with JSON only, no other text:
{{"start_now": true or false, "reason": "one short plain-English sentence for the homeowner"}}
""".replace("{{check_again_minutes}}", str(ASK_AI_EVERY_MINUTES))


PLAN_PROMPT = """You are helping a homeowner in the UK set up smart heating.

For the last few days the dashboard has recorded their house while their existing
ESi thermostat schedule ran as normal: room temperature, the temperature the
thermostat was set to, the mode, and the outside temperature for their postcode.
From this, work out what their current schedule looks like and suggest a better
plan.

The smart system works like this:
- "Warm periods": the house should be at a temperature from a time until a time,
  on every day, weekdays or weekends. The system starts heating early by itself,
  using how fast the house warms up, so the "ready" time is when they want it warm,
  not when the heating should start.
- "Weather rules" based on the outside temperature at the ready time:
  "skip" means don't heat for that period at all (e.g. 17° or above outside),
  "adjust" means change the period's temperature by an amount (e.g. +0.5° when
  it's 3° or below outside, because the walls are colder and it feels chillier).
- Outside the warm periods, their own ESi schedule carries on as it does now.

Aim for comfort when they're up and about, and not wasting gas when they're not.
Keep it simple: a few warm periods and a few weather rules. Base it on what the
data actually shows, and say so if there isn't much data yet.

If the homeowner asks a question, answer it plainly in the summary (up to 8
sentences), and still fill in the plan. If you think their current plan is
already right, repeat it as the plan.

{extra}
The data (temperatures in °C, times are local UK time):
{facts}

Reply with JSON only, no other text, in exactly this shape:
{{
  "summary": "2 to 4 plain-English sentences on what you can see in their current setup, plus the answer to any question they asked",
  "periods": [
    {{"days": "weekdays" or "weekends" or "every", "ready": "HH:MM", "until": "HH:MM",
      "temp": number, "why": "short reason"}}
  ],
  "weather_rules": [
    {{"when": "at_or_above" or "at_or_below", "outside": number,
      "action": "skip" or "adjust", "amount": number (for adjust, between -2 and 2),
      "why": "short reason"}}
  ],
  "tips": ["up to 3 short, practical tips"]
}}
"""


def parse_plan(text):
    """Pull a plan out of the AI's reply, keeping only the sensible parts."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    def short(value, length=160):
        return str(value or "").strip().replace("\n", " ")[:length]

    periods = []
    for p in data.get("periods") or []:
        try:
            days = str(p.get("days", "")).lower().replace(" ", "")
            days = {"everyday": "every", "daily": "every", "all": "every"}.get(days, days)
            ready, until = str(p["ready"]).strip()[:5], str(p["until"]).strip()[:5]
            temp = round(float(p["temp"]) * 2) / 2
            if (days in DAY_SETS and ready != until and 10 <= temp <= MAX_PLAN_TEMP
                    and all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t) for t in (ready, until))):
                periods.append({"days": days, "ready": ready, "until": until,
                                "temp": temp, "why": short(p.get("why"))})
        except (TypeError, ValueError, KeyError, AttributeError):
            continue

    rules = []
    for r in data.get("weather_rules") or []:
        try:
            rule = clean_rule(r.get("when"), r.get("outside"), r.get("action"),
                              r.get("amount", 0), short(r.get("why")))
            rules.append(rule)
        except (TypeError, ValueError, AttributeError):
            continue

    summary = short(data.get("summary"), 1500)
    if not periods and not rules and not summary:
        return None
    return {
        "summary": summary,
        "periods": periods[:8],
        "weather_rules": rules[:6],
        "tips": [short(t, 200) for t in (data.get("tips") or [])][:3],
    }


def clean_rule(when, outside, action, amount=0, why=""):
    """Check a weather rule makes sense, or raise ValueError."""
    when = str(when or "").lower()
    action = str(action or "").lower()
    if when not in ("at_or_above", "at_or_below"):
        raise ValueError("Pick 'at or above' or 'at or below'")
    if action not in ("skip", "adjust"):
        raise ValueError("Pick skip or adjust")
    outside = round(float(outside) * 2) / 2
    if not -15 <= outside <= 30:
        raise ValueError("Outside temperature should be between -15 and 30")
    amount = round(float(amount or 0) * 2) / 2 if action == "adjust" else 0
    if action == "adjust" and (amount == 0 or abs(amount) > 2):
        raise ValueError("The change should be between -2 and +2 degrees, and not 0")
    return {"when": when, "outside": outside, "action": action, "amount": amount, "why": why}


def describe_rule(r):
    side = "or above" if r["when"] == "at_or_above" else "or below"
    if r["action"] == "skip":
        return f"{r['outside']:g}° {side} outside: no warm-up"
    return f"{r['outside']:g}° {side} outside: {r['amount']:+g}°"


def apply_rules(rules, temp, outside):
    """Returns (temperature to use, or None to skip, and the rules that applied)."""
    if outside is None:
        return temp, []
    used = []
    for r in rules:
        hit = outside >= r["outside"] if r["when"] == "at_or_above" else outside <= r["outside"]
        if not hit:
            continue
        used.append(r)
        if r["action"] == "skip":
            return None, [r]
        temp += r["amount"]
    return min(MAX_PLAN_TEMP, max(10.0, temp)), used


def build_digest(points, model, periods, rules):
    """Boil days of 5-minute readings down to something an AI can read in one go."""
    days = {}
    for p in points:
        days.setdefault(datetime.fromtimestamp(p["ts"]).date(), []).append(p)

    def avg(values):
        values = [v for v in values if v is not None]
        return round(statistics.mean(values), 1) if values else None

    daily = []
    for d, pts in sorted(days.items()):
        changes, last = [], None
        for p in pts:
            state = (p["mode"], p["target"])
            if state != last:
                name = {"0": "schedule", "3": "boost", "4": "off", "5": "hold"}.get(p["mode"], p["mode"])
                changes.append(f"{hhmm(p['ts'])} {name} {p['target']}")
                last = state
        calling = [p for p in pts if p["room"] is not None and p["target"] is not None
                   and p["target"] - p["room"] >= 0.5]
        rooms = [p["room"] for p in pts if p["room"] is not None]
        outs = [p["outside"] for p in pts if p["outside"] is not None]
        daily.append({
            "date": d.strftime("%a %d %b"),
            "hours_recorded": round(len(pts) * HISTORY_MINUTES / 60, 1),
            "thermostat_changes": changes[:24],
            "room_min_avg_max": [min(rooms), avg(rooms), max(rooms)] if rooms else None,
            "outside_min_avg_max": [min(outs), avg(outs), max(outs)] if outs else None,
            "approx_hours_heating": round(len(calling) * HISTORY_MINUTES / 60, 1),
        })

    by_hour = {}
    for p in points:
        by_hour.setdefault(datetime.fromtimestamp(p["ts"]).hour, []).append(p)
    hourly = {f"{h:02d}:00": {"room": avg(q["room"] for q in v),
                              "set_to": avg(q["target"] for q in v),
                              "outside": avg(q["outside"] for q in v)}
              for h, v in sorted(by_hour.items())}

    return {
        "days_of_data": round((points[-1]["ts"] - points[0]["ts"]) / 86400, 1) if points else 0,
        "each_day": daily,
        "average_by_hour_of_day": hourly,
        "warm_up_speed": {
            "warm_ups_seen": len(model.samples),
            "degrees_per_hour_at_0C_outside": round(model.rate(0), 2),
            "degrees_per_hour_at_10C_outside": round(model.rate(10), 2),
        },
        "current_smart_plan": [{k: p[k] for k in ("days", "ready", "until", "temp")} for p in periods],
        "current_weather_rules": [describe_rule(r) for r in rules],
    }


# ---------- the planner ----------

class Planner:
    def __init__(self, heating, store, ai_config):
        self.heating = heating
        self.store = store
        self.ai = AiAdvisor(ai_config)
        self.forecast = ([], 0)    # (hourly forecast, fetched at)
        self.model = WarmupModel([])
        self.model_built = 0
        self.last_outside = None
        db = store.db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT, days TEXT,
                ready TEXT, until TEXT, temp REAL);
            CREATE TABLE IF NOT EXISTS smart (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS weather_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cond TEXT, outside REAL,
                action TEXT, amount REAL, why TEXT);
        """)
        db.commit()

    # ----- small helpers for saved state -----
    def _get(self, key, default=None):
        row = self.store.db.execute("SELECT value FROM smart WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self.store.db.execute("INSERT OR REPLACE INTO smart VALUES (?, ?)", (key, json.dumps(value)))
        self.store.db.commit()

    def _note(self, text, who=None):
        self._set("note", {"ts": int(time.time()), "text": text, "who": who})

    # ----- the plan -----
    def periods(self):
        rows = self.store.db.execute("SELECT id, days, ready, until, temp FROM plan ORDER BY ready")
        return [dict(zip(("id", "days", "ready", "until", "temp"), r)) for r in rows]

    def add_period(self, days, ready, until, temp):
        if days not in DAY_SETS:
            raise ValueError("Pick every day, weekdays or weekends")
        for t in (ready, until):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t or ""):
                raise ValueError("Times should look like 06:30")
        if ready == until:
            raise ValueError("The end time should be after the start time")
        temp = float(temp)
        if not 10 <= temp <= MAX_PLAN_TEMP:
            raise ValueError(f"Temperature should be between 10 and {MAX_PLAN_TEMP:g}")
        self.store.db.execute("INSERT INTO plan (days, ready, until, temp) VALUES (?, ?, ?, ?)",
                              (days, ready, until, temp))
        self.store.db.commit()

    def delete_period(self, period_id):
        self.store.db.execute("DELETE FROM plan WHERE id = ?", (int(period_id),))
        self.store.db.commit()

    def rules(self):
        rows = self.store.db.execute(
            "SELECT id, cond, outside, action, amount, why FROM weather_rules ORDER BY action DESC, outside")
        return [dict(zip(("id", "when", "outside", "action", "amount", "why"), r)) for r in rows]

    def add_rule(self, when, outside, action, amount=0, why=""):
        r = clean_rule(when, outside, action, amount, why)
        self.store.db.execute(
            "INSERT INTO weather_rules (cond, outside, action, amount, why) VALUES (?, ?, ?, ?, ?)",
            (r["when"], r["outside"], r["action"], r["amount"], r["why"]))
        self.store.db.commit()

    def delete_rule(self, rule_id):
        self.store.db.execute("DELETE FROM weather_rules WHERE id = ?", (int(rule_id),))
        self.store.db.commit()

    async def suggest_plan(self, device_id, extra=""):
        """Ask the AI to look at the recorded days and suggest a plan."""
        if not self.ai.available:
            raise ValueError("Add a Gemini or Claude key to heating_config.ini to get suggestions")
        points = self.store.history(device_id, 14 * 24)
        if len(points) < 12 * 6:
            raise ValueError("There isn't enough recorded yet. Leave the dashboard running for a day or two first.")
        digest = build_digest(points, self.warmup_model(device_id), self.periods(), self.rules())
        extra = str(extra or "").strip()[:500]
        prompt = PLAN_PROMPT.format(
            facts=json.dumps(digest, indent=1),
            extra=f"What the homeowner says: {extra}\n" if extra else "")
        problems = []
        result = await self.ai.ask(self.heating.web, prompt, parse_plan, max_tokens=3000, problems=problems)
        if not result:
            print(f"Smart heating: plan request failed: {problems}")
            raise ValueError("No plan this time: " + ("; ".join(problems) or "no reason given") + ".")
        plan, who = result
        plan.update({"who": who, "ts": int(time.time()), "days_of_data": digest["days_of_data"]})
        self._set("suggestion", plan)
        return plan

    def use_suggestion(self):
        plan = self._get("suggestion")
        if not plan:
            raise ValueError("There's no suggestion to use")
        if not plan["periods"] and not plan["weather_rules"]:
            raise ValueError("That answer didn't include a plan, so there's nothing to use")
        db = self.store.db
        db.execute("DELETE FROM plan")
        db.execute("DELETE FROM weather_rules")
        db.commit()
        for p in plan["periods"]:
            self.add_period(p["days"], p["ready"], p["until"], p["temp"])
        for r in plan["weather_rules"]:
            self.add_rule(r["when"], r["outside"], r["action"], r["amount"], r.get("why", ""))
        self._set("suggestion", None)
        self.store.log("", f"Smart: using the plan suggested by {plan.get('who', 'the AI')}")

    def dismiss_suggestion(self):
        self._set("suggestion", None)

    def occurrences(self, now):
        """Upcoming or current warm periods as (ready_ts, until_ts, temp), soonest first."""
        today = datetime.fromtimestamp(now).date()
        found = []
        for p in self.periods():
            days = DAY_SETS[p["days"]][1]
            for offset in (-1, 0, 1, 2):
                d = today + timedelta(days=offset)
                if d.weekday() not in days:
                    continue
                ready = datetime.combine(d, datetime.strptime(p["ready"], "%H:%M").time())
                until = datetime.combine(d, datetime.strptime(p["until"], "%H:%M").time())
                if until <= ready:
                    until += timedelta(days=1)   # runs past midnight
                if until.timestamp() > now:
                    found.append((ready.timestamp(), until.timestamp(), p["temp"]))
        return sorted(found)

    # ----- on / off / pause -----
    @property
    def enabled(self):
        return bool(self._get("enabled", False))

    def set_enabled(self, on):
        self._set("enabled", bool(on))
        self._set("paused_until", 0)
        if not on:
            self._set("our_boost", None)
        self._note("Smart heating switched on" if on else "Smart heating is off")

    @property
    def takeover(self):
        return bool(self._get("takeover", False))

    @property
    def background(self):
        return float(self._get("background", DEFAULT_BACKGROUND))

    def set_takeover(self, on, background=None):
        if background is not None and str(background).strip() != "":
            bg = round(float(background) * 2) / 2
            if not MIN_BACKGROUND <= bg <= MAX_BACKGROUND:
                raise ValueError(f"Background should be between {MIN_BACKGROUND:g} and {MAX_BACKGROUND:g}")
            self._set("background", bg)
        self._set("takeover", bool(on))
        self._note(f"Full control on: {self.background:.1f}° between warm periods" if on
                   else "Full control off: your ESi schedule runs between warm periods")

    def pause_after_manual(self, action=None):
        """You changed it by hand, so step back until the current/next warm period ends."""
        if not self.enabled:
            return
        if action == "schedule" and not self._get("our_boost") and not self.takeover:
            return   # back to schedule when we weren't doing anything: nothing to step back from
        now = time.time()
        upcoming = self.occurrences(now)
        until = upcoming[0][1] if upcoming and upcoming[0][0] - EARLIEST_START_HOURS * 3600 <= now \
            else now + 3 * 3600
        self._set("paused_until", until)
        self._set("our_boost", None)
        self._set("our_hold", None)
        self._note(f"Stepped back until {friendly_when(until, now)} because the heating was changed by hand")

    def resume(self):
        self._set("paused_until", 0)
        self._note("Smart heating resumed")

    # ----- facts -----
    async def get_forecast(self):
        hours, fetched = self.forecast
        if time.time() - fetched < 30 * 60:
            return hours
        try:
            params = {"latitude": self.heating.latitude, "longitude": self.heating.longitude,
                      "hourly": "temperature_2m", "forecast_hours": 24, "timezone": "auto"}
            async with self.heating.web.get("https://api.open-meteo.com/v1/forecast",
                                            params=params) as r:
                data = await r.json(content_type=None)
            hours = [{"time": t[-5:], "ts": datetime.fromisoformat(t).timestamp(), "outside": v}
                     for t, v in zip(data["hourly"]["time"], data["hourly"]["temperature_2m"])]
        except Exception:
            pass
        self.forecast = (hours, time.time())
        return hours

    async def outside_at(self, when, now):
        """Forecast outside temperature for a time, or the current one if it's now/unknown."""
        if when <= now + 1800:
            return self.last_outside
        for h in await self.get_forecast():
            if h.get("ts") is not None and abs(h["ts"] - when) <= 1800:
                return h["outside"]
        return self.last_outside

    def warmup_model(self, device_id):
        if time.time() - self.model_built > 3600:
            points = self.store.history(device_id, 30 * 24)
            self.model = WarmupModel(learn_warmup(points))
            self.model_built = time.time()
        return self.model

    def room_trend(self, device_id):
        points = [p for p in self.store.history(device_id, 1) if p["room"] is not None]
        if len(points) < 2:
            return None
        return round(points[-1]["room"] - points[0]["room"], 1)

    # ----- the main job -----
    async def run_forever(self):
        await asyncio.sleep(20)   # let the dashboard start up first
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                print(f"Smart heating: {err}")
            await asyncio.sleep(CHECK_EVERY_MINUTES * 60)

    async def tick(self, now=None):
        hold = self._get("our_hold")       # the background temperature we're holding, with full control
        if not self.enabled and hold is None:
            return
        now = now or time.time()
        devices = await self.heating.status()
        if not devices:
            return
        dev = devices[0]
        device_id, mode, room = dev["id"], dev["mode"], dev["room"]
        ours = self._get("our_boost")      # {"ends": ts, "temp": t} when we're boosting
        holding = hold is not None and self._is_our_hold(dev, hold)
        if hold is None and self.enabled and self.takeover and self._is_our_hold(dev, self.background):
            # Already sitting at the background temperature (e.g. after a restart): that's ours
            hold, holding = self.background, True
            self._set("our_hold", hold)

        # Full control switched off (or smart heating off): give the heating back to the ESi schedule
        if hold is not None and not (self.enabled and self.takeover):
            if holding or mode == MODE_BOOST:
                await self.heating.set_mode(device_id, MODE_SCHEDULE)
                self.store.log(device_id, "Smart: full control off, back to the ESi schedule")
            self._set("our_hold", None)
            if not self.enabled:
                return
            hold, holding = None, False

        outside = await self.heating.outside_temp()
        self.last_outside = outside

        if self._get("paused_until", 0) > now:
            return

        # Has someone set the heating by hand (Off, Hold, or a boost we didn't start)?
        ok_modes = (MODE_SCHEDULE, MODE_BOOST)
        if mode not in ok_modes and not holding or (mode == MODE_BOOST and not ours):
            if self.takeover and mode != MODE_OFF:
                # With full control, step back for a while, then take over again
                self.pause_after_manual()
                return
            if ours:
                self._set("our_boost", None)
            self._note(f"Leaving it alone: the heating is on {dev['mode_name']}, set by hand")
            return
        if ours and mode != MODE_BOOST:
            # Our boost ended or was cancelled in the app
            ours = None
            self._set("our_boost", None)

        # Full control: between warm periods, hold the background temperature
        if self.takeover and not ours and not holding:
            await self._hold_background(device_id)

        upcoming = self.occurrences(now)
        if not upcoming:
            if ours:
                await self._hand_back(device_id, "No warm periods in the plan")
            else:
                self._note("Nothing in the plan yet. Add a warm period below.")
            return

        ready, until, temp = upcoming[0]

        # Weather rules, judged on the outside temperature at the warm-by time
        if self.rules():
            judged_on = await self.outside_at(max(ready, now), now)
            temp, used = apply_rules(self.rules(), temp, judged_on)
            if temp is None:
                why = describe_rule(used[0])
                if ours:
                    await self._hand_back(device_id, f"Skipping {friendly_when(ready, now)} ({why})")
                else:
                    self._note(f"Skipping {friendly_when(ready, now)}: {judged_on:.1f}° outside ({why})")
                return

        # Our boost is still on but we're outside any warm period (e.g. the Pi was off): stop it
        if ours and not (ready - EARLIEST_START_HOURS * 3600 <= now < until):
            await self._hand_back(device_id, "Warm period over")
            return

        # The ESi schedule is already asking for this much or more: nothing to add
        if (not self.takeover and not ours and mode == MODE_SCHEDULE and dev["target"] is not None
                and dev["target"] >= temp):
            if ready - EARLIEST_START_HOURS * 3600 <= now < until:
                self._note(f"Your ESi schedule already has it at {dev['target']:.1f}°, "
                           f"so nothing to add for {friendly_when(ready, now)}")
                return

        # In the warm period: keep it at the temperature
        if ready <= now < until:
            if not ours:
                await self._boost(device_id, temp, now,
                                  f"Warm period until {hhmm(until)}: holding {temp:.1f}°")
                self._note(f"Holding {temp:.1f}° until {hhmm(until)}")
            else:
                await self._top_up(device_id, ours, temp, now)
                self._note(f"Holding {temp:.1f}° until {hhmm(until)}")
            if until - now < CHECK_EVERY_MINUTES * 60:
                await self._hand_back(device_id, f"{hhmm(until)}: warm period over")
            return

        # Before the warm period
        if ours:
            await self._top_up(device_id, ours, temp, now)
            self._note(f"Warming up to {temp:.1f}° for {friendly_when(ready, now)}")
            return

        if room is None:
            self._note("Can't read the room temperature, so waiting")
            return

        deficit = temp - room
        model = self.warmup_model(device_id)
        rate = model.rate(outside)
        needed_minutes = max(0, deficit) / rate * 60 * SAFETY_FACTOR + EXTRA_MINUTES
        latest_start = ready - needed_minutes * 60
        earliest_start = ready - EARLIEST_START_HOURS * 3600

        if deficit <= 0.2:
            self._note(f"Already {room:.1f}°, so no need to start early for {friendly_when(ready, now)}")
            return

        if now < earliest_start:
            self._note(f"Next: {temp:.1f}° by {friendly_when(ready, now)}. "
                       f"Expect to start around {friendly_when(max(latest_start, earliest_start), now)}")
            return

        if now >= latest_start:
            reason = (f"Room is {room:.1f}° and needs about {needed_minutes:.0f} min to reach "
                      f"{temp:.1f}° by {hhmm(ready)}, so starting now")
            await self._boost(device_id, temp, now, reason, who="Rules")
            return

        # In the window where it's a judgement call: ask the AI, but not too often
        last_ask = self._get("last_ask", 0)
        if self.ai.available and now - last_ask >= ASK_AI_EVERY_MINUTES * 60 - 30:
            self._set("last_ask", now)
            facts = {
                "time_now": hhmm(now),
                "ready_by": hhmm(ready),
                "minutes_until_ready": round((ready - now) / 60),
                "target_temp": temp,
                "room_temp": room,
                "room_change_last_hour": self.room_trend(device_id),
                "outside_now": outside,
                "forecast_next_hours": [{"time": h["time"], "outside": h["outside"]}
                                        for h in (await self.get_forecast())[:6]],
                "learned_warmup_degrees_per_hour": round(rate, 2),
                "warmups_learned_from": len(model.samples),
                "estimated_minutes_needed": round(needed_minutes),
                "latest_start": hhmm(latest_start),
                "earliest_allowed_start": hhmm(earliest_start),
            }
            answer = await self.ai.decide(self.heating.web, facts)
            if answer:
                start_now, reason, who = answer
                if start_now:
                    await self._boost(device_id, temp, now, reason or "Starting now", who=who)
                else:
                    self._note(f"{reason} (will start by {hhmm(latest_start)} at the latest)", who=who)
                return

        self._note(f"Plan to start around {hhmm(latest_start)} to reach {temp:.1f}° by "
                   f"{hhmm(ready)} (room {room:.1f}°)", who="Rules")

    async def _boost(self, device_id, temp, now, reason, who=None):
        await self.heating.boost(device_id, temp, 1)
        self._set("our_boost", {"ends": now + 3600, "temp": temp})
        self._note(reason, who=who)
        self.store.log(device_id, f"Smart: boost to {temp:.1f}°. {reason}"
                       + (f" ({who})" if who else ""))

    async def _top_up(self, device_id, ours, temp, now):
        if ours["ends"] - now <= TOP_UP_WHEN_MINUTES_LEFT * 60 or ours["temp"] != temp:
            await self.heating.boost(device_id, temp, 1)
            self._set("our_boost", {"ends": now + 3600, "temp": temp})

    async def _hand_back(self, device_id, why):
        if self.takeover:
            self._set("our_boost", None)
            await self._hold_background(device_id, why)
            return
        await self.heating.set_mode(device_id, MODE_SCHEDULE)
        self._set("our_boost", None)
        self._note(f"{why}, back to the ESi schedule")
        self.store.log(device_id, f"Smart: {why}, back to the ESi schedule")

    async def _hold_background(self, device_id, why=None):
        bg = self.background
        await self.heating.manual(device_id, bg)
        self._set("our_hold", bg)
        text = f"{why}, back to {bg:.1f}°" if why else f"Holding the background {bg:.1f}°"
        self._note(text)
        self.store.log(device_id, f"Smart: {text}")

    @staticmethod
    def _is_our_hold(dev, hold):
        return (dev["mode"] == MODE_MANUAL and dev.get("target") is not None
                and abs(dev["target"] - hold) < 0.3)

    async def shutdown(self):
        """The dashboard is closing: hand the heating back to the ESi schedule if we were holding it."""
        if self._get("our_hold") is None and not self._get("our_boost"):
            return
        try:
            devices = await asyncio.wait_for(self.heating.status(), 20)
            if devices:
                await asyncio.wait_for(self.heating.set_mode(devices[0]["id"], MODE_SCHEDULE), 20)
                self.store.log(devices[0]["id"], "Smart: dashboard closing, back to the ESi schedule")
        except Exception as err:
            print(f"Smart heating: couldn't hand back to the ESi schedule ({err})")
        self._set("our_hold", None)
        self._set("our_boost", None)

    # ----- for the page -----
    def summary(self, device_id=None):
        now = time.time()
        note = self._get("note") or {}
        upcoming = self.occurrences(now)
        nxt = None
        if upcoming:
            ready, until, temp = upcoming[0]
            nxt = {"ready": friendly_when(ready, now), "until": hhmm(until), "temp": temp,
                   "active": ready <= now < until}
        outside = self.last_outside if self.last_outside is not None else self.heating.weather[0]
        learned = self.warmup_model(device_id).describe(outside) if device_id else ""
        providers = self.ai.providers()
        return {
            "enabled": self.enabled,
            "takeover": self.takeover,
            "background": self.background,
            "paused_until": self._get("paused_until", 0) if self._get("paused_until", 0) > now else 0,
            "note": note.get("text", ""),
            "note_ts": note.get("ts"),
            "note_who": note.get("who"),
            "next": nxt,
            "learned": learned,
            "brain": (" then ".join(p.capitalize() for p in providers) + " then rules")
                     if providers else "Rules only (no AI keys set)",
            "periods": [dict(p, days_name=DAY_SETS[p["days"]][0]) for p in self.periods()],
            "rules": [dict(r, text=describe_rule(r)) for r in self.rules()],
            "suggestion": self._suggestion_for_page(),
            "days_recorded": round(self.store.history_span_days(device_id), 1) if device_id else 0,
        }

    def _suggestion_for_page(self):
        plan = self._get("suggestion")
        if not plan:
            return None
        plan = dict(plan)
        plan["periods"] = [dict(p, days_name=DAY_SETS[p["days"]][0]) for p in plan["periods"]]
        plan["weather_rules"] = [dict(r, text=describe_rule(r)) for r in plan["weather_rules"]]
        return plan

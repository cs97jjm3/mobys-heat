"""Tests for the smart heating logic: no thermostat or internet needed."""

import asyncio
import json
import time
from datetime import datetime, timedelta

import heating_dashboard as hd
import smart_heating as sh


def warmups(days=12, rate_at_0=0.8, slope=0.06):
    """Fake history: one 2-hour warm-up a day, at a range of outside temperatures."""
    points, t = [], 1_700_000_000
    for day in range(days):
        outside = -2 + day * 1.0
        room = 16.0
        for _ in range(24):
            points.append({"ts": t, "room": round(room, 2), "target": 21.0,
                           "mode": "3", "outside": outside})
            room += (rate_at_0 + slope * outside) / 12
            t += 300
        t += 86400
    return points


def test_learns_warm_up_speed_and_cold_slows_it():
    model = sh.WarmupModel(sh.learn_warmup(warmups()))
    assert model.learned
    assert abs(model.rate(0) - 0.8) < 0.05
    assert abs(model.rate(10) - 1.4) < 0.05


def test_assumes_a_default_until_it_has_seen_warm_ups():
    model = sh.WarmupModel([])
    assert not model.learned
    assert model.rate(5) == sh.DEFAULT_RATE
    assert "Still learning" in model.describe(5)


def test_reads_start_or_wait_answers():
    assert sh.parse_answer('{"start_now": true, "reason": "Cold"}') == (True, "Cold")
    assert sh.parse_answer('Sure ```json\n{"start_now": false, "reason": "Wait"}\n```') == (False, "Wait")
    assert sh.parse_answer("yes") is None
    assert sh.parse_answer('{"start_now": "yes"}') is None


def test_plan_suggestions_drop_anything_daft():
    reply = json.dumps({
        "summary": "Runs mornings and evenings.",
        "periods": [
            {"days": "weekdays", "ready": "06:30", "until": "08:30", "temp": 20},
            {"days": "every day", "ready": "17:30", "until": "22:00", "temp": 20.3},
            {"days": "sometimes", "ready": "09:00", "until": "10:00", "temp": 20},
            {"days": "weekends", "ready": "25:00", "until": "10:00", "temp": 20},
            {"days": "weekends", "ready": "08:00", "until": "11:00", "temp": 35},
        ],
        "weather_rules": [
            {"when": "at_or_above", "outside": 17, "action": "skip"},
            {"when": "at_or_below", "outside": 3, "action": "adjust", "amount": 0.5},
            {"when": "at_or_below", "outside": 0, "action": "adjust", "amount": 5},
        ],
        "tips": ["Bleed the radiators"],
    })
    plan = sh.parse_plan(reply)
    assert [(p["days"], p["ready"], p["temp"]) for p in plan["periods"]] == [
        ("weekdays", "06:30", 20.0), ("every", "17:30", 20.5)]
    assert [sh.describe_rule(r) for r in plan["weather_rules"]] == [
        "17° or above outside: no warm-up", "3° or below outside: +0.5°"]
    assert sh.parse_plan("no idea") is None


def test_weather_rules():
    rules = [sh.clean_rule("at_or_above", 17, "skip"),
             sh.clean_rule("at_or_below", 3, "adjust", 0.5)]
    assert sh.apply_rules(rules, 20, 18)[0] is None
    assert sh.apply_rules(rules, 20, 2)[0] == 20.5
    assert sh.apply_rules(rules, 20, 10)[0] == 20
    assert sh.apply_rules(rules, 20, None)[0] == 20


class FakeHeating:
    """Stands in for the thermostat."""

    def __init__(self, room=15.0, target=15.0, mode="0"):
        self.room, self.target, self.mode = room, target, mode
        self.calls, self.web, self.weather = [], None, (9.0, 0)

    async def status(self):
        return [{"id": "d", "mode": self.mode, "room": self.room, "target": self.target,
                 "mode_name": hd.MODE_NAMES.get(self.mode, self.mode)}]

    async def outside_temp(self):
        return 9.0

    async def boost(self, device_id, temp, hours):
        self.calls.append(("boost", temp, hours))
        self.mode, self.target = "3", temp

    async def set_mode(self, device_id, mode):
        self.calls.append(("mode", mode))
        self.mode = mode


def planner(heating):
    p = sh.Planner(heating, hd.Store(":memory:"), {})
    p.add_period("every", "06:30", "08:30", 20)
    p.set_enabled(True)
    return p


def at(hours, days_ahead=3):
    # A few days ahead, so a clock change tonight can't upset the sums
    day = datetime.now().date() + timedelta(days=days_ahead)
    return datetime.combine(day, datetime.min.time()).timestamp() + hours * 3600


def test_waits_then_starts_in_time_for_the_warm_period():
    heating = FakeHeating(room=17.0)
    p = planner(heating)
    asyncio.run(p.tick(now=at(2)))
    assert heating.calls == []
    asyncio.run(p.tick(now=at(5.5)))
    assert heating.calls == [("boost", 20.0, 1)]


def test_never_starts_more_than_three_hours_early():
    heating = FakeHeating(room=10.0)
    p = planner(heating)
    asyncio.run(p.tick(now=at(3.25)))
    assert heating.calls == []
    asyncio.run(p.tick(now=at(3.5)))
    assert heating.calls == [("boost", 20.0, 1)]


def test_leaves_it_alone_when_set_by_hand():
    heating = FakeHeating(room=15.0, mode="4")
    p = planner(heating)
    asyncio.run(p.tick(now=at(6)))
    assert heating.calls == []
    assert "set by hand" in p._get("note")["text"]


def test_skips_a_warm_period_on_a_warm_day():
    heating = FakeHeating(room=16.0)
    p = planner(heating)
    p.add_rule("at_or_above", 8, "skip")
    asyncio.run(p.tick(now=at(6)))
    assert heating.calls == []
    assert "Skipping" in p._get("note")["text"]


def test_hands_back_to_the_esi_schedule_afterwards():
    heating = FakeHeating(room=19.5, mode="3")
    p = planner(heating)
    p._set("our_boost", {"ends": at(9) + 600, "temp": 20})
    asyncio.run(p.tick(now=at(11)))
    assert heating.calls == [("mode", "0")]


def test_plan_answer_with_only_a_summary():
    plan = sh.parse_plan('{"summary": "Your house warms about 1 degree an hour.", "periods": [], "weather_rules": []}')
    assert plan["summary"].startswith("Your house") and plan["periods"] == []


def test_friendly_errors():
    assert "allowance" in sh.friendly_error(RuntimeError("HTTP 429 {'error': 'RESOURCE_EXHAUSTED'}"))
    assert "model name" in sh.friendly_error(RuntimeError("HTTP 404 models/x is not found"))
    assert "key" in sh.friendly_error(RuntimeError("HTTP 401 authentication_error"))


def test_failure_reasons_are_kept_per_request():
    import asyncio

    class Broken(sh.AiAdvisor):
        async def _gemini(self, session, prompt):
            raise RuntimeError("HTTP 429 RESOURCE_EXHAUSTED")

        async def _claude(self, session, prompt, max_tokens=200):
            return "Here are my thoughts, no JSON"

    ai = Broken({"gemini_api_key": "x", "claude_api_key": "y"})
    problems = []
    assert asyncio.run(ai.ask(None, "p", sh.parse_plan, problems=problems)) is None
    assert "Gemini didn't answer" in problems[0] and "Claude replied" in problems[1]


def test_blank_order_still_uses_both():
    # the settings loader passes "" when there's no order line in the file
    ai = sh.AiAdvisor({"order": "", "gemini_api_key": "x", "claude_api_key": "y"})
    assert ai.providers() == ["gemini", "claude"]


# ----- full control: the dashboard holds a background temperature between warm periods -----

class FakeHeat:
    def __init__(self, mode="0", target=19.0, room=18.0):
        self.dev = {"id": "d1", "mode": mode, "mode_name": "x", "target": target, "room": room}
        self.calls = []
        self.weather = (8.0, 0)
        self.web = None

    async def status(self):
        return [dict(self.dev)]

    async def outside_temp(self):
        return 8.0

    async def manual(self, device_id, celsius):
        self.calls.append(("manual", celsius))
        self.dev.update(mode="5", target=celsius)

    async def boost(self, device_id, celsius, hours):
        self.calls.append(("boost", celsius))
        self.dev.update(mode="3", target=celsius)

    async def set_mode(self, device_id, mode):
        self.calls.append(("mode", mode))
        self.dev.update(mode=mode)


def full_planner(heating, tmp_path):
    store = hd.Store(tmp_path / "h.db")
    p = sh.Planner(heating, store, {})
    p.set_enabled(True)
    return p


def at_hour(hour, minute=0):
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def test_full_control_holds_the_background_between_warm_periods(tmp_path):
    heat = FakeHeat()
    p = full_planner(heat, tmp_path)
    p.add_period("every", "19:00", "22:00", 20)
    p.set_takeover(True, 16)
    asyncio.run(p.tick(at_hour(12)))
    assert heat.calls == [("manual", 16.0)]
    asyncio.run(p.tick(at_hour(12, 5)))               # already holding: no more calls
    assert heat.calls == [("manual", 16.0)]


def test_full_control_warms_then_drops_back(tmp_path):
    heat = FakeHeat(mode="5", target=16.0, room=19.8)
    p = full_planner(heat, tmp_path)
    p.add_period("every", "19:00", "22:00", 20)
    p.set_takeover(True, 16)
    p._set("our_hold", 16.0)
    asyncio.run(p.tick(at_hour(20)))                  # in the warm period: boost to 20
    assert heat.calls[-1] == ("boost", 20.0)
    asyncio.run(p.tick(at_hour(21, 58)))              # end of the period: back to the background
    assert heat.calls[-1] == ("manual", 16.0)


def test_switching_full_control_off_hands_back_to_the_schedule(tmp_path):
    heat = FakeHeat(mode="5", target=16.0)
    p = full_planner(heat, tmp_path)
    p.set_takeover(True, 16)
    p._set("our_hold", 16.0)
    p.set_takeover(False)
    asyncio.run(p.tick(at_hour(12)))
    assert ("mode", "0") in heat.calls and p._get("our_hold") is None


def test_a_change_by_hand_is_respected_with_full_control(tmp_path):
    heat = FakeHeat(mode="5", target=22.0)    # someone held 22 in the ESi app
    p = full_planner(heat, tmp_path)
    p.add_period("every", "19:00", "22:00", 20)
    p.set_takeover(True, 16)
    p._set("our_hold", 16.0)
    asyncio.run(p.tick(at_hour(12)))
    assert heat.calls == [] and p._get("paused_until", 0) > at_hour(12)


def test_heating_off_by_hand_stays_off(tmp_path):
    heat = FakeHeat(mode="4", target=None)
    p = full_planner(heat, tmp_path)
    p.set_takeover(True, 16)
    asyncio.run(p.tick(at_hour(12)))
    assert heat.calls == []


def test_closing_the_dashboard_hands_back(tmp_path):
    heat = FakeHeat(mode="5", target=16.0)
    p = full_planner(heat, tmp_path)
    p.set_takeover(True, 16)
    p._set("our_hold", 16.0)
    asyncio.run(p.shutdown())
    assert heat.calls == [("mode", "0")]


def test_background_must_be_sensible(tmp_path):
    p = full_planner(FakeHeat(), tmp_path)
    try:
        p.set_takeover(True, 25)
        assert False
    except ValueError:
        pass


def test_already_at_the_background_counts_as_ours(tmp_path):
    heat = FakeHeat(mode="5", target=16.0)
    p = full_planner(heat, tmp_path)
    p.set_takeover(True, 16)
    asyncio.run(p.tick(at_hour(12)))
    assert heat.calls == [] and not p._get("paused_until", 0) and p._get("our_hold") == 16.0

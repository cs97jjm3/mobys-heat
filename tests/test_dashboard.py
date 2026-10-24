"""Tests for the web side of the dashboard, with a pretend thermostat."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer
from pyesicentro.thermostat import Thermostat

import heating_dashboard as hd


class FakeService:
    def __init__(self, email, password):
        class Session:
            async def close(self):
                pass
        self._session = Session()
        self.device = {"device_id": "d1", "device_name": "Test House", "online": True,
                       "inside_temparature": "195", "current_temprature": "175",
                       "work_mode": "0", "presetsTemp": "170|160|160", "batteryLow": False}

    async def authorise(self):
        pass

    async def get_devices(self):
        return {"d1": Thermostat(dict(self.device))}

    async def get_device(self, device_id):
        return {}

    async def enable_boost_mode(self, device_id, temp, hours):
        self.device.update(work_mode="3", current_temprature=temp)

    async def update_work_mode(self, device_id, mode):
        self.device.update(work_mode=mode)


def run(pin, check):
    async def main():
        hd.Service = FakeService

        async def outside(self):
            return 8.0

        async def no_location(self):
            pass

        hd.Heating.outside_temp = outside
        hd.Heating.find_location = no_location
        store = hd.Store(":memory:")
        app = hd.build_app(hd.Heating("e", "p", store), store, pin, {})
        async with TestClient(TestServer(app)) as client:
            await check(client)
    asyncio.run(main())


def test_status_page_and_icons():
    async def check(client):
        r = await client.get("/api/status")
        data = await r.json()
        assert data["devices"][0]["room"] == 19.5
        assert data["devices"][0]["presets"] == {"home": 17.0, "sleep": 16.0, "away": 16.0}
        assert (await client.get("/")).status == 200
        assert (await client.get("/manifest.webmanifest")).status == 200
        assert (await client.get("/static/icon-192.png")).status == 200
        assert (await client.get("/favicon.ico")).status == 200
    run("", check)


def test_boost_is_logged():
    async def check(client):
        r = await client.post("/api/action", json={"device_id": "d1", "action": "boost",
                                                   "temp": 20, "hours": 1})
        data = await r.json()
        assert data["devices"][0]["mode"] == "3"
        assert data["activity"][0]["text"] == "Boost to 20.0° for 1 hour"
    run("", check)


def test_silly_temperatures_are_refused():
    async def check(client):
        r = await client.post("/api/action", json={"device_id": "d1", "action": "boost",
                                                   "temp": 40, "hours": 1})
        assert r.status == 400
    run("", check)


def test_pin_and_lockout():
    async def check(client):
        assert (await client.get("/api/status")).status == 401
        for _ in range(hd.MAX_PIN_TRIES):
            await client.post("/api/login", json={"pin": "000000"})
        r = await client.post("/api/login", json={"pin": "482913"})
        assert r.status == 429   # locked, even with the right PIN
    run("482913", check)


def test_right_pin_lets_you_in():
    async def check(client):
        r = await client.post("/api/login", json={"pin": "482913"})
        assert r.status == 200
        assert (await client.get("/api/status")).status == 200
    run("482913", check)

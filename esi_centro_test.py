"""
Quick read-only test for an ESi Centro thermostat.

Logs in with your ESi Centro app details, lists your thermostats and
prints the temperatures. It does not change anything on the heating.

Setup:
    pip install pyesicentro aiohttp
Run:
    python esi_centro_test.py
"""

import asyncio
import getpass

from pyesicentro.service import Service

WORK_MODES = {"0": "Schedule", "3": "Boost", "4": "Off", "5": "Manual"}


def to_celsius(value):
    """ESi sends temperatures as tenths of a degree, e.g. '205' = 20.5C."""
    try:
        return f"{int(value) / 10:.1f}°C"
    except (TypeError, ValueError):
        return f"{value} (raw)"


async def main():
    email = input("ESi Centro email: ").strip()
    password = getpass.getpass("ESi Centro password: ")

    service = Service(email, password)
    try:
        await service.authorise()
        thermostats = await service.get_devices()

        if not thermostats:
            print("Logged in, but no thermostats came back.")
            return

        for device_id, stat in thermostats.items():
            mode = WORK_MODES.get(str(stat.work_mode), stat.work_mode)
            print()
            print(f"Name:          {stat.device_name}")
            print(f"Device ID:     {device_id}")
            print(f"Online:        {stat.online}")
            print(f"Room temp:     {to_celsius(stat.inside_temparature)}")
            print(f"Set to:        {to_celsius(stat.current_temperature)}")
            print(f"Mode:          {mode}")

        # Uncomment to see every field the API returns for your thermostat
        # import json
        # for stat in thermostats.values():
        #     print(json.dumps(stat.device_data, indent=2, default=str))

    finally:
        await service._session.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
Read-only look at what ESi sends back about your thermostat's settings,
so we can find where the schedule lives before building anything that
changes it.

It saves everything to esi_settings.json next to this script, with
passwords, tokens and network addresses blanked out. It changes nothing.

Run:
    python esi_schedule_peek.py
Uses heating_config.ini for your login if it's there, otherwise asks.
"""

import asyncio
import configparser
import getpass
import json
from pathlib import Path

from pyesicentro.service import Service

HERE = Path(__file__).parent
HIDE = ("password", "token", "mac", "ip", "sak", "sn", "remote", "email", "user_id")


def tidy(value):
    """Blank out anything that looks private."""
    if isinstance(value, dict):
        return {k: ("(hidden)" if any(h in k.lower() for h in HIDE) else tidy(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [tidy(v) for v in value]
    return value


async def main():
    config = configparser.ConfigParser(interpolation=None)
    config.read(HERE / "heating_config.ini", encoding="utf-8")
    email = config.get("esi", "email", fallback="").strip() or input("ESi Centro email: ").strip()
    password = (config.get("esi", "password", fallback="").strip()
                or getpass.getpass("ESi Centro password: "))

    service = Service(email, password)
    try:
        await service.authorise()
        devices = await service.get_devices()
        result = {}
        for device_id, stat in devices.items():
            settings = await service.get_device(device_id)
            result[stat.device_name or "thermostat"] = {
                "from_device_list": tidy(stat.device_data),
                "from_settings": tidy(settings),
            }
        out = HERE / "esi_settings.json"
        out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"Saved to {out.name}. Have a look, then send it over.")
    finally:
        await service._session.close()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""rtl_2_postgres.py -- feed Acurite Tower/Atlas rtl_433 readings into
sensors-backend-fastapi.

Reads rtl_433's JSON-lines output (from stdin, piped continuously by a
systemd service, or from one or more files) and POSTs readings to
sensors-backend-fastapi. Acurite Tower is a single-purpose
temperature+humidity sensor -- every line maps to one
POST /v1/thermohygrometers/{id_channel}/log. Acurite Atlas is a 7-in-1
weather station that rotates through three different packet layouts in
its transmission burst, all sharing the same "id" but each carrying a
different subset of fields:
  - wind_avg_mi_h + wind_dir_deg + rain_in  -> anemometers + raingauges
  - wind_avg_mi_h + temperature_F + humidity -> thermohygrometers (same
    endpoint Tower uses)
  - wind_avg_mi_h + uvi + lux                -> photo-uvmeters
Each line is dispatched to whichever endpoint(s) match the fields
actually present in it -- there's no cross-packet state to track, every
line is handled independently.

No database credentials are needed here at all: every reading goes
through the API rather than a direct SQL connection (the previous
version of this script connected to Postgres directly with a hardcoded
plaintext password -- removed entirely in this rework).

Deduplication (skip a reading if its value(s) match the most recent log
entry for that sensor) is handled server-side by the API, not here.

Usage:
    rtl_433 -F json -R <acurite-tower> -R <acurite-atlas> | python rtl_2_postgres.py
    python rtl_2_postgres.py capture.jsonl
    python rtl_2_postgres.py --verbose < capture.jsonl

Configuration comes from a .env file (see .env.example) or the ambient
environment.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from typing import Any, Iterable, Sequence

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("Missing dependency 'python-dotenv'.  Run: pip install -r requirements.txt")

try:
    import requests
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("Missing dependency 'requests'.  Run: pip install -r requirements.txt")

MILES_PER_KM = 0.621371
INCHES_PER_MM = 1 / 25.4


def env_int_set(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


def fahrenheit(reading: dict) -> float | None:
    """Acurite Tower/Atlas report temperature_C; some rtl_433 builds add
    a pre-converted temperature_F. Prefer the latter if present."""
    if "temperature_F" in reading:
        return float(reading["temperature_F"])
    if "temperature_C" in reading:
        return float(reading["temperature_C"]) * 1.8 + 32
    return None


def wind_mph(reading: dict) -> float | None:
    """Atlas natively reports wind_avg_mi_h; fall back to a km/h-suffixed
    key (some rtl_433 builds/configs may emit metric instead) and convert."""
    if "wind_avg_mi_h" in reading:
        return float(reading["wind_avg_mi_h"])
    if "wind_avg_km_h" in reading:
        return float(reading["wind_avg_km_h"]) * MILES_PER_KM
    return None


def rain_inches(reading: dict) -> float | None:
    """Atlas natively reports rain_in; fall back to a mm-suffixed key."""
    if "rain_in" in reading:
        return float(reading["rain_in"])
    if "rain_mm" in reading:
        return float(reading["rain_mm"]) * INCHES_PER_MM
    return None


def uv_index(reading: dict) -> float | None:
    """Atlas natively reports uvi; fall back to a shorter "uv" key."""
    if "uvi" in reading:
        return float(reading["uvi"])
    if "uv" in reading:
        return float(reading["uv"])
    return None


def read_lines(paths: Sequence[str]) -> Iterable[str]:
    if paths:
        files = [open(path, encoding="utf-8") for path in paths]
        return itertools.chain.from_iterable(files)
    return sys.stdin


class Pusher:
    """POSTs one reading to one endpoint, tallying pushed/failed counts
    and optionally printing per-line detail."""

    def __init__(self, api_base_url: str, verbose: bool) -> None:
        self.api_base_url = api_base_url
        self.verbose = verbose
        self.pushed = 0
        self.failed = 0

    def push(self, path: str, body: dict[str, Any], label: str) -> None:
        url = f"{self.api_base_url}/{path}"
        try:
            response = requests.post(url, json=body, timeout=10)
            response.raise_for_status()
            self.pushed += 1
            if self.verbose:
                created = response.json().get("created")
                print(f"{label}: created={created}")
        except requests.RequestException as exc:
            self.failed += 1
            print(f"error posting to {path}: {exc}", file=sys.stderr)


def handle_tower_or_temp_hum(
    pusher: Pusher, model: str, id_channel: str, reading: dict, battery_ok: bool | None
) -> None:
    temp_f = fahrenheit(reading)
    humidity = reading.get("humidity")
    if temp_f is None or humidity is None:
        return
    body = {
        "pretty_name": f"{model} {id_channel}",
        "temp_f": round(temp_f, 4),
        "humidity": round(float(humidity), 1),
        "battery_ok": battery_ok,
    }
    pusher.push(f"thermohygrometers/{id_channel}/log", body, f"{id_channel} temp/humidity")


def handle_atlas(pusher: Pusher, model: str, id_channel: str, reading: dict) -> None:
    battery_ok_raw = reading.get("battery_ok")
    battery_ok = bool(battery_ok_raw) if battery_ok_raw is not None else None

    # Message type: wind + direction + rain (always arrive together).
    speed = wind_mph(reading)
    direction = reading.get("wind_dir_deg")
    rain = rain_inches(reading)
    if speed is not None and direction is not None:
        body = {
            "name": f"{model} {id_channel}",
            "speed_mph": round(speed, 1),
            "direction_deg": round(float(direction), 1),
        }
        pusher.push(f"anemometers/{id_channel}/log", body, f"{id_channel} wind")
    if rain is not None:
        body = {"name": f"{model} {id_channel}", "rain_in": round(rain, 2)}
        pusher.push(f"raingauges/{id_channel}/log", body, f"{id_channel} rain")

    # Message type: wind + temp + humidity -- same endpoint Tower uses.
    handle_tower_or_temp_hum(pusher, model, id_channel, reading, battery_ok)

    # Message type: wind + UV index + lux (always arrive together).
    uvi = uv_index(reading)
    lux = reading.get("lux")
    if uvi is not None and lux is not None:
        body = {
            "name": f"{model} {id_channel}",
            "lumens": int(lux),
            "uv_index": int(round(uvi)),
        }
        pusher.push(f"photo-uvmeters/{id_channel}/log", body, f"{id_channel} uv/lux")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="POST rtl_433 Acurite Tower/Atlas readings to sensors-backend-fastapi."
    )
    parser.add_argument(
        "files", nargs="*", metavar="PATH", help="read from these files instead of stdin"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print each reading as it's pushed"
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default .env)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(args.env_file)

    api_base_url = os.getenv("API_BASE_URL", "").rstrip("/")
    if not api_base_url:
        sys.exit("API_BASE_URL must be set in .env")

    # Historically this script hardcoded an exclusion for sensor id 8 (likely
    # a malfunctioning or duplicate device at the time). That's not
    # necessarily still relevant, so it's now opt-in via env var rather than
    # baked into the code -- set EXCLUDE_SENSOR_IDS=8 to restore it.
    exclude_ids = env_int_set("EXCLUDE_SENSOR_IDS")

    pusher = Pusher(api_base_url, args.verbose)

    for line in read_lines(args.files):
        line = line.strip()
        if not line:
            continue
        try:
            reading = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"error decoding line: {exc}", file=sys.stderr)
            continue

        model = reading.get("model")
        if model is None:
            continue

        sensor_id = reading.get("id")
        if sensor_id is None or int(sensor_id) in exclude_ids:
            continue
        id_channel = str(sensor_id)

        if model == "Acurite-Tower":
            battery_ok_raw = reading.get("battery_ok")
            battery_ok = bool(battery_ok_raw) if battery_ok_raw is not None else None
            handle_tower_or_temp_hum(pusher, model, id_channel, reading, battery_ok)
        elif model == "Acurite-Atlas":
            handle_atlas(pusher, model, id_channel, reading)
        else:
            continue

    if args.verbose:
        print(f"pushed {pusher.pushed}, failed {pusher.failed}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

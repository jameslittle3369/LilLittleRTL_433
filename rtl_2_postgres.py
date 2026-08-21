#!/usr/bin/env python3
"""rtl_2_postgres.py -- feed Acurite Tower/Atlas rtl_433 readings into
sensors-backend-fastapi.

Reads rtl_433's JSON-lines output (from stdin, piped continuously by a
systemd service, or from one or more files) and POSTs each
temperature+humidity reading to
POST /v1/thermohygrometers/{id_channel}/log.

No database credentials are needed here at all: every reading goes
through the API rather than a direct SQL connection (the previous
version of this script connected to Postgres directly with a hardcoded
plaintext password -- removed entirely in this rework).

Deduplication (skip a reading if temp_f and humidity both match the
most recent log entry for that sensor) is handled server-side by the
API, not here -- the original script's own dedup was a global "same
timestamp as the previous line, regardless of which sensor" check,
which could incorrectly skip a different sensor's genuinely-new reading
if it happened to share a timestamp. Comparing actual values per-sensor
on the server is strictly more correct, so this client just posts every
line it decodes and lets the API decide.

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
from typing import Iterable, Sequence

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("Missing dependency 'python-dotenv'.  Run: pip install -r requirements.txt")

try:
    import requests
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("Missing dependency 'requests'.  Run: pip install -r requirements.txt")


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


def read_lines(paths: Sequence[str]) -> Iterable[str]:
    if paths:
        files = [open(path, encoding="utf-8") for path in paths]
        return itertools.chain.from_iterable(files)
    return sys.stdin


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

    pushed = 0
    skipped = 0
    for line in read_lines(args.files):
        line = line.strip()
        if not line:
            continue
        try:
            reading = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"error decoding line: {exc}", file=sys.stderr)
            continue

        if "model" not in reading:
            continue

        sensor_id = reading.get("id")
        if sensor_id is None or int(sensor_id) in exclude_ids:
            continue

        temp_f = fahrenheit(reading)
        humidity = reading.get("humidity")
        if temp_f is None or humidity is None:
            continue

        id_channel = str(sensor_id)
        url = f"{api_base_url}/thermohygrometers/{id_channel}/log"
        body = {
            "pretty_name": f"{reading['model']} {id_channel}",
            "temp_f": round(temp_f, 4),
            "humidity": round(float(humidity), 1),
        }
        try:
            response = requests.post(url, json=body, timeout=10)
            response.raise_for_status()
            pushed += 1
            if args.verbose:
                created = response.json().get("created")
                print(f"{id_channel}: temp_f={body['temp_f']} humidity={body['humidity']} "
                      f"created={created}")
        except requests.RequestException as exc:
            skipped += 1
            print(f"error posting reading for sensor {id_channel}: {exc}", file=sys.stderr)

    if args.verbose:
        print(f"pushed {pushed}, failed {skipped}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

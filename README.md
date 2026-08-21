# rtl_2_postgres

Feeds Acurite Tower / Acurite Atlas temperature+humidity sensor readings
(received over an RTL-SDR USB dongle, decoded by [`rtl_433`][rtl_433])
into `sensors-backend-fastapi`, via
`POST /v1/thermohygrometers/{id_channel}/log`.

[rtl_433]: https://github.com/merbanan/rtl_433

## What this replaces

The original version of this script connected directly to Postgres
with `psycopg2` -- including, at one point, a hardcoded plaintext
database password. This rework removes the direct database connection
entirely: readings now go through the API, which handles both
creating the sensor's device row on first sight and deduplicating
identical consecutive readings server-side.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
```

## Usage

Paired with `rtl_433` as a continuous pipeline (this is how it runs in
production, via a systemd service -- see `sensors-ansible/roles/app`):

```bash
rtl_433 -F json -R <acurite-tower-protocol-id> -R <acurite-atlas-protocol-id> \
  | python rtl_2_postgres.py
```

Find the exact `-R` protocol numbers for your `rtl_433` build with:

```bash
rtl_433 -R help | grep -i acurite
```

Also works against a captured file, or with verbose per-reading logging:

```bash
python rtl_2_postgres.py capture.jsonl
python rtl_2_postgres.py --verbose < capture.jsonl
```

## Configuration

See `.env.example`. `API_BASE_URL` is the only required setting.
`EXCLUDE_SENSOR_IDS` (comma-separated) skips specific sensor ids
entirely -- the original script hardcoded an exclusion for id `8`
(a known-faulty or duplicate device at the time); that's not
necessarily still relevant, so it's opt-in here instead of baked in.

## Scope

This script only extracts `temperature_C`/`temperature_F` +
`humidity` + `id` + `model` from each rtl_433 JSON line -- fields
outside that (wind, rain, lightning, etc., which some other rtl_433
protocols report) are ignored, since the API endpoint this feeds only
models temperature+humidity devices.

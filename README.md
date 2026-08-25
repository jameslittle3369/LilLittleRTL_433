# rtl_2_postgres

Feeds Acurite Tower / Acurite Atlas sensor readings (received over an
RTL-SDR USB dongle, decoded by [`rtl_433`][rtl_433]) into
`sensors-backend-fastapi`. Tower is a single-purpose temperature+humidity
sensor. Atlas is a 7-in-1 weather station that rotates through three
different packet layouts in its transmission burst (wind+direction+rain,
wind+temp+humidity, wind+UV+light) -- each line is dispatched to
whichever endpoint(s) match the fields actually present in it:

| Fields present | Endpoint |
| --- | --- |
| `temperature_F`/`temperature_C` + `humidity` | `POST /v1/thermohygrometers/{id_channel}/log` |
| `wind_avg_mi_h` + `wind_dir_deg` | `POST /v1/anemometers/{id_channel}/log` |
| `rain_in` | `POST /v1/raingauges/{id_channel}/log` |
| `uvi` + `lux` | `POST /v1/photo-uvmeters/{id_channel}/log` |

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

Only `Acurite-Tower` and `Acurite-Atlas` (`model` field) are handled --
any other model is ignored. Both report `battery_ok` (1=normal, 0=low)
on every packet, forwarded alongside the temperature/humidity reading.
Wind speed/rain are natively imperial in `rtl_433`'s own Atlas decoder
(`wind_avg_mi_h`, `rain_in`) -- this script reads those directly, but
also defensively checks for a metric-suffixed alternate key
(`wind_avg_km_h`, `rain_mm`) and converts if that's ever what's actually
present. `raingauges`' current value is the raw accumulating hardware
counter as rtl_433 reports it (resets every 5.11in) -- not a true
rolling 24-hour figure.

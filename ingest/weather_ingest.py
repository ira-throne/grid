"""
Grid: hourly weather from Open-Meteo (no API key).

For each active station in grid.dim_weather_station, pulls recent hours
plus a 2 day forecast: temperature and humidity (demand), sunlight and
cloud cover (solar), and wind speed at turbine height (wind). Past hours
are marked observed, future hours are marked forecast and get
overwritten on later runs. (Forecasts kept for model training live in
grid.fact_weather_forecast; see forecast/weather.py.)

Usage:
  python weather_ingest.py                    # incremental (run hourly)
  python weather_ingest.py --backfill-days 30 # manual backfill (max 92)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import common

API_URL = "https://api.open-meteo.com/v1/forecast"
DATASET = "weather"
INCREMENTAL_PAST_DAYS = 2
FORECAST_DAYS = 2
MAX_PAST_DAYS = 92               # Open-Meteo forecast API limit
MAX_RETRIES = 4
# Open-Meteo variable names, in the order they map to table columns.
# shortwave_radiation is the hour's average sunlight on flat ground (W/m²).
HOURLY_VARS = ("temperature_2m,relative_humidity_2m,shortwave_radiation,"
               "cloud_cover,wind_speed_80m")

log = logging.getLogger("weather_ingest")

UPSERT = """
    INSERT INTO grid.fact_weather_hourly
        (station_id, period_utc, temperature_f, humidity_pct,
         shortwave_wm2, cloud_cover_pct, wind_80m_ms, is_forecast)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (station_id, period_utc) DO UPDATE SET
        temperature_f   = EXCLUDED.temperature_f,
        humidity_pct    = EXCLUDED.humidity_pct,
        shortwave_wm2   = EXCLUDED.shortwave_wm2,
        cloud_cover_pct = EXCLUDED.cloud_cover_pct,
        wind_80m_ms     = EXCLUDED.wind_80m_ms,
        is_forecast     = EXCLUDED.is_forecast,
        loaded_at       = now()
    WHERE (grid.fact_weather_hourly.temperature_f, grid.fact_weather_hourly.humidity_pct,
           grid.fact_weather_hourly.shortwave_wm2, grid.fact_weather_hourly.cloud_cover_pct,
           grid.fact_weather_hourly.wind_80m_ms, grid.fact_weather_hourly.is_forecast)
      IS DISTINCT FROM (EXCLUDED.temperature_f, EXCLUDED.humidity_pct, EXCLUDED.shortwave_wm2,
                        EXCLUDED.cloud_cover_pct, EXCLUDED.wind_80m_ms, EXCLUDED.is_forecast)
"""


def fetch_station(session, lat: float, lon: float, past_days: int) -> tuple[dict, dict]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": HOURLY_VARS,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "ms",
        "timezone": "GMT",
        "past_days": past_days,
        "forecast_days": FORECAST_DAYS,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        resp = session.get(API_URL, params=params, timeout=60)
        if resp.status_code == 200:
            return params, resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            log.warning("Open-Meteo %s, retrying in %ds", resp.status_code, wait)
            time.sleep(wait)
            continue
        raise RuntimeError(f"Open-Meteo {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError("Open-Meteo failed after retries")


def to_rows(station_id: str, payload: dict, now: datetime) -> list[tuple]:
    hourly = payload["hourly"]
    rows = []
    for t, temp, hum, sw, cloud, wind in zip(
            hourly["time"], hourly["temperature_2m"], hourly["relative_humidity_2m"],
            hourly["shortwave_radiation"], hourly["cloud_cover"], hourly["wind_speed_80m"]):
        period = datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        rows.append((station_id, period, temp, hum, sw, cloud, wind, period > now))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest hourly weather for grid regions")
    parser.add_argument("--backfill-days", type=int, default=None)
    args = parser.parse_args()
    common.setup_logging()

    past_days = min(args.backfill_days or INCREMENTAL_PAST_DAYS, MAX_PAST_DAYS)
    now = datetime.now(timezone.utc)
    started = now

    with common.connect() as conn, requests.Session() as session:
        stations = conn.execute(
            """SELECT station_id, latitude, longitude FROM grid.dim_weather_station
               WHERE is_active ORDER BY station_id""").fetchall()
        log.info("%d stations, %d past days + %d forecast days",
                 len(stations), past_days, FORECAST_DAYS)

        fetched = upserted = failures = 0
        max_observed = None
        for station_id, lat, lon in stations:
            try:
                params, payload = fetch_station(session, float(lat), float(lon), past_days)
                rows = to_rows(station_id, payload, now)
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO raw.weather_response (station_id, request_params, payload)
                           VALUES (%s, %s, %s)""",
                        (station_id, json.dumps(params), json.dumps(payload["hourly"])),
                    )
                    cur.executemany(UPSERT, rows)
                    upserted += max(cur.rowcount, 0)
                fetched += len(rows)
                observed = [r[1] for r in rows if not r[7]]
                if observed:
                    max_observed = max(observed) if max_observed is None else max(max_observed, max(observed))
            except Exception:
                failures += 1
                log.exception("%s failed", station_id)
            time.sleep(0.2)

        status = "success" if failures == 0 else f"partial ({failures} stations failed)"
        with conn.transaction():
            common.set_watermark(conn, DATASET, max_observed, fetched)
            common.log_run(conn, DATASET, started, now - timedelta(days=past_days),
                           now + timedelta(days=FORECAST_DAYS), fetched, upserted, status)
        log.info("done, %d fetched, %d inserted or changed, %d failures",
                 fetched, upserted, failures)
        return 1 if failures == len(stations) and stations else 0


if __name__ == "__main__":
    sys.exit(main())

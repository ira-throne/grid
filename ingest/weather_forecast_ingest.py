"""
Grid: weather forecasts kept for model training and live forecasting.

Fills grid.fact_weather_forecast with forecasts as they were known about
a day ahead (lead_days = 1). Two modes:

  live (default)  Fetch the current Open-Meteo forecast and store every
                  hour of tomorrow (Pacific) for each active station.
                  Run once a day shortly before forecast/run.py, which
                  reads exactly these rows. Nothing is overwritten later,
                  so the scorecard always reflects what the model knew.

  --backfill-start YYYY-MM-DD
                  Pull history from Open-Meteo's Previous Runs API, which
                  archives what each model run predicted. The
                  *_previous_day1 variables are the forecast made 24 hours
                  before each hour. Most models are archived from
                  January 2024, which sets how far back training can go.

Usage:
  python weather_forecast_ingest.py
  python weather_forecast_ingest.py --backfill-start 2024-01-15
  python weather_forecast_ingest.py --backfill-start 2024-01-01 --stations CISO_NM_S,CISO_NM_N
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import common

LIVE_URL = "https://api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
DATASET = "weather_forecast"
PACIFIC = ZoneInfo("America/Los_Angeles")
BACKFILL_CHUNK_DAYS = 90
MAX_RETRIES = 4

# Open-Meteo name -> our column, in table column order.
VARIABLES = {
    "temperature_2m": "temperature_f",
    "relative_humidity_2m": "humidity_pct",
    "shortwave_radiation": "shortwave_wm2",
    "cloud_cover": "cloud_cover_pct",
    "wind_speed_80m": "wind_80m_ms",
}

log = logging.getLogger("weather_forecast_ingest")

UPSERT = """
    INSERT INTO grid.fact_weather_forecast
        (station_id, period_utc, lead_days, temperature_f, humidity_pct,
         shortwave_wm2, cloud_cover_pct, wind_80m_ms, source, issued_at)
    VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (station_id, period_utc, lead_days) DO NOTHING
"""
# DO NOTHING on purpose: the first forecast stored for an hour is the one
# that was known a day ahead. A rerun later in the day must not replace
# it with a fresher (more accurate) forecast.


def get_json(session, url: str, params: dict) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        resp = session.get(url, params=params, timeout=90)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = 5 * 2 ** attempt
            log.warning("Open-Meteo %s, retrying in %ds", resp.status_code, wait)
            time.sleep(wait)
            continue
        raise RuntimeError(f"Open-Meteo {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError("Open-Meteo failed after retries")


def to_rows(station_id: str, hourly: dict, suffix: str, source: str,
            issued_at: datetime | None, keep) -> list[tuple]:
    rows = []
    columns = [hourly.get(name + suffix) or [None] * len(hourly["time"]) for name in VARIABLES]
    for i, t in enumerate(hourly["time"]):
        period = datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        if not keep(period):
            continue
        values = [col[i] for col in columns]
        if all(v is None for v in values):
            continue  # hour not archived
        rows.append((station_id, period, *values, source, issued_at))
    return rows


def base_params(lat, lon) -> dict:
    return {
        "latitude": float(lat),
        "longitude": float(lon),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "ms",
        "timezone": "GMT",
    }


def run_live(conn, session, stations) -> tuple[int, int]:
    """Store tomorrow's hours (Pacific) from the current forecast."""
    issued = datetime.now(timezone.utc)
    tomorrow = (issued.astimezone(PACIFIC) + timedelta(days=1)).date()
    start = datetime.combine(tomorrow, datetime.min.time(), PACIFIC).astimezone(timezone.utc)
    end = datetime.combine(tomorrow + timedelta(days=1), datetime.min.time(),
                           PACIFIC).astimezone(timezone.utc)
    # Open-Meteo labels sunlight with the END of the hour it averages, so
    # keep one extra hour on the end; forecast/features.py does the shift.
    keep = lambda p: start <= p <= end
    fetched = inserted = 0
    for station_id, lat, lon in stations:
        params = base_params(lat, lon) | {"hourly": ",".join(VARIABLES), "forecast_days": 3}
        payload = get_json(session, LIVE_URL, params)
        rows = to_rows(station_id, payload["hourly"], "", "live", issued, keep)
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(UPSERT, rows)
            inserted += max(cur.rowcount, 0)
        fetched += len(rows)
        time.sleep(0.2)
    log.info("live: tomorrow is %s, %d rows fetched, %d new", tomorrow, fetched, inserted)
    return fetched, inserted


def run_backfill(conn, session, stations, first: date) -> tuple[int, int]:
    last = datetime.now(timezone.utc).date() - timedelta(days=1)
    hourly = ",".join(f"{name}_previous_day1" for name in VARIABLES)
    fetched = inserted = 0
    for station_id, lat, lon in stations:
        chunk = first
        while chunk <= last:
            chunk_end = min(chunk + timedelta(days=BACKFILL_CHUNK_DAYS - 1), last)
            params = base_params(lat, lon) | {
                "hourly": hourly,
                "start_date": chunk.isoformat(),
                "end_date": chunk_end.isoformat(),
            }
            payload = get_json(session, PREVIOUS_RUNS_URL, params)
            rows = to_rows(station_id, payload["hourly"], "_previous_day1",
                           "previous_runs", None, lambda p: True)
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(UPSERT, rows)
                inserted += max(cur.rowcount, 0)
            fetched += len(rows)
            log.info("%s %s to %s: %d hours", station_id, chunk, chunk_end, len(rows))
            chunk = chunk_end + timedelta(days=1)
            time.sleep(1)  # be polite; this is a free API
    return fetched, inserted


def main() -> int:
    parser = argparse.ArgumentParser(description="Store day ahead weather forecasts")
    parser.add_argument("--backfill-start", type=date.fromisoformat, default=None)
    parser.add_argument("--stations", type=str, default=None,
                        help="comma separated station ids, e.g. to backfill only new stations")
    args = parser.parse_args()
    common.setup_logging()
    started = datetime.now(timezone.utc)

    with common.connect() as conn, requests.Session() as session:
        stations = conn.execute(
            """SELECT station_id, latitude, longitude FROM grid.dim_weather_station
               WHERE is_active ORDER BY station_id""").fetchall()
        if args.stations:
            wanted = {s.strip() for s in args.stations.split(",")}
            stations = [s for s in stations if s[0] in wanted]
        try:
            if args.backfill_start:
                fetched, inserted = run_backfill(conn, session, stations, args.backfill_start)
            else:
                fetched, inserted = run_live(conn, session, stations)
            with conn.transaction():
                latest = conn.execute(
                    "SELECT MAX(period_utc) FROM grid.fact_weather_forecast").fetchone()[0]
                common.set_watermark(conn, DATASET, latest, fetched)
                common.log_run(conn, DATASET, started, None, None, fetched, inserted, "success")
            log.info("done, %d fetched, %d new", fetched, inserted)
            return 0
        except Exception as exc:
            log.exception("failed")
            with conn.transaction():
                common.log_run(conn, DATASET, started, None, None, 0, 0, "failed", str(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())

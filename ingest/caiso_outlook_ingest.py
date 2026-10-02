"""
Grid: CAISO 5 minute demand and fuel mix from Today's Outlook.

CAISO publishes two small CSV files per day behind its Today's Outlook
page, with one row per 5 minutes in Pacific time:

  demand.csv      Time, Day ahead forecast, Hour ahead forecast, Current demand, ...
  fuelsource.csv  Time, Solar, Wind, Geothermal, ..., Batteries, Imports, Other

Today's files live under /outlook/current/, finished days under
/outlook/history/YYYYMMDD/. Both land in grid.fact_caiso_outlook_5min.
This is what the dashboard's "right now" numbers read; it's fresher and
finer than EIA's hourly feed. Each run also rolls the 5 minute demand up
into hourly demand actuals for the forecast (see HOURLY_DEMAND_SQL).

Usage:
  python caiso_outlook_ingest.py                          # today, plus yesterday if incomplete
  python caiso_outlook_ingest.py --backfill-start 2026-01-01
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import common

CURRENT_BASE = "https://www.caiso.com/outlook/current"
HISTORY_BASE = "https://www.caiso.com/outlook/history"
DATASET = "caiso_outlook"
PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_RETRIES = 4

# CSV header (lowercased) -> table column, per file
FILES = {
    "demand": {
        "day ahead forecast": "da_forecast_mw",
        "hour ahead forecast": "ha_forecast_mw",
        "current demand": "demand_mw",
    },
    "fuelsource": {
        "solar": "solar_mw", "wind": "wind_mw", "geothermal": "geothermal_mw",
        "biomass": "biomass_mw", "biogas": "biogas_mw", "small hydro": "small_hydro_mw",
        "coal": "coal_mw", "nuclear": "nuclear_mw", "natural gas": "natural_gas_mw",
        "large hydro": "large_hydro_mw", "batteries": "batteries_mw",
        "imports": "imports_mw", "other": "other_mw",
    },
}

# A column that only has values for intervals that already happened
ACTUAL_COL = {"demand": "demand_mw", "fuelsource": "solar_mw"}

log = logging.getLogger("caiso_outlook_ingest")


def upsert_sql(columns: list[str]) -> str:
    """Each file only updates its own columns, so demand and fuel rows merge."""
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * (len(columns) + 1))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
    old = ", ".join(f"grid.fact_caiso_outlook_5min.{c}" for c in columns)
    new = ", ".join(f"EXCLUDED.{c}" for c in columns)
    return f"""
        INSERT INTO grid.fact_caiso_outlook_5min (interval_start_utc, {cols})
        VALUES ({placeholders})
        ON CONFLICT (interval_start_utc) DO UPDATE SET {updates}, loaded_at = now()
        WHERE ({old}) IS DISTINCT FROM ({new})
    """


def get_csv(session, url: str) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        # The query string defeats CAISO's CDN cache, which otherwise can
        # serve a stale copy of the "current" file for a while.
        resp = session.get(url, params={"_": int(time.time())}, timeout=60)
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 404:
            return None
        wait = 2 ** attempt
        log.warning("%s returned %s, retrying in %ds", url, resp.status_code, wait)
        time.sleep(wait)
    raise RuntimeError(f"{url} failed after retries")


def parse(csv_text: str, day: date, mapping: dict) -> list[tuple]:
    """Turn local HH:MM rows into UTC timestamps.

    Two calendar quirks:
      * In November the clock falls back, so 01:00 to 01:55 appears twice.
        When the clock goes backward mid day, later rows are the second
        pass (fold=1 in Python's terms).
      * Some files end with a 00:00 row that is really the next midnight.
    Rows with every value blank (the skipped spring forward hour) are dropped.
    """
    reader = csv.reader(io.StringIO(csv_text))
    header = [h.strip().lower() for h in next(reader)]
    index = {col: header.index(name) for name, col in mapping.items() if name in header}
    rows, prev_minutes, fold = [], -1, 0
    for rec in reader:
        if not rec or not rec[0].strip():
            continue
        hh, mm = (int(x) for x in rec[0].strip().split(":")[:2])
        minutes = hh * 60 + mm
        if minutes < prev_minutes:
            if prev_minutes >= 23 * 60:
                break                    # trailing next day midnight
            fold = 1                     # fall back: the repeated hour
        prev_minutes = minutes
        values = []
        for col in mapping.values():
            i = index.get(col)
            raw = rec[i].strip() if i is not None and i < len(rec) else ""
            values.append(float(raw) if raw not in ("", "NA") else None)
        if all(v is None for v in values):
            continue
        local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=PACIFIC, fold=fold)
        rows.append((local.astimezone(timezone.utc), *values))
    return rows


def load_day(conn, session, day: date, current: bool) -> int:
    total = 0
    for name, mapping in FILES.items():
        url = f"{CURRENT_BASE}/{name}.csv" if current else \
              f"{HISTORY_BASE}/{day:%Y%m%d}/{name}.csv"
        text = get_csv(session, url)
        if text is None:
            log.info("%s %s: not published", day, name)
            continue
        rows = parse(text, day, mapping)
        if current and rows:
            # Just after midnight the "current" file can still be yesterday's.
            # Its last measured row would then be later than the clock says
            # it is now. (demand.csv carries the day ahead forecast for the
            # whole day, so look at the measured column, not the last row.)
            k = 1 + list(mapping.values()).index(ACTUAL_COL[name])
            measured = [r for r in rows if r[k] is not None]
            latest_local = measured[-1][0].astimezone(PACIFIC) if measured else None
            if latest_local and latest_local > datetime.now(PACIFIC) + timedelta(minutes=10):
                log.info("current %s is still yesterday's file, skipping", name)
                continue
        with conn.transaction(), conn.cursor() as cur:
            if not current:   # raw copy of finished days only (current changes every 5 min)
                cur.execute(
                    """INSERT INTO raw.caiso_report (report, report_date, url, payload)
                       VALUES (%s, %s, %s, %s)""",
                    (f"outlook_{name}", day, url, json.dumps({"csv": text})))
            cur.executemany(upsert_sql(list(mapping.values())), rows)
        total += len(rows)
        log.info("%s %s: %d intervals", day, name, len(rows))
    return total


def day_is_complete(conn, day: date) -> bool:
    start = datetime.combine(day, datetime.min.time(), PACIFIC).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), PACIFIC).astimezone(timezone.utc)
    expected = int((end - start).total_seconds() // 300)
    have = conn.execute(
        """SELECT COUNT(*) FROM grid.fact_caiso_outlook_5min
           WHERE interval_start_utc >= %s AND interval_start_utc < %s
             AND demand_mw IS NOT NULL AND solar_mw IS NOT NULL""", (start, end)).fetchone()[0]
    return have >= expected - 2


# Hourly demand actuals for the forecast, built from the 5 minute file.
# CAISO's OASIS "ACTUAL" demand for the CAISO area runs about 2,500 MW
# above the demand its own day ahead forecast is made for, so grading the
# forecast against it was unfair to every model. Today's Outlook pairs
# "Current demand" with the "Day ahead forecast" OASIS publishes, so its
# hourly average is the matching actual. Only complete hours count.
HOURLY_DEMAND_SQL = """
    INSERT INTO grid.fact_caiso_hourly (series, market_run_id, period_utc, mw)
    SELECT 'demand', 'ACTUAL', date_trunc('hour', interval_start_utc), ROUND(AVG(demand_mw), 2)
    FROM grid.fact_caiso_outlook_5min
    WHERE interval_start_utc >= %(start)s AND demand_mw IS NOT NULL
    GROUP BY 3
    HAVING COUNT(*) >= 11
    ON CONFLICT (series, market_run_id, period_utc) DO UPDATE
       SET mw = EXCLUDED.mw, loaded_at = now()
     WHERE grid.fact_caiso_hourly.mw IS DISTINCT FROM EXCLUDED.mw
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest CAISO Today's Outlook 5 minute data")
    parser.add_argument("--backfill-start", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    common.setup_logging()
    started = datetime.now(timezone.utc)
    today = datetime.now(PACIFIC).date()

    with common.connect() as conn, requests.Session() as session:
        fetched = 0
        try:
            if args.backfill_start:
                day = args.backfill_start
                while day < today:
                    if not day_is_complete(conn, day):
                        fetched += load_day(conn, session, day, current=False)
                        time.sleep(1)
                    day += timedelta(days=1)
            else:
                yesterday = today - timedelta(days=1)
                if not day_is_complete(conn, yesterday):
                    fetched += load_day(conn, session, yesterday, current=False)
                fetched += load_day(conn, session, today, current=True)

            first_day = args.backfill_start or today - timedelta(days=1)
            hourly_start = datetime.combine(first_day, datetime.min.time(), PACIFIC)
            with conn.transaction():
                n = conn.execute(HOURLY_DEMAND_SQL, {"start": hourly_start}).rowcount
            log.info("hourly demand actuals: %d hours inserted or changed", n)

            with conn.transaction():
                latest = conn.execute(
                    "SELECT MAX(interval_start_utc) FROM grid.fact_caiso_outlook_5min").fetchone()[0]
                common.set_watermark(conn, DATASET, latest, fetched)
                common.log_run(conn, DATASET, started, None, None, fetched, fetched, "success")
            log.info("done, %d intervals", fetched)
            return 0
        except Exception as exc:
            log.exception("failed")
            with conn.transaction():
                common.log_run(conn, DATASET, started, None, None, fetched, 0, "failed", str(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())

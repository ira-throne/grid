"""Make tomorrow's forecast (the live one the scorecard grades).

    .venv/bin/python -m forecast.run          # what cron runs, every hour
    .venv/bin/python -m forecast.run --now    # run immediately, ignoring the 9 am rule

Cron calls this every hour so it works whatever time zone the server
clock is in. It does nothing until 9 am Pacific, then:

  1. stores the current weather forecast for tomorrow
     (ingest/weather_forecast_ingest.py)
  2. builds tomorrow's features from that weather and from actuals up to now
  3. predicts each hour of tomorrow with the saved models
  4. writes the forecasts to grid.forecast_run / grid.forecast_value,
     plus the naive "same hour last week" baseline

Once tomorrow's forecast exists it does nothing until the next day. 9 am
is a fair comparison point: CAISO's day ahead forecast for tomorrow is
also out by then.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import model as M
from .db import connect
from .features import SERIES, add_lags, build

PACIFIC = ZoneInfo("America/Los_Angeles")
RUN_AFTER = time(9, 0)
PROJECT = Path(__file__).resolve().parent.parent
log = logging.getLogger("forecast.run")


def already_done(conn, target) -> bool:
    n = conn.execute(
        """SELECT COUNT(*) FROM grid.forecast_run
           WHERE kind = 'live' AND model = 'gbm' AND target_date = %s""", (target,)).fetchone()[0]
    return n == len(SERIES)


def store(conn, model: str, series: str, target, version: str | None, values) -> bool:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO grid.forecast_run (model, series, target_date, kind, model_version)
               VALUES (%s, %s, %s, 'live', %s)
               ON CONFLICT (model, series, target_date, kind) DO NOTHING
               RETURNING run_id""", (model, series, target, version))
        row = cur.fetchone()
        if row is None:
            return False                       # first forecast made is the one that counts
        cur.executemany(
            "INSERT INTO grid.forecast_value (run_id, period_utc, mw) VALUES (%s, %s, %s)",
            [(row[0], t.to_pydatetime(), round(float(v), 2)) for t, v in values])
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Forecast tomorrow")
    parser.add_argument("--now", action="store_true", help="skip the 9 am Pacific check")
    parser.add_argument("--skip-weather", action="store_true",
                        help="use tomorrow's weather already stored instead of fetching it")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    now = datetime.now(PACIFIC)
    target = now.date() + timedelta(days=1)
    with connect() as conn:
        if already_done(conn, target):
            log.info("forecast for %s already made", target)
            return 0
        if now.time() < RUN_AFTER and not args.now:
            log.info("before %s Pacific, nothing to do", RUN_AFTER)
            return 0

        # 1. Tomorrow's weather, saved before it's used
        weather = subprocess.run([sys.executable, "ingest/weather_forecast_ingest.py"],
                                 cwd=PROJECT) if not args.skip_weather else None
        if weather is not None and weather.returncode != 0:
            log.error("weather forecast ingest failed")
            return 1

        # 2. Features for tomorrow's hours
        start = datetime.combine(target, time.min, PACIFIC)
        end = datetime.combine(target + timedelta(days=1), time.min, PACIFIC)
        base = build(conn, start, end)

        failures = 0
        for series in SERIES:
            df = add_lags(base, series)
            df = df[df.index >= start]
            ready = M.usable(df, series, need_target=False)
            if len(ready) < len(df):
                log.error("%s: weather missing for %d of %d hours", series, len(df) - len(ready), len(df))
                failures += 1
                continue
            # 3. Predict
            try:
                bundle = M.load(series)
            except FileNotFoundError:
                log.error("%s: no saved model; run python -m forecast.train --save", series)
                failures += 1
                continue
            pred = M.predict(bundle, df, series)
            # 4. Store ours and the naive baseline
            made = store(conn, "gbm", series, target, bundle.get("trained_at"), zip(df.index, pred))
            naive = df["lag168"].dropna()
            store(conn, "naive", series, target, None, naive.items())
            log.info("%s: %s, peak %.0f MW at %s", series, "stored" if made else "already existed",
                     pred.max(), df.index[pred.argmax()].tz_convert(PACIFIC).strftime("%I %p"))
        # Bookkeeping so the dashboard's status chips can see the job
        conn.execute(
            """INSERT INTO grid.etl_watermark (dataset, last_period_utc, last_run_at, last_row_count)
               VALUES ('forecast', %s, now(), %s)
               ON CONFLICT (dataset) DO UPDATE SET last_period_utc = EXCLUDED.last_period_utc,
                   last_run_at = now(), last_row_count = EXCLUDED.last_row_count""",
            (end, len(SERIES) - failures))
        conn.execute(
            """INSERT INTO grid.etl_run_log (dataset, started_at, finished_at, window_start,
                   window_end, rows_fetched, rows_upserted, status)
               VALUES ('forecast', %s, now(), %s, %s, %s, %s, %s)""",
            (now, start, end, len(SERIES) - failures, len(SERIES) - failures,
             "failed" if failures else "success"))
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

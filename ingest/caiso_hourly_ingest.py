"""
Grid: CAISO hourly demand, solar and wind, actual and day ahead forecast.

Four OASIS pulls land in grid.fact_caiso_hourly:

  query          market_run_id  series        what
  SLD_FCST       DAM            demand        CAISO's day ahead demand forecast
  SLD_REN_FCST   ACTUAL         solar, wind   what solar and wind produced
  SLD_REN_FCST   DAM            solar, wind   CAISO's day ahead solar and wind forecast

The ACTUAL rows are what our model trains on and is scored against; the
DAM rows are the benchmark. Actual demand comes from Today's Outlook
instead (caiso_outlook_ingest.py): OASIS's ACTUAL demand for the CAISO
area measures a larger total than the day ahead forecast covers, about
2,500 MW higher, so it isn't a fair comparison. Demand is the CAISO area total ("CA ISO-TAC");
solar and wind are summed across the three trading hubs (NP15, SP15, ZP26).

Usage:
  python caiso_hourly_ingest.py                     # incremental (run hourly)
  python caiso_hourly_ingest.py --backfill-days 1000
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import common
import oasis

DATASET = "caiso_hourly"
PACIFIC = ZoneInfo("America/Los_Angeles")
LOOKBACK = timedelta(days=3)      # actuals get revised for a few days
DEFAULT_BACKFILL = timedelta(days=14)
CHUNK = timedelta(days=30)        # OASIS allows up to 31 days per request
CAISO_TAC = "CA ISO-TAC"
RENEWABLE_SERIES = {"Solar": "solar", "Wind": "wind"}

log = logging.getLogger("caiso_hourly_ingest")

UPSERT = """
    INSERT INTO grid.fact_caiso_hourly (series, market_run_id, period_utc, mw)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (series, market_run_id, period_utc) DO UPDATE
       SET mw = EXCLUDED.mw, loaded_at = now()
     WHERE grid.fact_caiso_hourly.mw IS DISTINCT FROM EXCLUDED.mw
"""


def num(v):
    return None if v in (None, "") else float(v)


def parse_demand(csv_text: str, market: str) -> list[tuple]:
    out: dict[datetime, float] = {}
    for row in oasis.rows(csv_text):
        if row.get("TAC_AREA_NAME") != CAISO_TAC:
            continue
        out[oasis.interval_start(row)] = num(row.get("MW"))
    return [("demand", market, t, v) for t, v in out.items()]


def parse_renewables(csv_text: str, market: str) -> list[tuple]:
    # (hub, type, hour) -> MW first, so a duplicated row can't double count
    by_hub: dict[tuple, float] = {}
    for row in oasis.rows(csv_text):
        series = RENEWABLE_SERIES.get(row.get("RENEWABLE_TYPE") or "")
        value = num(row.get("MW"))
        if series and value is not None:
            by_hub[(row.get("TRADING_HUB"), series, oasis.interval_start(row))] = value
    totals: dict[tuple, float] = defaultdict(float)
    for (_, series, t), v in by_hub.items():
        totals[(series, t)] += v
    return [(series, market, t, round(v, 2)) for (series, t), v in totals.items()]


PULLS = [
    # (queryname, version, market_run_id, parser)
    ("SLD_FCST", "1", "DAM", parse_demand),
    ("SLD_REN_FCST", "1", "ACTUAL", parse_renewables),
    ("SLD_REN_FCST", "1", "DAM", parse_renewables),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest CAISO hourly actuals and forecasts")
    parser.add_argument("--backfill-days", type=int, default=None)
    args = parser.parse_args()
    common.setup_logging()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    day_after = (now.astimezone(PACIFIC) + timedelta(days=2)).date()
    end = datetime.combine(day_after, datetime.min.time(), PACIFIC).astimezone(timezone.utc)
    started = datetime.now(timezone.utc)

    with common.connect() as conn, requests.Session() as session:
        if args.backfill_days:
            start = now - timedelta(days=args.backfill_days)
        else:
            wm = conn.execute(
                """SELECT MAX(period_utc) FROM grid.fact_caiso_hourly
                   WHERE market_run_id = 'ACTUAL' AND series <> 'demand'""").fetchone()[0]
            start = (wm - LOOKBACK) if wm else now - DEFAULT_BACKFILL
        log.info("window %s to %s UTC", start, end)

        fetched = upserted = 0
        try:
            for queryname, version, market, parse in PULLS:
                chunk_start = start
                pull_end = now + timedelta(hours=1) if market == "ACTUAL" else end
                while chunk_start < pull_end:
                    chunk_end = min(chunk_start + CHUNK, pull_end)
                    result = oasis.fetch(session, queryname, version, chunk_start, chunk_end,
                                         market_run_id=market)
                    rows = parse(result[2], market) if result else []
                    if rows:
                        with conn.transaction(), conn.cursor() as cur:
                            cur.executemany(UPSERT, rows)
                            upserted += max(cur.rowcount, 0)
                    fetched += len(rows)
                    log.info("%s %s %s to %s: %d rows", queryname, market,
                             chunk_start, chunk_end, len(rows))
                    chunk_start = chunk_end
                    time.sleep(oasis.PAUSE_SECONDS)

            with conn.transaction():
                latest = conn.execute(
                    """SELECT MAX(period_utc) FROM grid.fact_caiso_hourly
                       WHERE market_run_id = 'ACTUAL' AND series <> 'demand'""").fetchone()[0]
                common.set_watermark(conn, DATASET, latest, fetched)
                common.log_run(conn, DATASET, started, start, end, fetched, upserted, "success")
            log.info("done, %d fetched, %d inserted or changed", fetched, upserted)
            return 0
        except Exception as exc:
            log.exception("failed")
            with conn.transaction():
                common.log_run(conn, DATASET, started, start, end, fetched, upserted, "failed", str(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())

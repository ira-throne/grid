"""
Grid: CAISO wholesale prices (LMPs) from OASIS.

Two markets, one script:
  RTM  real time, 5 minute intervals   -> grid.fact_caiso_lmp_5min   (every 5 minutes)
  DAM  day ahead, hourly               -> grid.fact_caiso_lmp_dam    (hourly; tomorrow's
                                          prices appear around 1 pm Pacific)

OASIS sends one row per price component per node per interval. This
script pivots the components into one row per node per interval and
upserts.

Usage:
  python caiso_ingest.py                              # RTM, incremental
  python caiso_ingest.py --market DAM                 # DAM, incremental
  python caiso_ingest.py --backfill-days 7            # manual backfill
  python caiso_ingest.py --market DAM --backfill-days 1000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import common
import oasis

PACIFIC = ZoneInfo("America/Los_Angeles")

# LMP_TYPE code -> our column. Older/newer file versions may only
# carry XML_DATA_ITEM, so map both.
COMPONENTS = {
    "LMP": "lmp", "MCE": "energy", "MCC": "congestion", "MCL": "loss", "MGHG": "ghg",
    "LMP_PRC": "lmp", "LMP_ENE_PRC": "energy", "LMP_CONG_PRC": "congestion",
    "LMP_LOSS_PRC": "loss", "LMP_GHG_PRC": "ghg",
}


@dataclass(frozen=True)
class Market:
    dataset: str
    queryname: str
    version: str
    table: str
    time_col: str
    lookback: timedelta        # re-pull recent data in case CAISO revises it
    default_backfill: timedelta
    chunk: timedelta           # one request covers this much time


MARKETS = {
    "RTM": Market("caiso_lmp", "PRC_INTVL_LMP", "2", "grid.fact_caiso_lmp_5min",
                  "interval_start_utc", timedelta(hours=1), timedelta(days=3), timedelta(days=1)),
    "DAM": Market("caiso_lmp_dam", "PRC_LMP", "12", "grid.fact_caiso_lmp_dam",
                  "period_utc", timedelta(days=2), timedelta(days=30), timedelta(days=7)),
}

log = logging.getLogger("caiso_ingest")


def parse_csv(csv_text: str) -> list[tuple]:
    """Pivot component rows into (node, interval_start, lmp, energy, congestion, loss, ghg)."""
    records = oasis.rows(csv_text)
    if not records:
        return []
    value_col = "VALUE" if "VALUE" in records[0] else "MW"
    pivot: dict[tuple, dict] = defaultdict(dict)
    for row in records:
        comp = COMPONENTS.get(row.get("LMP_TYPE") or "") or COMPONENTS.get(row.get("XML_DATA_ITEM") or "")
        if not comp:
            continue
        # Group on the interval's own timestamp; OPR_HR would merge 12 intervals.
        start = oasis.interval_start(row)
        raw = row.get(value_col)
        pivot[(row.get("NODE") or row.get("NODE_ID"), start)][comp] = float(raw) if raw not in (None, "") else None
    return [
        (node, start, c.get("lmp"), c.get("energy"), c.get("congestion"), c.get("loss"), c.get("ghg"))
        for (node, start), c in pivot.items()
    ]


def upsert_sql(m: Market) -> str:
    t = m.table
    return f"""
        INSERT INTO {t} (node_id, {m.time_col}, lmp, energy, congestion, loss, ghg)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (node_id, {m.time_col}) DO UPDATE SET
            lmp = EXCLUDED.lmp, energy = EXCLUDED.energy, congestion = EXCLUDED.congestion,
            loss = EXCLUDED.loss, ghg = EXCLUDED.ghg, loaded_at = now()
        WHERE ({t}.lmp, {t}.energy, {t}.congestion, {t}.loss, {t}.ghg)
          IS DISTINCT FROM
              (EXCLUDED.lmp, EXCLUDED.energy, EXCLUDED.congestion, EXCLUDED.loss, EXCLUDED.ghg)
    """


def window_end(market: str) -> datetime:
    now = datetime.now(timezone.utc)
    if market == "RTM":
        return now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
    # DAM: through the end of tomorrow, Pacific
    day_after = (now.astimezone(PACIFIC) + timedelta(days=2)).date()
    return datetime.combine(day_after, datetime.min.time(), PACIFIC).astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest CAISO prices (LMPs)")
    parser.add_argument("--market", choices=list(MARKETS), default="RTM")
    parser.add_argument("--backfill-days", type=int, default=None)
    args = parser.parse_args()
    common.setup_logging()
    m = MARKETS[args.market]
    sql = upsert_sql(m)

    end = window_end(args.market)
    started = datetime.now(timezone.utc)

    with common.connect() as conn, requests.Session() as session:
        nodes = [r[0] for r in conn.execute(
            "SELECT node_id FROM grid.dim_caiso_node WHERE is_active ORDER BY node_id")]
        if args.backfill_days:
            start = end - timedelta(days=args.backfill_days)
        else:
            wm = common.get_watermark(conn, m.dataset)
            start = (wm - m.lookback) if wm else end - m.default_backfill
            if args.market == "DAM":
                # Watermark is the latest hour loaded, which is tomorrow once
                # tomorrow's prices are out; always recheck the last two days.
                start = min(start, end - timedelta(days=3))
        start = start.replace(minute=0, second=0, microsecond=0)
        log.info("%s window %s to %s UTC, %d nodes", args.market, start, end, len(nodes))

        fetched = upserted = 0
        max_interval = None
        try:
            chunk_start = start
            while chunk_start < end:
                chunk_end = min(chunk_start + m.chunk, end)
                result = oasis.fetch(session, m.queryname, m.version, chunk_start, chunk_end,
                                     market_run_id=args.market, node=",".join(nodes))
                if result:
                    params, file_name, csv_text = result
                    rows = parse_csv(csv_text)
                    with conn.transaction(), conn.cursor() as cur:
                        # Raw copy for the 5 minute feed only: the day ahead
                        # backfill would add hundreds of MB for little value.
                        if args.market == "RTM":
                            cur.execute(
                                """INSERT INTO raw.caiso_response
                                       (request_params, file_name, csv_text, row_count)
                                   VALUES (%s, %s, %s, %s)""",
                                (json.dumps(params), file_name, csv_text, len(rows)),
                            )
                        cur.executemany(sql, rows)
                        upserted += max(cur.rowcount, 0)
                    fetched += len(rows)
                    if rows:
                        chunk_max = max(r[1] for r in rows)
                        max_interval = chunk_max if max_interval is None else max(max_interval, chunk_max)
                    log.info("%s to %s: %d intervals", chunk_start, chunk_end, len(rows))
                else:
                    log.info("%s to %s: no data yet", chunk_start, chunk_end)
                chunk_start = chunk_end
                if chunk_start < end:
                    time.sleep(oasis.PAUSE_SECONDS)

            with conn.transaction():
                common.set_watermark(conn, m.dataset, max_interval, fetched)
                common.log_run(conn, m.dataset, started, start, end, fetched, upserted, "success")
            log.info("done, %d fetched, %d inserted or changed", fetched, upserted)
            return 0
        except Exception as exc:
            log.exception("failed")
            with conn.transaction():
                common.log_run(conn, m.dataset, started, start, end, fetched, upserted, "failed", str(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())

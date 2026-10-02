"""
Grid: hourly wind and solar curtailment from CAISO's daily renewable report.

Curtailment is generation that was available but switched off: on sunny
spring afternoons California often has more solar than it can use or
export. CAISO stopped publishing its curtailment spreadsheets and PDFs in
June 2025; the numbers now live in a daily HTML report whose charts are
drawn from JavaScript arrays embedded in the page, e.g.

    curt_hr_tot_solar_econ_system_mwh = [0, 0, ..., 812.4, ...]

one value per hour of the report's day (Pacific). This script pulls those
arrays out with a regular expression, the same approach the open source
gridstatus library uses.

Array names: curt_hr_tot_{fuel}_{reason}_{scope}_mwh
  fuel   solar | wind
  reason econ (economic) | ss (self schedule cut) | oi (operator instruction)
  scope  local | system

Usage:
  python curtailment_ingest.py                         # days since the last one loaded
  python curtailment_ingest.py --backfill-start 2025-06-01
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import common

URL = "https://www.caiso.com/documents/daily-renewable-report-{slug}.html"
DATASET = "caiso_curtailment"
PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_CATCHUP_DAYS = 14      # the report appears a few days late; keep trying recent days
FUELS = ["solar", "wind"]
REASONS = {"econ": "economic", "ss": "selfschcut", "oi": "operator"}
SCOPES = ["local", "system"]

log = logging.getLogger("curtailment_ingest")

UPSERT = """
    INSERT INTO grid.fact_caiso_curtailment (period_utc, fuel, reason, scope, mwh)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (period_utc, fuel, reason, scope) DO UPDATE
       SET mwh = EXCLUDED.mwh, loaded_at = now()
     WHERE grid.fact_caiso_curtailment.mwh IS DISTINCT FROM EXCLUDED.mwh
"""


def fetch(session, day: date) -> tuple[str, str] | None:
    slug = day.strftime("%b-%d-%Y").lower()          # e.g. sep-27-2026
    for url in (URL.format(slug=slug), URL.format(slug=slug + "-corrected")):
        resp = session.get(url, timeout=60)
        if resp.status_code == 200:
            return url, resp.text
        if resp.status_code != 404:
            resp.raise_for_status()
    return None


def extract_array(html: str, var_name: str) -> list[float | None] | None:
    """Find `var_name = [ ... ]` (sometimes wrapped in JSON.parse) and parse it."""
    match = re.search(rf'\b{var_name}\s*=\s*(?:JSON\.parse\(\["?)?\[([^\]]*)\]', html)
    if not match:
        return None
    values = []
    for item in match.group(1).split(","):
        item = item.strip().strip('"').strip("'")
        try:
            values.append(float(item))
        except ValueError:
            values.append(None)   # "NA" or blank
    return values


def hour_starts(day: date, n_values: int) -> list[datetime | None]:
    """UTC start of the hour each array value belongs to.

    The report has one value per hour of the Pacific day. On the two
    clock change days that day isn't 24 hours long:
      * March: 2 am doesn't exist (23 hours). If the report still has 24
        values, the 2 am slot is skipped (None) rather than shifting
        every later hour by one.
      * November: 1 am happens twice (25 hours). With 25 values each gets
        its own hour; with 24 the single 1 am value goes on the first 1 am.
    """
    start = datetime.combine(day, datetime.min.time(), PACIFIC).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), PACIFIC).astimezone(timezone.utc)
    real_hours = int((end - start).total_seconds() // 3600)
    if n_values == real_hours:
        return [start + timedelta(hours=i) for i in range(real_hours)]
    if n_values == 24:
        out = []
        for h in range(24):
            local = datetime(day.year, day.month, day.day, h, tzinfo=PACIFIC)
            utc = local.astimezone(timezone.utc)
            exists = utc.astimezone(PACIFIC).hour == h     # False for the skipped March hour
            out.append(utc if exists else None)
        return out
    raise ValueError(f"{day}: {n_values} hourly values for a {real_hours} hour day")


def parse(html: str, day: date) -> tuple[list[tuple], dict]:
    rows, arrays = [], {}
    for fuel in FUELS:
        for code, reason in REASONS.items():
            for scope in SCOPES:
                name = f"curt_hr_tot_{fuel}_{code}_{scope}_mwh"
                values = extract_array(html, name)
                if values is None:
                    raise ValueError(f"{day}: {name} not found; the report layout may have changed")
                arrays[name] = values
                for t, v in zip(hour_starts(day, len(values)), values):
                    if t is not None:
                        rows.append((t, fuel, reason, scope, v or 0.0))
    return rows, arrays


def load_day(conn, session, day: date) -> int | None:
    result = fetch(session, day)
    if result is None:
        log.info("%s: report not published yet", day)
        return None
    url, html = result
    rows, arrays = parse(html, day)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO raw.caiso_report (report, report_date, url, payload)
               VALUES ('daily_renewable', %s, %s, %s)""",
            (day, url, json.dumps(arrays)))
        cur.executemany(UPSERT, rows)
    total = sum(r[4] for r in rows)
    log.info("%s: %.0f MWh curtailed", day, total)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest CAISO hourly curtailment")
    parser.add_argument("--backfill-start", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    common.setup_logging()
    started = datetime.now(timezone.utc)
    yesterday = datetime.now(PACIFIC).date() - timedelta(days=1)

    with common.connect() as conn, requests.Session() as session:
        loaded_days = {r[0] for r in conn.execute(
            """SELECT DISTINCT (period_utc AT TIME ZONE 'America/Los_Angeles')::date
               FROM grid.fact_caiso_curtailment""")}
        first = args.backfill_start or yesterday - timedelta(days=MAX_CATCHUP_DAYS)
        days = [first + timedelta(days=i) for i in range((yesterday - first).days + 1)]
        fetched, failures = 0, 0
        latest = None
        for day in days:
            if day in loaded_days:
                continue
            try:
                n = load_day(conn, session, day)
                if n:
                    fetched += n
                    latest = day
            except Exception:
                failures += 1
                log.exception("%s failed", day)
            time.sleep(1)

        status = "success" if failures == 0 else f"partial ({failures} days failed)"
        with conn.transaction():
            wm = conn.execute("SELECT MAX(period_utc) FROM grid.fact_caiso_curtailment").fetchone()[0]
            common.set_watermark(conn, DATASET, wm, fetched)
            common.log_run(conn, DATASET, started, None, None, fetched, fetched,
                           "failed" if failures and not fetched else status)
        log.info("done, %d rows, newest day loaded this run: %s", fetched, latest)
        return 1 if failures and not fetched else 0


if __name__ == "__main__":
    sys.exit(main())

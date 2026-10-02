"""Shared helpers for Grid Pulse ingest scripts: DB connection, watermarks, run log."""

from __future__ import annotations

import logging
import os
from datetime import datetime

import psycopg
from dotenv import load_dotenv

load_dotenv()


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def get_watermark(conn, dataset: str) -> datetime | None:
    row = conn.execute(
        "SELECT last_period_utc FROM grid.etl_watermark WHERE dataset = %s", (dataset,)
    ).fetchone()
    return row[0] if row else None


def set_watermark(conn, dataset: str, last_period: datetime | None, row_count: int) -> None:
    conn.execute(
        """INSERT INTO grid.etl_watermark (dataset, last_period_utc, last_run_at, last_row_count)
           VALUES (%s, %s, now(), %s)
           ON CONFLICT (dataset) DO UPDATE SET
               last_period_utc = GREATEST(grid.etl_watermark.last_period_utc,
                                          EXCLUDED.last_period_utc),
               last_run_at = now(),
               last_row_count = EXCLUDED.last_row_count""",
        (dataset, last_period, row_count),
    )


def log_run(conn, dataset, started, window_start, window_end,
            fetched, upserted, status, error=None) -> None:
    conn.execute(
        """INSERT INTO grid.etl_run_log (dataset, started_at, finished_at, window_start,
               window_end, rows_fetched, rows_upserted, status, error)
           VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s)""",
        (dataset, started, window_start, window_end, fetched, upserted, status,
         error[:2000] if error else None),
    )

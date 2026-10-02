"""Train and evaluate the day ahead models.

    .venv/bin/python -m forecast.train            # evaluate only, print the scorecard
    .venv/bin/python -m forecast.train --save     # also save models and backtest forecasts

Evaluation is a time split: train on everything before the last
--test-days, then forecast those held out days. A random split would let
the model peek at the hours on either side of each test hour, which
flatters it. Three forecasts are graded on the same hours:

  Grid model            ours
  CAISO day ahead       CAISO's own forecast, the benchmark to beat
  Same hour last week   naive baseline; a model that can't beat this is useless

With --save the models are refit on all the data (so the live model uses
the newest history) and the held out forecasts go into grid.forecast_run
with kind = 'backtest', so the dashboard scorecard has numbers before
live forecasts accumulate.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import model as M
from .db import connect, frame
from .features import SERIES, training_frame

PACIFIC = ZoneInfo("America/Los_Angeles")
log = logging.getLogger("forecast.train")


def caiso_dam(conn, series: str, start, end) -> pd.Series:
    df = frame(conn, """SELECT period_utc, mw FROM grid.fact_caiso_hourly
                        WHERE series = %(s)s AND market_run_id = 'DAM'
                          AND period_utc >= %(start)s AND period_utc < %(end)s""",
               {"s": series, "start": start, "end": end})
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index(pd.to_datetime(df["period_utc"], utc=True))["mw"]


def score(actual: pd.Series, pred: pd.Series) -> dict:
    err = pred - actual
    return {"mae_mw": float(np.abs(err).mean()),
            "nmae_pct": float(100 * np.abs(err).sum() / actual.sum()),
            "bias_mw": float(err.mean())}


def write_backtest(conn, series: str, test: pd.DataFrame, version: str) -> None:
    conn.execute("DELETE FROM grid.forecast_run WHERE kind = 'backtest' AND series = %s", (series,))
    local_day = test.index.tz_convert(PACIFIC).date
    with conn.transaction(), conn.cursor() as cur:
        for model_name, col in (("gbm", "pred"), ("naive", "lag168")):
            for day, g in test.groupby(local_day):
                g = g[g[col].notna()]
                if g.empty:
                    continue
                cur.execute(
                    """INSERT INTO grid.forecast_run (model, series, target_date, kind, model_version)
                       VALUES (%s, %s, %s, 'backtest', %s) RETURNING run_id""",
                    (model_name, series, day, version))
                run_id = cur.fetchone()[0]
                cur.executemany(
                    "INSERT INTO grid.forecast_value (run_id, period_utc, mw) VALUES (%s, %s, %s)",
                    [(run_id, t.to_pydatetime(), round(float(v), 2)) for t, v in g[col].items()])


def main() -> int:
    parser = argparse.ArgumentParser(description="Train and evaluate the day ahead models")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 8),
                        help="first day of training data (weather forecast history starts Jan 2024)")
    # 60 days: long enough to grade on, short enough that training still
    # sees a few months of SunZia (New Mexico wind that began spring 2026)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    start = datetime.combine(args.start, time.min, PACIFIC)
    end = datetime.combine(datetime.now(PACIFIC).date(), time.min, PACIFIC)
    split = end - timedelta(days=args.test_days)
    version = datetime.now(PACIFIC).strftime("%Y-%m-%d %H:%M")

    results = []
    with connect() as conn:
        for series in SERIES:
            df = M.usable(training_frame(conn, start, end, series), series)
            train, test = df[df.index < split], df[df.index >= split].copy()
            if len(train) < 24 * 60 or len(test) < 24 * 7:
                log.warning("%s: not enough data (%d train, %d test hours); backfill first",
                            series, len(train), len(test))
                continue
            bundle = M.fit(train, series)
            test["pred"] = M.predict(bundle, test, series)
            test["caiso"] = caiso_dam(conn, series, split, end).reindex(test.index)

            graded = test.dropna(subset=["pred", "caiso", "lag168"])
            for name, col in (("Grid model", "pred"), ("CAISO day ahead", "caiso"),
                              ("Same hour last week", "lag168")):
                results.append({"series": series, "model": name, "hours": len(graded),
                                **score(graded[series], graded[col])})
            log.info("%s: trained on %d hours, tested on %d", series, len(train), len(graded))

            if args.save:
                final = M.fit(df, series)          # refit on everything for live use
                final |= {"trained_at": version, "train_start": str(args.start),
                          "rows": len(df)}
                path = M.save(final, series)
                write_backtest(conn, series, test, version)
                log.info("%s: saved %s and %d backtest days", series, path,
                         test.index.tz_convert(PACIFIC).normalize().nunique())

    if results:
        table = pd.DataFrame(results).set_index(["series", "model"])
        with pd.option_context("display.float_format", "{:,.1f}".format, "display.width", 120):
            print(f"\nHeld out: {split:%Y-%m-%d} to {end:%Y-%m-%d} (Pacific)\n")
            print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

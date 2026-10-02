-- Forecasts and how good they are. Run after 07_california_data.sql.
-- Safe to run more than once.

-- ---------------------------------------------------------------
-- One row per forecast made: which model, for which series and day.
--   kind = 'live'      made the morning before, by forecast/run.py
--   kind = 'backtest'  made by forecast/train.py on held out history,
--                      so the scorecard has numbers from day one
-- One live run per model, series and day: the first one made is kept,
-- so nobody (including us) can quietly swap in a better late forecast.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grid.forecast_run (
    run_id         BIGSERIAL PRIMARY KEY,
    model          TEXT        NOT NULL,          -- 'gbm' (ours) or 'naive' (same hour last week)
    series         TEXT        NOT NULL CHECK (series IN ('demand', 'solar', 'wind')),
    target_date    DATE        NOT NULL,          -- the Pacific day being forecast
    kind           TEXT        NOT NULL CHECK (kind IN ('live', 'backtest')),
    model_version  TEXT,                          -- when the model was trained
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (model, series, target_date, kind)
);

CREATE TABLE IF NOT EXISTS grid.forecast_value (
    run_id      BIGINT      NOT NULL REFERENCES grid.forecast_run ON DELETE CASCADE,
    period_utc  TIMESTAMPTZ NOT NULL,             -- hour start
    mw          NUMERIC(10,2),
    PRIMARY KEY (run_id, period_utc)
);

-- ---------------------------------------------------------------
-- Every forecast for an hour next to what happened, one column per
-- model. CAISO's day ahead forecast is the benchmark.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW grid.v_forecast_compare AS
WITH ours AS (
    SELECT r.kind, r.series, v.period_utc,
           MAX(v.mw) FILTER (WHERE r.model = 'gbm')   AS gbm_mw,
           MAX(v.mw) FILTER (WHERE r.model = 'naive') AS naive_mw
    FROM grid.forecast_run r
    JOIN grid.forecast_value v USING (run_id)
    GROUP BY r.kind, r.series, v.period_utc
)
SELECT o.kind, o.series, o.period_utc,
       a.mw AS actual_mw,
       o.gbm_mw,
       c.mw AS caiso_mw,
       o.naive_mw
FROM ours o
LEFT JOIN grid.fact_caiso_hourly a
       ON a.series = o.series AND a.period_utc = o.period_utc AND a.market_run_id = 'ACTUAL'
LEFT JOIN grid.fact_caiso_hourly c
       ON c.series = o.series AND c.period_utc = o.period_utc AND c.market_run_id = 'DAM';

-- ---------------------------------------------------------------
-- Scorecard: error over the last 30 days with an actual, per model.
-- Only hours where all three forecasts exist count, so every model is
-- graded on exactly the same hours.
--   mae_mw   average miss in MW
--   nmae_pct total miss as a % of total actual. Used instead of MAPE
--            because solar is 0 at night and MAPE divides by it.
--   bias_mw  average signed miss (positive = forecast too high)
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW grid.v_forecast_scorecard AS
WITH graded AS (
    SELECT * FROM grid.v_forecast_compare
    WHERE actual_mw IS NOT NULL AND gbm_mw IS NOT NULL
      AND caiso_mw IS NOT NULL AND naive_mw IS NOT NULL
      AND (kind = 'backtest'
           OR period_utc >= (SELECT MAX(period_utc) FROM grid.fact_caiso_hourly
                             WHERE market_run_id = 'ACTUAL') - interval '30 days')
), long AS (
    SELECT kind, series, period_utc, actual_mw, 'Grid model' AS model, 1 AS model_order, gbm_mw AS mw FROM graded
    UNION ALL
    SELECT kind, series, period_utc, actual_mw, 'CAISO day ahead', 2, caiso_mw FROM graded
    UNION ALL
    SELECT kind, series, period_utc, actual_mw, 'Same hour last week', 3, naive_mw FROM graded
)
SELECT kind, series, model, model_order,
       COUNT(*)                                                   AS hours,
       MIN(period_utc)                                            AS first_hour_utc,
       MAX(period_utc)                                            AS last_hour_utc,
       ROUND(AVG(ABS(mw - actual_mw)), 0)                         AS mae_mw,
       ROUND(100 * SUM(ABS(mw - actual_mw)) / NULLIF(SUM(actual_mw), 0), 2) AS nmae_pct,
       ROUND(AVG(mw - actual_mw), 0)                              AS bias_mw
FROM long
GROUP BY kind, series, model, model_order;

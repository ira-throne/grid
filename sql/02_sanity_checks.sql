-- Run after the first ingest to confirm data landed sensibly

-- 1. Did every dataset run, and how recent is it?
SELECT dataset, last_period_utc, last_run_at, last_row_count
FROM grid.etl_watermark ORDER BY dataset;

-- 2. Latest run status per dataset
SELECT DISTINCT ON (dataset) dataset, status, rows_fetched, rows_upserted, error
FROM grid.etl_run_log ORDER BY dataset, started_at DESC;

-- 3. Latest CAISO price per node (should be within ~15 minutes of now)
SELECT DISTINCT ON (node_id) node_id, interval_start_utc AT TIME ZONE 'America/Los_Angeles' AS interval_pt,
       lmp, energy, congestion, loss
FROM grid.fact_caiso_lmp_5min
ORDER BY node_id, interval_start_utc DESC;

-- ---------------------------------------------------------------
-- California checks
-- ---------------------------------------------------------------

-- 4. Hourly CAISO data: newest hour per series (ACTUAL should be within
--    a couple of hours of now, DAM should reach the end of tomorrow after
--    the morning)
SELECT series, market_run_id, COUNT(*) AS hours,
       MIN(period_utc) AT TIME ZONE 'America/Los_Angeles' AS first_pt,
       MAX(period_utc) AT TIME ZONE 'America/Los_Angeles' AS last_pt
FROM grid.fact_caiso_hourly GROUP BY 1, 2 ORDER BY 1, 2;

-- 5. Weather forecasts available for training, per station role
SELECT s.role, COUNT(DISTINCT f.period_utc) AS hours,
       MIN(f.period_utc)::date AS first_day, MAX(f.period_utc)::date AS last_day
FROM grid.fact_weather_forecast f
JOIN grid.dim_weather_station s USING (station_id)
GROUP BY 1 ORDER BY 1;

-- 6. Latest 5 minute mix
SELECT interval_start_utc AT TIME ZONE 'America/Los_Angeles' AS interval_pt,
       demand_mw, solar_mw, wind_mw, clean_pct, est_kg_co2_per_mwh
FROM grid.v_outlook_5min ORDER BY interval_start_utc DESC LIMIT 1;

-- 7. Curtailment by month
SELECT date_trunc('month', day)::date AS month,
       ROUND(SUM(curtailed_solar_mwh)) AS solar_mwh, ROUND(SUM(curtailed_wind_mwh)) AS wind_mwh
FROM grid.v_curtailment_daily GROUP BY 1 ORDER BY 1;

-- 8. Forecast scorecard
SELECT kind, series, model, hours, mae_mw, nmae_pct, bias_mw
FROM grid.v_forecast_scorecard ORDER BY kind, series, model_order;

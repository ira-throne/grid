-- California data: everything the forecasting, curtailment and plug in
-- features read from. Run after 06_california.sql. Safe to run more than once.
--
--   fact_weather_hourly      + solar radiation, cloud cover, wind speed columns
--   fact_weather_forecast    weather forecasts kept with their lead time (never overwritten)
--   fact_caiso_hourly        CAISO's own hourly actuals and day ahead forecasts
--   fact_caiso_lmp_dam       day ahead hourly prices
--   fact_caiso_outlook_5min  5 minute demand and fuel mix (CAISO Today's Outlook)
--   fact_caiso_curtailment   hourly wind and solar curtailment

-- ---------------------------------------------------------------
-- WEATHER: extra variables for solar and wind
-- ---------------------------------------------------------------
ALTER TABLE grid.fact_weather_hourly
    ADD COLUMN IF NOT EXISTS shortwave_wm2   NUMERIC(6,1),   -- sunlight reaching the ground, W/m²
    ADD COLUMN IF NOT EXISTS cloud_cover_pct NUMERIC(5,1),
    ADD COLUMN IF NOT EXISTS wind_80m_ms     NUMERIC(5,1);   -- wind at turbine height, m/s

-- Forecasts as they were known in advance. fact_weather_hourly overwrites
-- each forecast hour until it's observed, which is fine for charts but
-- useless for training: a model trained on observed weather looks far
-- better in testing than it can be in real life (it "knows" tomorrow's
-- weather perfectly). Here each forecast is kept with its lead time.
--   lead_days = 1: forecast made about a day before the hour it describes
CREATE TABLE IF NOT EXISTS grid.fact_weather_forecast (
    station_id      TEXT        NOT NULL REFERENCES grid.dim_weather_station,
    period_utc      TIMESTAMPTZ NOT NULL,
    lead_days       SMALLINT    NOT NULL,
    temperature_f   NUMERIC(5,1),
    humidity_pct    NUMERIC(5,1),
    shortwave_wm2   NUMERIC(6,1),
    cloud_cover_pct NUMERIC(5,1),
    wind_80m_ms     NUMERIC(5,1),
    source          TEXT        NOT NULL,     -- 'previous_runs' (backfill) or 'live'
    issued_at       TIMESTAMPTZ,              -- when we fetched it (live only)
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (station_id, period_utc, lead_days)
);
CREATE INDEX IF NOT EXISTS ix_weather_fc_period ON grid.fact_weather_forecast (period_utc);

-- ---------------------------------------------------------------
-- CAISO HOURLY ACTUALS AND FORECASTS (OASIS)
-- ---------------------------------------------------------------
-- series         : demand, solar, wind (MW averaged over the hour = MWh)
-- market_run_id  : ACTUAL = what happened, DAM = CAISO's day ahead forecast
-- Actuals and forecasts both come from CAISO, so they cover exactly the
-- same plants and the comparison with our model is fair.
CREATE TABLE IF NOT EXISTS grid.fact_caiso_hourly (
    series         TEXT        NOT NULL CHECK (series IN ('demand', 'solar', 'wind')),
    market_run_id  TEXT        NOT NULL CHECK (market_run_id IN ('ACTUAL', 'DAM')),
    period_utc     TIMESTAMPTZ NOT NULL,      -- hour start
    mw             NUMERIC(10,2),
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (series, market_run_id, period_utc)
);
CREATE INDEX IF NOT EXISTS ix_caiso_hourly_period ON grid.fact_caiso_hourly (period_utc);

-- ---------------------------------------------------------------
-- DAY AHEAD PRICES
-- ---------------------------------------------------------------
-- Published around 1 pm Pacific for the next day, so tomorrow's prices
-- are known today. Same nodes and components as the 5 minute table.
CREATE TABLE IF NOT EXISTS grid.fact_caiso_lmp_dam (
    node_id           TEXT        NOT NULL REFERENCES grid.dim_caiso_node,
    period_utc        TIMESTAMPTZ NOT NULL,   -- hour start
    lmp               NUMERIC(10,5),
    energy            NUMERIC(10,5),
    congestion        NUMERIC(10,5),
    loss              NUMERIC(10,5),
    ghg               NUMERIC(10,5),
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, period_utc)
);
CREATE INDEX IF NOT EXISTS ix_lmp_dam_period ON grid.fact_caiso_lmp_dam (period_utc);

-- ---------------------------------------------------------------
-- 5 MINUTE DEMAND AND FUEL MIX (Today's Outlook)
-- ---------------------------------------------------------------
-- Two CSV files per day (demand.csv, fuelsource.csv) land in one row per
-- interval; each file's ingest only updates its own columns.
-- MW. Batteries are negative while charging. Imports = net imports.
CREATE TABLE IF NOT EXISTS grid.fact_caiso_outlook_5min (
    interval_start_utc  TIMESTAMPTZ PRIMARY KEY,
    demand_mw           NUMERIC(9,1),
    da_forecast_mw      NUMERIC(9,1),
    ha_forecast_mw      NUMERIC(9,1),
    solar_mw            NUMERIC(9,1),
    wind_mw             NUMERIC(9,1),
    geothermal_mw       NUMERIC(9,1),
    biomass_mw          NUMERIC(9,1),
    biogas_mw           NUMERIC(9,1),
    small_hydro_mw      NUMERIC(9,1),
    coal_mw             NUMERIC(9,1),
    nuclear_mw          NUMERIC(9,1),
    natural_gas_mw      NUMERIC(9,1),
    large_hydro_mw      NUMERIC(9,1),
    batteries_mw        NUMERIC(9,1),
    imports_mw          NUMERIC(9,1),
    other_mw            NUMERIC(9,1),
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------
-- CURTAILMENT (CAISO daily renewable report)
-- ---------------------------------------------------------------
-- fuel   : solar, wind
-- reason : economic (cheaper to switch off than to run, often at negative
--          prices), selfschcut (self schedule cut), operator (operator
--          instruction, usually a reliability problem)
-- scope  : local (a transmission bottleneck) or system (too much supply
--          for the whole grid)
CREATE TABLE IF NOT EXISTS grid.fact_caiso_curtailment (
    period_utc  TIMESTAMPTZ NOT NULL,         -- hour start
    fuel        TEXT        NOT NULL CHECK (fuel IN ('solar', 'wind')),
    reason      TEXT        NOT NULL CHECK (reason IN ('economic', 'selfschcut', 'operator')),
    scope       TEXT        NOT NULL CHECK (scope IN ('local', 'system')),
    mwh         NUMERIC(10,2),
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (period_utc, fuel, reason, scope)
);

-- Raw landing table for the report pages (HTML is large; keep the
-- extracted arrays instead, which are enough to replay the parse).
CREATE TABLE IF NOT EXISTS raw.caiso_report (
    response_id  BIGSERIAL PRIMARY KEY,
    report       TEXT        NOT NULL,        -- e.g. daily_renewable, outlook_demand
    report_date  DATE        NOT NULL,
    url          TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_raw_caiso_report ON raw.caiso_report (report, report_date);

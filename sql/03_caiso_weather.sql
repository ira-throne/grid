-- CAISO 5 minute prices + Open-Meteo weather
-- Safe to run more than once.

-- ---------------------------------------------------------------
-- RAW LAYER
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.caiso_response (
    response_id     BIGSERIAL PRIMARY KEY,
    request_params  JSONB       NOT NULL,
    file_name       TEXT,
    csv_text        TEXT        NOT NULL,   -- the CSV exactly as CAISO sent it
    row_count       INT         NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.weather_response (
    response_id     BIGSERIAL PRIMARY KEY,
    station_id      TEXT        NOT NULL,
    request_params  JSONB       NOT NULL,
    payload         JSONB       NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------
-- CAISO PRICES
-- ---------------------------------------------------------------
-- Pricing nodes we track. Trading hubs are the benchmark prices;
-- DLAPs are what the three big utilities' customers are priced at.
CREATE TABLE IF NOT EXISTS grid.dim_caiso_node (
    node_id      TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    node_type    TEXT NOT NULL CHECK (node_type IN ('HUB', 'DLAP')),
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO grid.dim_caiso_node (node_id, description, node_type) VALUES
    ('TH_NP15_GEN-APND', 'NP15 trading hub (Northern California)', 'HUB'),
    ('TH_SP15_GEN-APND', 'SP15 trading hub (Southern California)', 'HUB'),
    ('TH_ZP26_GEN-APND', 'ZP26 trading hub (Central California)',  'HUB'),
    ('DLAP_PGAE-APND',   'PG&E load area',                         'DLAP'),
    ('DLAP_SCE-APND',    'Southern California Edison load area',   'DLAP'),
    ('DLAP_SDGE-APND',   'San Diego Gas & Electric load area',     'DLAP')
ON CONFLICT (node_id) DO NOTHING;

-- One row per node per 5 minute interval, price components pivoted
-- into columns. $/MWh. lmp = energy + congestion + loss (+ ghg).
CREATE TABLE IF NOT EXISTS grid.fact_caiso_lmp_5min (
    node_id             TEXT        NOT NULL REFERENCES grid.dim_caiso_node,
    interval_start_utc  TIMESTAMPTZ NOT NULL,
    lmp                 NUMERIC(10,5),
    energy              NUMERIC(10,5),
    congestion          NUMERIC(10,5),
    loss                NUMERIC(10,5),
    ghg                 NUMERIC(10,5),
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, interval_start_utc)
);
CREATE INDEX IF NOT EXISTS ix_caiso_lmp_interval
    ON grid.fact_caiso_lmp_5min (interval_start_utc);

-- ---------------------------------------------------------------
-- WEATHER
-- ---------------------------------------------------------------
-- Weather stations. 06_california.sql adds roles and weights and the
-- solar and wind farm sites.
CREATE TABLE IF NOT EXISTS grid.dim_weather_station (
    station_id   TEXT PRIMARY KEY,
    region_code  TEXT NOT NULL REFERENCES grid.dim_region,
    city         TEXT NOT NULL,
    latitude     NUMERIC(8,5) NOT NULL,
    longitude    NUMERIC(8,5) NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO grid.dim_weather_station (station_id, region_code, city, latitude, longitude)
SELECT s.station_id, s.region_code, s.city, s.latitude, s.longitude
FROM (VALUES
    ('CISO_LA',    'CISO', 'Los Angeles',     34.05, -118.24),
    ('CISO_SJ',    'CISO', 'San Jose',        37.34, -121.89),
    ('CISO_SAC',   'CISO', 'Sacramento',      38.58, -121.49),
    ('CISO_FRE',   'CISO', 'Fresno',          36.74, -119.79)
) AS s (station_id, region_code, city, latitude, longitude)
JOIN grid.dim_region r ON r.region_code = s.region_code   -- skip regions not loaded yet
ON CONFLICT (station_id) DO NOTHING;

-- Hourly observations; future hours are forecasts and get overwritten
-- with newer values on every run until they're in the past.
CREATE TABLE IF NOT EXISTS grid.fact_weather_hourly (
    station_id     TEXT        NOT NULL REFERENCES grid.dim_weather_station,
    period_utc     TIMESTAMPTZ NOT NULL,
    temperature_f  NUMERIC(5,1),
    humidity_pct   NUMERIC(5,1),
    is_forecast    BOOLEAN     NOT NULL,
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (station_id, period_utc)
);
CREATE INDEX IF NOT EXISTS ix_weather_period
    ON grid.fact_weather_hourly (period_utc);

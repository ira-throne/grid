-- Grid base schema
-- raw  : API responses exactly as they landed (replayable, auditable)
-- grid : cleaned dimensions and facts used for analysis

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS grid;

-- ---------------------------------------------------------------
-- DIMENSIONS
-- ---------------------------------------------------------------
-- Balancing authorities. Grid only covers CAISO; weather stations
-- reference this table.
CREATE TABLE IF NOT EXISTS grid.dim_region (
    region_code  TEXT PRIMARY KEY,                  -- e.g. CISO
    region_name  TEXT NOT NULL,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO grid.dim_region (region_code, region_name)
VALUES ('CISO', 'California Independent System Operator')
ON CONFLICT (region_code) DO NOTHING;

-- ---------------------------------------------------------------
-- ETL BOOKKEEPING
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grid.etl_watermark (
    dataset          TEXT PRIMARY KEY,
    last_period_utc  TIMESTAMPTZ,
    last_run_at      TIMESTAMPTZ,
    last_row_count   INT
);

CREATE TABLE IF NOT EXISTS grid.etl_run_log (
    run_id       BIGSERIAL PRIMARY KEY,
    dataset      TEXT        NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    window_start TIMESTAMPTZ,
    window_end   TIMESTAMPTZ,
    rows_fetched INT,
    rows_upserted INT,
    status       TEXT,
    error        TEXT
);

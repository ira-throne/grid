-- California focus: CAISO only weather stations, with roles and weights.
-- Safe to run more than once.
--
-- role   : what the station's weather is used for
--          load  = temperature where people live (drives demand)
--          solar = sunlight where the solar farms are (drives solar output)
--          wind  = wind where the turbines are (drives wind output)
-- weight : how much a load station counts when averaging temperature
--          across the region. Roughly metro population in millions,
--          so LA counts for more than Fresno. Solar and wind sites
--          are averaged evenly (weight 1).

-- ---------------------------------------------------------------
-- 1. Turn off every station outside CAISO. LADWP (City of LA) is its
--    own balancing authority, so LDWP_LA goes too. Rows stay in place
--    so old weather data keeps its foreign key; views filter on is_active.
-- ---------------------------------------------------------------
UPDATE grid.dim_weather_station
SET is_active = FALSE
WHERE region_code <> 'CISO' AND is_active;

-- ---------------------------------------------------------------
-- 2. Role and weight columns
-- ---------------------------------------------------------------
ALTER TABLE grid.dim_weather_station
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'load',
    ADD COLUMN IF NOT EXISTS weight NUMERIC(4,2) NOT NULL DEFAULT 1;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'dim_weather_station_role_check') THEN
        ALTER TABLE grid.dim_weather_station
            ADD CONSTRAINT dim_weather_station_role_check
            CHECK (role IN ('load', 'solar', 'wind'));
    END IF;
END $$;

-- ---------------------------------------------------------------
-- 3. Stations: insert new ones, and set role and weight on all CAISO
--    stations (existing ones included) so rerunning picks up changes.
--    Load weights = approximate metro population, millions, CAISO
--    served share only (LA metro minus the City of LA's LADWP area).
-- ---------------------------------------------------------------
INSERT INTO grid.dim_weather_station
    (station_id, region_code, city, latitude, longitude, role, weight)
VALUES
    ('CISO_LA',  'CISO', 'Los Angeles',       34.05, -118.24, 'load',  6.00),
    ('CISO_RIV', 'CISO', 'Riverside',         33.95, -117.40, 'load',  4.60),
    ('CISO_SD',  'CISO', 'San Diego',         32.72, -117.16, 'load',  3.30),
    ('CISO_SJ',  'CISO', 'San Jose',          37.34, -121.89, 'load',  2.00),
    ('CISO_SAC', 'CISO', 'Sacramento',        38.58, -121.49, 'load',  2.40),
    ('CISO_FRE', 'CISO', 'Fresno',            36.74, -119.79, 'load',  1.00),
    ('CISO_MOJ', 'CISO', 'Mojave (Kern solar)', 35.05, -118.17, 'solar', 1.00),
    ('CISO_BLY', 'CISO', 'Blythe',            33.61, -114.60, 'solar', 1.00),
    ('CISO_IMP', 'CISO', 'Imperial Valley',   32.85, -115.57, 'solar', 1.00),
    ('CISO_TEH', 'CISO', 'Tehachapi Pass',    35.13, -118.45, 'wind',  1.00),
    ('CISO_ALT', 'CISO', 'Altamont Pass',     37.74, -121.65, 'wind',  1.00),
    ('CISO_SGP', 'CISO', 'San Gorgonio Pass', 33.92, -116.60, 'wind',  1.00)
ON CONFLICT (station_id) DO UPDATE SET
    role      = EXCLUDED.role,
    weight    = EXCLUDED.weight,
    is_active = TRUE;

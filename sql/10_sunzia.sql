-- SunZia: New Mexico wind delivered to CAISO. Run after 09. Safe to run more than once.
--
-- SunZia (about 3,650 MW, the largest wind farm in the US) started
-- producing in April 2026 and reached commercial operation in June. About
-- 2,100 MW of it is delivered to Southern California and counted in
-- CAISO's wind totals, so from spring 2026 a big share of "California
-- wind" depends on New Mexico weather. These stations give the wind model
-- that weather. role = 'wind_remote' keeps them out of the California
-- wind average.

ALTER TABLE grid.dim_weather_station DROP CONSTRAINT IF EXISTS dim_weather_station_role_check;
ALTER TABLE grid.dim_weather_station
    ADD CONSTRAINT dim_weather_station_role_check
    CHECK (role IN ('load', 'solar', 'wind', 'wind_remote'));

INSERT INTO grid.dim_weather_station
    (station_id, region_code, city, latitude, longitude, role, weight)
VALUES
    ('CISO_NM_S', 'CISO', 'SunZia south (Corona, NM)',       34.25, -105.60, 'wind_remote', 1.00),
    ('CISO_NM_N', 'CISO', 'SunZia north (San Miguel Co., NM)', 34.85, -105.10, 'wind_remote', 1.00)
ON CONFLICT (station_id) DO UPDATE SET
    role = EXCLUDED.role, weight = EXCLUDED.weight, is_active = TRUE;

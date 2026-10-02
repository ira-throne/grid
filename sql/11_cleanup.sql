-- One time cleanup: removes what the old US version used and the
-- California version replaced. Run once on a database that ran the US
-- version (the laptop and the server). Safe to run more than once.

-- Views that read the EIA tables
DROP VIEW IF EXISTS grid.v_region_hourly;
DROP VIEW IF EXISTS grid.v_region_temp_hourly;
DROP VIEW IF EXISTS grid.v_fuel_mix_hourly;
DROP VIEW IF EXISTS grid.v_carbon_hourly;

-- EIA data (replaced by CAISO's own hourly and 5 minute data)
DROP TABLE IF EXISTS grid.fact_region_hourly;
DROP TABLE IF EXISTS grid.fact_generation_hourly;
DROP TABLE IF EXISTS grid.fact_interchange_hourly;
DROP TABLE IF EXISTS grid.dim_fuel;
DROP TABLE IF EXISTS raw.eia_response;
DELETE FROM grid.etl_watermark WHERE dataset IN ('region', 'fuel', 'interchange');

-- Weather stations outside California, with their weather rows
DELETE FROM grid.fact_weather_hourly
 WHERE station_id IN (SELECT station_id FROM grid.dim_weather_station WHERE region_code <> 'CISO');
DELETE FROM grid.fact_weather_forecast
 WHERE station_id IN (SELECT station_id FROM grid.dim_weather_station WHERE region_code <> 'CISO');
DELETE FROM grid.dim_weather_station WHERE region_code <> 'CISO';

-- Regions other than CAISO
DELETE FROM grid.dim_region WHERE region_code <> 'CISO';

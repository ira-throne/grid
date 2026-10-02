-- Views the California dashboard reads. Run after 08_forecast.sql.
-- Safe to run more than once.

-- ---------------------------------------------------------------
-- RIGHT NOW: 5 minute supply mix with a clean share and an estimated
-- carbon intensity.
--   clean   = solar, wind, geothermal, hydro, nuclear, biomass, biogas
--   carbon  = gas, coal and imports. CAISO doesn't say what imports are
--             made of; 428 kg/MWh is California's (CARB) default factor
--             for "unspecified" imports. Gas and coal use the same
--             US average factors (440 and 1,043 kg/MWh). An estimate, labeled as such.
-- Battery discharge counts toward supply but neither clean nor dirty
-- (it's stored energy); charging (negative) is left out of supply.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW grid.v_outlook_5min AS
SELECT
    o.*,
    s.supply_mw,
    ROUND(100 * s.clean_mw / NULLIF(s.supply_mw, 0), 1)              AS clean_pct,
    ROUND(100 * (COALESCE(o.solar_mw, 0) + COALESCE(o.wind_mw, 0))
          / NULLIF(o.demand_mw, 0), 1)                               AS solar_wind_pct_of_demand,
    ROUND((COALESCE(o.natural_gas_mw, 0) * 440 + COALESCE(o.coal_mw, 0) * 1043
           + GREATEST(COALESCE(o.imports_mw, 0), 0) * 428)
          / NULLIF(s.supply_mw, 0))                                  AS est_kg_co2_per_mwh
FROM grid.fact_caiso_outlook_5min o
CROSS JOIN LATERAL (
    SELECT
        GREATEST(COALESCE(o.solar_mw, 0), 0) + COALESCE(o.wind_mw, 0) + COALESCE(o.geothermal_mw, 0)
          + COALESCE(o.small_hydro_mw, 0) + COALESCE(o.large_hydro_mw, 0) + COALESCE(o.nuclear_mw, 0)
          + COALESCE(o.biomass_mw, 0) + COALESCE(o.biogas_mw, 0)                    AS clean_mw,
        GREATEST(COALESCE(o.solar_mw, 0), 0) + COALESCE(o.wind_mw, 0) + COALESCE(o.geothermal_mw, 0)
          + COALESCE(o.small_hydro_mw, 0) + COALESCE(o.large_hydro_mw, 0) + COALESCE(o.nuclear_mw, 0)
          + COALESCE(o.biomass_mw, 0) + COALESCE(o.biogas_mw, 0) + COALESCE(o.natural_gas_mw, 0)
          + COALESCE(o.coal_mw, 0) + COALESCE(o.other_mw, 0)
          + GREATEST(COALESCE(o.batteries_mw, 0), 0) + GREATEST(COALESCE(o.imports_mw, 0), 0) AS supply_mw
) s
WHERE o.solar_mw IS NOT NULL;   -- fuel mix rows (demand.csv also carries future forecast rows)

-- ---------------------------------------------------------------
-- CURTAILMENT AND NEGATIVE PRICES, one row per Pacific day.
--   curtailed_*_mwh      energy switched off
--   solar_curtailed_pct  share of available solar thrown away
--                        (curtailed / (produced + curtailed))
--   negative_hours       hours of SP15 real time price below $0
--                        (5 minute intervals / 12)
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW grid.v_curtailment_daily AS
WITH c AS (
    SELECT (period_utc AT TIME ZONE 'America/Los_Angeles')::date AS day,
           SUM(mwh) FILTER (WHERE fuel = 'solar')          AS curtailed_solar_mwh,
           SUM(mwh) FILTER (WHERE fuel = 'wind')           AS curtailed_wind_mwh,
           SUM(mwh) FILTER (WHERE reason = 'economic')     AS economic_mwh,
           SUM(mwh) FILTER (WHERE scope = 'local')         AS local_mwh,
           SUM(mwh) FILTER (WHERE scope = 'system')        AS system_mwh
    FROM grid.fact_caiso_curtailment
    GROUP BY 1
), s AS (
    SELECT (period_utc AT TIME ZONE 'America/Los_Angeles')::date AS day,
           SUM(mw) AS solar_mwh
    FROM grid.fact_caiso_hourly
    WHERE series = 'solar' AND market_run_id = 'ACTUAL'
    GROUP BY 1
), p AS (
    SELECT (interval_start_utc AT TIME ZONE 'America/Los_Angeles')::date AS day,
           ROUND(COUNT(*) FILTER (WHERE lmp < 0) / 12.0, 1) AS negative_hours,
           MIN(lmp)                                          AS min_price,
           ROUND(AVG(lmp), 2)                                AS avg_price
    FROM grid.fact_caiso_lmp_5min
    WHERE node_id = 'TH_SP15_GEN-APND'
    GROUP BY 1
)
SELECT COALESCE(c.day, p.day) AS day,
       c.curtailed_solar_mwh, c.curtailed_wind_mwh, c.economic_mwh, c.local_mwh, c.system_mwh,
       s.solar_mwh,
       ROUND(100 * c.curtailed_solar_mwh / NULLIF(s.solar_mwh + c.curtailed_solar_mwh, 0), 2)
           AS solar_curtailed_pct,
       p.negative_hours, p.min_price, p.avg_price
FROM c
FULL JOIN p ON p.day = c.day
LEFT JOIN s ON s.day = COALESCE(c.day, p.day);

-- Hour by hour: how much gets curtailed, and how often the price is
-- negative, by hour of the Pacific day over the last 90 days. Shows that
-- both pile up in the same midday hours.
CREATE OR REPLACE VIEW grid.v_curtailment_by_hour AS
WITH c AS (
    SELECT EXTRACT(HOUR FROM period_utc AT TIME ZONE 'America/Los_Angeles')::int AS hour,
           SUM(mwh) / COUNT(DISTINCT (period_utc AT TIME ZONE 'America/Los_Angeles')::date)
               AS avg_curtailed_mwh
    FROM grid.fact_caiso_curtailment
    WHERE period_utc >= now() - interval '90 days'
    GROUP BY 1
), p AS (
    SELECT EXTRACT(HOUR FROM interval_start_utc AT TIME ZONE 'America/Los_Angeles')::int AS hour,
           ROUND(100.0 * COUNT(*) FILTER (WHERE lmp < 0) / COUNT(*), 1) AS negative_pct
    FROM grid.fact_caiso_lmp_5min
    WHERE node_id = 'TH_SP15_GEN-APND' AND interval_start_utc >= now() - interval '90 days'
    GROUP BY 1
)
SELECT h.hour, ROUND(COALESCE(c.avg_curtailed_mwh, 0), 1) AS avg_curtailed_mwh,
       COALESCE(p.negative_pct, 0) AS negative_pct
FROM generate_series(0, 23) AS h(hour)
LEFT JOIN c USING (hour)
LEFT JOIN p USING (hour);

-- ---------------------------------------------------------------
-- WHEN TO PLUG IN: every hour from now to the end of tomorrow with the
-- forecast solar + wind share of demand and the day ahead price in the
-- SCE load area (most of Southern California outside the City of LA).
-- Uses our model's forecast where it exists, CAISO's otherwise.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW grid.v_plugin_hours AS
WITH hours AS (
    SELECT generate_series(date_trunc('hour', now()),
                           (date_trunc('day', now() AT TIME ZONE 'America/Los_Angeles')
                              + interval '2 days') AT TIME ZONE 'America/Los_Angeles'
                             - interval '1 hour',
                           interval '1 hour') AS period_utc
), ours AS (
    SELECT DISTINCT ON (r.series, v.period_utc) r.series, v.period_utc, v.mw
    FROM grid.forecast_run r
    JOIN grid.forecast_value v USING (run_id)
    WHERE r.kind = 'live' AND r.model = 'gbm' AND v.period_utc >= date_trunc('hour', now())
    ORDER BY r.series, v.period_utc, r.issued_at DESC
), fc AS (
    SELECT h.period_utc, s.series,
           COALESCE(o.mw, c.mw)                           AS mw,
           CASE WHEN o.mw IS NOT NULL THEN 'Grid model'
                WHEN c.mw IS NOT NULL THEN 'CAISO' END    AS source
    FROM hours h
    CROSS JOIN (VALUES ('demand'), ('solar'), ('wind')) AS s(series)
    LEFT JOIN ours o ON o.series = s.series AND o.period_utc = h.period_utc
    LEFT JOIN grid.fact_caiso_hourly c
           ON c.series = s.series AND c.period_utc = h.period_utc AND c.market_run_id = 'DAM'
)
SELECT h.period_utc,
       h.period_utc AT TIME ZONE 'America/Los_Angeles'                    AS period_local,
       MAX(fc.mw) FILTER (WHERE fc.series = 'demand')                     AS demand_mw,
       MAX(fc.mw) FILTER (WHERE fc.series = 'solar')                      AS solar_mw,
       MAX(fc.mw) FILTER (WHERE fc.series = 'wind')                       AS wind_mw,
       ROUND(100 * (MAX(fc.mw) FILTER (WHERE fc.series = 'solar')
                    + MAX(fc.mw) FILTER (WHERE fc.series = 'wind'))
             / NULLIF(MAX(fc.mw) FILTER (WHERE fc.series = 'demand'), 0), 1) AS renewable_pct,
       CASE WHEN BOOL_AND(fc.source = 'Grid model') THEN 'Grid model'
            WHEN BOOL_OR(fc.source IS NOT NULL)      THEN 'CAISO' END      AS source,
       p.lmp                                                              AS price_mwh
FROM hours h
LEFT JOIN fc USING (period_utc)
LEFT JOIN grid.fact_caiso_lmp_dam p
       ON p.period_utc = h.period_utc AND p.node_id = 'DLAP_SCE-APND'
GROUP BY h.period_utc, p.lmp;

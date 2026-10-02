"""Every SQL query the dashboard runs, one function per chart or tile.

Times come back from Postgres as UTC. Each query converts them to the
display time zone in SQL (AT TIME ZONE) so pandas and plotly never have
to guess. Anything a visitor can choose (node type, series) always goes
through %(name)s params, never f-strings.
"""

from __future__ import annotations

import pandas as pd

from .db import query

DISPLAY_TZ = "America/Los_Angeles"


# ---------------------------------------------------------------
# Right now (CAISO Today's Outlook, 5 minute)
# ---------------------------------------------------------------
def outlook_today() -> pd.DataFrame:
    """Supply mix and demand over the last 24 hours of data."""
    return query(
        """
        SELECT interval_start_utc AT TIME ZONE %(tz)s AS interval_local, *
        FROM grid.v_outlook_5min
        WHERE interval_start_utc >= (SELECT MAX(interval_start_utc) FROM grid.v_outlook_5min)
                                    - interval '24 hours'
        ORDER BY interval_start_utc
        """,
        {"tz": DISPLAY_TZ},
    )


def outlook_latest() -> dict:
    df = query(
        """
        SELECT interval_start_utc AT TIME ZONE %(tz)s AS interval_local,
               o.demand_mw, d.da_forecast_mw, o.solar_mw, o.wind_mw, o.clean_pct,
               o.solar_wind_pct_of_demand, o.est_kg_co2_per_mwh, o.batteries_mw
        FROM grid.v_outlook_5min o
        LEFT JOIN grid.fact_caiso_outlook_5min d USING (interval_start_utc)
        WHERE o.demand_mw IS NOT NULL
        ORDER BY interval_start_utc DESC LIMIT 1
        """,
        {"tz": DISPLAY_TZ},
    )
    return df.iloc[0].to_dict() if len(df) else {}


# ---------------------------------------------------------------
# When to plug in
# ---------------------------------------------------------------
def plugin_hours() -> pd.DataFrame:
    return query(
        """
        SELECT period_local, demand_mw, solar_mw, wind_mw, renewable_pct, source, price_mwh
        FROM grid.v_plugin_hours
        ORDER BY period_utc
        """
    )


# ---------------------------------------------------------------
# Forecast vs actual
# ---------------------------------------------------------------
def forecast_compare(series: str, days_back: int = 7) -> pd.DataFrame:
    """Actual, our forecast and CAISO's, for the last week and tomorrow.
    Live forecasts where they exist; backtest forecasts fill earlier days."""
    return query(
        """
        WITH hours AS (
            SELECT DISTINCT ON (period_utc) *
            FROM grid.v_forecast_compare
            WHERE series = %(series)s
              AND period_utc >= now() - make_interval(days => %(days)s)
            ORDER BY period_utc, (kind = 'live') DESC
        ), dam AS (   -- CAISO's forecast exists even for hours we haven't forecast
            SELECT period_utc, mw AS caiso_mw
            FROM grid.fact_caiso_hourly
            WHERE series = %(series)s AND market_run_id = 'DAM'
              AND period_utc >= now() - make_interval(days => %(days)s)
        ), act AS (
            SELECT period_utc, mw AS actual_mw
            FROM grid.fact_caiso_hourly
            WHERE series = %(series)s AND market_run_id = 'ACTUAL'
              AND period_utc >= now() - make_interval(days => %(days)s)
        )
        SELECT d.period_utc AT TIME ZONE %(tz)s AS period_local,
               a.actual_mw, h.gbm_mw, d.caiso_mw, h.kind
        FROM dam d
        LEFT JOIN act a USING (period_utc)
        LEFT JOIN hours h USING (period_utc)
        ORDER BY d.period_utc
        """,
        {"tz": DISPLAY_TZ, "series": series, "days": days_back},
    )


def scorecard(series: str) -> tuple[pd.DataFrame, str]:
    """Live scores once there are two weeks of them, backtest before that."""
    df = query(
        """
        SELECT kind, model, hours, mae_mw, nmae_pct, bias_mw,
               first_hour_utc AT TIME ZONE %(tz)s AS first_local,
               last_hour_utc AT TIME ZONE %(tz)s AS last_local
        FROM grid.v_forecast_scorecard
        WHERE series = %(series)s
        ORDER BY kind, model_order
        """,
        {"tz": DISPLAY_TZ, "series": series},
    )
    live = df[df["kind"] == "live"]
    if len(live) and live["hours"].min() >= 14 * 24:
        return live, "live"
    return df[df["kind"] == "backtest"], "backtest"


# ---------------------------------------------------------------
# Curtailment and negative prices
# ---------------------------------------------------------------
def curtailment_daily(days: int = 120) -> pd.DataFrame:
    return query(
        """
        SELECT day, curtailed_solar_mwh, curtailed_wind_mwh, solar_curtailed_pct,
               negative_hours, min_price
        FROM grid.v_curtailment_daily
        WHERE day >= (now() AT TIME ZONE %(tz)s)::date - %(days)s
        ORDER BY day
        """,
        {"tz": DISPLAY_TZ, "days": days},
    )


def curtailment_by_hour() -> pd.DataFrame:
    return query("SELECT * FROM grid.v_curtailment_by_hour ORDER BY hour")


def curtailment_summary(days: int = 30) -> dict:
    df = query(
        """
        SELECT SUM(COALESCE(curtailed_solar_mwh, 0) + COALESCE(curtailed_wind_mwh, 0)) AS curtailed_mwh,
               ROUND(100 * SUM(curtailed_solar_mwh)
                     / NULLIF(SUM(solar_mwh) + SUM(curtailed_solar_mwh), 0), 1)     AS solar_curtailed_pct,
               SUM(negative_hours)                                                   AS negative_hours,
               MIN(min_price)                                                        AS min_price,
               MAX(day) FILTER (WHERE curtailed_solar_mwh IS NOT NULL)               AS latest_day
        FROM grid.v_curtailment_daily
        WHERE day >= (now() AT TIME ZONE %(tz)s)::date - %(days)s
        """,
        {"tz": DISPLAY_TZ, "days": days},
    )
    return df.iloc[0].to_dict() if len(df) else {}


# ---------------------------------------------------------------
# CAISO prices (5 minute)
# ---------------------------------------------------------------
def caiso_prices(node_type: str, hours: int = 24) -> pd.DataFrame:
    """LMP per node for the last N hours. node_type is 'HUB' or 'DLAP'."""
    return query(
        """
        SELECT n.node_id,
               n.description,
               p.interval_start_utc AT TIME ZONE %(tz)s AS interval_local,
               p.lmp, p.energy, p.congestion, p.loss
        FROM grid.fact_caiso_lmp_5min p
        JOIN grid.dim_caiso_node n USING (node_id)
        WHERE n.node_type = %(node_type)s
          AND n.is_active
          AND p.interval_start_utc >= now() - make_interval(hours => %(hours)s)
        ORDER BY n.node_id, p.interval_start_utc
        """,
        {"tz": DISPLAY_TZ, "node_type": node_type, "hours": hours},
    )


def caiso_latest(node_type: str) -> pd.DataFrame:
    """Latest price per node, plus the price one hour earlier for the change."""
    return query(
        """
        WITH latest AS (
            SELECT DISTINCT ON (node_id) node_id, interval_start_utc, lmp
            FROM grid.fact_caiso_lmp_5min
            ORDER BY node_id, interval_start_utc DESC
        )
        SELECT n.node_id, n.description,
               l.interval_start_utc AT TIME ZONE %(tz)s AS interval_local,
               l.lmp,
               prev.lmp AS lmp_1h_ago
        FROM latest l
        JOIN grid.dim_caiso_node n USING (node_id)
        LEFT JOIN grid.fact_caiso_lmp_5min prev
               ON prev.node_id = l.node_id
              AND prev.interval_start_utc = l.interval_start_utc - interval '1 hour'
        WHERE n.node_type = %(node_type)s AND n.is_active
        ORDER BY n.node_id
        """,
        {"tz": DISPLAY_TZ, "node_type": node_type},
    )


def dam_prices(node_type: str) -> pd.DataFrame:
    """Day ahead hourly prices from 24 hours ago through tomorrow."""
    return query(
        """
        SELECT p.node_id, p.period_utc AT TIME ZONE %(tz)s AS period_local, p.lmp
        FROM grid.fact_caiso_lmp_dam p
        JOIN grid.dim_caiso_node n USING (node_id)
        WHERE n.node_type = %(node_type)s AND n.is_active
          AND p.period_utc >= now() - interval '24 hours'
        ORDER BY p.node_id, p.period_utc
        """,
        {"tz": DISPLAY_TZ, "node_type": node_type},
    )


# ---------------------------------------------------------------
# Pipeline health
# ---------------------------------------------------------------
def pipeline_health() -> pd.DataFrame:
    return query("SELECT * FROM grid.v_pipeline_health ORDER BY dataset")


def latest_forecast_run() -> dict:
    df = query(
        """
        SELECT MAX(target_date) AS target_date, MAX(issued_at) AS issued_at
        FROM grid.forecast_run WHERE kind = 'live' AND model = 'gbm'
        """
    )
    return df.iloc[0].to_dict() if len(df) else {}

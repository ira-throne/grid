"""Build the hourly table the models learn from.

One row per hour (UTC hour start). For each hour the columns are only
things that would have been known the morning before, when the
forecast is made:

  weather     the forecast for that hour made about a day earlier
              (grid.fact_weather_forecast, lead_days = 1), averaged over
              the stations whose role matters for each series
  calendar    hour of day, day of week, time of year, holidays (Pacific)
  lags        the same series 2 days and 7 days earlier. Not 1 day:
              "24 hours before tomorrow 3 pm" is today 3 pm, which hasn't
              happened yet at 9 am. 48 hours is the shortest lag that is
              known for every hour of tomorrow.
  capacity    for solar: the highest output at that hour over the previous
              two weeks; for wind: the highest output at any hour over
              the previous 60 days (both lagged 2 days). California keeps
              adding solar and wind, and tree models can't extrapolate
              beyond values they've seen, so these tell the model "how
              big the fleet is lately".

Leakage is the thing to watch in any forecasting project: one feature
that sneaks in information from after the forecast was made makes the
backtest look great and the live forecast bad. Every lag here is at
least 48 hours and the weather is a stored forecast, never an observation.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

PACIFIC = "America/Los_Angeles"
SERIES = ["demand", "solar", "wind"]

WEATHER_SQL = """
-- Open-Meteo labels each hour with its END (13:00 = average of 12:00 to 13:00),
-- while CAISO labels with the START. Shifting back one hour lines them up.
SELECT f.period_utc - interval '1 hour' AS period_utc,
       SUM(f.temperature_f * s.weight) FILTER (WHERE s.role = 'load')
         / NULLIF(SUM(s.weight) FILTER (WHERE s.role = 'load' AND f.temperature_f IS NOT NULL), 0)
                                                               AS temp_f,
       SUM(f.humidity_pct * s.weight) FILTER (WHERE s.role = 'load')
         / NULLIF(SUM(s.weight) FILTER (WHERE s.role = 'load' AND f.humidity_pct IS NOT NULL), 0)
                                                               AS humidity_pct,
       MAX(f.temperature_f) FILTER (WHERE s.role = 'load')     AS temp_max_city_f,
       AVG(f.shortwave_wm2)   FILTER (WHERE s.role = 'solar')  AS solar_wm2,
       AVG(f.cloud_cover_pct) FILTER (WHERE s.role = 'solar')  AS solar_cloud_pct,
       AVG(f.shortwave_wm2)   FILTER (WHERE s.role = 'load')   AS city_wm2,
       AVG(f.wind_80m_ms)     FILTER (WHERE s.role = 'wind')   AS wind_ms,
       MAX(f.wind_80m_ms)     FILTER (WHERE s.role = 'wind')   AS wind_max_ms,
       AVG(f.wind_80m_ms)     FILTER (WHERE s.role = 'wind_remote') AS nm_wind_ms
FROM grid.fact_weather_forecast f
JOIN grid.dim_weather_station s USING (station_id)
WHERE s.is_active AND f.lead_days = 1
  AND f.period_utc >  %(start)s
  AND f.period_utc <= %(end)s + interval '1 hour'
GROUP BY 1
"""

ACTUALS_SQL = """
SELECT period_utc,
       MAX(mw) FILTER (WHERE series = 'demand') AS demand,
       MAX(mw) FILTER (WHERE series = 'solar')  AS solar,
       MAX(mw) FILTER (WHERE series = 'wind')   AS wind
FROM grid.fact_caiso_hourly
WHERE market_run_id = 'ACTUAL'
  AND period_utc >= %(start)s - interval '30 days'
  AND period_utc <  %(end)s
GROUP BY period_utc
"""

FEATURES = {
    "demand": ["hour", "dow", "is_holiday", "doy_sin", "doy_cos",
               "temp_f", "cooling_deg", "heating_deg", "humidity_pct", "temp_max_city_f",
               "city_wm2", "solar_wm2",
               "lag48", "lag168", "lag48_day_mean"],
    "solar": ["hour", "doy_sin", "doy_cos",
              "solar_wm2", "solar_cloud_pct", "city_wm2",
              "cap_proxy", "lag48", "lag168"],
    "wind": ["hour", "doy_sin", "doy_cos",
             "wind_ms", "wind_max_ms", "wind_cubed", "nm_wind_ms", "nm_wind_cubed", "temp_f",
             "fleet_cap", "lag48", "lag168"],
}


def build(conn, start: datetime, end: datetime) -> pd.DataFrame:
    """Hourly features and actuals for [start, end). Times are UTC."""
    from .db import frame

    params = {"start": start, "end": end}
    weather = frame(conn, WEATHER_SQL, params)
    actuals = frame(conn, ACTUALS_SQL, params)

    # A complete hourly index makes shift(48) mean "48 hours earlier"
    # even when a few hours are missing from the data.
    index = pd.date_range(pd.Timestamp(start).tz_convert("UTC") - pd.Timedelta(days=30),
                          pd.Timestamp(end).tz_convert("UTC"), freq="h", inclusive="left",
                          name="period_utc")
    df = pd.DataFrame(index=index)
    for part in (weather, actuals):
        if len(part):
            part["period_utc"] = pd.to_datetime(part["period_utc"], utc=True)
            df = df.join(part.set_index("period_utc"))
    for col in ["temp_f", "humidity_pct", "temp_max_city_f", "solar_wm2", "solar_cloud_pct",
                "city_wm2", "wind_ms", "wind_max_ms", "nm_wind_ms", *SERIES]:
        if col not in df:
            df[col] = np.nan

    # Calendar, in Pacific time because that's the clock people live by
    local = df.index.tz_convert(PACIFIC)
    df["hour"] = local.hour
    df["dow"] = local.dayofweek
    doy = local.dayofyear
    df["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    df["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)
    holidays = USFederalHolidayCalendar().holidays(local.min().tz_localize(None),
                                                   local.max().tz_localize(None))
    df["is_holiday"] = pd.Index(local.tz_localize(None).normalize()).isin(holidays).astype(int)

    # Weather shapes that relate better to the target than raw values
    df["cooling_deg"] = (df["temp_f"] - 65).clip(lower=0)   # air conditioning
    df["heating_deg"] = (55 - df["temp_f"]).clip(lower=0)   # heating
    df["wind_cubed"] = df["wind_ms"] ** 3                   # turbine power ~ wind speed cubed
    df["nm_wind_cubed"] = df["nm_wind_ms"] ** 3             # SunZia, New Mexico (sql/10_sunzia.sql)

    df["_local_day"] = local.tz_localize(None).normalize()
    return df


def add_lags(df: pd.DataFrame, series: str) -> pd.DataFrame:
    """Lag and capacity features for one series (all at least 48 hours old)."""
    out = df.copy()
    y = out[series]
    out["lag48"] = y.shift(48)
    out["lag168"] = y.shift(168)
    # Mean over the Pacific day that was 2 days before: overall level lately
    day_mean = y.groupby(out["_local_day"]).transform("mean")
    out["lag48_day_mean"] = day_mean.shift(48)
    # Highest value at this hour over the 14 days ending 2 days ago
    by_hour = y.shift(48).groupby(out["hour"])
    out["cap_proxy"] = by_hour.transform(lambda s: s.rolling(14, min_periods=3).max())
    # Highest hourly value at any hour over the 60 days ending 2 days ago:
    # a rough measure of fleet size, used to scale wind (see model.py)
    out["fleet_cap"] = y.shift(48).rolling(24 * 60, min_periods=24 * 7).max()
    return out


def training_frame(conn, start: datetime, end: datetime, series: str) -> pd.DataFrame:
    df = add_lags(build(conn, start, end), series)
    df = df[df.index >= pd.Timestamp(start).tz_convert("UTC")]
    return df

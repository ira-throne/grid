"""Grid web dashboard: California's electricity grid.

Run from the project root:
    .venv/bin/python -m dashboard.app
then open http://127.0.0.1:8050

How it works: the layout below is a tree of HTML and chart components,
each with an id. Callbacks (the @app.callback functions) say "when these
inputs change, recompute these outputs". Two dcc.Interval timers act as
inputs that tick on their own, which is what makes the page live: 5
minute data refreshes every minute, hourly data every five minutes.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import pandas as pd
from dash import Dash, Input, Output, dcc, html

from . import charts, queries

TZ = ZoneInfo(queries.DISPLAY_TZ)
FAST_MS = 60 * 1000
SLOW_MS = 5 * 60 * 1000
GRAPH_CONFIG = {"displayModeBar": False, "responsive": True}
WINDOW_HOURS = 3     # length of the recommended plug in window

# Expected minutes between runs, for the pipeline status chips.
CADENCE_MIN = {"caiso_lmp": 5, "caiso_lmp_dam": 60, "caiso_hourly": 60, "caiso_outlook": 5,
               "caiso_curtailment": 24 * 60, "weather": 60, "forecast": 24 * 60}
CHIP_GROUPS = {"Prices": ["caiso_lmp", "caiso_lmp_dam"],
               "Grid data": ["caiso_outlook", "caiso_hourly"],
               "Curtailment": ["caiso_curtailment"],
               "Weather": ["weather"],
               "Forecast": ["forecast"]}

# Where the dashboard lives in the URL. "/" on the laptop; on the server
# .env sets URL_BASE_PATH=/grid/ so the site is at ira-t.net/grid/.
BASE_PATH = os.environ.get("URL_BASE_PATH", "/")

app = Dash(__name__, title="Grid", url_base_pathname=BASE_PATH)
server = app.server  # what gunicorn imports


def now_local() -> pd.Timestamp:
    return pd.Timestamp.now(tz=TZ).tz_localize(None)


# ---------------------------------------------------------------
# Small layout helpers
# ---------------------------------------------------------------
def tile(label: str, value: str, sub: str = "") -> html.Div:
    return html.Div(className="tile", children=[
        html.Div(label, className="tile-label"),
        html.Div(value, className="tile-value"),
        html.Div(sub, className="tile-sub"),
    ])


def section(title: str, subtitle, *children) -> html.Section:
    return html.Section(className="card", children=[
        html.H2(title), html.P(subtitle, className="card-sub"), *children,
    ])


def fmt(v, pattern: str, missing: str = "n/a") -> str:
    return missing if v is None or pd.isna(v) else pattern.format(v)


def money(v) -> str:
    if v is None or pd.isna(v):
        return "n/a"
    return f"{'-' if v < 0 else ''}${abs(v):,.0f}/MWh"


def hour_label(t: pd.Timestamp) -> str:
    return t.strftime("%-I %p").replace(":00", "")


def best_window(df: pd.DataFrame, metric: str, highest: bool) -> pd.DataFrame:
    """The WINDOW_HOURS consecutive hours with the best average of metric."""
    s = df.set_index("period_local")[metric].dropna()
    if len(s) < WINDOW_HOURS:
        return df.iloc[0:0]
    rolling = s.rolling(WINDOW_HOURS).mean()
    end = rolling.idxmax() if highest else rolling.idxmin()
    pos = s.index.get_loc(end)
    hours = s.index[pos - WINDOW_HOURS + 1: pos + 1]
    return df[df["period_local"].isin(hours)]


# ---------------------------------------------------------------
# Layout
# ---------------------------------------------------------------
app.layout = html.Div(className="page", children=[
    dcc.Interval(id="tick-fast", interval=FAST_MS),
    dcc.Interval(id="tick-slow", interval=SLOW_MS),

    html.Header(className="header", children=[
        html.Div([
            html.A("← ira-t.net", href="/", className="home-link") if BASE_PATH != "/" else None,
            html.H1("Grid"),
            html.P("California's electricity grid, live, with a day ahead forecast that's "
                   "scored in public against CAISO's own. Times are Pacific.", className="lede"),
        ]),
        html.Div(id="pipeline-status", className="status-row"),
    ]),

    section(
        "Right now",
        "Where California's power has come from over the last 24 hours, every 5 minutes.",
        html.Div(id="now-tiles", className="tiles"),
        dcc.Graph(id="supply-chart", config=GRAPH_CONFIG),
    ),

    section(
        "When to plug in",
        ["The best hours in the next day and a half to charge an EV, run the dryer or "
         "the dishwasher, for Southern California Edison customers. ",
         html.Span("The City of Los Angeles (LADWP) runs its own grid and isn't covered.",
                   className="muted")],
        dcc.RadioItems(
            id="plugin-metric", className="toggle", inline=True, value="renewable_pct",
            options=[{"label": "Cleanest (most solar and wind)", "value": "renewable_pct"},
                     {"label": "Cheapest wholesale price", "value": "price_mwh"}],
        ),
        html.Div(id="plugin-callout", className="callout"),
        dcc.Graph(id="plugin-chart", config=GRAPH_CONFIG),
        html.P(id="plugin-note", className="note"),
    ),

    section(
        "Day ahead forecast",
        "Every morning at 9 the Grid model forecasts tomorrow hour by hour from weather "
        "forecasts and recent history. The first forecast made is locked in and graded "
        "against what happened, next to CAISO's own day ahead forecast.",
        dcc.RadioItems(
            id="fc-series", className="toggle", inline=True, value="demand",
            options=[{"label": "Demand", "value": "demand"},
                     {"label": "Solar", "value": "solar"},
                     {"label": "Wind", "value": "wind"}],
        ),
        dcc.Graph(id="fc-chart", config=GRAPH_CONFIG),
        html.H3(id="score-title"),
        html.Div(id="score-table"),
        html.P("Average miss is in MW. Miss % is the total miss as a share of the total "
               "actual, which works for solar too (a plain percentage error divides by zero "
               "at night). Bias above zero means the forecast ran high.", className="note"),
    ),

    section(
        "Curtailment and negative prices",
        "On sunny afternoons California often makes more solar power than it can use or "
        "export. Prices go below zero, and CAISO has solar and wind farms switch off "
        "(curtailment).",
        html.Div(id="curt-tiles", className="tiles"),
        html.H3("Curtailed per day"),
        dcc.Graph(id="curt-daily", config=GRAPH_CONFIG),
        html.H3("By hour of day, last 90 days"),
        dcc.Graph(id="curt-hourly", config=GRAPH_CONFIG),
    ),

    section(
        "Wholesale prices",
        "Real time price every 5 minutes (solid) and the day ahead market's hourly price "
        "through tomorrow (dotted), $/MWh.",
        dcc.RadioItems(
            id="node-type", className="toggle", inline=True, value="HUB",
            options=[{"label": "Trading hubs", "value": "HUB"},
                     {"label": "Utility load areas", "value": "DLAP"}],
        ),
        html.Div(id="price-tiles", className="tiles"),
        dcc.Graph(id="price-chart", config=GRAPH_CONFIG),
    ),

    html.Footer(className="footer", children=[
        html.P(["Data: CAISO (OASIS, Today's Outlook, daily renewable reports) and "
                "Open-Meteo. Carbon figures are estimates. ",
                html.Span(id="footer-time")]),
    ]),
])


# ---------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------
@app.callback(Output("pipeline-status", "children"), Output("footer-time", "children"),
              Input("tick-fast", "n_intervals"))
def update_status(_):
    health = queries.pipeline_health().set_index("dataset")
    chips = []
    for label, datasets in CHIP_GROUPS.items():
        rows = health.reindex(datasets)
        if rows["last_run_at"].isna().any():
            state, text = "critical", "no runs"
        elif (rows["last_status"] == "failed").any():
            state, text = "critical", "last run failed"
        elif any(rows.loc[d, "minutes_since_run"] > 2.5 * CADENCE_MIN[d] for d in datasets):
            state, text = "warning", "late"
        else:
            state, text = "good", "up to date"
        icon = {"good": "✓", "warning": "!", "critical": "✕"}[state]
        chips.append(html.Span(className=f"chip chip-{state}", children=[
            html.Span(icon, className="chip-icon"), f"{label}: {text}",
        ]))
    return chips, f"Page refreshed {now_local():%b %d, %-I:%M %p} PT."


@app.callback(Output("now-tiles", "children"), Output("supply-chart", "figure"),
              Input("tick-fast", "n_intervals"))
def update_now(_):
    s = queries.outlook_latest()
    at = s.get("interval_local")
    when = f"at {at:%-I:%M %p}" if at is not None and not pd.isna(at) else ""
    vs = ""
    if s.get("da_forecast_mw") and s.get("demand_mw") is not None and not pd.isna(s["demand_mw"]):
        vs = f"{100 * (s['demand_mw'] - s['da_forecast_mw']) / s['da_forecast_mw']:+.1f}% vs day ahead"
    batt = s.get("batteries_mw")
    batt_sub = "" if batt is None or pd.isna(batt) else \
        (f"batteries charging {abs(batt):,.0f} MW" if batt < 0 else f"batteries supplying {batt:,.0f} MW")
    tiles = [
        tile("Demand", fmt(s.get("demand_mw"), "{:,.0f} MW"), f"{when}  {vs}".strip()),
        tile("Solar + wind", fmt(s.get("solar_wind_pct_of_demand"), "{:.0f}%"), "of demand"),
        tile("Clean share", fmt(s.get("clean_pct"), "{:.0f}%"), batt_sub or "of supply"),
        tile("Carbon (est.)", fmt(s.get("est_kg_co2_per_mwh"), "{:,.0f} kg/MWh"), "CO2 per MWh supplied"),
    ]
    return tiles, charts.supply_chart(queries.outlook_today())


@app.callback(Output("plugin-callout", "children"), Output("plugin-chart", "figure"),
              Output("plugin-note", "children"),
              Input("tick-slow", "n_intervals"), Input("plugin-metric", "value"))
def update_plugin(_, metric):
    df = queries.plugin_hours()
    clean = metric == "renewable_pct"
    best = best_window(df, metric, highest=clean)
    if best.empty:
        callout = "Not enough forecast data yet."
    else:
        start, end = best["period_local"].iloc[0], best["period_local"].iloc[-1] + pd.Timedelta(hours=1)
        today = now_local().normalize()
        day = "today" if start.normalize() == today else \
              "tomorrow" if start.normalize() == today + pd.Timedelta(days=1) else f"{start:%A}"
        value = (f"{best['renewable_pct'].mean():.0f}% of demand from solar and wind" if clean
                 else f"about ${best['price_mwh'].mean():.0f}/MWh wholesale")
        callout = [html.Span("Best window: ", className="muted"),
                   html.Strong(f"{hour_label(start)} to {hour_label(end)} {day}"),
                   f", {value}."]
    sources = set(df["source"].dropna())
    basis = "the Grid model's forecast" if sources == {"Grid model"} else \
            "CAISO's forecast" if sources == {"CAISO"} else "the Grid model's and CAISO's forecasts"
    note = (f"Based on {basis} and the day ahead market. Most households don't pay the "
            "wholesale price: SCE's common time of use plans charge the most from 4 to 9 pm "
            "every day, so the cleanest hours are usually also the cheapest at home.")
    return callout, charts.plugin_chart(df, metric, list(best["period_local"]), now_local()), note


@app.callback(Output("fc-chart", "figure"), Output("score-title", "children"),
              Output("score-table", "children"),
              Input("tick-slow", "n_intervals"), Input("fc-series", "value"))
def update_forecast(_, series):
    fig = charts.forecast_chart(queries.forecast_compare(series), series, now_local())
    scores, kind = queries.scorecard(series)
    if scores.empty:
        return fig, "Scorecard", html.P("No graded forecasts yet.", className="note")
    first, last = scores["first_local"].min(), scores["last_local"].max()
    title = (f"Scorecard, last 30 days of live forecasts ({first:%b %-d} to {last:%b %-d})"
             if kind == "live" else
             f"Scorecard, backtest on held out days ({first:%b %-d, %Y} to {last:%b %-d, %Y}). "
             "Live scores replace this after two weeks of daily forecasts.")
    header = html.Tr([html.Th(c) for c in ("Forecast", "Average miss", "Miss %", "Bias", "Hours")])
    rows = [html.Tr(className="best" if r.mae_mw == scores["mae_mw"].min() else None, children=[
        html.Td(r.model), html.Td(f"{r.mae_mw:,.0f} MW"), html.Td(f"{r.nmae_pct:.1f}%"),
        html.Td(f"{r.bias_mw:+,.0f} MW"), html.Td(f"{r.hours:,}"),
    ]) for r in scores.itertuples()]
    return fig, title, html.Table(className="score", children=[html.Thead(header), html.Tbody(rows)])


@app.callback(Output("curt-tiles", "children"), Output("curt-daily", "figure"),
              Output("curt-hourly", "figure"), Input("tick-slow", "n_intervals"))
def update_curtailment(_):
    s = queries.curtailment_summary()
    latest = s.get("latest_day")
    tiles = [
        tile("Curtailed, last 30 days", fmt(s.get("curtailed_mwh"), "{:,.0f} MWh"),
             f"reports through {latest:%b %-d}" if latest is not None and not pd.isna(latest) else ""),
        tile("Solar thrown away", fmt(s.get("solar_curtailed_pct"), "{:.1f}%"), "of solar available"),
        tile("Hours below $0", fmt(s.get("negative_hours"), "{:,.0f}"), "SP15 real time, last 30 days"),
        tile("Lowest price", money(s.get("min_price")), "last 30 days"),
    ]
    return (tiles, charts.curtailment_daily_chart(queries.curtailment_daily()),
            charts.curtailment_hour_chart(queries.curtailment_by_hour()))


@app.callback(Output("price-tiles", "children"), Output("price-chart", "figure"),
              Input("tick-fast", "n_intervals"), Input("node-type", "value"))
def update_prices(_, node_type):
    latest = queries.caiso_latest(node_type)
    tiles = []
    for r in latest.itertuples():
        change = "" if pd.isna(r.lmp_1h_ago) else \
            f"{'▲' if r.lmp >= r.lmp_1h_ago else '▼'} {abs(r.lmp - r.lmp_1h_ago):.2f} vs 1 hour ago"
        tiles.append(tile(charts.NODE_LABELS.get(r.node_id, r.node_id),
                          f"{'-' if r.lmp < 0 else ''}${abs(r.lmp):.2f}/MWh",
                          f"{r.interval_local:%-I:%M %p}  {change}"))
    fig = charts.price_chart_with_dam(queries.caiso_prices(node_type),
                                      queries.dam_prices(node_type), now_local())
    return tiles, fig


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8050)),
            debug=os.environ.get("DASH_DEBUG") == "1")

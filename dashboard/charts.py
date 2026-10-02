"""Plotly figure builders. Each takes a DataFrame from queries.py and
returns a figure. No database code in here, so you can test any chart
with a hand made DataFrame.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# ---------------------------------------------------------------
# Colors. Categorical slots are used in this fixed order (an order
# checked for color blind separation), and each entity keeps its color
# no matter which other series are on screen.
# ---------------------------------------------------------------
# Dark theme: the same eight hues stepped for a dark background
# (checked for color blind separation and 3:1 contrast on SURFACE).
SLOTS = ["#3987e5", "#d95926", "#199e70", "#c98500",
         "#d55181", "#008300", "#9085e9", "#e66767"]

INK = "#f2f1ec"
INK_2 = "#b4b3ad"
MUTED = "#7d7c77"
GRID = "#2a2d35"
AXIS = "#3a3d45"
SURFACE = "#16181d"
TOOLTIP_BG = "#0e0f12"

# Price nodes: hubs and utility areas each get slots 1 to 3 in node order.
NODE_COLORS = {
    "TH_NP15_GEN-APND": SLOTS[0], "TH_SP15_GEN-APND": SLOTS[1], "TH_ZP26_GEN-APND": SLOTS[2],
    "DLAP_PGAE-APND": SLOTS[0], "DLAP_SCE-APND": SLOTS[1], "DLAP_SDGE-APND": SLOTS[2],
}
NODE_LABELS = {
    "TH_NP15_GEN-APND": "NP15 (North)", "TH_SP15_GEN-APND": "SP15 (South)",
    "TH_ZP26_GEN-APND": "ZP26 (Central)", "DLAP_PGAE-APND": "PG&E",
    "DLAP_SCE-APND": "SCE", "DLAP_SDGE-APND": "SDG&E",
}

# Supply stack (Today's Outlook columns), bottom to top. Steady sources
# at the bottom, the ones that swing through the day on top.
SUPPLY = [
    ("Nuclear", ["nuclear_mw"], SLOTS[6]),
    ("Other renewables", ["geothermal_mw", "biomass_mw", "biogas_mw", "small_hydro_mw"], SLOTS[5]),
    ("Hydro", ["large_hydro_mw"], SLOTS[0]),
    ("Wind", ["wind_mw"], SLOTS[2]),
    ("Solar", ["solar_mw"], SLOTS[3]),
    ("Batteries", ["batteries_mw"], SLOTS[7]),
    ("Gas and other", ["natural_gas_mw", "coal_mw", "other_mw"], SLOTS[1]),
    ("Imports", ["imports_mw"], SLOTS[4]),
]
SERIES_LABELS = {"demand": "Demand", "solar": "Solar", "wind": "Wind"}
ACCENT = "#2ad4c0"


def _base_layout(fig: go.Figure, y_title: str, height: int = 320) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif", size=12, color=INK_2),
        hovermode="x unified",
        hoverlabel=dict(bgcolor=TOOLTIP_BG, bordercolor=AXIS, font=dict(color=INK)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickcolor=AXIS, ticks="outside",
                     tickfont=dict(color=MUTED))
    fig.update_yaxes(title=dict(text=y_title, font=dict(color=MUTED)), gridcolor=GRID,
                     zeroline=False, tickfont=dict(color=MUTED))
    return fig


def hourly_axis(fig: go.Figure) -> go.Figure:
    """12 hour clock on time axes, matching the rest of the page."""
    fig.update_xaxes(tickformat="%-I %p<br>%b %-d")
    return fig


def empty_figure(message: str, height: int = 320) -> go.Figure:
    """Shown when a query comes back empty, so a chart never silently disappears."""
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font=dict(color=MUTED, size=13))
    fig.update_layout(height=height, paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
                      margin=dict(l=8, r=8, t=8, b=8))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def _now_line(fig: go.Figure, now_local: pd.Timestamp) -> None:
    fig.add_vline(x=now_local, line=dict(color=AXIS, width=1, dash="dot"))
    fig.add_annotation(x=now_local, y=1, yref="paper", text="now", showarrow=False,
                       xanchor="left", yanchor="top", font=dict(color=MUTED, size=11))


# ---------------------------------------------------------------
# Charts
# ---------------------------------------------------------------
def price_chart(df: pd.DataFrame) -> go.Figure:
    if df.empty:
        return empty_figure("No CAISO prices in the last 24 hours")
    fig = go.Figure()
    for node_id, g in df.groupby("node_id", sort=True):
        fig.add_trace(go.Scatter(
            x=g["interval_local"], y=g["lmp"], name=NODE_LABELS.get(node_id, node_id),
            mode="lines", line=dict(color=NODE_COLORS.get(node_id, MUTED), width=2),
            hovertemplate="$%{y:.2f}/MWh",
        ))
    return _base_layout(fig, "$/MWh")




def price_chart_with_dam(rt: pd.DataFrame, dam: pd.DataFrame, now_local: pd.Timestamp) -> go.Figure:
    """Real time prices (solid) and day ahead prices through tomorrow (dotted)."""
    if rt.empty and dam.empty:
        return empty_figure("No CAISO prices in the last 24 hours")
    fig = price_chart(rt) if not rt.empty else _base_layout(go.Figure(), "$/MWh")
    for node_id, g in dam.groupby("node_id", sort=True):
        fig.add_trace(go.Scatter(
            x=g["period_local"], y=g["lmp"], name=f"{NODE_LABELS.get(node_id, node_id)} day ahead",
            mode="lines", line=dict(color=NODE_COLORS.get(node_id, MUTED), width=1.5, dash="dot",
                                    shape="hv"),
            hovertemplate="$%{y:.2f}/MWh (day ahead)", showlegend=False,
        ))
    fig.add_hline(y=0, line=dict(color=AXIS, width=1))
    _now_line(fig, now_local)
    return hourly_axis(fig)


def supply_chart(df: pd.DataFrame) -> go.Figure:
    """Today's 5 minute supply stack with demand drawn on top."""
    if df.empty:
        return empty_figure("No 5 minute data yet today")
    fig = go.Figure()
    for label, cols, color in SUPPLY:
        y = df[cols].fillna(0).sum(axis=1).clip(lower=0)   # charging batteries and net exports drop out
        if y.max() <= 0:
            continue
        fig.add_trace(go.Scatter(
            x=df["interval_local"], y=y, name=label, stackgroup="supply", mode="lines",
            line=dict(color=SURFACE, width=0.5), fillcolor=color,
            hovertemplate="%{y:,.0f} MW",
        ))
    demand = df.dropna(subset=["demand_mw"])
    fig.add_trace(go.Scatter(
        x=demand["interval_local"], y=demand["demand_mw"], name="Demand", mode="lines",
        line=dict(color=INK, width=2), hovertemplate="%{y:,.0f} MW",
    ))
    fig = _base_layout(fig, "MW", height=340)
    fig.update_layout(legend=dict(traceorder="reversed"))
    return hourly_axis(fig)


def plugin_chart(df: pd.DataFrame, metric: str, best: list, now_local: pd.Timestamp) -> go.Figure:
    """Hourly bars for the next day and a half; the best window is highlighted."""
    if df.empty or df[metric].isna().all():
        return empty_figure("No forecast for the coming hours yet")
    is_best = df["period_local"].isin(best)
    if metric == "renewable_pct":
        y_title, tmpl = "Solar + wind, % of demand", "%{y:.0f}% solar and wind"
    else:
        y_title, tmpl = "$/MWh (day ahead, SCE area)", "$%{y:.2f}/MWh"
    fig = go.Figure(go.Bar(
        x=df["period_local"], y=df[metric],
        marker=dict(color=[ACCENT if b else "#2a4f6e" for b in is_best],
                    line=dict(color=SURFACE, width=1)),
        hovertemplate=tmpl + "<extra></extra>",
    ))
    fig = _base_layout(fig, y_title, height=260)
    fig.update_layout(showlegend=False, bargap=0.1, hovermode="closest")
    return hourly_axis(fig)


def forecast_chart(df: pd.DataFrame, series: str, now_local: pd.Timestamp) -> go.Figure:
    if df.empty:
        return empty_figure("No forecasts yet")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["period_local"], y=df["caiso_mw"], name="CAISO day ahead",
        mode="lines", line=dict(color=MUTED, width=1.5, dash="dash"), hovertemplate="%{y:,.0f} MW",
    ))
    fig.add_trace(go.Scatter(
        x=df["period_local"], y=df["gbm_mw"], name="Grid model",
        mode="lines", line=dict(color=ACCENT, width=2), hovertemplate="%{y:,.0f} MW",
    ))
    fig.add_trace(go.Scatter(
        x=df["period_local"], y=df["actual_mw"], name="Actual",
        mode="lines", line=dict(color=INK, width=2), hovertemplate="%{y:,.0f} MW",
    ))
    _now_line(fig, now_local)
    fig = _base_layout(fig, f"{SERIES_LABELS[series]}, MW")
    fig.update_xaxes(tickformat="%b %-d")
    return fig


def curtailment_daily_chart(df: pd.DataFrame) -> go.Figure:
    if df.empty or df[["curtailed_solar_mwh", "curtailed_wind_mwh"]].isna().all().all():
        return empty_figure("No curtailment reports loaded yet")
    fig = go.Figure()
    for col, name, color in (("curtailed_solar_mwh", "Solar", SLOTS[3]),
                             ("curtailed_wind_mwh", "Wind", SLOTS[2])):
        fig.add_trace(go.Bar(x=df["day"], y=df[col], name=name, marker=dict(color=color),
                             hovertemplate="%{y:,.0f} MWh"))
    fig = _base_layout(fig, "MWh curtailed per day")
    fig.update_layout(barmode="stack", bargap=0.15)
    return fig


def curtailment_hour_chart(df: pd.DataFrame) -> go.Figure:
    """Two panels sharing the hour axis: curtailment and negative prices
    pile up in the same midday hours."""
    from plotly.subplots import make_subplots

    if df.empty:
        return empty_figure("No data yet")
    labels = [pd.Timestamp(2000, 1, 1, h).strftime("%-I %p") for h in df["hour"]]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12,
                        subplot_titles=("Average MWh curtailed", "% of 5 minute intervals below $0 (SP15)"))
    fig.add_trace(go.Bar(x=labels, y=df["avg_curtailed_mwh"], marker=dict(color=SLOTS[3]),
                         name="Curtailed", hovertemplate="%{y:,.0f} MWh<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Bar(x=labels, y=df["negative_pct"], marker=dict(color=SLOTS[7]),
                         name="Negative price", hovertemplate="%{y:.0f}%<extra></extra>"), row=2, col=1)
    fig = _base_layout(fig, "", height=360)
    fig.update_layout(showlegend=False, hovermode="closest", bargap=0.15,
                      margin=dict(l=8, r=8, t=28, b=8))
    fig.update_annotations(font=dict(color=INK_2, size=12), x=0, xanchor="left")
    return fig

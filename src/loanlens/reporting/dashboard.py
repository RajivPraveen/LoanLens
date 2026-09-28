"""Self-contained interactive HTML dashboard (reports/dashboard.html).

A runnable twin of the Power BI report (same pages, same measures) for environments without
Power BI Desktop. plotly.js is inlined so the file works offline.

Chart rules applied (validated reference palette): categorical slots in fixed order, one
y-axis per chart, 2px lines, >= 8px markers with a 2px surface ring, 4px rounded bar ends,
hairline solid grids, unified hover (crosshair + every series) on time series, per-mark
tooltips elsewhere, a table view behind every chart, and a selected dark theme (the dark
steps of the same hues, not an automatic inversion).
"""

from __future__ import annotations

import html
import json
import math

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

from loanlens.config import Settings
from loanlens.warehouse import connect

LIGHT = {"surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e",
         "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "deemph": "#c9c8c1",
         "band": "rgba(42,120,214,0.14)", "recession": "rgba(137,135,129,0.12)"}
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
SEQ_SCALE = [[i / (len(SEQ) - 1), c] for i, c in enumerate(SEQ)]
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Tile-grid map of U.S. states: (column, row). Offline-safe and every state is the same size.
TILE_GRID = {
    "AK": (0, 0), "ME": (11, 0), "VT": (10, 1), "NH": (11, 1),
    "WA": (1, 2), "ID": (2, 2), "MT": (3, 2), "ND": (4, 2), "MN": (5, 2), "IL": (6, 2), "WI": (7, 2),
    "MI": (8, 2), "NY": (9, 2), "RI": (10, 2), "MA": (11, 2),
    "OR": (1, 3), "NV": (2, 3), "WY": (3, 3), "SD": (4, 3), "IA": (5, 3), "IN": (6, 3), "OH": (7, 3),
    "PA": (8, 3), "NJ": (9, 3), "CT": (10, 3),
    "CA": (1, 4), "UT": (2, 4), "CO": (3, 4), "NE": (4, 4), "MO": (5, 4), "KY": (6, 4), "WV": (7, 4),
    "VA": (8, 4), "MD": (9, 4), "DE": (10, 4),
    "AZ": (2, 5), "NM": (3, 5), "KS": (4, 5), "AR": (5, 5), "TN": (6, 5), "NC": (7, 5), "SC": (8, 5),
    "DC": (9, 5), "OK": (4, 6), "LA": (5, 6), "MS": (6, 6), "AL": (7, 6), "GA": (8, 6),
    "HI": (0, 7), "TX": (4, 7), "FL": (9, 7),
}
RECESSIONS = [("2001-03-01", "2001-11-30"), ("2007-12-01", "2009-06-30"), ("2020-02-01", "2020-04-30")]


# ---- formatting helpers --------------------------------------------------------------------

def compact(v: float, money: bool = False) -> str:
    prefix = "$" if money else ""
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{prefix}{v / div:,.1f}{suf}"
    return f"{prefix}{v:,.0f}"


def pct(v: float, d: int = 2) -> str:
    return "–" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:.{d}f}%"


def base_layout(height: int = 340, **kw) -> dict:
    lay = dict(
        height=height, margin=dict(l=56, r=20, t=16, b=44),
        paper_bgcolor=LIGHT["surface"], plot_bgcolor=LIGHT["surface"],
        font=dict(family=FONT, size=12, color=LIGHT["ink2"]),
        hoverlabel=dict(bgcolor=LIGHT["surface"], bordercolor=LIGHT["axis"], font=dict(color=LIGHT["ink"], family=FONT)),
        xaxis=dict(gridcolor=LIGHT["grid"], linecolor=LIGHT["axis"], zeroline=False, showgrid=False,
                   ticks="outside", tickcolor=LIGHT["axis"]),
        yaxis=dict(gridcolor=LIGHT["grid"], linecolor=LIGHT["axis"], zeroline=False, gridwidth=1),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, bgcolor="rgba(0,0,0,0)", traceorder="normal"),
        showlegend=True,
    )
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(lay.get(k), dict):
            lay[k] = {**lay[k], **v}
        else:
            lay[k] = v
    for ax in ("xaxis", "yaxis"):  # plotly 6+: axis titles must be objects
        t = lay[ax].get("title")
        if isinstance(t, str):
            lay[ax]["title"] = {"text": t, "font": {"color": LIGHT["ink2"], "size": 12}}
    return lay


def time_axis(lay: dict) -> dict:
    lay["hovermode"] = "x unified"
    lay["xaxis"] = {**lay["xaxis"], "showspikes": True, "spikemode": "across", "spikethickness": 1,
                    "spikecolor": LIGHT["muted"], "spikedash": "solid"}
    lay["shapes"] = [dict(type="rect", xref="x", yref="paper", x0=a, x1=b, y0=0, y1=1,
                          fillcolor=LIGHT["recession"], line=dict(width=0), layer="below")
                     for a, b in RECESSIONS]
    return lay


def line(x, y, name, color, fmt=".2%", width=2, **kw) -> go.Scatter:
    return go.Scatter(x=x, y=y, name=name, mode="lines", line=dict(color=color, width=width, shape="linear"),
                      hovertemplate=f"<b>%{{y:{fmt}}}</b> {html.escape(name)}<extra></extra>", **kw)


def table_html(df: pd.DataFrame, formats: dict | None = None, max_rows: int = 60) -> str:
    formats = formats or {}
    df = df.head(max_rows)
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = []
    for _, r in df.iterrows():
        cells = []
        for c in df.columns:
            v = r[c]
            txt = formats[c](v) if c in formats else (f"{v:,.4g}" if isinstance(v, float) else str(v))
            cells.append(f"<td>{html.escape(txt)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


class Page:
    def __init__(self, key: str, title: str):
        self.key, self.title, self.blocks, self.figs = key, title, [], []

    def tiles(self, tiles: list[dict]) -> None:
        parts = []
        for t in tiles:
            spark = t.get("spark")
            svg = ""
            if spark is not None and len(spark) > 1:
                s = np.asarray(spark, float)
                lo, hi = np.nanmin(s), np.nanmax(s)
                ys = 22 - (s - lo) / (hi - lo + 1e-12) * 20
                xs = np.linspace(1, 99, len(s))
                pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
                svg = (f'<svg class="spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">'
                       f'<polyline points="{pts}" class="spark-line"/>'
                       f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2.5" class="spark-dot"/></svg>')
            delta = ""
            if t.get("delta"):
                good = t.get("delta_good")
                cls = "neutral" if good is None else ("good" if good else "bad")
                icon = {"good": "▲" if t.get("up", True) else "▼", "bad": "▲" if t.get("up", True) else "▼",
                        "neutral": "•"}[cls]
                delta = f'<div class="delta {cls}"><span aria-hidden="true">{icon}</span> {html.escape(t["delta"])}</div>'
            hero = " hero" if t.get("hero") else ""
            parts.append(f'<div class="tile{hero}"><div class="tile-label">{html.escape(t["label"])}</div>'
                         f'<div class="tile-value">{html.escape(t["value"])}</div>{delta}{svg}</div>')
        self.blocks.append(f'<div class="tiles">{"".join(parts)}</div>')

    def chart(self, fig_data: list, layout: dict, title: str, subtitle: str = "",
              table: str = "", wide: bool = False) -> None:
        cid = f"{self.key}-{len(self.figs)}"
        self.figs.append({"id": cid, "data": fig_data, "layout": layout})
        tv = (f'<button class="tv" aria-expanded="false" aria-controls="{cid}-t">Table view</button>'
              if table else "")
        self.blocks.append(
            f'<figure class="card{" wide" if wide else ""}"><figcaption><div><h3>{html.escape(title)}</h3>'
            f'<p class="sub">{html.escape(subtitle)}</p></div>{tv}</figcaption>'
            f'<div class="plot" id="{cid}"></div>'
            f'<div class="table-view" id="{cid}-t" hidden>{table}</div></figure>')

    def html_block(self, content: str) -> None:
        self.blocks.append(content)


def _fig_json(traces, layout) -> tuple[list, dict]:
    fig = go.Figure(traces, layout)
    j = json.loads(fig.to_json())
    return j["data"], j["layout"]


# ---- pages ---------------------------------------------------------------------------------

def _overview(con, s: dict) -> Page:
    p = Page("overview", "Portfolio overview")
    pm = con.execute("select * from analytics.mart_portfolio_monthly order by period_month").df()
    pm["period_month"] = pd.to_datetime(pm["period_month"])
    last, prev = pm.iloc[-1], pm.iloc[-13]
    tail = pm.tail(24)
    ch = lambda a, b: f"{(a - b) * 1e4:+.0f} bps vs 12 months ago"  # noqa: E731
    p.tiles([
        {"label": "Outstanding balance", "value": compact(last["active_upb"], True), "hero": True,
         "delta": f"{last['active_upb'] / prev['active_upb'] - 1:+.1%} vs 12 months ago", "delta_good": None,
         "spark": tail["active_upb"]},
        {"label": "Active loans", "value": f"{int(last['active_loans']):,}",
         "delta": f"{last['active_loans'] / prev['active_loans'] - 1:+.1%} vs 12 months ago", "delta_good": None,
         "spark": tail["active_loans"]},
        {"label": "30+ days delinquent", "value": pct(last["dq30_plus_rate"]),
         "delta": ch(last["dq30_plus_rate"], prev["dq30_plus_rate"]),
         "delta_good": bool(last["dq30_plus_rate"] <= prev["dq30_plus_rate"]),
         "up": bool(last["dq30_plus_rate"] > prev["dq30_plus_rate"]), "spark": tail["dq30_plus_rate"]},
        {"label": "Seriously delinquent (90+)", "value": pct(last["serious_dq_rate"]),
         "delta": ch(last["serious_dq_rate"], prev["serious_dq_rate"]),
         "delta_good": bool(last["serious_dq_rate"] <= prev["serious_dq_rate"]),
         "up": bool(last["serious_dq_rate"] > prev["serious_dq_rate"]), "spark": tail["serious_dq_rate"]},
        {"label": "12-month expected loss", "value": compact(float(s["book_expected_loss_12m"]), True),
         "delta": f"{float(s['book_pd_12m_upb_weighted']):.2%} balance-weighted PD", "delta_good": None},
        {"label": "Stress loss, 99th percentile", "value": pct(float(s["var99_loss_rate"])),
         "delta": f"{compact(float(s['var99_loss_amount']), True)} over {s['horizon_months']} months", "delta_good": None},
    ])
    x = pm["period_month"]
    lay = time_axis(base_layout(yaxis=dict(tickformat=".1%", rangemode="tozero")))
    p.chart(_fig_json([line(x, pm["dq30_plus_rate"], "30+ days delinquent", SERIES_LIGHT[2]),
                       line(x, pm["serious_dq_rate_ex_forbearance"], "90+ excluding COVID forbearance", SERIES_LIGHT[1]),
                       line(x, pm["serious_dq_rate"], "90+ days delinquent", SERIES_LIGHT[0])], lay)[0], lay,
            "Delinquency rate", "Share of active loans; shaded bands are NBER recessions",
            table_html(pm[["period_month", "dq30_plus_rate", "serious_dq_rate", "serious_dq_rate_ex_forbearance"]]
                       .iloc[::-12].head(30), {"period_month": lambda v: v.strftime("%Y-%m"),
                                               "dq30_plus_rate": pct, "serious_dq_rate": pct,
                                               "serious_dq_rate_ex_forbearance": pct}), wide=True)
    lay = time_axis(base_layout(yaxis=dict(tickprefix="$", ticksuffix="B", tickformat=",.0f"), showlegend=False))
    p.chart(_fig_json([go.Scatter(x=x, y=pm["active_upb"] / 1e9, name="Outstanding balance", mode="lines",
                                  line=dict(color=SERIES_LIGHT[0], width=2), fill="tozeroy",
                                  fillcolor="rgba(42,120,214,0.10)",
                                  hovertemplate="<b>$%{y:,.2f}B</b> outstanding<extra></extra>")], lay)[0], lay,
            "Outstanding balance", "Unpaid principal of active loans",
            table_html(pm[["period_month", "active_loans", "active_upb"]].iloc[::-12].head(30),
                       {"period_month": lambda v: v.strftime("%Y-%m"), "active_loans": lambda v: f"{int(v):,}",
                        "active_upb": lambda v: compact(v, True)}))
    for col, name, sub in (("cpr", "Prepayment speed (CPR)", "Annualised voluntary payoff rate - refinance waves in 2003, 2012, 2020-21"),
                           ("cdr", "Default speed (CDR)", "Annualised rate of balance entering default")):
        lay = time_axis(base_layout(yaxis=dict(tickformat=".0%" if col == "cpr" else ".1%", rangemode="tozero"),
                                    showlegend=False))
        p.chart(_fig_json([line(x, pm[col].rolling(3, min_periods=1).mean(), name + ", 3-month average",
                                SERIES_LIGHT[0], ".1%")], lay)[0], lay, name, sub,
                table_html(pm[["period_month", col]].iloc[::-12].head(30),
                           {"period_month": lambda v: v.strftime("%Y-%m"), col: lambda v: pct(v, 1)}))
    return p


def _vintages(con) -> Page:
    p = Page("vintages", "Vintage performance")
    vc = con.execute("select * from analytics.mart_vintage_curves where months_since_first_payment <= 180").df()
    lv = con.execute("select * from analytics.mart_loss_by_vintage order by vintage_year").df()
    highlight = {2003: SERIES_LIGHT[0], 2007: SERIES_LIGHT[1], 2016: SERIES_LIGHT[2]}
    for metric, title, sub in (("cum_default_rate", "Cumulative default rate by vintage",
                                "Share of each origination year's loans that reached a credit event, by months since first payment"),
                               ("cum_loss_rate", "Cumulative loss rate by vintage",
                                "Net credit losses as a share of original balance")):
        traces = []
        for year, g in vc.groupby("vintage_year"):
            if year in highlight:
                continue
            traces.append(go.Scatter(x=g["months_since_first_payment"], y=g[metric], mode="lines",
                                     line=dict(color=LIGHT["deemph"], width=1), name="Other vintages",
                                     legendgroup="other", showlegend=bool(len(traces) == 0),
                                     hovertemplate=f"<b>%{{y:.2%}}</b> {year} vintage, month %{{x}}<extra></extra>"))
        for year, color in highlight.items():
            g = vc[vc["vintage_year"] == year]
            traces.append(go.Scatter(x=g["months_since_first_payment"], y=g[metric], mode="lines",
                                     line=dict(color=color, width=2.5), name=f"{year} vintage",
                                     hovertemplate=f"<b>%{{y:.2%}}</b> {year} vintage, month %{{x}}<extra></extra>"))
            if len(g):
                end = g.iloc[-1]
                traces.append(go.Scatter(x=[end["months_since_first_payment"]], y=[end[metric]], mode="markers+text",
                                         marker=dict(color=color, size=8, line=dict(color=LIGHT["surface"], width=2)),
                                         text=[str(year)], textposition="middle right", showlegend=False,
                                         textfont=dict(color=LIGHT["ink2"]), hoverinfo="skip"))
        lay = base_layout(height=380, yaxis=dict(tickformat=".1%" if metric == "cum_loss_rate" else ".0%"),
                          xaxis=dict(title="Months since first payment", showgrid=False), hovermode="closest")
        tbl = lv[["vintage_year", "loans", "avg_credit_score", "default_rate_24m", "lifetime_default_rate",
                  "loss_severity", "cum_loss_rate", "prepaid_share"]]
        p.chart(_fig_json(traces, lay)[0], lay, title, sub + ". Crisis (2007), pre-crisis (2003) and post-crisis (2016) vintages highlighted.",
                table_html(tbl, {"vintage_year": str, "loans": lambda v: f"{int(v):,}", "avg_credit_score": lambda v: f"{v:.0f}",
                                 "default_rate_24m": pct, "lifetime_default_rate": pct, "loss_severity": lambda v: pct(v, 1),
                                 "cum_loss_rate": pct, "prepaid_share": lambda v: pct(v, 1)}), wide=True)
    return p


def _roll_rates(con) -> Page:
    p = Page("rolls", "Roll rates")
    last = con.execute("select max(period_month) from analytics.mart_roll_rates").fetchone()[0]
    rr = con.execute(f"""
        select from_state, to_state, sum(loans) as loans
        from analytics.mart_roll_rates where period_month > date '{last}' - interval 12 month group by all""").df()
    order_from = ["Current", "30", "60", "90", "120+", "REO"]
    order_to = ["Current", "30", "60", "90", "120+", "REO", "Prepaid", "Liquidated"]
    rr = rr[rr["from_state"].isin(order_from) & rr["to_state"].isin(order_to)]
    # Non-numeric labels: plotly would read "30"/"60" as numbers (or category indices).
    label = {"30": "30 days", "60": "60 days", "90": "90 days", "120+": "120+ days"}
    rr["from_state"], rr["to_state"] = rr["from_state"].replace(label), rr["to_state"].replace(label)
    order_from = [label.get(x, x) for x in order_from]
    order_to = [label.get(x, x) for x in order_to]
    rr["rate"] = rr["loans"] / rr.groupby("from_state")["loans"].transform("sum")
    mat = rr.pivot(index="from_state", columns="to_state", values="rate").reindex(index=order_from, columns=order_to)
    z = mat.to_numpy()
    text = [[("" if np.isnan(v) else f"{v:.1%}") for v in row] for row in z]
    zmax = np.nanmax(z)
    tcolor = [["#ffffff" if (not np.isnan(v) and v > zmax * 0.45) else LIGHT["ink"] for v in row] for row in z]
    heat = go.Heatmap(z=z, x=order_to, y=order_from, colorscale=SEQ_SCALE, zmin=0, zmax=1, xgap=2, ygap=2,
                      colorbar=dict(title="Share", tickformat=".0%", outlinewidth=0, thickness=12),
                      hovertemplate="<b>%{z:.2%}</b> of loans at %{y} moved to %{x}<extra></extra>")
    ann = [dict(x=order_to[j], y=order_from[i], text=text[i][j], showarrow=False, font=dict(color=tcolor[i][j], size=11))
           for i in range(len(order_from)) for j in range(len(order_to)) if text[i][j]]
    lay = base_layout(height=380, showlegend=False, annotations=ann,
                      xaxis=dict(title="State next month", side="top", showgrid=False, ticks="", type="category",
                                 categoryorder="array", categoryarray=order_to),
                      yaxis=dict(title="State this month", autorange="reversed", showgrid=False, type="category",
                                 categoryorder="array", categoryarray=order_from),
                      margin=dict(l=90, r=20, t=60, b=20))
    tbl = mat.reset_index().rename(columns={"from_state": "from \\ to"})
    p.chart(_fig_json([heat], lay)[0], lay, "Transition matrix, last 12 months",
            "Where loans in each delinquency state went the following month",
            table_html(tbl, {c: pct for c in order_to}), wide=True)

    ts = con.execute("select * from analytics.mart_roll_rate_summary order by period_month").df()
    ts["period_month"] = pd.to_datetime(ts["period_month"])
    sm = lambda c: ts[c].rolling(3, min_periods=1).mean()  # noqa: E731
    x = ts["period_month"]
    lay = time_axis(base_layout(yaxis=dict(tickformat=".2%", rangemode="tozero"), showlegend=False))
    p.chart(_fig_json([line(x, sm("current_to_30"), "Current to 30 days", SERIES_LIGHT[0])], lay)[0], lay,
            "Current to 30 days late", "Monthly share of current loans missing a payment (3-month average) - the earliest warning signal",
            table_html(ts[["period_month", "current_to_30"]].iloc[::-12].head(30),
                       {"period_month": lambda v: v.strftime("%Y-%m"), "current_to_30": lambda v: pct(v, 3)}))
    lay = time_axis(base_layout(yaxis=dict(tickformat=".0%", rangemode="tozero")))
    p.chart(_fig_json([line(x, sm("dq30_to_60"), "30 to 60 days", SERIES_LIGHT[0], ".1%"),
                       line(x, sm("dq60_to_90"), "60 to 90 days", SERIES_LIGHT[1], ".1%"),
                       line(x, sm("dq30_cure"), "30 days back to current (cure)", SERIES_LIGHT[2], ".1%")], lay)[0], lay,
            "Roll-forward and cure rates", "Rising roll rates with falling cures signal trouble building in the pipeline",
            table_html(ts[["period_month", "dq30_to_60", "dq60_to_90", "dq30_cure"]].iloc[::-12].head(30),
                       {"period_month": lambda v: v.strftime("%Y-%m"), "dq30_to_60": lambda v: pct(v, 1),
                        "dq60_to_90": lambda v: pct(v, 1), "dq30_cure": lambda v: pct(v, 1)}))
    return p


def _segments(con) -> Page:
    p = Page("segments", "Risk segments")
    seg = con.execute("select * from reporting.rpt_risk_segments_el").df()
    fico_order = ["<620", "620-679", "680-719", "720-759", "760+"]
    ltv_order = ["<=60", "61-80", "81-90", "91-95", ">95"]
    g = seg.groupby(["fico_band", "ltv_band"]).agg(upb=("upb", "sum"), el=("expected_loss", "sum"),
                                                    loans=("loans", "sum")).reset_index()
    g["el_rate"] = g["el"] / g["upb"]
    mat = g.pivot(index="fico_band", columns="ltv_band", values="el_rate").reindex(index=fico_order, columns=ltv_order)
    loans = g.pivot(index="fico_band", columns="ltv_band", values="loans").reindex(index=fico_order, columns=ltv_order)
    z = mat.to_numpy()
    zmax = np.nanmax(z)
    heat = go.Heatmap(z=z, x=ltv_order, y=fico_order, colorscale=SEQ_SCALE, zmin=0, xgap=2, ygap=2,
                      customdata=loans.to_numpy(),
                      colorbar=dict(title="EL rate", tickformat=".2%", outlinewidth=0, thickness=12),
                      hovertemplate="<b>%{z:.3%}</b> 12m expected loss rate<br>FICO %{y}, LTV %{x}<br>%{customdata:,} loans<extra></extra>")
    ann = [dict(x=ltv_order[j], y=fico_order[i], text=f"{z[i][j]:.2%}", showarrow=False,
                font=dict(size=11, color="#ffffff" if z[i][j] > zmax * 0.45 else LIGHT["ink"]))
           for i in range(len(fico_order)) for j in range(len(ltv_order)) if not np.isnan(z[i][j])]
    lay = base_layout(height=360, showlegend=False, annotations=ann,
                      xaxis=dict(title="Original LTV band", showgrid=False, type="category"),
                      yaxis=dict(title="Credit score band", showgrid=False, type="category"),
                      margin=dict(l=100, r=20, t=16, b=50))
    p.chart(_fig_json([heat], lay)[0], lay, "Expected loss rate by credit score and LTV",
            "12-month PD x LGD x balance under the baseline economy, as a share of balance",
            table_html(g.assign(el_rate=g["el_rate"]).sort_values(["fico_band", "ltv_band"]),
                       {"upb": lambda v: compact(v, True), "el": lambda v: compact(v, True),
                        "loans": lambda v: f"{int(v):,}", "el_rate": lambda v: pct(v, 3)}))

    st = seg.groupby("state_code").agg(upb=("upb", "sum"), el=("expected_loss", "sum"), loans=("loans", "sum")).reset_index()
    st["el_rate"] = st["el"] / st["upb"]
    st = st[st["state_code"].isin(TILE_GRID)]
    xs = [TILE_GRID[s][0] for s in st["state_code"]]
    ys = [TILE_GRID[s][1] for s in st["state_code"]]
    vmax = st["el_rate"].max()
    tile = go.Scatter(x=xs, y=ys, mode="markers+text", text=st["state_code"], customdata=np.c_[st["el_rate"], st["loans"], st["el"]],
                      marker=dict(symbol="square", size=34, color=st["el_rate"], colorscale=SEQ_SCALE, cmin=0, cmax=vmax,
                                  line=dict(color=LIGHT["surface"], width=2),
                                  colorbar=dict(title="EL rate", tickformat=".3%", outlinewidth=0, thickness=12)),
                      textfont=dict(size=10, color=["#ffffff" if v > vmax * 0.45 else LIGHT["ink"] for v in st["el_rate"]]),
                      hovertemplate="<b>%{customdata[0]:.3%}</b> expected loss rate<br>%{text}: %{customdata[1]:,.0f} loans, $%{customdata[2]:,.0f} EL<extra></extra>",
                      showlegend=False)
    lay = base_layout(height=380, showlegend=False,
                      xaxis=dict(visible=False, range=[-0.7, 11.7]), yaxis=dict(visible=False, autorange="reversed", range=[7.7, -0.7]))
    p.chart(_fig_json([tile], lay)[0], lay, "Geographic risk map", "12-month expected loss rate by state (tile map: every state the same size)",
            table_html(st.sort_values("el_rate", ascending=False)[["state_code", "loans", "upb", "el", "el_rate"]],
                       {"loans": lambda v: f"{int(v):,}", "upb": lambda v: compact(v, True), "el": lambda v: compact(v, True),
                        "el_rate": lambda v: pct(v, 3)}))

    ms = con.execute("""select fico_band, sum(loans) as loans, sum(upb) as upb,
                               sum(dq30_plus_upb) / sum(upb) as dq30_rate_upb
                        from analytics.mart_risk_segments group by 1""").df()
    ms = ms.set_index("fico_band").reindex(fico_order).reset_index()
    bar = go.Bar(x=ms["fico_band"], y=ms["dq30_rate_upb"], marker=dict(color=SERIES_LIGHT[0], cornerradius=4),
                 width=0.5, hovertemplate="<b>%{y:.2%}</b> of balance 30+ days late<br>FICO %{x}<extra></extra>",
                 text=[pct(v) for v in ms["dq30_rate_upb"]], textposition="outside", textfont=dict(color=LIGHT["ink2"]))
    lay = base_layout(height=340, showlegend=False, yaxis=dict(tickformat=".1%", rangemode="tozero"),
                      xaxis=dict(title="Credit score band"))
    p.chart(_fig_json([bar], lay)[0], lay, "Current delinquency by credit score",
            "Share of balance 30+ days delinquent at the latest month",
            table_html(ms, {"loans": lambda v: f"{int(v):,}", "upb": lambda v: compact(v, True), "dq30_rate_upb": pct}))
    return p


def _stress(con, s: dict) -> Page:
    p = Page("stress", "Stress test")
    sc = con.execute("select * from reporting.rpt_stress_loss_distribution").df()
    mc = sc[sc["scenario_type"] == "monte_carlo"]
    named = sc[sc["scenario_type"] == "named"]
    sev = named[named["scenario_id"] == "Severely adverse"]
    p.tiles([
        {"label": "Expected loss", "value": pct(float(s["expected_loss_rate"])),
         "delta": f"{compact(float(s['expected_loss_amount']), True)} mean of {int(s['n_scenarios']):,} scenarios", "delta_good": None},
        {"label": "99th percentile loss", "value": pct(float(s["var99_loss_rate"])),
         "delta": compact(float(s["var99_loss_amount"]), True), "delta_good": None},
        {"label": "Expected shortfall, worst 1%", "value": pct(float(s["es99_loss_rate"])), "delta_good": None},
        {"label": "Severely adverse scenario", "value": pct(float(sev["loss_rate"].iloc[0])) if len(sev) else "–",
         "delta": "unemployment to 10%, house prices -25%", "delta_good": None},
    ])
    hist = go.Histogram(x=mc["loss_rate"].clip(upper=float(mc["loss_rate"].quantile(0.999))), nbinsx=60, marker=dict(color=SERIES_LIGHT[0], line=dict(color=LIGHT["surface"], width=1)),
                        hovertemplate="<b>%{y:,}</b> scenarios with loss %{x}<extra></extra>", showlegend=False)
    shapes, ann = [], []
    for v, label, y in ((float(s["expected_loss_rate"]), "Expected loss", 1.0), (float(s["var99_loss_rate"]), "99th percentile", 0.82)):
        shapes.append(dict(type="line", x0=v, x1=v, y0=0, y1=y, yref="paper", line=dict(color=LIGHT["ink2"], width=1)))
        ann.append(dict(x=v, y=y, yref="paper", text=f"{label} {v:.2%}", showarrow=False, xanchor="left", xshift=4,
                        yanchor="top", font=dict(color=LIGHT["ink2"], size=11)))
    x_hi = float(mc["loss_rate"].quantile(0.999))
    worst = float(mc["loss_rate"].max())
    lay = base_layout(height=340, showlegend=False, shapes=shapes, annotations=ann, bargap=0.02,
                      xaxis=dict(title=f"Credit loss over {s['horizon_months']} months (% of balance) - axis ends at the 99.9th "
                                       f"percentile; worst scenario {worst:.2%}", tickformat=".2%",
                                 range=[float(mc["loss_rate"].min()) * 0.9, x_hi * 1.05]),
                      yaxis=dict(title="Scenarios"))
    bins = pd.cut(mc["loss_rate"], 12)
    tbl = mc.groupby(bins, observed=True).size().reset_index(name="scenarios")
    tbl["loss_rate"] = tbl["loss_rate"].astype(str)
    p.chart(_fig_json([hist], lay)[0], lay, "Loss distribution", "Each bar counts simulated economies by 3-year credit loss",
            table_html(tbl))

    paths = con.execute("select * from ml.stress_paths").df()
    paths["month"] = pd.to_datetime(paths["month"])
    colors = {"Baseline": SERIES_LIGHT[2], "Adverse": SERIES_LIGHT[3], "Severely adverse": SERIES_LIGHT[1],
              "GFC replay (2008-2010)": SERIES_LIGHT[6]}
    for measure, title, fmt in (("unemployment_rate", "Unemployment rate paths", ".1f"),
                                ("hpi_relative", "House-price paths (vs today)", ".0%")):
        d = paths[paths["measure"] == measure].copy()
        if measure == "hpi_relative":
            d["value"] = d["value"] - 1
        piv = d.pivot(index="month", columns="path", values="value")
        x = piv.index
        traces = [go.Scatter(x=x, y=piv["Monte Carlo p95"], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"),
                  go.Scatter(x=x, y=piv["Monte Carlo p5"], mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor=LIGHT["band"], name="Monte Carlo 5-95%", hoverinfo="skip"),
                  line(x, piv["Monte Carlo p50"], "Monte Carlo median", SERIES_LIGHT[0], fmt)]
        traces += [line(x, piv[n], n, c, fmt) for n, c in colors.items() if n in piv]
        lay = base_layout(height=340, hovermode="x unified",
                          yaxis=dict(ticksuffix="%" if measure == "unemployment_rate" else "",
                                     tickformat=".0%" if measure == "hpi_relative" else ".1f"))
        p.chart(_fig_json(traces, lay)[0], lay, title, "Named scenarios against the simulated 5th-95th percentile band",
                table_html(piv.iloc[::6].reset_index(), {"month": lambda v: v.strftime("%Y-%m")} |
                           {c: (lambda v, m=measure: f"{v:.2f}" if m == "unemployment_rate" else pct(v, 1)) for c in piv.columns}))

    pts = go.Scatter(x=mc["unemployment_rise"], y=mc["loss_rate"], mode="markers",
                     marker=dict(size=6, color=-mc["hpi_trough_change"], colorscale=SEQ_SCALE, cmin=0,
                                 colorbar=dict(title="House-price fall", tickformat=".0%", outlinewidth=0, thickness=12),
                                 line=dict(width=0)),
                     customdata=np.c_[-mc["hpi_trough_change"], mc["peak_unemployment"]],
                     hovertemplate="<b>%{y:.3%}</b> loss<br>unemployment +%{x:.1f} pts (peak %{customdata[1]:.1f}%)<br>house prices -%{customdata[0]:.0%}<extra></extra>",
                     showlegend=False)
    lay = base_layout(height=380, showlegend=False, hovermode="closest", hoverdistance=12,
                      xaxis=dict(title="Rise in national unemployment (pts)", showgrid=True, gridcolor=LIGHT["grid"]),
                      yaxis=dict(title="Credit loss", tickformat=".2%"))
    top = mc.sort_values("loss_rate", ascending=False).head(25)[["scenario_id", "peak_unemployment", "hpi_trough_change", "loss_rate"]]
    p.chart(_fig_json([pts], lay)[0], lay, "What drives the losses",
            "Every dot is a simulated economy; darker = deeper house-price fall",
            table_html(top, {"peak_unemployment": lambda v: f"{v:.1f}%", "hpi_trough_change": lambda v: pct(v, 1),
                             "loss_rate": lambda v: pct(v, 3)}))

    bt = con.execute("select * from ml.backtest_monthly order by month").df()
    bt["month"] = pd.to_datetime(bt["month"])
    lay = base_layout(height=360, hovermode="x unified", yaxis=dict(title="Defaults per month", rangemode="tozero"))
    p.chart(_fig_json([line(bt["month"], bt["defaults"], "Actual", LIGHT["ink"], ",.0f"),
                       line(bt["month"], bt["projected_defaults_in_sample"], "Model, all history", SERIES_LIGHT[0], ",.0f"),
                       line(bt["month"], bt["projected_defaults"], "Model, crisis excluded", SERIES_LIGHT[1], ",.0f"),
                       line(bt["month"], bt["naive_defaults"], "Naive through-the-cycle", LIGHT["muted"], ",.0f", width=1.5)],
                      lay)[0], lay, "Backtest: 2008-2010",
            "The December 2007 book projected through the realised economy, against what happened",
            table_html(bt[["month", "defaults", "projected_defaults_in_sample", "projected_defaults", "naive_defaults"]].iloc[::3],
                       {"month": lambda v: v.strftime("%Y-%m")} | {c: (lambda v: f"{v:,.0f}") for c in
                                                                    ("defaults", "projected_defaults_in_sample", "projected_defaults", "naive_defaults")}),
            wide=True)
    return p


def _quality(con) -> Page:
    p = Page("quality", "Data quality")
    dq = con.execute("""select suite, count(*) as checks, sum(case when success then 1 else 0 end) as passed,
                               sum(coalesce(unexpected_count,0)) as unexpected_values,
                               count(distinct batch_id) as batches
                        from meta.dq_results group by 1 order by 1""").df()
    ing = con.execute("""select status, count(*) as n, sum(orig_rows) as loans, sum(perf_rows) as records
                         from meta.ingest_log group by 1""").df()
    loaded = ing[ing["status"] == "loaded"]
    total_checks, passed = int(dq["checks"].sum()), int(dq["passed"].sum())
    p.tiles([
        {"label": "Validation checks run", "value": f"{total_checks:,}", "delta": f"{int(dq['batches'].sum()):,} batches", "delta_good": None},
        {"label": "Check pass rate", "value": pct(passed / max(total_checks, 1), 2), "delta_good": None},
        {"label": "Source loads", "value": f"{int(loaded['n'].sum()) if len(loaded) else 0:,}",
         "delta": f"{int(ing.loc[ing['status'] == 'failed', 'n'].sum()) if 'failed' in set(ing['status']) else 0} failed", "delta_good": None},
        {"label": "Monthly records loaded", "value": compact(float(loaded["records"].sum()) if len(loaded) else 0)},
    ])
    dq["pass_rate"] = dq["passed"] / dq["checks"]
    bar = go.Bar(y=dq["suite"], x=dq["pass_rate"], orientation="h", marker=dict(color=SERIES_LIGHT[0], cornerradius=4),
                 width=0.45, text=[pct(v) for v in dq["pass_rate"]], textposition="outside",
                 textfont=dict(color=LIGHT["ink2"]),
                 hovertemplate="<b>%{x:.2%}</b> of checks passed<br>%{y}<extra></extra>")
    lay = base_layout(height=260, showlegend=False, xaxis=dict(tickformat=".0%", range=[0, 1.12], showgrid=True, gridcolor=LIGHT["grid"]),
                      yaxis=dict(showgrid=False), margin=dict(l=160, r=30, t=16, b=40))
    p.chart(_fig_json([bar], lay)[0], lay, "Great Expectations pass rate by suite", "Every batch is validated before it is loaded",
            table_html(dq, {"checks": lambda v: f"{int(v):,}", "passed": lambda v: f"{int(v):,}",
                            "unexpected_values": lambda v: f"{int(v):,}", "batches": lambda v: f"{int(v):,}", "pass_rate": pct}))
    hist = con.execute("""select source_period, status, orig_rows, perf_rows, finished_at, message
                          from meta.ingest_log order by finished_at desc limit 15""").df()
    p.html_block('<figure class="card wide"><figcaption><div><h3>Latest loads</h3>'
                 '<p class="sub">Incremental: unchanged files are skipped, restated files replace their period</p></div></figcaption>'
                 f'<div class="table-static">{table_html(hist, {"orig_rows": lambda v: "–" if pd.isna(v) else f"{int(v):,}", "perf_rows": lambda v: "–" if pd.isna(v) else f"{int(v):,}", "finished_at": lambda v: str(v)[:19]})}</div></figure>')
    return p


# ---- assembly ------------------------------------------------------------------------------

CSS = """
.viz-root{color-scheme:light;--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
 --grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--accent:#2a78d6;--deemph:#c9c8c1;
 --good:#006300;--bad:#d03b3b}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface:#1a1a19;--page:#0d0d0d;--ink:#ffffff;--ink2:#c3c2b7;
 --muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--accent:#3987e5;--deemph:#4a4a46;
 --good:#0ca30c;--bad:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
.viz-root{background:var(--page);color:var(--ink);min-height:100vh}
html{background:#f9f9f7} :root[data-theme="dark"]{background:#0d0d0d}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;padding:20px 28px 8px}
header h1{margin:0;font-size:22px;font-weight:600}
header p{margin:4px 0 0;color:var(--ink2);font-size:13px}
.note{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12px;color:var(--ink2)}
nav{display:flex;gap:4px;padding:8px 28px 0;border-bottom:1px solid var(--grid);overflow-x:auto}
nav button{background:none;border:0;border-bottom:2px solid transparent;padding:10px 12px;font:inherit;font-size:14px;color:var(--ink2);cursor:pointer}
nav button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.filters[hidden]{display:none}
.filters{display:flex;gap:8px;align-items:center;padding:14px 28px 0;font-size:13px;color:var(--ink2)}
.tv{white-space:nowrap;flex:none}
.filters button,.theme,.tv{font:inherit;font-size:12px;border:1px solid var(--border);background:var(--surface);color:var(--ink);
 border-radius:6px;padding:5px 10px;cursor:pointer}
.filters button[aria-pressed="true"]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent);font-weight:600}
main{padding:16px 28px 40px}
section{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
section[hidden]{display:none}
.tiles{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile-label{font-size:12px;color:var(--ink2)}
.tile-value{font-size:26px;font-weight:600;margin-top:4px}
.tile.hero .tile-value{font-size:48px;line-height:1.05}
.delta{font-size:12px;color:var(--ink2);margin-top:4px}
.delta.good{color:var(--good)} .delta.bad{color:var(--bad)}
.spark{width:100%;height:24px;margin-top:6px;display:block}
.spark-line{fill:none;stroke:var(--deemph);stroke-width:1.5;vector-effect:non-scaling-stroke}
.spark-dot{fill:var(--accent)}
.card{margin:0;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px 8px;min-width:0}
.card.wide{grid-column:1/-1}
figcaption{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
figcaption h3{margin:0;font-size:15px;font-weight:600}
.sub{margin:3px 0 0;font-size:12px;color:var(--ink2)}
.table-view,.table-static{max-height:340px;overflow:auto;margin:8px 0}
table{border-collapse:collapse;font-size:12px;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:600;position:sticky;top:0;background:var(--surface)}
footer{padding:0 28px 28px;color:var(--muted);font-size:12px}
@media (max-width:900px){section{grid-template-columns:1fr}}
"""

JS = """
const LIGHT = %(light)s, DARK = {surface:'#1a1a19', ink:'#ffffff', ink2:'#c3c2b7', muted:'#898781', grid:'#2c2c2a', axis:'#383835', deemph:'#4a4a46'};
const SL = %(sl)s, SD = %(sd)s;
const FIGS = %(figs)s;
const rendered = new Set();
function mapColor(c, dark){ if(typeof c!=='string') return c; const i=(dark?SL:SD).indexOf(c); if(i>=0) return (dark?SD:SL)[i];
  const from = dark?LIGHT:DARK, to = dark?DARK:LIGHT; for (const k of ['ink','ink2','muted','deemph','surface']) if (c===from[k]) return to[k]; return c; }
// Text inside a filled mark (heat cell, map tile) is chosen for the fill's luminance, which does
// not change with the theme - only chrome text (secondary ink / muted) is remapped.
function mapText(c, dark){ const from = dark?LIGHT:DARK, to = dark?DARK:LIGHT;
  for (const k of ['ink2','muted']) if (c===from[k]) return to[k]; return c; }
function recolorTrace(t, dark){ if(t.line&&t.line.color) t.line.color=mapColor(t.line.color,dark);
  if(t.marker){ if(typeof t.marker.color==='string') t.marker.color=mapColor(t.marker.color,dark);
    if(t.marker.line&&t.marker.line.color) t.marker.line.color=mapColor(t.marker.line.color,dark); }
  if(t.textfont&&t.textfont.color){ t.textfont.color = Array.isArray(t.textfont.color)? t.textfont.color.map(c=>mapText(c,dark)) : mapText(t.textfont.color,dark); } }
function themeLayout(l, dark){ const P = dark?DARK:LIGHT; l.paper_bgcolor=P.surface; l.plot_bgcolor=P.surface; l.font=Object.assign({},l.font,{color:P.ink2});
  l.hoverlabel=Object.assign({},l.hoverlabel,{bgcolor:P.surface,bordercolor:P.axis,font:{color:P.ink}});
  for (const ax of ['xaxis','yaxis']) if(l[ax]) { l[ax].gridcolor=P.grid; l[ax].linecolor=P.axis; l[ax].tickcolor=P.axis;
    if (l[ax].title && l[ax].title.font) l[ax].title.font.color = P.ink2; }
  if (l.annotations) l.annotations.forEach(a=>{ if(a.font&&a.font.color) a.font.color=mapText(a.font.color,dark); });
  if (l.shapes) l.shapes.forEach(s=>{ if(s.line&&s.line.color) s.line.color=mapColor(s.line.color,dark); }); return l; }
function render(id){ const f = FIGS[id]; const dark = document.documentElement.dataset.theme==='dark';
  const data = JSON.parse(JSON.stringify(f.data)); const layout = themeLayout(JSON.parse(JSON.stringify(f.layout)), dark);
  if (dark) data.forEach(t=>recolorTrace(t,true));
  Plotly.react(id, data, layout, {displaylogo:false, responsive:true, modeBarButtonsToRemove:['lasso2d','select2d']}); rendered.add(id); }
function showPage(key){ document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-selected', b.dataset.page===key));
  document.querySelectorAll('main section').forEach(s=>s.hidden = s.id!==key);
  document.getElementById('filters').hidden = !['overview','rolls'].includes(key);
  document.querySelectorAll('#'+key+' .plot').forEach(el=>render(el.id)); history.replaceState(null,'','#'+key); window.scrollTo(0,0); }
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>showPage(b.dataset.page)));
document.querySelectorAll('.tv').forEach(b=>b.addEventListener('click',()=>{ const t=document.getElementById(b.getAttribute('aria-controls'));
  t.hidden=!t.hidden; b.setAttribute('aria-expanded', String(!t.hidden)); b.textContent = t.hidden?'Table view':'Hide table'; }));
document.getElementById('theme').addEventListener('click',()=>{ const r=document.documentElement; r.dataset.theme = r.dataset.theme==='dark'?'light':'dark';
  document.getElementById('theme').textContent = r.dataset.theme==='dark'?'Light theme':'Dark theme'; rendered.forEach(render); });
const END = new Date('%(end)s');
document.querySelectorAll('#filters button').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('#filters button').forEach(x=>x.setAttribute('aria-pressed', x===b));
  const yrs = +b.dataset.years; const range = yrs ? [new Date(END.getFullYear()-yrs, END.getMonth(), 1).toISOString().slice(0,10), END.toISOString().slice(0,10)] : null;
  document.querySelectorAll('#overview .plot, #rolls .plot').forEach(el=>{ const f=FIGS[el.id]; if (!f.layout.hovermode || f.layout.hovermode!=='x unified') return;
    f.layout.xaxis = Object.assign({}, f.layout.xaxis, range?{range:range, autorange:false}:{autorange:true}); if (!range) delete f.layout.xaxis.range;
    if (rendered.has(el.id)) render(el.id); }); }));
if (window.matchMedia('(prefers-color-scheme: dark)').matches) document.getElementById('theme').click();
showPage((location.hash||'#overview').slice(1));
"""


def build_dashboard(settings: Settings) -> str:
    with connect(settings, read_only=True) as con:
        s = dict(con.execute("select metric, value from ml.stress_summary").fetchall())
        pages = [_overview(con, s), _vintages(con), _roll_rates(con), _segments(con), _stress(con, s), _quality(con)]
        end = con.execute("select max(period_month) from analytics.mart_portfolio_monthly").fetchone()[0]
        profile_note = "synthetic demonstration data" if settings["synthetic"] else "Freddie Mac loan-level data"
    figs = {f["id"]: {"data": f["data"], "layout": f["layout"]} for p in pages for f in p.figs}
    nav = "".join(f'<button data-page="{p.key}" aria-selected="false">{html.escape(p.title)}</button>' for p in pages)
    sections = "".join(f'<section id="{p.key}" hidden>{"".join(p.blocks)}</section>' for p in pages)
    js = JS % {"light": json.dumps({"surface": LIGHT["surface"], "ink": LIGHT["ink"], "ink2": LIGHT["ink2"],
                                    "muted": LIGHT["muted"], "grid": LIGHT["grid"], "axis": LIGHT["axis"],
                                    "deemph": LIGHT["deemph"]}),
               "sl": json.dumps(SERIES_LIGHT), "sd": json.dumps(SERIES_DARK), "figs": json.dumps(figs),
               "end": str(end)[:10]}
    doc = f"""<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LoanLens - portfolio risk dashboard</title>
<style>{CSS}</style><script>{get_plotlyjs()}</script></head>
<body><div class="viz-root">
<header><div><h1>LoanLens portfolio risk</h1><p>As of {str(end)[:7]} · {html.escape(profile_note)} · FRED macro data</p></div>
<div style="display:flex;gap:8px;align-items:center"><span class="note">Power BI twin: same pages and measures as powerbi/</span>
<button class="theme" id="theme">Dark theme</button></div></header>
<nav role="tablist">{nav}</nav>
<div class="filters" id="filters" hidden><span>Time range</span>
<button data-years="0" aria-pressed="true">All history</button><button data-years="10" aria-pressed="false">Last 10 years</button>
<button data-years="5" aria-pressed="false">Last 5 years</button><button data-years="2" aria-pressed="false">Last 24 months</button></div>
<main>{sections}</main>
<footer>Generated by <code>loanlens report</code>. Shaded bands: NBER recessions. Every chart has a table view.</footer>
</div><script>{js}</script></body></html>"""
    path = settings.reports_dir / "dashboard.html"
    path.write_text(doc)
    return str(path)

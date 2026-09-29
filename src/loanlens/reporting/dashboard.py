"""Self-contained interactive HTML dashboard (reports/dashboard.html).

The portfolio, vintage, roll-rate, segment, stress and data-quality pages mirror the Power BI
report; the start page and the interactive pieces (vintage picker, stress-scenario explorer,
approval-policy simulator) exist only here. plotly.js is inlined so the file works offline.

Design: calm and minimal - neutral surfaces, one navy accent for the UI, a muted data palette
(validated with the dataviz palette validator in both themes), plain-English labels, and an
always-visible "How to read this" line under every chart title.

Chart rules applied: categorical slots in fixed order, one
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

LIGHT = {"surface": "#ffffff", "page": "#f6f7f9", "ink": "#16202c", "ink2": "#4a5563",
         "muted": "#8a929c", "grid": "#e9ebee", "axis": "#d2d6dc", "deemph": "#cfd3d8",
         "band": "rgba(59,110,168,0.13)", "recession": "rgba(138,146,156,0.13)"}
SERIES_LIGHT = ["#3b6ea8", "#d0643c", "#2f9a7e", "#d19a1e", "#c8628b", "#5f8a2e", "#6a5aa8", "#b8433f"]
SERIES_DARK = ["#5b8fd0", "#d9764f", "#3aa889", "#b5861c", "#c4688f", "#6f9c3c", "#8f82d8", "#d8625d"]
SEQ = ["#e4ecf5", "#bccfe7", "#93b1d6", "#6590c4", "#3b6ea8", "#2a5285", "#1b3a61"]
SEQ_SCALE = [[i / (len(SEQ) - 1), c] for i, c in enumerate(SEQ)]
FONT = 'Inter, system-ui, -apple-system, "Segoe UI", sans-serif'

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

    def intro(self, question: str, answer: str) -> None:
        """Plain-English lead for the tab: the question it answers and the short answer."""
        self.blocks.append(f'<div class="intro"><div><span class="lab">The question</span>'
                           f'<p class="q">{html.escape(question)}</p></div>'
                           f'<div><span class="lab">The short answer</span><p class="a">{answer}</p></div></div>')

    def chart(self, fig_data: list, layout: dict, title: str, subtitle: str = "",
              table: str = "", wide: bool = False, explain: str = "", controls: str = "",
              cid: str | None = None) -> None:
        cid = cid or f"{self.key}-{len(self.figs)}"
        self.figs.append({"id": cid, "data": fig_data, "layout": layout})
        tv = (f'<button class="tv" aria-expanded="false" aria-controls="{cid}-t">Table view</button>'
              if table else "")
        why = f'<p class="why"><b>How to read this:</b> {explain}</p>' if explain else ""
        self.blocks.append(
            f'<figure class="card{" wide" if wide else ""}"><figcaption><div><h3>{html.escape(title)}</h3>'
            f'<p class="sub">{html.escape(subtitle)}</p></div>{tv}</figcaption>{why}{controls}'
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
    p = Page("overview", "How loans are doing")
    pm = con.execute("select * from analytics.mart_portfolio_monthly order by period_month").df()
    pm["period_month"] = pd.to_datetime(pm["period_month"])
    last, prev = pm.iloc[-1], pm.iloc[-13]
    tail = pm.tail(24)
    ch = lambda a, b: f"{(a - b) * 100:+.2f} points vs 12 months ago"  # noqa: E731
    peak = pm.loc[pm["serious_dq_rate"].idxmax()]
    p.intro("How are the loans doing right now?",
            f"<b>{int(last['active_loans']):,}</b> loans worth <b>{compact(last['active_upb'], True)}</b> are still being repaid. "
            f"<b>{pct(last['serious_dq_rate'])}</b> of them are seriously behind (90+ days late), against a peak of "
            f"{pct(peak['serious_dq_rate'], 1)} in {peak['period_month']:%B %Y} during the housing crisis.")
    p.tiles([
        {"label": "Money still owed", "value": compact(last["active_upb"], True), "hero": True,
         "delta": f"{last['active_upb'] / prev['active_upb'] - 1:+.1%} vs 12 months ago", "delta_good": None,
         "spark": tail["active_upb"]},
        {"label": "Loans being repaid", "value": f"{int(last['active_loans']):,}",
         "delta": f"{last['active_loans'] / prev['active_loans'] - 1:+.1%} vs 12 months ago", "delta_good": None,
         "spark": tail["active_loans"]},
        {"label": "Behind on payments (30+ days)", "value": pct(last["dq30_plus_rate"]),
         "delta": ch(last["dq30_plus_rate"], prev["dq30_plus_rate"]),
         "delta_good": bool(last["dq30_plus_rate"] <= prev["dq30_plus_rate"]),
         "up": bool(last["dq30_plus_rate"] > prev["dq30_plus_rate"]), "spark": tail["dq30_plus_rate"]},
        {"label": "Seriously behind (90+ days)", "value": pct(last["serious_dq_rate"]),
         "delta": ch(last["serious_dq_rate"], prev["serious_dq_rate"]),
         "delta_good": bool(last["serious_dq_rate"] <= prev["serious_dq_rate"]),
         "up": bool(last["serious_dq_rate"] > prev["serious_dq_rate"]), "spark": tail["serious_dq_rate"]},
        {"label": "Expected losses, next 12 months", "value": compact(float(s["book_expected_loss_12m"]), True),
         "delta": f"average predicted default risk {float(s['book_pd_12m_upb_weighted']):.2%}", "delta_good": None},
        {"label": "Loss in a 1-in-100 recession", "value": pct(float(s["var99_loss_rate"])),
         "delta": f"{compact(float(s['var99_loss_amount']), True)} over {s['horizon_months']} months", "delta_good": None},
    ])
    x = pm["period_month"]
    lay = time_axis(base_layout(yaxis=dict(tickformat=".1%", rangemode="tozero")))
    p.chart(_fig_json([line(x, pm["dq30_plus_rate"], "30+ days late", SERIES_LIGHT[2]),
                       line(x, pm["serious_dq_rate_ex_forbearance"], "90+ days late, excluding COVID payment pauses", SERIES_LIGHT[1]),
                       line(x, pm["serious_dq_rate"], "90+ days late", SERIES_LIGHT[0])], lay)[0], lay,
            "Share of borrowers behind on payments", "Share of loans being repaid; grey bands are US recessions",
            table_html(pm[["period_month", "dq30_plus_rate", "serious_dq_rate", "serious_dq_rate_ex_forbearance"]]
                       .iloc[::-12].head(30), {"period_month": lambda v: v.strftime("%Y-%m"),
                                               "dq30_plus_rate": pct, "serious_dq_rate": pct,
                                               "serious_dq_rate_ex_forbearance": pct}), wide=True,
            explain="A loan is <b>delinquent</b> when the borrower is behind on payments. 30+ days late is an early warning; "
                    "90+ days late (<b>serious delinquency</b>) usually ends in a loss. The 2008-2010 crisis pushed serious "
                    "delinquency to its highest level. In 2020 the blue line jumped again, but the orange line (which leaves "
                    "out borrowers in COVID forbearance, who were allowed to pause payments) barely moved: most of those "
                    "borrowers resumed paying, so it was not a credit crisis.")
    lay = time_axis(base_layout(yaxis=dict(tickprefix="$", ticksuffix="B", tickformat=",.0f"), showlegend=False))
    p.chart(_fig_json([go.Scatter(x=x, y=pm["active_upb"] / 1e9, name="Outstanding balance", mode="lines",
                                  line=dict(color=SERIES_LIGHT[0], width=2), fill="tozeroy",
                                  fillcolor="rgba(59,110,168,0.10)",
                                  hovertemplate="<b>$%{y:,.2f}B</b> outstanding<extra></extra>")], lay)[0], lay,
            "Money still owed", "Unpaid balance of loans still being repaid",
            table_html(pm[["period_month", "active_loans", "active_upb"]].iloc[::-12].head(30),
                       {"period_month": lambda v: v.strftime("%Y-%m"), "active_loans": lambda v: f"{int(v):,}",
                        "active_upb": lambda v: compact(v, True)}),
            explain="How much money is still owed on loans that are being repaid: the amount at risk. It grows as new "
                    "loans are added each year and shrinks when borrowers pay off early, as in the 2020-2021 refinance boom.")
    explain = {"cpr": "The share of the book paid off early each year, mostly because borrowers refinance when "
                      "mortgage rates fall (2003, 2012, 2020-21). Early payoffs are not losses, but they end the "
                      "interest income a lender or investor was counting on.",
               "cdr": "The share of the book falling into default each year. It spiked in 2009-2010 as house prices "
                      "fell and unemployment rose, the combination that turns missed payments into losses."}
    for col, name, sub in (("cpr", "Loans paid off early", "Share of the balance paid off early each year (CPR) - refinance waves in 2003, 2012, 2020-21"),
                           ("cdr", "Loans defaulting", "Share of the balance defaulting each year (CDR)")):
        lay = time_axis(base_layout(yaxis=dict(tickformat=".0%" if col == "cpr" else ".1%", rangemode="tozero"),
                                    showlegend=False))
        p.chart(_fig_json([line(x, pm[col].rolling(3, min_periods=1).mean(), name + ", 3-month average",
                                SERIES_LIGHT[0], ".1%")], lay)[0], lay, name, sub,
                table_html(pm[["period_month", col]].iloc[::-12].head(30),
                           {"period_month": lambda v: v.strftime("%Y-%m"), col: lambda v: pct(v, 1)}),
                explain=explain[col])
    return p


def _vintages(con) -> tuple[Page, dict]:
    """Vintage curves with a picker: any origination years can be highlighted (up to four).

    The traces are drawn in the browser from VINT so the picker can restyle them; colors are
    assigned to a year when it is picked and kept until it is unpicked (color follows the entity).
    """
    p = Page("vintages", "Loans by year made")
    vc = con.execute("select * from analytics.mart_vintage_curves where months_since_first_payment <= 180 "
                     "order by vintage_year, months_since_first_payment").df()
    lv = con.execute("select * from analytics.mart_loss_by_vintage order by vintage_year").df()
    worst = lv.loc[lv["lifetime_default_rate"].idxmax()]
    p.intro("Which loans went bad, and when?",
            f"Loans are grouped by the year they were made (their <b>vintage</b>). The worst year was "
            f"<b>{int(worst['vintage_year'])}</b>: {pct(worst['lifetime_default_rate'], 1)} of its loans defaulted and it lost "
            f"{pct(worst['cum_loss_rate'], 1)} of what was lent. Pick any years below to compare them.")
    years = sorted(int(y) for y in vc["vintage_year"].unique())
    defaults = [y for y in (2003, 2007, 2016) if y in years] or years[-3:]
    vint = {"years": years, "default": defaults,
            "curves": {str(int(y)): {"m": g["months_since_first_payment"].astype(int).tolist(),
                                     "d": g["cum_default_rate"].round(6).tolist(),
                                     "l": g["cum_loss_rate"].round(6).tolist()}
                       for y, g in vc.groupby("vintage_year")}}
    chips = "".join(f'<button class="chip" data-year="{y}" aria-pressed="false"><span class="sw"></span>{y}</button>'
                    for y in years)
    p.html_block('<div class="card wide picker"><div class="picker-head"><b>Compare years</b>'
                 '<span class="sub">Pick up to four years the loans were made; the rest stay in grey for context.</span>'
                 '<button class="linkbtn" id="vint-reset">Reset</button></div>'
                 f'<div class="chips" role="group" aria-label="Vintages to highlight">{chips}</div>'
                 '<p class="sub" id="vint-note" aria-live="polite"></p></div>')
    tbl = lv[["vintage_year", "loans", "avg_credit_score", "default_rate_24m", "lifetime_default_rate",
              "loss_severity", "cum_loss_rate", "prepaid_share"]]
    fmt = {"vintage_year": str, "loans": lambda v: f"{int(v):,}", "avg_credit_score": lambda v: f"{v:.0f}",
           "default_rate_24m": pct, "lifetime_default_rate": pct, "loss_severity": lambda v: pct(v, 1),
           "cum_loss_rate": pct, "prepaid_share": lambda v: pct(v, 1)}
    for key, title, sub, tick, explain in (
            ("d", "Share of loans that defaulted, by year made",
             "Running total since the first payment (default = 90+ days late, foreclosure or distressed sale)", ".0%",
             "Each line follows one year's loans from their first payment onwards. A steep, high line means many of "
             "those borrowers stopped paying. Loans made in 2005-2008 look very different from the rest, even though "
             "their borrowers had similar credit scores: they were made at the top of the housing market."),
            ("l", "Money lost, by year made", "Running total of losses after the home was sold, as a share of the amount lent", ".1%",
             "Not every default costs money: the house is sold and mortgage insurance pays part of the shortfall. "
             "This is what was actually lost, as a share of the amount lent. Losses arrive later than defaults, "
             "because selling a foreclosed home takes a year or more.")):
        lay = base_layout(height=380, yaxis=dict(tickformat=tick), hovermode="closest",
                          xaxis=dict(title="Months since first payment", showgrid=False))
        p.chart([], lay, title, sub, table_html(tbl, fmt), wide=True, explain=explain, cid=f"vint-{key}")
    return p, vint


def _roll_rates(con) -> Page:
    p = Page("rolls", "Late payments")
    last = con.execute("select max(period_month) from analytics.mart_roll_rates").fetchone()[0]
    rr = con.execute(f"""
        select from_state, to_state, sum(loans) as loans
        from analytics.mart_roll_rates where period_month > date '{last}' - interval 12 month group by all""").df()
    order_from = ["Current", "30", "60", "90", "120+", "REO"]
    order_to = ["Current", "30", "60", "90", "120+", "REO", "Prepaid", "Liquidated"]
    rr = rr[rr["from_state"].isin(order_from) & rr["to_state"].isin(order_to)]
    # Non-numeric labels: plotly would read "30"/"60" as numbers (or category indices).
    label = {"30": "30 days late", "60": "60 days late", "90": "90 days late", "120+": "120+ days late",
             "Current": "Paying on time", "REO": "Foreclosed", "Prepaid": "Paid off", "Liquidated": "Sold at a loss"}
    rr["from_state"], rr["to_state"] = rr["from_state"].replace(label), rr["to_state"].replace(label)
    order_from = [label.get(x, x) for x in order_from]
    order_to = [label.get(x, x) for x in order_to]
    rr["rate"] = rr["loans"] / rr.groupby("from_state")["loans"].transform("sum")
    mat = rr.pivot(index="from_state", columns="to_state", values="rate").reindex(index=order_from, columns=order_to)
    cure30 = mat.loc["30 days late", "Paying on time"]
    worse30 = mat.loc["30 days late", "60 days late"]
    stay120 = mat.loc["120+ days late", "120+ days late"]
    p.intro("Are late borrowers catching up or falling further behind?",
            f"Early on, many catch up: of borrowers 30 days late, <b>{cure30:.0%}</b> are back on time a month later, "
            f"{worse30:.0%} fall further behind and the rest stay 30 days late. Once a loan is 120+ days late it rarely recovers: <b>{stay120:.0%}</b> "
            "are still that late the next month. These month-to-month moves (<b>roll rates</b>) are the earliest "
            "warning that trouble is building.")
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
                      margin=dict(l=120, r=20, t=70, b=20))
    tbl = mat.reset_index().rename(columns={"from_state": "from \\ to"})
    p.chart(_fig_json([heat], lay)[0], lay, "Where loans went the next month",
            "Last 12 months: each row is how late a loan was this month, each column where it was a month later",
            table_html(tbl, {c: pct for c in order_to}), wide=True,
            explain="Read across a row: of the loans in that state this month, where were they next month? Almost all "
                    "current loans stay current. The further behind a borrower is, the less likely they are to catch up "
                    "(the diagonal) and the more likely the loan ends in foreclosure or a distressed sale.")

    ts = con.execute("select * from analytics.mart_roll_rate_summary order by period_month").df()
    ts["period_month"] = pd.to_datetime(ts["period_month"])
    sm = lambda c: ts[c].rolling(3, min_periods=1).mean()  # noqa: E731
    x = ts["period_month"]
    lay = time_axis(base_layout(yaxis=dict(tickformat=".2%", rangemode="tozero"), showlegend=False))
    p.chart(_fig_json([line(x, sm("current_to_30"), "On time to 30 days late", SERIES_LIGHT[0])], lay)[0], lay,
            "Borrowers who just missed a payment", "Share of on-time borrowers who fell 30 days behind each month (3-month average) - the earliest warning",
            table_html(ts[["period_month", "current_to_30"]].iloc[::-12].head(30),
                       {"period_month": lambda v: v.strftime("%Y-%m"), "current_to_30": lambda v: pct(v, 3)}),
            explain="The share of borrowers who were paying on time last month but missed a payment this month. "
                    "It moves first when the economy weakens, months before defaults show up.")
    lay = time_axis(base_layout(yaxis=dict(tickformat=".0%", rangemode="tozero")))
    p.chart(_fig_json([line(x, sm("dq30_to_60"), "30 to 60 days", SERIES_LIGHT[0], ".1%"),
                       line(x, sm("dq60_to_90"), "60 to 90 days", SERIES_LIGHT[1], ".1%"),
                       line(x, sm("dq30_cure"), "30 days late back to on time (cured)", SERIES_LIGHT[2], ".1%")], lay)[0], lay,
            "Falling further behind vs. catching up", "Rising 'further behind' lines with a falling 'caught up' line mean trouble is building",
            table_html(ts[["period_month", "dq30_to_60", "dq60_to_90", "dq30_cure"]].iloc[::-12].head(30),
                       {"period_month": lambda v: v.strftime("%Y-%m"), "dq30_to_60": lambda v: pct(v, 1),
                        "dq60_to_90": lambda v: pct(v, 1), "dq30_cure": lambda v: pct(v, 1)}),
            explain="Roll-forward rates show how often late borrowers fall further behind; the cure rate shows how "
                    "often a borrower 30 days late gets back to current. Rising roll-forwards with falling cures, as in "
                    "2008-2009, mean losses are on the way.")
    return p


def _segments(con) -> Page:
    p = Page("segments", "Where the risk is")
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
    st_ = mat.stack()
    (hf, hl), (lf, ll) = st_.idxmax(), st_.idxmin()
    p.intro("Where is the risk concentrated?",
            f"The riskiest group is borrowers with a credit score of <b>{hf}</b> who borrowed <b>{hl}%</b> of the "
            f"home's value: expected loss over the next year is <b>{st_.max():.2%}</b> of the balance. The safest "
            f"(score {lf}, borrowed {ll}%) is <b>{st_.min():.3%}</b>, about <b>{st_.max() / st_.min():,.0f}x</b> less. "
            "(Expected loss = chance of default x share lost x amount owed.)")
    heat = go.Heatmap(z=z, x=ltv_order, y=fico_order, colorscale=SEQ_SCALE, zmin=0, xgap=2, ygap=2,
                      customdata=loans.to_numpy(),
                      colorbar=dict(title="Expected<br>loss", tickformat=".2%", outlinewidth=0, thickness=12),
                      hovertemplate="<b>%{z:.3%}</b> expected loss over 12 months<br>Credit score %{y}, borrowed %{x}% of home value<br>%{customdata:,} loans<extra></extra>")
    ann = [dict(x=ltv_order[j], y=fico_order[i], text=f"{z[i][j]:.2%}", showarrow=False,
                font=dict(size=11, color="#ffffff" if z[i][j] > zmax * 0.45 else LIGHT["ink"]))
           for i in range(len(fico_order)) for j in range(len(ltv_order)) if not np.isnan(z[i][j])]
    lay = base_layout(height=360, showlegend=False, annotations=ann,
                      xaxis=dict(title="Loan as a % of the home's value (LTV)", showgrid=False, type="category"),
                      yaxis=dict(title="Credit score band", showgrid=False, type="category"),
                      margin=dict(l=100, r=20, t=16, b=50))
    p.chart(_fig_json([heat], lay)[0], lay, "Expected loss by credit score and loan size vs. home value",
            "Next 12 months, normal economy: chance of default x share lost x balance, as a % of the balance",
            table_html(g.assign(el_rate=g["el_rate"]).sort_values(["fico_band", "ltv_band"]),
                       {"upb": lambda v: compact(v, True), "el": lambda v: compact(v, True),
                        "loans": lambda v: f"{int(v):,}", "el_rate": lambda v: pct(v, 3)}),
            explain="Each cell is a group of today's loans, by the borrower's credit score and the loan-to-value ratio "
                    "(LTV: how much was borrowed compared with the home's value). Darker cells are expected to lose more "
                    "over the next 12 months. The rows with credit scores below 680 are the darkest.")

    st = seg.groupby("state_code").agg(upb=("upb", "sum"), el=("expected_loss", "sum"), loans=("loans", "sum")).reset_index()
    st["el_rate"] = st["el"] / st["upb"]
    st = st[st["state_code"].isin(TILE_GRID)]
    xs = [TILE_GRID[s][0] for s in st["state_code"]]
    ys = [TILE_GRID[s][1] for s in st["state_code"]]
    vmax = st["el_rate"].max()
    tile = go.Scatter(x=xs, y=ys, mode="markers+text", text=st["state_code"], customdata=np.c_[st["el_rate"], st["loans"], st["el"]],
                      marker=dict(symbol="square", size=34, color=st["el_rate"], colorscale=SEQ_SCALE, cmin=0, cmax=vmax,
                                  line=dict(color=LIGHT["surface"], width=2),
                                  colorbar=dict(title="Expected<br>loss", tickformat=".3%", outlinewidth=0, thickness=12)),
                      textfont=dict(size=10, color=["#ffffff" if v > vmax * 0.45 else LIGHT["ink"] for v in st["el_rate"]]),
                      hovertemplate="<b>%{customdata[0]:.3%}</b> expected loss<br>%{text}: %{customdata[1]:,.0f} loans, $%{customdata[2]:,.0f} expected loss<extra></extra>",
                      showlegend=False)
    lay = base_layout(height=380, showlegend=False,
                      xaxis=dict(visible=False, range=[-0.7, 11.7]), yaxis=dict(visible=False, autorange="reversed", range=[7.7, -0.7]))
    p.chart(_fig_json([tile], lay)[0], lay, "Expected loss by state", "Next 12 months, as a % of the balance (every state drawn the same size)",
            table_html(st.sort_values("el_rate", ascending=False)[["state_code", "loans", "upb", "el", "el_rate"]],
                       {"loans": lambda v: f"{int(v):,}", "upb": lambda v: compact(v, True), "el": lambda v: compact(v, True),
                        "el_rate": lambda v: pct(v, 3)}),
            explain="The same expected-loss rate by state. Differences come from local unemployment, house-price "
                    "trends and the mix of borrowers in each state. Every state is drawn the same size so small "
                    "states are as visible as large ones.")

    ms = con.execute("""select fico_band, sum(loans) as loans, sum(upb) as upb,
                               sum(dq30_plus_upb) / sum(upb) as dq30_rate_upb
                        from analytics.mart_risk_segments group by 1""").df()
    ms = ms.set_index("fico_band").reindex(fico_order).reset_index()
    bar = go.Bar(x=ms["fico_band"], y=ms["dq30_rate_upb"], marker=dict(color=SERIES_LIGHT[0], cornerradius=4),
                 width=0.5, hovertemplate="<b>%{y:.2%}</b> of balance 30+ days late<br>Credit score %{x}<extra></extra>",
                 text=[pct(v) for v in ms["dq30_rate_upb"]], textposition="outside", textfont=dict(color=LIGHT["ink2"]))
    lay = base_layout(height=340, showlegend=False, yaxis=dict(tickformat=".1%", rangemode="tozero"),
                      xaxis=dict(title="Credit score band"))
    p.chart(_fig_json([bar], lay)[0], lay, "Borrowers behind on payments, by credit score",
            "Share of the balance 30+ days late, latest month",
            table_html(ms, {"loans": lambda v: f"{int(v):,}", "upb": lambda v: compact(v, True), "dq30_rate_upb": pct}),
            explain="Credit score is the single strongest predictor of default in the model. Borrowers with scores "
                    "under 620 are many times more likely to be behind on payments today than those above 760.")
    return p


def _stress(con, s: dict) -> tuple[Page, dict]:
    p = Page("stress", "Recession test")
    sc = con.execute("select * from reporting.rpt_stress_loss_distribution").df()
    mc = sc[sc["scenario_type"] == "monte_carlo"]
    named = sc[sc["scenario_type"] == "named"]
    sev = named[named["scenario_id"] == "Severely adverse"]
    p.intro("How much could we lose if the economy turns?",
            f"We simulated <b>{int(s['n_scenarios']):,}</b> possible economies over the next {s['horizon_months']} months, "
            f"from calm to severe recessions. In the average one the book loses <b>{pct(float(s['expected_loss_rate']))}</b> "
            f"({compact(float(s['expected_loss_amount']), True)}); in the worst 1 in 100 it loses "
            f"<b>{pct(float(s['var99_loss_rate']))}</b> ({compact(float(s['var99_loss_amount']), True)}).")
    p.tiles([
        {"label": "Loss in an average economy", "value": pct(float(s["expected_loss_rate"])),
         "delta": f"{compact(float(s['expected_loss_amount']), True)}, average of {int(s['n_scenarios']):,} simulations", "delta_good": None},
        {"label": "Loss in a 1-in-100 recession", "value": pct(float(s["var99_loss_rate"])),
         "delta": f"{compact(float(s['var99_loss_amount']), True)} (99th percentile)", "delta_good": None},
        {"label": "Average of the worst 1%", "value": pct(float(s["es99_loss_rate"])), "delta": "expected shortfall", "delta_good": None},
        {"label": "Regulator's severe scenario", "value": pct(float(sev["loss_rate"].iloc[0])) if len(sev) else "–",
         "delta": "unemployment to 10%, house prices -25%", "delta_good": None},
    ])
    hist = go.Histogram(x=mc["loss_rate"].clip(upper=float(mc["loss_rate"].quantile(0.999))), nbinsx=60, marker=dict(color=SERIES_LIGHT[0], line=dict(color=LIGHT["surface"], width=1)),
                        hovertemplate="<b>%{y:,}</b> scenarios with loss %{x}<extra></extra>", showlegend=False)
    shapes, ann = [], []
    for v, label, y in ((float(s["expected_loss_rate"]), "Average", 1.0), (float(s["var99_loss_rate"]), "1-in-100", 0.82)):
        shapes.append(dict(type="line", x0=v, x1=v, y0=0, y1=y, yref="paper", line=dict(color=LIGHT["ink2"], width=1)))
        ann.append(dict(x=v, y=y, yref="paper", text=f"{label} {v:.2%}", showarrow=False, xanchor="left", xshift=4,
                        yanchor="top", font=dict(color=LIGHT["ink2"], size=11)))
    x_hi = float(mc["loss_rate"].quantile(0.999))
    worst = float(mc["loss_rate"].max())
    lay = base_layout(height=340, showlegend=False, shapes=shapes, annotations=ann, bargap=0.02,
                      xaxis=dict(title=f"Loss over {s['horizon_months']} months (% of balance) - axis ends at the 1-in-1,000 "
                                       f"case; the single worst was {worst:.2%}", tickformat=".2%",
                                 range=[float(mc["loss_rate"].min()) * 0.9, x_hi * 1.05]),
                      yaxis=dict(title="Simulated economies"))
    bins = pd.cut(mc["loss_rate"], 12)
    tbl = mc.groupby(bins, observed=True).size().reset_index(name="scenarios")
    tbl["loss_rate"] = tbl["loss_rate"].astype(str)
    p.chart(_fig_json([hist], lay)[0], lay, "How much could be lost", "Each bar counts simulated economies by their 3-year loss",
            table_html(tbl),
            explain="Most simulated economies are calm, so most bars sit at small losses. The long tail to the right is "
                    "the recessions. Risk teams and regulators focus on the tail: the <b>99th percentile</b> is the loss "
                    "exceeded in only 1 economy in 100, and the capital a lender holds has to cover it.")

    paths = con.execute("select * from ml.stress_paths").df()
    paths["month"] = pd.to_datetime(paths["month"])
    colors = {"Baseline": SERIES_LIGHT[2], "Adverse": SERIES_LIGHT[3], "Severely adverse": SERIES_LIGHT[1],
              "GFC replay (2008-2010)": SERIES_LIGHT[6]}
    for measure, title, fmt in (("unemployment_rate", "Unemployment in each scenario", ".1f"),
                                ("hpi_relative", "House prices in each scenario (vs. today)", ".0%")):
        d = paths[paths["measure"] == measure].copy()
        if measure == "hpi_relative":
            d["value"] = d["value"] - 1
        piv = d.pivot(index="month", columns="path", values="value")
        x = piv.index
        traces = [go.Scatter(x=x, y=piv["Monte Carlo p95"], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"),
                  go.Scatter(x=x, y=piv["Monte Carlo p5"], mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor=LIGHT["band"], name="Middle 90% of simulations", hoverinfo="skip"),
                  line(x, piv["Monte Carlo p50"], "Typical simulation", SERIES_LIGHT[0], fmt)]
        shown = {"GFC replay (2008-2010)": "Replay of 2008-2010", "Severely adverse": "Severe recession",
                 "Adverse": "Mild recession"}
        traces += [line(x, piv[n], shown.get(n, n), c, fmt) for n, c in colors.items() if n in piv]
        lay = base_layout(height=340, hovermode="x unified",
                          yaxis=dict(ticksuffix="%" if measure == "unemployment_rate" else "",
                                     tickformat=".0%" if measure == "hpi_relative" else ".1f"))
        p.chart(_fig_json(traces, lay)[0], lay, title, "Named scenarios compared with the range of simulated economies",
                explain=("The shaded band holds 90% of the simulated futures. The coloured lines are the named "
                         "scenarios regulators ask for, from a baseline to a severe recession, plus a replay of "
                         "what actually happened in 2008-2010." if measure == "unemployment_rate" else
                         "House prices matter because a borrower whose home is worth less than the loan (underwater) "
                         "is far more likely to default, and the lender recovers less when the home is sold."),
                table=table_html(piv.iloc[::6].reset_index(), {"month": lambda v: v.strftime("%Y-%m")} |
                           {c: (lambda v, m=measure: f"{v:.2f}" if m == "unemployment_rate" else pct(v, 1)) for c in piv.columns}))

    # Scenario explorer: the scatter is drawn in the browser from MC so the sliders can filter it.
    mcd = {"u": mc["unemployment_rise"].round(3).tolist(), "h": (-mc["hpi_trough_change"]).round(4).tolist(),
           "l": mc["loss_rate"].round(6).tolist(), "pk": mc["peak_unemployment"].round(2).tolist()}
    controls = ('<div class="controls" role="group" aria-label="Scenario filters">'
                '<label><span>Unemployment rises by at least <output id="sx-u-out">0</output> points</span>'
                '<input type="range" id="sx-u" min="0" max="8" step="0.5" value="0"></label>'
                '<label><span>House prices fall by at least <output id="sx-h-out">0%</output></span>'
                '<input type="range" id="sx-h" min="0" max="40" step="2.5" value="0"></label>'
                '<button class="linkbtn" id="sx-reset">Reset</button></div>'
                '<p class="readout" id="sx-read" aria-live="polite"></p>')
    lay = base_layout(height=380, showlegend=True, hovermode="closest", hoverdistance=12,
                      xaxis=dict(title="Rise in national unemployment (percentage points)", showgrid=True, gridcolor=LIGHT["grid"]),
                      yaxis=dict(title="Loss (% of balance)", tickformat=".2%"))
    top = mc.sort_values("loss_rate", ascending=False).head(25)[["scenario_id", "peak_unemployment", "hpi_trough_change", "loss_rate"]]
    p.chart([], lay, "Explore the scenarios",
            "Every dot is a simulated economy; drag the sliders to keep only the harsher ones",
            table_html(top, {"peak_unemployment": lambda v: f"{v:.1f}%", "hpi_trough_change": lambda v: pct(v, 1),
                             "loss_rate": lambda v: pct(v, 3)}),
            explain="Losses stay small until unemployment and house prices both move against borrowers. Job losses make "
                    "people unable to pay; falling prices take away the option to sell the house and repay. Use the "
                    "sliders to ask, for example, what the book loses when unemployment rises 4+ points and prices fall 20%+.",
            controls=controls, cid="stress-drv")

    bt = con.execute("select * from ml.backtest_monthly order by month").df()
    bt["month"] = pd.to_datetime(bt["month"])
    lay = base_layout(height=360, hovermode="x unified", yaxis=dict(title="Defaults per month", rangemode="tozero"))
    p.chart(_fig_json([line(bt["month"], bt["defaults"], "Actual", LIGHT["ink"], ",.0f"),
                       line(bt["month"], bt["projected_defaults_in_sample"], "Model trained on all history", SERIES_LIGHT[0], ",.0f"),
                       line(bt["month"], bt["projected_defaults"], "Model that never saw the crisis", SERIES_LIGHT[1], ",.0f"),
                       line(bt["month"], bt["naive_defaults"], "Simple long-run average", LIGHT["muted"], ",.0f", width=1.5)],
                      lay)[0], lay, "Did the model get 2008-2010 right?",
            "Loans held in December 2007, run through the real economy that followed, against what actually happened",
            explain=_backtest_explain(bt),
            table=table_html(bt[["month", "defaults", "projected_defaults_in_sample", "projected_defaults", "naive_defaults"]].iloc[::3],
                       {"month": lambda v: v.strftime("%Y-%m")} | {c: (lambda v: f"{v:,.0f}") for c in
                                                                    ("defaults", "projected_defaults_in_sample", "projected_defaults", "naive_defaults")}),
            wide=True)
    return p, mcd


def _backtest_explain(bt: pd.DataFrame) -> str:
    actual = bt["defaults"].sum()
    err = lambda col: bt[col].sum() / actual - 1  # noqa: E731
    fit, oos, naive = err("projected_defaults_in_sample"), err("projected_defaults"), err("naive_defaults")
    word = lambda e: f"{'over' if e > 0 else 'under'}-predicts by {abs(e):.0%}"  # noqa: E731
    return ("The honest test of a stress model: take the loans that were being repaid in December 2007, feed the model "
            "the economy that actually followed, and compare with the defaults that really happened. "
            f"Trained on all history (blue) the model {word(fit)}. With the crisis hidden from it (orange) it {word(oos)}, "
            "because it had never seen an economy that bad. A naive long-run average (grey) "
            f"{word(naive)}: without a model of the economy, a crisis is invisible.")


def _quality(con) -> Page:
    p = Page("quality", "Data checks")
    p.intro("Can we trust the data?",
            "Every file is checked automatically before it is loaded: missing IDs, impossible values (a credit score of "
            "900), duplicates, and payment records for loans that do not exist. A file that fails is rejected whole, "
            "so a bad delivery can never reach the models.")
    dq = con.execute("""select suite, count(*) as checks, sum(case when success then 1 else 0 end) as passed,
                               sum(coalesce(unexpected_count,0)) as unexpected_values,
                               count(distinct batch_id) as batches
                        from meta.dq_results group by 1 order by 1""").df()
    ing = con.execute("""select status, count(*) as n, sum(orig_rows) as loans, sum(perf_rows) as records
                         from meta.ingest_log group by 1""").df()
    loaded = ing[ing["status"] == "loaded"]
    total_checks, passed = int(dq["checks"].sum()), int(dq["passed"].sum())
    p.tiles([
        {"label": "Automatic checks run", "value": f"{total_checks:,}", "delta": f"on {int(dq['batches'].sum()):,} batches of data", "delta_good": None},
        {"label": "Checks passed", "value": pct(passed / max(total_checks, 1), 2), "delta_good": None},
        {"label": "Files loaded", "value": f"{int(loaded['n'].sum()) if len(loaded) else 0:,}",
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
    p.chart(_fig_json([bar], lay)[0], lay, "Checks passed, by type of file", "Every batch is checked (with Great Expectations) before it is loaded",
            table=table_html(dq, {"checks": lambda v: f"{int(v):,}", "passed": lambda v: f"{int(v):,}",
                                  "unexpected_values": lambda v: f"{int(v):,}", "batches": lambda v: f"{int(v):,}",
                                  "pass_rate": pct}),
            explain="The share of automated checks that passed, by type of file. Checks on a rejected batch count as "
                    "failures here even after the file was fixed and reloaded, so a figure just under 100% is the gate "
                    "doing its job.")
    hist = con.execute("""select source_period, status, orig_rows, perf_rows, finished_at, message
                          from meta.ingest_log order by finished_at desc limit 15""").df()
    p.html_block('<figure class="card wide"><figcaption><div><h3>Latest loads</h3>'
                 '<p class="sub">Unchanged files are skipped; corrected files replace their period</p></div></figcaption>'
                 f'<div class="table-static">{table_html(hist, {"orig_rows": lambda v: "–" if pd.isna(v) else f"{int(v):,}", "perf_rows": lambda v: "–" if pd.isna(v) else f"{int(v):,}", "finished_at": lambda v: str(v)[:19]})}</div></figure>')
    return p


def _start(con, s: dict, cut: dict | None, pdm: dict | None) -> Page:
    """Landing tab: what the dashboard is, the four questions it answers, how to use it, key terms."""
    p = Page("start", "Start here")
    pm = con.execute("select period_month, active_loans, active_upb, serious_dq_rate from analytics.mart_portfolio_monthly "
                     "order by period_month").df()
    last, peak = pm.iloc[-1], pm.loc[pm["serious_dq_rate"].idxmax()]
    lv = con.execute("select vintage_year, lifetime_default_rate from analytics.mart_loss_by_vintage").df()
    worst = lv.loc[lv["lifetime_default_rate"].idxmax()]
    vol = con.execute("select count(*) as loans from core.dim_loan").fetchone()[0]
    first_year = int(lv["vintage_year"].min())
    p.html_block(
        '<div class="card wide lead"><h2>What this dashboard is</h2>'
        f'<p>When a bank lends money for a home, it is betting the borrower will repay over the next 30 years. '
        f'This dashboard follows <b>{vol / 1e6:.2f} million real home loans</b> bought by Freddie Mac since {first_year}, '
        f'month by month through the 2008 housing crisis, COVID-19 and the 2022 rate shock. '
        f'<b>{int(last["active_loans"]):,}</b> of them (<b>{compact(last["active_upb"], True)}</b>) are still being repaid today.</p>'
        '<p>It answers the four questions a lender\'s risk team asks every month. Pick one to jump to the answer.</p></div>')
    cards = [("overview", "1", "How are the loans doing?", pct(last["serious_dq_rate"]),
              f"are seriously late today, against {pct(peak['serious_dq_rate'], 1)} at the {peak['period_month']:%Y} peak"),
             ("vintages", "2", "Which loans go bad, and why?", pct(worst["lifetime_default_rate"], 1),
              f"of loans made in {int(worst['vintage_year'])} defaulted: timing mattered as much as the borrower"),
             ("stress", "3", "What if a recession hits?", pct(float(s["var99_loss_rate"])),
              f"lost in the worst 1 in 100 simulated economies ({compact(float(s['var99_loss_amount']), True)})")]
    if cut:
        t = cut["test"]
        cards.append(("policy", "4", "Which new loans should we approve?", pct(cut["cutoff_pd"], 1),
                      f"is the cut-off: decline applicants with a higher predicted default risk. That still approves "
                      f"{pct(t['approval_rate'], 1)} of them and adds {t['profit_uplift'] * 100:.2f}% to profit"))
    p.html_block('<div class="qgrid">' + "".join(
        f'<button class="qcard" data-go="{k}"><span class="qn">{n}</span><span class="qt">{html.escape(q)}</span>'
        f'<span class="qv">{html.escape(v)}</span><span class="qs">{html.escape(sub)}</span>'
        f'<span class="qgo">See the answer &rarr;</span></button>' for k, n, q, v, sub in cards) + '</div>')
    steps = [("Load and check", "Every file passes automated quality checks before it is stored"),
             ("Organise", "One tested definition of default and of every metric, built with dbt"),
             ("Predict", "Models estimate each loan's chance of default and early payoff"),
             ("Stress-test", "Thousands of simulated economies show how bad losses could get"),
             ("Decide", "Turn the risk estimates into an approval policy, and check it is fair")]
    p.html_block('<div class="card wide"><h3>How it works</h3><ol class="flow">' + "".join(
        f'<li><b>{html.escape(a)}</b><span>{html.escape(b)}</span></li>' for a, b in steps) + '</ol></div>')
    auc = f"{pdm['test']['xgboost']['auc']:.2f}" if pdm else "–"
    p.html_block(
        '<div class="card"><h3>How to use this dashboard</h3><ul class="tips">'
        '<li><b>Hover</b> any chart to read exact values.</li>'
        '<li><b>"How to read this"</b> under every chart title explains it in plain English.</li>'
        '<li><b>Table view</b> shows the numbers behind every chart.</li>'
        '<li><b>Time range</b> buttons zoom the history charts; drag on a chart to zoom, double-click to reset.</li>'
        '<li><b>Try it:</b> pick vintages to compare, filter the stress scenarios, and move the approval slider.</li>'
        '<li><b>Links:</b> every tab has its own address (for example <code>#stress</code>) you can share.</li></ul></div>')
    terms = [("Delinquent", "Behind on payments: 30, 60 or 90+ days late. 90+ days is <i>serious</i> delinquency."),
             ("Default", "A loan 90+ days late, in foreclosure, or sold at a loss (COVID forbearance excluded)."),
             ("Vintage", "The year a loan was made. Loans from the same year share the same economy."),
             ("LTV", "Loan-to-value: the loan as a share of the home's value. Above 100% the borrower is underwater."),
             ("PD", "Probability of default: the model's estimate that a loan defaults within 24 months."),
             ("LGD", "Loss given default: the share of the balance lost when a loan defaults, after the home is sold."),
             ("Expected loss", "PD x LGD x balance: what a loan is expected to cost on average."),
             ("CPR / CDR", "The share of the balance paid off early / defaulting each year."),
             ("Stress test", "Projecting losses through simulated bad economies."),
             ("99th percentile loss", "The loss exceeded in only 1 of 100 simulated economies."),
             ("AUC", f"How well the model ranks risk: 0.5 is a coin flip, 1.0 is perfect. Here: {auc}.")]
    p.html_block('<div class="card"><h3>Key terms</h3><dl class="terms">' + "".join(
        f'<dt>{html.escape(a)}</dt><dd>{b}</dd>' for a, b in terms) + '</dl></div>')
    return p


def _policy(cut: dict, curve: pd.DataFrame, pdm: dict | None) -> tuple[Page, dict]:
    """Approval-policy simulator: a PD-cutoff slider with margin and severity what-ifs.

    Profit is recomputed in the browser as margin x horizon x performing balance - severity x
    default losses, from the realised outcomes of the test vintages (see decision/cutoff.py).
    """
    p = Page("policy", "Who to approve")
    t, a = cut["test"], cut["assumptions"]
    curve = curve.sort_values("cutoff_pd").reset_index(drop=True)
    rec = int((curve["cutoff_pd"] - cut["cutoff_pd"]).abs().idxmin())
    lift = pdm["test"]["xgboost"]["recall_top10"] if pdm else None
    gain = t["realised_profit"] - t["realised_profit_approve_all"]
    p.intro("Which new loans should we approve?",
            f"Almost all of them. Decline only applicants whose predicted chance of default is above "
            f"<b>{pct(cut['cutoff_pd'], 1)}</b>, about 1 in {1 / (1 - t['approval_rate']):,.0f}. Tested on "
            f"<b>{t['loans']:,}</b> real loans made in {a['evaluated_on'].split('[')[-1].rstrip(']').replace(', ', '-')}, "
            f"that avoids <b>{pct(t['defaults_avoided_share'], 1)}</b> of defaults and adds <b>{compact(gain, True)}</b> "
            "of profit. Declining many more would lose money, because good borrowers get turned away with the bad. "
            "Move the slider to try other cut-offs."
            + (f" The model ranks risk well: the riskiest 10% of applicants account for <b>{pct(lift, 0)}</b> of defaults." if lift else ""))
    margins = sorted({0.0025, round(a["net_margin_annual"] * 2 / 3, 4), a["net_margin_annual"],
                      round(a["net_margin_annual"] * 4 / 3, 4), 0.01})
    mopts = "".join(f'<option value="{m}"{" selected" if m == a["net_margin_annual"] else ""}>{m * 100:.2f}% a year</option>'
                    for m in margins)
    sopts = "".join(f'<option value="{k}"{" selected" if k == 1.0 else ""}>{k:.2f}x{" (as observed)" if k == 1.0 else ""}</option>'
                    for k in (0.75, 1.0, 1.25, 1.5, 2.0))
    tiles = "".join(f'<div class="tile"><div class="tile-label">{lbl}</div><div class="tile-value" id="pol-{k}">–</div>'
                    f'<div class="delta neutral" id="pol-{k}-sub"></div></div>'
                    for k, lbl in (("approve", "Applications approved"), ("dr", "Default rate of approved loans"),
                                   ("avoided", "Defaults avoided"), ("profit", "2-year profit vs approving everyone")))
    p.html_block(
        '<div class="card wide sim"><div class="controls" role="group" aria-label="Approval policy">'
        '<label class="grow"><span>Decline applicants whose predicted default risk is above <output id="pol-cut">–</output></span>'
        f'<input type="range" id="pol-slider" min="0" max="{len(curve) - 1}" step="1" value="{rec}" '
        'aria-describedby="pol-scale"><span class="scale" id="pol-scale"><span>stricter</span><span>more lenient</span></span></label>'
        f'<label><span>Margin earned on a good loan</span><select id="pol-margin">{mopts}</select></label>'
        f'<label><span>Loss when a loan defaults</span><select id="pol-sev">{sopts}</select></label>'
        '<div class="btns"><button class="linkbtn" id="pol-rec">Recommended cutoff</button>'
        '<button class="linkbtn" id="pol-best">Most profitable for these assumptions</button></div></div>'
        f'<div class="tiles">{tiles}</div><p class="readout" id="pol-read" aria-live="polite"></p></div>')
    lay = base_layout(height=340, showlegend=True, hovermode="closest",
                      xaxis=dict(title="Share of applications approved", tickformat=".0%"),
                      yaxis=dict(title="Change in 2-year profit, $ millions", tickformat=",.0f", zeroline=True,
                                 zerolinecolor=LIGHT["axis"]),
                      margin=dict(l=72, r=20, t=16, b=44))
    p.chart([], lay, "Profit at every cutoff", "Realised outcomes of the test loans under the chosen margin and loss assumptions",
            explain="Approving everyone is the zero line. Declining the very riskiest applicants raises profit, because their "
                    "expected losses exceed the margin they would earn. Decline too many and profit falls again: you turn "
                    "away good borrowers along with the bad. The best policy sits at the top of the curve. The recommended "
                    f"cutoff was chosen on earlier loans ({a['chosen_on'].split('[')[-1].rstrip(']').replace(', ', '-')}) "
                    "so the result shown here is a fair, out-of-sample test.", cid="pol-curve")
    lay = base_layout(height=340, showlegend=True, hovermode="closest",
                      xaxis=dict(title="Share of applications declined (riskiest first)", tickformat=".0%"),
                      yaxis=dict(title="Share of defaults avoided", tickformat=".0%", rangemode="tozero"))
    p.chart([], lay, "How much risk the model catches", "Declining the riskiest applicants first, against declining at random",
            explain="If the model were useless, declining 10% of applicants would avoid about 10% of defaults (the grey "
                    "line). The further the blue curve sits above it, the better the model separates risky borrowers from "
                    "safe ones.", cid="pol-gains")
    pol = {"c": curve["cutoff_pd"].round(6).tolist(), "ar": curve["approval_rate"].round(6).tolist(),
           "al": curve["approved_loans"].astype(int).tolist(), "dra": curve["default_rate_approved"].round(6).tolist(),
           "ad": curve["approved_defaults"].astype(int).tolist(), "perf": curve["performing_upb"].round(0).tolist(),
           "loss": curve["default_loss"].round(0).tolist(), "horizon": a["horizon_years"], "rec": rec}
    return p, pol


# ---- assembly ------------------------------------------------------------------------------

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
.viz-root{color-scheme:light;--surface:#ffffff;--page:#f6f7f9;--ink:#16202c;--ink2:#4a5563;--muted:#8a929c;
 --grid:#e9ebee;--axis:#d2d6dc;--border:#e4e7eb;--accent:#1f4e79;--accent-soft:#e8eef5;--deemph:#cfd3d8;
 --good:#2f7d4f;--bad:#b83c3c}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface:#17191c;--page:#0f1113;--ink:#f2f4f7;--ink2:#b7bec8;
 --muted:#8a929c;--grid:#262a2f;--axis:#343a41;--border:#262a2f;--accent:#7fa7d6;--accent-soft:#1c2733;--deemph:#3d434a;
 --good:#4fae74;--bad:#e06a66}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
.viz-root{background:var(--page);color:var(--ink);min-height:100vh}
html{background:#f6f7f9} :root[data-theme="dark"]{background:#0f1113}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;padding:26px 28px 10px;max-width:1360px;margin:0 auto}
header .kicker{color:var(--accent);text-transform:uppercase;letter-spacing:.12em;font-size:11px;font-weight:600}
header h1{margin:4px 0 0;font-size:24px;font-weight:650;letter-spacing:-.01em}
header p{margin:6px 0 0;color:var(--ink2);font-size:13px}
.note{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12px;color:var(--ink2)}
nav{display:flex;gap:4px;padding:8px max(28px,calc((100% - 1360px)/2 + 28px)) 0;border-bottom:1px solid var(--grid);overflow-x:auto}
nav button{background:none;border:0;border-bottom:2px solid transparent;padding:10px 12px;font:inherit;font-size:14px;color:var(--ink2);cursor:pointer;white-space:nowrap}
nav button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.filters[hidden]{display:none}
.filters{display:flex;gap:8px;align-items:center;padding:14px 28px 0;font-size:13px;color:var(--ink2);max-width:1360px;margin:0 auto}
.tv{white-space:nowrap;flex:none}
.filters button,.theme,.tv{font:inherit;font-size:12px;border:1px solid var(--border);background:var(--surface);color:var(--ink);
 border-radius:6px;padding:5px 10px;cursor:pointer}
.filters button[aria-pressed="true"]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent);font-weight:600}
main{padding:18px 28px 40px;max-width:1360px;margin:0 auto}
section{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
section[hidden]{display:none}
.tiles{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tile-label{font-size:12.5px;color:var(--ink2)}
.tile-value{font-size:26px;font-weight:600;margin-top:4px}
.tile.hero .tile-value{font-size:48px;line-height:1.05}
.delta{font-size:12px;color:var(--ink2);margin-top:4px}
.delta.good{color:var(--good)} .delta.bad{color:var(--bad)}
.spark{width:100%;height:24px;margin-top:6px;display:block}
.spark-line{fill:none;stroke:var(--deemph);stroke-width:1.5;vector-effect:non-scaling-stroke}
.spark-dot{fill:var(--accent)}
.card{margin:0;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px 10px;min-width:0}
.card.wide{grid-column:1/-1}
figcaption{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
figcaption h3{margin:0;font-size:15px;font-weight:600}
.sub{margin:3px 0 0;font-size:12px;color:var(--ink2)}
.table-view,.table-static{max-height:340px;overflow:auto;margin:8px 0}
table{border-collapse:collapse;font-size:12px;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:600;position:sticky;top:0;background:var(--surface)}
footer{padding:0 28px 28px;color:var(--muted);font-size:12px;max-width:1360px;margin:0 auto}
.intro{grid-column:1/-1;display:grid;grid-template-columns:1fr 1.4fr;background:var(--surface);border:1px solid var(--border);
 border-radius:12px;overflow:hidden}
.intro>div{padding:14px 18px}
.intro>div+div{border-left:1px solid var(--border)}
.intro .lab{color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:10.5px;font-weight:600}
.intro .q{margin:4px 0 0;font-size:18px;font-weight:600;line-height:1.35}
.intro .a{margin:4px 0 0;font-size:14px;line-height:1.55;color:var(--ink2)}
@media (max-width:900px){.intro{grid-template-columns:1fr}.intro>div+div{border-left:0;border-top:1px solid var(--border)}}
.intro b,.lead b{color:var(--ink)}
.why{margin:8px 0 4px;font-size:13px;line-height:1.55;color:var(--ink2);max-width:980px}
.why>b:first-child{color:var(--ink);font-weight:600}
.controls{display:flex;flex-wrap:wrap;gap:14px 22px;align-items:flex-end;margin:10px 0 4px;font-size:13px;color:var(--ink2)}
.controls label{display:flex;flex-direction:column;gap:6px;min-width:200px}
.controls label>span:first-child{white-space:nowrap}
.controls label.grow{flex:1 1 360px}
.controls output{color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}
.controls select{font:inherit;font-size:13px;padding:5px 8px;border-radius:6px;border:1px solid var(--border);
 background:var(--surface);color:var(--ink)}
input[type=range]{width:100%;accent-color:var(--accent);cursor:pointer}
.scale{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}
.btns{display:flex;gap:8px;flex-wrap:wrap}
.linkbtn{font:inherit;font-size:12px;border:1px solid var(--border);background:var(--surface);color:var(--ink);
 border-radius:6px;padding:5px 10px;cursor:pointer}
.linkbtn:hover,.chip:hover,.qcard:hover{border-color:var(--accent)}
.readout{margin:6px 0 4px;font-size:13px;color:var(--ink2);min-height:18px}
.readout b{color:var(--ink)}
.sim .tiles{margin-top:10px}
.picker-head{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline}
.picker-head .linkbtn{margin-left:auto}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{font:inherit;font-size:12px;border:1px solid var(--border);background:var(--surface);color:var(--ink2);
 border-radius:999px;padding:4px 10px 4px 8px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}
.chip .sw{width:10px;height:10px;border-radius:50%;background:var(--deemph)}
.chip[aria-pressed="true"]{color:var(--ink);font-weight:600;border-color:var(--ink2)}
.lead h2{margin:0 0 8px;font-size:20px;font-weight:650}
.lead p{margin:6px 0;font-size:15px;line-height:1.6;color:var(--ink2);max-width:1000px}
.qgrid{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.qcard{text-align:left;font:inherit;background:var(--surface);color:var(--ink);border:1px solid var(--border);
 border-radius:12px;padding:16px;cursor:pointer;display:flex;flex-direction:column;gap:6px}
.qn{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--accent);color:#fff;font-size:12px;font-weight:700}
.qt{font-size:15px;font-weight:600}
.qv{font-size:34px;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}
.qs{font-size:13px;color:var(--ink2);line-height:1.45;flex:1}
.qgo{font-size:12px;color:var(--accent);font-weight:600}
.flow{list-style:none;counter-reset:step;display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;padding:0;margin:10px 0 4px}
.flow li{counter-increment:step;background:var(--page);border:1px solid var(--border);border-radius:10px;padding:12px;font-size:13px;
 color:var(--ink2);display:flex;flex-direction:column;gap:4px}
.flow li b{color:var(--ink);font-size:14px}
.flow li b::before{content:counter(step) "  ";color:var(--accent)}
.tips{margin:8px 0 4px;padding-left:18px;font-size:13px;line-height:1.7;color:var(--ink2)}
.tips b{color:var(--ink)}
.terms{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;margin:10px 0 4px;font-size:13px;line-height:1.45}
.terms dt{font-weight:600;color:var(--ink)} .terms dd{margin:0;color:var(--ink2)}
code{font-size:12px}
@media (max-width:900px){section{grid-template-columns:1fr}}
"""

JS = """
const LIGHT = %(light)s, DARK = {surface:'#17191c', ink:'#f2f4f7', ink2:'#b7bec8', muted:'#8a929c', grid:'#262a2f', axis:'#343a41', deemph:'#3d434a'};
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
  document.querySelectorAll('main section').forEach(s=>s.hidden = s.id!=='page-'+key);
  document.getElementById('filters').hidden = !['overview','rolls'].includes(key);
  document.querySelectorAll('#page-'+key+' .plot').forEach(el=>render(el.id)); history.replaceState(null,'','#'+key); window.scrollTo(0,0); }
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>showPage(b.dataset.page)));
document.querySelectorAll('.tv').forEach(b=>b.addEventListener('click',()=>{ const t=document.getElementById(b.getAttribute('aria-controls'));
  t.hidden=!t.hidden; b.setAttribute('aria-expanded', String(!t.hidden)); b.textContent = t.hidden?'Table view':'Hide table'; }));
document.getElementById('theme').addEventListener('click',()=>{ const r=document.documentElement; r.dataset.theme = r.dataset.theme==='dark'?'light':'dark';
  document.getElementById('theme').textContent = r.dataset.theme==='dark'?'Light theme':'Dark theme'; rendered.forEach(render); });
const END = new Date('%(end)s');
document.querySelectorAll('#filters button').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('#filters button').forEach(x=>x.setAttribute('aria-pressed', x===b));
  const yrs = +b.dataset.years; const range = yrs ? [new Date(END.getFullYear()-yrs, END.getMonth(), 1).toISOString().slice(0,10), END.toISOString().slice(0,10)] : null;
  document.querySelectorAll('#page-overview .plot, #page-rolls .plot').forEach(el=>{ const f=FIGS[el.id]; if (!f.layout.hovermode || f.layout.hovermode!=='x unified') return;
    f.layout.xaxis = Object.assign({}, f.layout.xaxis, range?{range:range, autorange:false}:{autorange:true}); if (!range) delete f.layout.xaxis.range;
    if (rendered.has(el.id)) render(el.id); }); }));
if (window.matchMedia('(prefers-color-scheme: dark)').matches) document.getElementById('theme').click();
showPage((location.hash||'#start').slice(1));
"""


# Interactive pieces (vintage picker, scenario explorer, policy simulator). A plain string - not
# %-formatted - so plotly templates such as %{y:.2%} need no escaping; data arrives in EXTRA.
JS_INTERACTIVE = """
const X = EXTRA;
const $ = id => document.getElementById(id);
const isDark = () => document.documentElement.dataset.theme === 'dark';
const fmtPct = (v, d=2) => (v*100).toFixed(d) + '%';
const fmtMoney = v => (v < 0 ? '-' : '') + '$' + (Math.abs(v) >= 1e9 ? (Math.abs(v)/1e9).toFixed(2) + 'B' : (Math.abs(v)/1e6).toFixed(1) + 'M');
function refresh(id){ if (rendered.has(id)) render(id); }
document.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => showPage(b.dataset.go)));

// ---- vintage picker: colours are assigned when a year is picked and kept until it is unpicked
const vSel = new Map();
function vPick(y){ const used = new Set(vSel.values()); for (let k = 0; k < 4; k++) if (!used.has(k)) { vSel.set(y, k); return true; } return false; }
function vintTraces(key){
  const tr = []; let first = true;
  for (const y of X.vint.years) { if (vSel.has(y)) continue; const c = X.vint.curves[String(y)];
    tr.push({type:'scatter', mode:'lines', x:c.m, y:c[key], name:'Other vintages', legendgroup:'other', showlegend:first,
             line:{color:LIGHT.deemph, width:1}, hovertemplate:'<b>%{y:.2%}</b> ' + y + ' vintage, month %{x}<extra></extra>'});
    first = false; }
  [...vSel.entries()].sort((a, b) => a[1] - b[1]).forEach(([y, k]) => { const c = X.vint.curves[String(y)], col = SL[k];
    tr.push({type:'scatter', mode:'lines', x:c.m, y:c[key], name: y + ' vintage', line:{color:col, width:2.5},
             hovertemplate:'<b>%{y:.2%}</b> ' + y + ' vintage, month %{x}<extra></extra>'});
    tr.push({type:'scatter', mode:'markers+text', x:[c.m[c.m.length-1]], y:[c[key][c[key].length-1]], text:[String(y)],
             textposition:'middle right', marker:{color:col, size:8, line:{color:LIGHT.surface, width:2}},
             textfont:{color:LIGHT.ink2}, showlegend:false, hoverinfo:'skip'}); });
  return tr; }
function vintUpdate(msg){
  for (const key of ['d', 'l']) { FIGS['vint-' + key].data = vintTraces(key); refresh('vint-' + key); }
  document.querySelectorAll('.chip').forEach(ch => { const y = +ch.dataset.year, on = vSel.has(y);
    ch.setAttribute('aria-pressed', on); ch.querySelector('.sw').style.background = on ? (isDark() ? SD : SL)[vSel.get(y)] : ''; });
  $('vint-note').textContent = msg || (vSel.size ? 'Highlighted: ' + [...vSel.keys()].sort().join(', ') : 'Pick a vintage to highlight it.'); }
if (X.vint) {
  X.vint.default.forEach(vPick);
  document.querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => { const y = +ch.dataset.year;
    if (vSel.has(y)) { vSel.delete(y); vintUpdate(); }
    else if (vPick(y)) vintUpdate(); else vintUpdate('Up to four vintages at a time: unpick one first.'); }));
  $('vint-reset').addEventListener('click', () => { vSel.clear(); X.vint.default.forEach(vPick); vintUpdate(); });
  vintUpdate(); }

// ---- stress scenario explorer
function sxUpdate(){
  const du = +$('sx-u').value, dh = +$('sx-h').value / 100, M = X.mc;
  $('sx-u-out').textContent = du.toFixed(1).replace('.0', ''); $('sx-h-out').textContent = (dh*100).toFixed(1).replace('.0', '') + '%';
  const inX = [], inY = [], inC = [], inD = [], outX = [], outY = [];
  for (let i = 0; i < M.u.length; i++) {
    if ((du <= 0 || M.u[i] >= du) && (dh <= 0 || M.h[i] >= dh)) { inX.push(M.u[i]); inY.push(M.l[i]); inC.push(M.h[i]); inD.push([M.h[i], M.pk[i]]); }
    else { outX.push(M.u[i]); outY.push(M.l[i]); } }
  const n = inY.length, hmax = Math.max(...M.h);
  FIGS['stress-drv'].data = [
    {type:'scatter', mode:'markers', x:outX, y:outY, name:'Filtered out', marker:{size:5, color:LIGHT.deemph}, hoverinfo:'skip'},
    {type:'scatter', mode:'markers', x:inX, y:inY, name:'Matching economies', customdata:inD,
     marker:{size:6, color:inC, colorscale:X.seq, cmin:0, cmax:hmax, line:{width:0},
             colorbar:{title:{text:'House-price fall'}, tickformat:'.0%', outlinewidth:0, thickness:12}},
     hovertemplate:'<b>%{y:.3%}</b> loss<br>unemployment +%{x:.1f} pts (peak %{customdata[1]:.1f}%)<br>house prices -%{customdata[0]:.0%}<extra></extra>'}];
  if (!n) $('sx-read').innerHTML = 'No simulated economy is that severe. Loosen a filter.';
  else { const mean = inY.reduce((a, b) => a + b, 0) / n, worst = Math.max(...inY);
    $('sx-read').innerHTML = '<b>' + n.toLocaleString() + '</b> of ' + M.u.length.toLocaleString() + ' simulated economies match. ' +
      'Average loss <b>' + fmtPct(mean) + '</b> (' + fmtMoney(mean * X.book) + '), worst <b>' + fmtPct(worst) + '</b> (' + fmtMoney(worst * X.book) + ').'; }
  refresh('stress-drv'); }
if (X.mc) { ['sx-u', 'sx-h'].forEach(id => $(id).addEventListener('input', sxUpdate));
  $('sx-reset').addEventListener('click', () => { $('sx-u').value = 0; $('sx-h').value = 0; sxUpdate(); }); sxUpdate(); }

// ---- approval policy simulator: profit = margin x horizon x performing balance - severity x default losses
function polProfit(i, m, k){ const P = X.pol; return m * P.horizon * P.perf[i] - k * P.loss[i]; }
function polBest(m, k){ let b = 0; for (let i = 1; i < X.pol.c.length; i++) if (polProfit(i, m, k) > polProfit(b, m, k)) b = i; return b; }
function polUpdate(){
  const P = X.pol, i = +$('pol-slider').value, m = +$('pol-margin').value, k = +$('pol-sev').value, all = P.c.length - 1;
  const base = polProfit(all, m, k), gain = P.c.map((_, j) => (polProfit(j, m, k) - base) / 1e6);
  const totD = P.ad[all], totL = P.al[all], best = polBest(m, k);
  $('pol-cut').textContent = fmtPct(P.c[i]);
  $('pol-approve').textContent = fmtPct(P.ar[i], 1);
  $('pol-approve-sub').textContent = (totL - P.al[i]).toLocaleString() + ' of ' + totL.toLocaleString() + ' declined';
  $('pol-dr').textContent = fmtPct(P.dra[i]);
  $('pol-dr-sub').textContent = 'vs ' + fmtPct(P.dra[all]) + ' if everyone is approved';
  $('pol-avoided').textContent = fmtPct(1 - P.ad[i] / totD, 1);
  $('pol-avoided-sub').textContent = (totD - P.ad[i]).toLocaleString() + ' of ' + totD.toLocaleString() + ' defaults';
  const d = gain[i] * 1e6;
  $('pol-profit').textContent = (d >= 0 ? '+' : '-') + fmtMoney(Math.abs(d)).replace('-', '');
  $('pol-profit-sub').textContent = base > 0 ? ((d / base) * 100).toFixed(2).replace(/^(?!-)/, '+') + '% vs approving everyone' : 'approving everyone loses money here';
  $('pol-read').innerHTML = i === P.rec ? 'This is the <b>recommended cutoff</b>, chosen on earlier loans and tested here on later ones.'
    : (i === best ? 'This is the most profitable cutoff for these assumptions, found with hindsight on these loans.'
       : 'The most profitable cutoff for these assumptions is <b>' + fmtPct(P.c[best]) + '</b> (approving ' + fmtPct(P.ar[best], 1) + ').');
  const ring = {color:LIGHT.surface, width:2};
  FIGS['pol-curve'].data = [
    {type:'scatter', mode:'lines', x:P.ar, y:gain, name:'Profit vs approving everyone', line:{color:SL[0], width:2},
     hovertemplate:'Approve <b>%{x:.1%}</b>: <b>%{y:+$,.1f}M</b><extra></extra>'},
    {type:'scatter', mode:'markers', x:[P.ar[P.rec]], y:[gain[P.rec]], name:'Recommended cutoff',
     marker:{color:LIGHT.ink2, size:11, symbol:'diamond', line:ring}, hovertemplate:'Recommended: %{y:+$,.1f}M<extra></extra>'},
    {type:'scatter', mode:'markers', x:[P.ar[i]], y:[gain[i]], name:'Your cutoff', marker:{color:SL[1], size:12, line:ring},
     hovertemplate:'Your cutoff: approve %{x:.1%}, %{y:+$,.1f}M<extra></extra>'}];
  const declined = P.ar.map(v => 1 - v), avoided = P.ad.map(v => 1 - v / totD);
  FIGS['pol-gains'].data = [
    {type:'scatter', mode:'lines', x:[0, Math.max(...declined)], y:[0, Math.max(...declined)], name:'Declining at random',
     line:{color:LIGHT.muted, width:1.5}, hoverinfo:'skip'},
    {type:'scatter', mode:'lines', x:declined, y:avoided, name:'Declining by model risk', line:{color:SL[0], width:2},
     hovertemplate:'Decline <b>%{x:.1%}</b> of applicants, avoid <b>%{y:.1%}</b> of defaults<extra></extra>'},
    {type:'scatter', mode:'markers', x:[declined[i]], y:[avoided[i]], name:'Your cutoff', marker:{color:SL[1], size:12, line:ring},
     hoverinfo:'skip'}];
  // Open on the 90-100% approval range where the decision is made; widen to keep the chosen point in view.
  const lo = Math.max(0, Math.min(0.9, P.ar[i] - 0.02, P.ar[best] - 0.02)), vis = gain.filter((_, j) => P.ar[j] >= lo);
  const ylo = Math.min(...vis), yhi = Math.max(...vis), pad = (yhi - ylo) * 0.12 || 1;
  const L = FIGS['pol-curve'].layout;
  L.xaxis = Object.assign({}, L.xaxis, {range:[lo, 1.004], autorange:false});
  L.yaxis = Object.assign({}, L.yaxis, {range:[ylo - pad, yhi + pad], autorange:false});
  refresh('pol-curve'); refresh('pol-gains'); }
if (X.pol) { ['pol-slider', 'pol-margin', 'pol-sev'].forEach(id => $(id).addEventListener('input', polUpdate));
  $('pol-rec').addEventListener('click', () => { $('pol-slider').value = X.pol.rec; polUpdate(); });
  $('pol-best').addEventListener('click', () => { $('pol-slider').value = polBest(+$('pol-margin').value, +$('pol-sev').value); polUpdate(); });
  polUpdate(); }
$('theme').addEventListener('click', () => { if (X.vint) vintUpdate(); });
"""


def build_dashboard(settings: Settings) -> str:
    art = settings.artifacts_dir
    load = lambda path: json.loads(path.read_text()) if path.exists() else None  # noqa: E731
    cut, pdm = load(art / "decision" / "cutoff.json"), load(art / "models" / "pd_metrics.json")
    curve_path = art / "decision" / "tradeoff_test.csv"
    curve = pd.read_csv(curve_path) if curve_path.exists() else None
    with connect(settings, read_only=True) as con:
        s = dict(con.execute("select metric, value from ml.stress_summary").fetchall())
        vint_page, vint = _vintages(con)
        stress_page, mc = _stress(con, s)
        pages = [_start(con, s, cut, pdm), _overview(con, s), vint_page, _roll_rates(con), _segments(con), stress_page]
        extra = {"vint": vint, "mc": mc, "seq": SEQ_SCALE, "book": float(s["portfolio_upb"]), "pol": None}
        if cut and curve is not None and "performing_upb" in curve:
            policy_page, extra["pol"] = _policy(cut, curve, pdm)
            pages.append(policy_page)
        pages.append(_quality(con))
        end = con.execute("select max(period_month) from analytics.mart_portfolio_monthly").fetchone()[0]
        loans = con.execute("select count(*) from core.dim_loan").fetchone()[0]
        profile_note = ("synthetic test data" if settings["synthetic"]
                        else f"{loans / 1e6:.2f}M Freddie Mac loans")
    figs = {f["id"]: {"data": f["data"], "layout": f["layout"]} for p in pages for f in p.figs}
    nav = "".join(f'<button data-page="{p.key}" aria-selected="false">{html.escape(p.title)}</button>' for p in pages)
    # "page-" prefix: the URL hash (#stress) must not match an element id, or the browser scrolls to it on load
    sections = "".join(f'<section id="page-{p.key}" hidden>{"".join(p.blocks)}</section>' for p in pages)
    js = JS % {"light": json.dumps({"surface": LIGHT["surface"], "ink": LIGHT["ink"], "ink2": LIGHT["ink2"],
                                    "muted": LIGHT["muted"], "grid": LIGHT["grid"], "axis": LIGHT["axis"],
                                    "deemph": LIGHT["deemph"]}),
               "sl": json.dumps(SERIES_LIGHT), "sd": json.dumps(SERIES_DARK), "figs": json.dumps(figs),
               "end": str(end)[:10]}
    doc = f"""<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LoanLens - portfolio risk dashboard</title>
<style>{CSS}</style><script>{get_plotlyjs()}</script></head>
<body><div class="viz-root">
<header><div><span class="kicker">LoanLens · mortgage risk</span><h1>Which home loans will go bad, and what should a lender do about it?</h1><p>{html.escape(profile_note)} · followed month by month through {str(end)[:7]} · economic data from FRED</p></div>
<div style="display:flex;gap:8px;align-items:center"><span class="note">Also available as a Power BI kit</span>
<button class="theme" id="theme">Dark theme</button></div></header>
<nav role="tablist">{nav}</nav>
<div class="filters" id="filters" hidden><span>Time range</span>
<button data-years="0" aria-pressed="true">All history</button><button data-years="10" aria-pressed="false">Last 10 years</button>
<button data-years="5" aria-pressed="false">Last 5 years</button><button data-years="2" aria-pressed="false">Last 24 months</button></div>
<main>{sections}</main>
<footer>Generated by <code>loanlens report</code>. Grey bands on history charts are US recessions (NBER). Every chart has a table view.</footer>
</div><script>{js}</script>
<script>const EXTRA = {json.dumps(extra)};</script><script>{JS_INTERACTIVE}</script></body></html>"""
    path = settings.reports_dir / "dashboard.html"
    path.write_text(doc)
    return str(path)

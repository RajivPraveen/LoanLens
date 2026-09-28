"""reports/figures/banner.svg - the README header, drawn from the warehouse on every report run.

A single series (serious-delinquency rate of the whole sample, 2000 to the data end) on the
palette's dark surface, with the two peaks direct-labelled, so the banner is itself a true chart
of the data the project analyses.
"""

from __future__ import annotations

import html

import pandas as pd

from loanlens.config import Settings
from loanlens.warehouse import query

W, H = 1280, 400
PAD = 56
SURFACE, INK, INK_2, MUTED, GRID = "#1a1a19", "#ffffff", "#c3c2b7", "#898781", "#3a3a37"
SERIES = "#3987e5"          # categorical slot 1, dark-mode step
FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"


def _peak(df: pd.DataFrame, lo: str, hi: str) -> pd.Series | None:
    window = df[(df["period_month"] >= lo) & (df["period_month"] <= hi)].dropna(subset=["serious_dq_rate"])
    return None if window.empty else window.loc[window["serious_dq_rate"].idxmax()]


def build_banner(settings: Settings) -> None:
    df = query(settings, """select period_month, serious_dq_rate from analytics.mart_portfolio_monthly
                            where period_month >= date '2000-01-01' order by period_month""")
    vol = query(settings, "select count(*) as loans, sum(months_reported) as records from core.dim_loan").iloc[0]
    df["period_month"] = pd.to_datetime(df["period_month"])
    first, last = df["period_month"].min(), df["period_month"].max()

    x0, x1, y0, y1 = PAD, W - PAD, 372, 252          # chart box (y grows downward)
    top = df["serious_dq_rate"].max() * 1.12

    def x(t):
        return x0 + (x1 - x0) * (t - first) / (last - first)

    def y(v):
        return y0 - (y0 - y1) * v / top

    pts = [(x(t), y(v)) for t, v in zip(df["period_month"], df["serious_dq_rate"], strict=True)]
    line = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = f"{line} L{pts[-1][0]:.1f},{y0} L{pts[0][0]:.1f},{y0} Z"

    ticks = "".join(
        f'<text x="{x(pd.Timestamp(f"{yr}-01-01")):.1f}" y="{y0 + 20}" fill="{MUTED}" font-size="13" '
        f'text-anchor="middle">{yr}</text>'
        for yr in range(2000, last.year + 1, 5))

    labels, notes = [], []
    for (lo, hi, what) in (("2008-01-01", "2012-12-31", "housing-crisis peak"),
                           ("2020-01-01", "2021-12-31", "COVID forbearance")):
        p = _peak(df, lo, hi)
        if p is None:           # period not covered by the data
            continue
        px, py = x(p["period_month"]), y(p["serious_dq_rate"])
        text = f"{p['period_month']:%b %Y} · {p['serious_dq_rate'] * 100:.1f}% · {what}"
        notes.append(text)
        labels.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5" fill="{SERIES}" stroke="{SURFACE}" stroke-width="2"/>'
            f'<text x="{px + 10:.1f}" y="{py - 11:.1f}" fill="{INK}" font-size="14">{html.escape(text)}</text>')

    stats = (f"{vol['loans'] / 1e6:.2f}M Freddie Mac loans  ·  {vol['records'] / 1e6:.0f}M loan-months  ·  "
             f"{first.year - 1}–{last:%Y}  ·  FRED unemployment, house prices and mortgage rates")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">
<title id="t">LoanLens - mortgage credit risk and portfolio analytics</title>
<desc id="d">Serious-delinquency rate of the Freddie Mac loan sample, {first:%Y} to {last:%Y}. Peaks: {html.escape('; '.join(notes))}.</desc>
<rect width="{W}" height="{H}" rx="16" fill="{SURFACE}"/>
<g font-family="{FONT}">
<text x="{PAD}" y="104" fill="{INK}" font-size="64" font-weight="700" letter-spacing="-1">LoanLens</text>
<text x="{PAD}" y="146" fill="{INK_2}" font-size="26">Mortgage credit risk and portfolio analytics, end to end</text>
<text x="{PAD}" y="182" fill="{INK_2}" font-size="17">{html.escape(stats)}</text>
<text x="{PAD}" y="226" fill="{MUTED}" font-size="13">Serious delinquency (90+ days or REO), share of active loans</text>
<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" stroke="{GRID}" stroke-width="1"/>
<path d="{area}" fill="{SERIES}" fill-opacity="0.18"/>
<path d="{line}" fill="none" stroke="{SERIES}" stroke-width="2" stroke-linejoin="round"/>
{''.join(labels)}
{ticks}
</g>
</svg>
"""
    settings.figures_dir.mkdir(parents=True, exist_ok=True)
    (settings.figures_dir / "banner.svg").write_text(svg)

"""Tiny helpers for writing the generated markdown reports."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import pandas as pd


def fmt_pct(x, decimals: int = 2) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{decimals}f}%"


def fmt_num(x, decimals: int = 3) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    if isinstance(x, int | float) and abs(x) >= 1000 and float(x).is_integer():
        return f"{int(x):,}"
    return f"{x:,.{decimals}f}" if isinstance(x, float) else str(x)


def fmt_money(x, unit: str = "") -> str:
    if unit == "m":
        return f"${x / 1e6:,.1f}M"
    if unit == "bn":
        return f"${x / 1e9:,.2f}B"
    return f"${x:,.0f}"


def table(df: pd.DataFrame, formats: dict[str, Callable] | None = None,
          align: Sequence[str] | None = None) -> str:
    """Render a DataFrame as a GitHub-flavoured markdown table."""
    formats = formats or {}
    cols = list(df.columns)
    header = "| " + " | ".join(str(c) for c in cols) + " |"
    if align is None:
        align = ["right" if pd.api.types.is_numeric_dtype(df[c]) else "left" for c in cols]
    sep = "|" + "|".join("---:" if a == "right" else ":---" for a in align) + "|"
    rows = []
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            if c in formats:
                cells.append(formats[c](v))
            elif isinstance(v, float):
                cells.append(fmt_num(v))
            else:
                cells.append(str(v))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, sep, *rows])

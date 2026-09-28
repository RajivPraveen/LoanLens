"""Small shared helpers: state reference data, month arithmetic and quarter labels."""

from __future__ import annotations

import re
from functools import lru_cache

import pandas as pd

from loanlens.config import REPO_ROOT

QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")


@lru_cache(maxsize=1)
def state_reference() -> pd.DataFrame:
    path = REPO_ROOT / "dbt" / "seeds" / "state_reference.csv"
    return pd.read_csv(path, dtype={"zip3": str})


def resolve_states(setting) -> list[str]:
    all_states = state_reference()["state_code"].tolist()
    if setting in (None, "all"):
        return all_states
    unknown = set(setting) - set(all_states)
    if unknown:
        raise ValueError(f"Unknown states in config: {sorted(unknown)}")
    return [s for s in all_states if s in setting]


def parse_quarter(label: str) -> tuple[int, int]:
    match = QUARTER_RE.match(label)
    if not match:
        raise ValueError(f"Quarter labels look like 2005Q1, got {label!r}")
    return int(match.group(1)), int(match.group(2))


def quarter_range(first: str, last: str) -> list[str]:
    y0, q0 = parse_quarter(first)
    y1, q1 = parse_quarter(last)
    out, y, q = [], y0, q0
    while (y, q) <= (y1, q1):
        out.append(f"{y}Q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def month_index(period: str | pd.Period) -> int:
    """Months since 1970-01; works for 'YYYY-MM', 'YYYYMM' or a Period."""
    s = str(period).replace("-", "")[:6]
    return (int(s[:4]) - 1970) * 12 + int(s[4:6]) - 1


def index_to_yyyymm(idx):
    """Vectorised inverse of month_index returning YYYYMM integers."""
    year = idx // 12 + 1970
    month = idx % 12 + 1
    return year * 100 + month

"""FRED (Federal Reserve Economic Data) client.

Uses the official API when ``FRED_API_KEY`` is set, otherwise the public ``fredgraph.csv``
endpoint (no key required). Every series is cached as CSV under ``data/raw/fred`` so that
reruns are offline and reproducible.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from loanlens.config import Settings
from loanlens.reference import resolve_states

log = logging.getLogger(__name__)

API_URL = "https://api.stlouisfed.org/fred/series/observations"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


@dataclass(frozen=True)
class SeriesSpec:
    series_id: str
    geo: str        # "US" or a state code
    measure: str    # unemployment_rate | hpi | mortgage_rate_30y


def series_catalog(settings: Settings) -> list[SeriesSpec]:
    fred_cfg = settings["fred"]
    specs = [SeriesSpec(sid, "US", measure) for sid, measure in fred_cfg["national_series"].items()]
    states = resolve_states((settings["synthetic"] or {}).get("states", "all"))
    for template, measure in fred_cfg["state_series"].items():
        specs += [SeriesSpec(template.format(st=st), st, measure) for st in states]
    return specs


def _fetch_remote(series_id: str, start: str, session: requests.Session) -> pd.DataFrame:
    api_key = os.environ.get("FRED_API_KEY")
    if api_key:
        resp = session.get(
            API_URL,
            params={"series_id": series_id, "api_key": api_key, "file_type": "json",
                    "observation_start": start},
            timeout=30,
        )
        resp.raise_for_status()
        obs = resp.json()["observations"]
        df = pd.DataFrame(obs)[["date", "value"]]
    else:
        resp = session.get(CSV_URL, params={"id": series_id, "cosd": start}, timeout=30)
        resp.raise_for_status()
        from io import StringIO

        df = pd.read_csv(StringIO(resp.text))
        df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"].replace(".", np.nan), errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df[df["date"] >= pd.Timestamp(start)]


def fetch_all(settings: Settings, refresh: bool = False, session: requests.Session | None = None) -> list[Path]:
    """Download (or reuse cached) CSVs for every series in the catalog."""
    fred_cfg = settings["fred"]
    session = session or requests.Session()
    out_dir = settings.raw_fred_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stale_before = datetime.now() - timedelta(days=fred_cfg["cache_days"])
    paths = []
    for spec in series_catalog(settings):
        path = out_dir / f"{spec.series_id}.csv"
        fresh = path.exists() and datetime.fromtimestamp(path.stat().st_mtime) > stale_before
        if fresh and not refresh:
            paths.append(path)
            continue
        for attempt in range(3):
            try:
                df = _fetch_remote(spec.series_id, fred_cfg["start"], session)
                break
            except requests.RequestException as exc:
                if attempt == 2:
                    if path.exists():
                        log.warning("FRED fetch failed for %s, using stale cache: %s", spec.series_id, exc)
                        df = None
                        break
                    raise
                time.sleep(2 ** attempt)
        if df is not None:
            df.to_csv(path, index=False)
            time.sleep(fred_cfg["request_pause_seconds"])
        paths.append(path)
    log.info("FRED: %d series available in %s", len(paths), out_dir)
    return paths


def load_observations(settings: Settings) -> pd.DataFrame:
    """Long table of cached observations joined to the catalog."""
    frames = []
    for spec in series_catalog(settings):
        path = settings.raw_fred_dir / f"{spec.series_id}.csv"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing - run `loanlens fetch-fred` first")
        df = pd.read_csv(path, parse_dates=["date"])
        df["series_id"], df["geo"], df["measure"] = spec.series_id, spec.geo, spec.measure
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    return out.rename(columns={"date": "observation_date"})


def monthly_panel(obs: pd.DataFrame, end_month: str) -> pd.DataFrame:
    """Monthly geo x month panel. Weekly series are averaged, quarterly series forward-filled
    within the quarter, and the final value is carried forward to ``end_month``.

    This mirrors the logic of the dbt model ``stg_fred__macro_monthly``.
    """
    obs = obs.dropna(subset=["value"]).copy()
    obs["month"] = obs["observation_date"].dt.to_period("M")
    monthly = obs.groupby(["geo", "measure", "month"], as_index=False)["value"].mean()
    wide = monthly.pivot_table(index=["geo", "month"], columns="measure", values="value")
    frames = []
    end = pd.Period(end_month, freq="M")
    for geo, g in wide.groupby(level="geo"):
        g = g.droplevel("geo")
        full = pd.period_range(g.index.min(), max(end, g.index.max()), freq="M")
        g = g.reindex(full).ffill()
        g.index.name = "month"
        g["geo"] = geo
        frames.append(g.reset_index())
    return pd.concat(frames, ignore_index=True)

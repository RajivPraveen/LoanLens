"""Shared fixtures. Everything runs offline in a temporary workspace on the `tiny` profile.

FRED is replaced by deterministic fixture series with a realistic shape (a 2001 recession,
the 2008 crisis with falling house prices, a 2020 unemployment spike) so the whole pipeline -
generator, validation, dbt, models, stress test - can be exercised end to end in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from loanlens.config import load_settings


def _unemployment(months: pd.DatetimeIndex, level: float) -> np.ndarray:
    t = np.arange(len(months))
    year = months.year + (months.month - 1) / 12
    u = np.full(len(months), level)
    u += 1.8 * np.exp(-((year - 2002.5) ** 2) / 0.8)
    u += 5.0 * np.exp(-((year - 2010.0) ** 2) / 2.5)
    u += 9.0 * np.exp(-((year - 2020.4) ** 2) / 0.02)
    return np.round(u + 0.05 * np.sin(t / 5), 1)


def _hpi(quarters: pd.DatetimeIndex, beta: float) -> np.ndarray:
    year = quarters.year + (quarters.month - 1) / 12
    growth = np.where(year < 2006.5, 0.06, np.where(year < 2011.5, -0.06 * beta, 0.05))
    return 100 * np.exp(np.cumsum(growth / 4))


def write_fred_fixtures(settings) -> None:
    from loanlens.ingest.fred import series_catalog

    out = settings.raw_fred_dir
    out.mkdir(parents=True, exist_ok=True)
    monthly = pd.date_range("1990-01-01", "2019-12-01", freq="MS")
    quarterly = pd.date_range("1990-01-01", "2019-10-01", freq="QS")
    weekly = pd.date_range("1990-01-05", "2019-12-27", freq="W-FRI")
    for i, spec in enumerate(series_catalog(settings)):
        if spec.measure == "unemployment_rate":
            df = pd.DataFrame({"date": monthly, "value": _unemployment(monthly, 4.5 + 0.2 * (i % 5))})
        elif spec.measure == "hpi":
            df = pd.DataFrame({"date": quarterly, "value": np.round(_hpi(quarterly, 1.0 + 0.3 * (i % 3)), 2)})
        else:
            year = weekly.year + (weekly.dayofyear / 365)
            rate = 8.0 - 0.25 * (year - 1990) + 0.4 * np.sin(year)
            df = pd.DataFrame({"date": weekly, "value": np.round(np.clip(rate, 3.0, 9.0), 2)})
        df.to_csv(out / f"{spec.series_id}.csv", index=False)


@pytest.fixture(scope="session")
def workspace(tmp_path_factory) -> Path:
    ws = tmp_path_factory.mktemp("loanlens_ws")
    os.environ["LOANLENS_WORKSPACE"] = str(ws)
    os.environ["LOANLENS_PROFILE"] = "tiny"
    return ws


@pytest.fixture(scope="session")
def settings(workspace):
    s = load_settings("tiny", workspace)
    write_fred_fixtures(s)
    return s


@pytest.fixture()
def fresh_settings(tmp_path):
    """An isolated, empty workspace for tests that mutate files or the warehouse."""
    s = load_settings("tiny", tmp_path)
    write_fred_fixtures(s)
    return s


@pytest.fixture(scope="session")
def built(settings):
    """The full pipeline run once on the tiny profile (used by the slow end-to-end tests)."""
    from loanlens import pipeline

    pipeline.synthesize(settings)
    pipeline.ingest(settings)
    pipeline.transform(settings)
    return settings

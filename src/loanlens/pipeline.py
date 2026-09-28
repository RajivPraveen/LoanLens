"""Pipeline steps shared by the CLI, the Dagster assets and the tests.

    fetch_fred -> synthesize (optional) -> ingest -> transform (dbt core)
      -> train (PD) -> survival -> stress (+ backtest, book scoring) -> decide -> fairness
      -> report (dbt reporting layer, BI exports, dashboard, results summary)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from loanlens.config import Settings

log = logging.getLogger(__name__)


def fetch_fred(settings: Settings, refresh: bool = False) -> int:
    from loanlens.ingest.fred import fetch_all

    return len(fetch_all(settings, refresh=refresh))


def synthesize(settings: Settings, quarters: list[str] | None = None, overwrite: bool = False) -> int:
    from loanlens.synthetic.generator import generate

    return len(generate(settings, quarters=quarters, overwrite=overwrite))


def ingest(settings: Settings, periods: list[str] | None = None, force: bool = False) -> dict:
    from loanlens.ingest.freddie import ingest_fred, ingest_freddie

    fred_rows = ingest_fred(settings)
    results = ingest_freddie(settings, periods=periods, force=force)
    return {
        "fred_observations": fred_rows,
        "loaded": [r.period for r in results if r.status == "loaded"],
        "skipped": [r.period for r in results if r.status == "skipped"],
        "orig_rows": sum(r.orig_rows for r in results),
        "perf_rows": sum(r.perf_rows for r in results),
    }


def transform(settings: Settings, full_refresh: bool = False) -> None:
    from loanlens.dbt_runner import build_core

    build_core(settings, full_refresh=full_refresh)


def train(settings: Settings) -> dict:
    from loanlens.models.default_model import train as train_pd

    return train_pd(settings)


def survival(settings: Settings) -> dict:
    from loanlens.models.survival import run

    return run(settings)


def stress(settings: Settings) -> dict:
    from loanlens.stress.engine import run_stress

    return run_stress(settings)


def decide(settings: Settings) -> dict:
    from loanlens.decision.cutoff import optimise

    return optimise(settings)


def fairness(settings: Settings) -> dict:
    from loanlens.models.fairness import run

    return run(settings)


def report(settings: Settings) -> None:
    from loanlens.dbt_runner import build_reporting
    from loanlens.reporting.banner import build_banner
    from loanlens.reporting.bi_export import export_powerbi
    from loanlens.reporting.dashboard import build_dashboard
    from loanlens.reporting.results import write_results_summary

    build_reporting(settings)
    export_powerbi(settings)
    build_dashboard(settings)
    build_banner(settings)
    write_results_summary(settings)


STEPS: list[tuple[str, Callable]] = [
    ("fetch_fred", fetch_fred),
    ("synthesize", synthesize),
    ("ingest", ingest),
    ("transform", transform),
    ("train", train),
    ("survival", survival),
    ("stress", stress),
    ("decide", decide),
    ("fairness", fairness),
    ("report", report),
]


def run_all(settings: Settings, skip_synthetic: bool = False) -> dict[str, float]:
    timings = {}
    for name, step in STEPS:
        if name == "synthesize" and (skip_synthetic or not settings["synthetic"]):
            continue
        start = time.time()
        log.info("=== %s ===", name)
        step(settings)
        timings[name] = round(time.time() - start, 1)
    log.info("Pipeline finished: %s", timings)
    return timings

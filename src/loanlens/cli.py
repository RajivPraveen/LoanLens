"""`loanlens` command line interface."""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="LoanLens: mortgage credit risk and portfolio analytics platform.")

ProfileOpt = Annotated[str | None, typer.Option("--profile", "-p", help="Config profile (default: $LOANLENS_PROFILE or freddie)")]


def _settings(profile: str | None):
    from loanlens.config import load_settings

    logging.basicConfig(level=os.environ.get("LOANLENS_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S")
    for noisy in ("great_expectations", "matplotlib", "urllib3", "shap"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return load_settings(profile)


def _echo(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj)


@app.command("fetch-fred")
def fetch_fred(profile: ProfileOpt = None, refresh: bool = typer.Option(False, help="Ignore the local cache")):
    """Download FRED unemployment, house-price and mortgage-rate series."""
    from loanlens import pipeline

    _echo(f"{pipeline.fetch_fred(_settings(profile), refresh)} series cached")


@app.command()
def synth(profile: ProfileOpt = None,
          quarters: Annotated[list[str] | None, typer.Option("--quarter", "-q", help="e.g. 2005Q1 (repeatable)")] = None,
          overwrite: bool = False):
    """Generate synthetic loan files in the Freddie Mac layout."""
    from loanlens import pipeline

    _echo(f"{pipeline.synthesize(_settings(profile), quarters, overwrite)} quarterly files written")


@app.command()
def ingest(profile: ProfileOpt = None,
           periods: Annotated[list[str] | None, typer.Option("--period", help="Only these source periods")] = None,
           force: bool = typer.Option(False, help="Reload even if the file is unchanged")):
    """Validate and incrementally load FRED and Freddie Mac files into the warehouse."""
    from loanlens import pipeline

    result = pipeline.ingest(_settings(profile), periods, force)
    _echo({k: (v if not isinstance(v, list) or len(v) < 8 else f"{len(v)} periods") for k, v in result.items()})


@app.command()
def transform(profile: ProfileOpt = None, full_refresh: bool = False):
    """Build and test the dbt star schema and analytics marts."""
    from loanlens import pipeline

    pipeline.transform(_settings(profile), full_refresh)


@app.command()
def train(profile: ProfileOpt = None):
    """Train and evaluate the origination default models (logistic vs XGBoost)."""
    from loanlens import pipeline

    m = pipeline.train(_settings(profile))
    _echo({k: {kk: round(vv, 4) for kk, vv in v.items() if kk in ("auc", "pr_auc", "ks", "brier", "ece", "mean_pd", "base_rate")}
           for k, v in m["test"].items()})


@app.command()
def survival(profile: ProfileOpt = None):
    """Kaplan-Meier, Aalen-Johansen and Cox survival models for default and prepayment."""
    from loanlens import pipeline

    s = pipeline.survival(_settings(profile))
    _echo({"events": s["events"], "cox_default_concordance": s["cox_default"]["concordance"]})


@app.command()
def stress(profile: ProfileOpt = None):
    """Monte Carlo stress test, 2008-2010 backtest and current-book scoring."""
    from loanlens import pipeline

    s = pipeline.stress(_settings(profile))
    _echo({k: v for k, v in s.items() if k.endswith("_rate") or k in ("n_scenarios", "portfolio_loans")})


@app.command()
def decide(profile: ProfileOpt = None):
    """Choose the profit-maximising PD approval cutoff and write the lending memo."""
    from loanlens import pipeline

    r = pipeline.decide(_settings(profile))
    _echo({"cutoff_pd": r["cutoff_pd"], **r["test"]})


@app.command()
def fairness(profile: ProfileOpt = None):
    """Fair-lending review of the default model by geography and borrower group."""
    from loanlens import pipeline

    _echo(pipeline.fairness(_settings(profile)))


@app.command()
def report(profile: ProfileOpt = None):
    """Reporting marts, Power BI exports, HTML dashboard and results summary."""
    from loanlens import pipeline

    pipeline.report(_settings(profile))


@app.command("all")
def run_all(profile: ProfileOpt = None, skip_synthetic: bool = typer.Option(False, help="Use files already in data/raw/freddie")):
    """Run the whole pipeline end to end."""
    from loanlens import pipeline

    _echo(pipeline.run_all(_settings(profile), skip_synthetic))


@app.command()
def status(profile: ProfileOpt = None):
    """Show load history and data-quality pass rates."""
    from loanlens.warehouse import query

    s = _settings(profile)
    typer.echo(query(s, """select status, count(*) as periods, sum(orig_rows) as loans,
                           sum(perf_rows) as monthly_records, max(finished_at) as last_load
                           from meta.ingest_log group by 1""").to_string(index=False))
    typer.echo(query(s, """select suite, count(*) as checks, round(avg(success::int) * 100, 2) as pass_rate_pct
                           from meta.dq_results group by 1 order by 1""").to_string(index=False))


@app.command("publish-postgres")
def publish_postgres(profile: ProfileOpt = None,
                     dsn: str = typer.Option(None, envvar="LOANLENS_POSTGRES_DSN",
                                             help="postgresql://user:pass@host:5432/db")):
    """Copy the star schema and marts to PostgreSQL (serving layer for Power BI)."""
    from loanlens.reporting.postgres import publish

    _echo(publish(_settings(profile), dsn))


if __name__ == "__main__":
    app()

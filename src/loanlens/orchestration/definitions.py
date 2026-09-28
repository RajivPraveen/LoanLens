"""Dagster orchestration: the whole platform as software-defined assets.

    dagster dev -m loanlens.orchestration.definitions

Lineage: FRED + Freddie files -> raw tables (validated) -> dbt star schema & marts (every
model and test is its own asset/check via dagster-dbt) -> PD model / survival / stress test ->
dbt reporting layer -> Power BI exports, dashboard, memo, fairness review.

* Retries: every Python asset retries up to 3 times with exponential backoff.
* Schedule: monthly on the 2nd at 06:00 (Freddie Mac publishes quarterly; FRED monthly).
* Sensor: new or changed files in data/raw/freddie trigger an incremental run.
* Alerts: failed runs raise an alert through loanlens.alerts (log + file + optional webhook);
  data-quality failures inside ingestion alert and stop the load on their own.
* Single-process executor: the DuckDB warehouse allows one writer at a time.
"""

# No `from __future__ import annotations`: Dagster inspects the context type hints at runtime.

import os
import subprocess
from pathlib import Path

from dagster import (
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    AssetSpec,
    Backoff,
    DefaultSensorStatus,
    Definitions,
    MaterializeResult,
    RetryPolicy,
    RunFailureSensorContext,
    RunRequest,
    ScheduleDefinition,
    SensorEvaluationContext,
    SkipReason,
    asset,
    define_asset_job,
    in_process_executor,
    multi_asset,
    run_failure_sensor,
    sensor,
)
from dagster_dbt import DbtCliResource, DbtProject, dbt_assets

from loanlens import pipeline
from loanlens.alerts import send_alert
from loanlens.config import load_settings
from loanlens.dbt_runner import dbt_env, dbt_executable

SETTINGS = load_settings()
os.environ.update(dbt_env(SETTINGS))
RETRY = RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL)
GROUP_INGEST, GROUP_MODELS, GROUP_REPORTING = "ingestion", "models", "reporting"

DBT_TARGET = SETTINGS.artifacts_dir / "dagster_dbt_target"
dbt_project = DbtProject(project_dir=SETTINGS.dbt_project_dir, profiles_dir=SETTINGS.dbt_project_dir,
                         target_path=DBT_TARGET)
if not Path(dbt_project.manifest_path).exists():
    subprocess.run([dbt_executable(), "parse", "--project-dir", str(SETTINGS.dbt_project_dir),
                    "--profiles-dir", str(SETTINGS.dbt_project_dir), "--target-path", str(DBT_TARGET)],
                   check=True, env={**os.environ})
dbt_project.prepare_if_dev()


# ---- ingestion -----------------------------------------------------------------------------

@asset(group_name=GROUP_INGEST, retry_policy=RETRY, compute_kind="python",
       description="FRED unemployment, FHFA house-price and 30y mortgage-rate series (cached CSV).")
def fred_series_files() -> MaterializeResult:
    n = pipeline.fetch_fred(SETTINGS)
    return MaterializeResult(metadata={"series": n})


@asset(group_name=GROUP_INGEST, compute_kind="python",
       description="Quarterly loan files in the Freddie Mac layout (synthetic generator; no-op for real data).")
def freddie_source_files() -> MaterializeResult:
    n = pipeline.synthesize(SETTINGS) if SETTINGS["synthetic"] else 0
    files = len(list(SETTINGS.raw_freddie_dir.glob("*")))
    return MaterializeResult(metadata={"generated": n, "files_present": files})


@multi_asset(
    specs=[AssetSpec(AssetKey(["freddie", "origination"]), deps=[freddie_source_files]),
           AssetSpec(AssetKey(["freddie", "performance"]), deps=[freddie_source_files]),
           AssetSpec(AssetKey(["fred", "fred_observations"]), deps=[fred_series_files]),
           AssetSpec(AssetKey(["fred", "fred_series"]), deps=[fred_series_files]),
           AssetSpec(AssetKey(["meta", "ingest_log"])),
           AssetSpec(AssetKey(["meta", "dq_results"]))],
    group_name=GROUP_INGEST, retry_policy=RETRY, compute_kind="great_expectations",
)
def raw_warehouse_tables(context: AssetExecutionContext):
    """Validate every batch with Great Expectations and load new/changed periods incrementally."""
    result = pipeline.ingest(SETTINGS)
    context.log.info("Loaded %d periods, skipped %d", len(result["loaded"]), len(result["skipped"]))
    for spec_key in context.selected_asset_keys:
        yield MaterializeResult(asset_key=spec_key, metadata={
            "periods_loaded": len(result["loaded"]), "periods_skipped": len(result["skipped"]),
            "monthly_records_loaded": result["perf_rows"]})


# ---- dbt -----------------------------------------------------------------------------------

@dbt_assets(manifest=dbt_project.manifest_path, exclude="tag:post_model", name="dbt_core")
def dbt_core(context: AssetExecutionContext, dbt: DbtCliResource):
    """Staging, star schema (dim_loan, fct_loan_monthly, dim_macro...) and analytics marts."""
    yield from dbt.cli(["build"], context=context).stream()


@dbt_assets(manifest=dbt_project.manifest_path, select="tag:post_model", name="dbt_reporting")
def dbt_reporting(context: AssetExecutionContext, dbt: DbtCliResource):
    """Reporting marts that join model outputs (PD, expected loss, stress results)."""
    yield from dbt.cli(["build"], context=context).stream()


# ---- models --------------------------------------------------------------------------------

@asset(group_name=GROUP_MODELS, retry_policy=RETRY, compute_kind="xgboost",
       deps=[AssetKey(["core", "dim_loan"])], key=AssetKey(["ml", "pd_origination_scores"]))
def pd_model() -> MaterializeResult:
    """Origination PD: logistic regression baseline vs XGBoost, recalibrated, SHAP explained."""
    m = pipeline.train(SETTINGS)
    t = m["test"]
    return MaterializeResult(metadata={"auc_xgboost": t["xgboost"]["auc"],
                                       "auc_logistic": t["logistic_regression"]["auc"],
                                       "test_default_rate": t["xgboost"]["base_rate"]})


@asset(group_name=GROUP_MODELS, retry_policy=RETRY, compute_kind="lifelines",
       deps=[AssetKey(["core", "dim_loan"]), AssetKey(["core", "fct_loan_monthly"])], key=AssetKey(["ml", "survival_curves"]))
def survival_models() -> MaterializeResult:
    """Kaplan-Meier, Aalen-Johansen and (time-varying) Cox models for default and prepayment."""
    s = pipeline.survival(SETTINGS)
    return MaterializeResult(metadata={"cox_default_concordance": s["cox_default"]["concordance"]})


@multi_asset(
    specs=[AssetSpec(AssetKey(["ml", "loan_scores"]), deps=[AssetKey(["core", "fct_loan_monthly"]), AssetKey(["core", "dim_macro"]),
                                                              AssetKey(["ml", "pd_origination_scores"])]),
           AssetSpec(AssetKey(["ml", "stress_scenarios"])),
           AssetSpec(AssetKey(["ml", "stress_summary"]))],
    group_name=GROUP_MODELS, retry_policy=RETRY, compute_kind="numpy",
)
def stress_test(context: AssetExecutionContext):
    """Monte Carlo stress test, 2008-2010 backtest and baseline 12-month PD / EL per loan."""
    s = pipeline.stress(SETTINGS)
    for key in context.selected_asset_keys:
        yield MaterializeResult(asset_key=key, metadata={
            "expected_loss_rate": s["expected_loss_rate"], "var99_loss_rate": s["var99_loss_rate"],
            "scenarios": s["n_scenarios"]})


@asset(group_name=GROUP_REPORTING, retry_policy=RETRY, compute_kind="python",
       deps=[AssetKey(["ml", "pd_origination_scores"])])
def lending_memo() -> MaterializeResult:
    """Profit-maximising PD approval cutoff and the one-page recommendation memo."""
    r = pipeline.decide(SETTINGS)
    return MaterializeResult(metadata={"cutoff_pd": r["cutoff_pd"], "profit_uplift": r["test"]["profit_uplift"]})


@asset(group_name=GROUP_REPORTING, retry_policy=RETRY, compute_kind="python", deps=[lending_memo])
def fairness_review() -> MaterializeResult:
    """Fair-lending review by geography and borrower group."""
    f = pipeline.fairness(SETTINGS)
    return MaterializeResult(metadata={"flags": len(f["flags"]), "min_air": f["min_air_recommended"]})


@asset(group_name=GROUP_REPORTING, retry_policy=RETRY, compute_kind="powerbi",
       deps=[AssetKey(["reporting", "rpt_active_portfolio_risk"]), AssetKey(["reporting", "rpt_risk_segments_el"]),
             AssetKey(["reporting", "rpt_stress_loss_distribution"]), fairness_review, AssetKey(["ml", "survival_curves"])])
def bi_exports_and_dashboard() -> MaterializeResult:
    """Power BI Parquet/CSV exports, the HTML dashboard and the results summary."""
    from loanlens.reporting.bi_export import export_powerbi
    from loanlens.reporting.dashboard import build_dashboard
    from loanlens.reporting.results import write_results_summary

    counts = export_powerbi(SETTINGS)
    path = build_dashboard(SETTINGS)
    write_results_summary(SETTINGS)
    return MaterializeResult(metadata={"tables_exported": len(counts), "dashboard": path})


# ---- jobs, schedule, sensors ---------------------------------------------------------------

everything = define_asset_job("loanlens_full_refresh", selection=AssetSelection.all(),
                              executor_def=in_process_executor)
monthly = ScheduleDefinition(job=everything, cron_schedule="0 6 2 * *", execution_timezone="America/New_York")


@sensor(job=everything, minimum_interval_seconds=300, default_status=DefaultSensorStatus.STOPPED)
def new_freddie_files(context: SensorEvaluationContext):
    """Fire when a quarterly file is added or replaced in data/raw/freddie."""
    files = sorted(SETTINGS.raw_freddie_dir.glob("*.zip")) + sorted(SETTINGS.raw_freddie_dir.glob("*.txt"))
    if not files:
        return SkipReason("no source files")
    fingerprint = f"{len(files)}-{max(int(f.stat().st_mtime) for f in files)}"
    if fingerprint == context.cursor:
        return SkipReason("no new or changed files")
    context.update_cursor(fingerprint)
    return RunRequest(run_key=fingerprint)


@run_failure_sensor(default_status=DefaultSensorStatus.RUNNING)
def alert_on_failure(context: RunFailureSensorContext):
    send_alert(SETTINGS, f"Dagster run failed: {context.dagster_run.job_name}",
               context.failure_event.message or "see Dagster run logs")


defs = Definitions(
    assets=[fred_series_files, freddie_source_files, raw_warehouse_tables, dbt_core, pd_model,
            survival_models, stress_test, dbt_reporting, lending_memo, fairness_review,
            bi_exports_and_dashboard],
    jobs=[everything],
    schedules=[monthly],
    sensors=[new_freddie_files, alert_on_failure],
    resources={"dbt": DbtCliResource(project_dir=dbt_project, profiles_dir=str(SETTINGS.dbt_project_dir),
                                     dbt_executable=dbt_executable())},
    executor=in_process_executor,
)

"""Publish warehouse tables to PostgreSQL - the serving layer Power BI connects to live.

Uses DuckDB's postgres extension, so no row-by-row Python transfer is involved.
"""

from __future__ import annotations

import logging

import duckdb

from loanlens.config import Settings

log = logging.getLogger(__name__)

PUBLISH = {
    "core": ["dim_loan", "dim_date", "dim_geography", "dim_macro", "fct_loan_monthly"],
    "analytics": ["mart_portfolio_monthly", "mart_vintage_curves", "mart_roll_rates",
                  "mart_roll_rate_summary", "mart_risk_segments", "mart_loss_by_vintage",
                  "mart_data_quality", "mart_ingest_history"],
    "reporting": ["rpt_active_portfolio_risk", "rpt_risk_segments_el", "rpt_stress_loss_distribution"],
    "ml": ["loan_scores", "stress_scenarios", "stress_summary", "stress_paths", "backtest_monthly",
           "survival_curves", "pd_origination_scores"],
}


def publish(settings: Settings, dsn: str | None) -> dict[str, int]:
    if not dsn:
        raise ValueError("Provide --dsn or set LOANLENS_POSTGRES_DSN, e.g. postgresql://loanlens:loanlens@localhost:5432/loanlens")
    counts = {}
    # In-memory session: the warehouse is attached read-only, Postgres read-write.
    con = duckdb.connect()
    try:
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute(f"ATTACH '{settings.warehouse_path}' AS wh (READ_ONLY)")
        con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres)")
        for schema, tables in PUBLISH.items():
            con.execute(f"CREATE SCHEMA IF NOT EXISTS pg.{schema}")
            for table in tables:
                con.execute(f"DROP TABLE IF EXISTS pg.{schema}.{table}")
                con.execute(f"CREATE TABLE pg.{schema}.{table} AS SELECT * FROM wh.{schema}.{table}")
                counts[f"{schema}.{table}"] = con.execute(f"select count(*) from wh.{schema}.{table}").fetchone()[0]
                log.info("published %s.%s (%d rows)", schema, table, counts[f"{schema}.{table}"])
    finally:
        con.close()
    return counts

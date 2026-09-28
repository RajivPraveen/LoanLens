"""DuckDB warehouse connection helpers.

Schemas:
    raw     loaded files, exactly as delivered (typed, sentinels intact)
    meta    ingest log and data-quality results
    ml      model outputs written back for reporting (scores, stress results)
    main_*  dbt models (staging, marts, reporting)
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import duckdb
import pandas as pd

from loanlens.config import Settings
from loanlens.schemas import RAW_ORIGINATION, RAW_PERFORMANCE, raw_table_ddl

META_DDL = [
    """CREATE TABLE IF NOT EXISTS meta.ingest_log (
        source_period VARCHAR, source_file VARCHAR, file_sha256 VARCHAR, file_bytes BIGINT,
        orig_rows BIGINT, perf_rows BIGINT, status VARCHAR, batch_id VARCHAR, run_id VARCHAR,
        started_at TIMESTAMP, finished_at TIMESTAMP, message VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS meta.dq_results (
        run_id VARCHAR, batch_id VARCHAR, source_period VARCHAR, suite VARCHAR,
        expectation VARCHAR, "column" VARCHAR, success BOOLEAN, element_count BIGINT,
        unexpected_count BIGINT, unexpected_percent DOUBLE, examples VARCHAR,
        validated_at TIMESTAMP)""",
]


@contextmanager
def connect(settings: Settings, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    settings.warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    # Bounded memory (the real dataset is 75M loan-months): DuckDB spills to disk instead of failing.
    config = {"memory_limit": os.environ.get("LOANLENS_DUCKDB_MEMORY", "8GB"), "preserve_insertion_order": False}
    con = duckdb.connect(str(settings.warehouse_path), read_only=read_only, config=config)
    try:
        yield con
    finally:
        con.close()


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    for schema in ("raw", "meta", "ml"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    con.execute(raw_table_ddl("raw.origination", RAW_ORIGINATION))
    con.execute(raw_table_ddl("raw.performance", RAW_PERFORMANCE))
    for ddl in META_DDL:
        con.execute(ddl)


def query(settings: Settings, sql: str, params: list | None = None) -> pd.DataFrame:
    with connect(settings, read_only=True) as con:
        return con.execute(sql, params or []).df()


def write_table(settings: Settings, table: str, df: pd.DataFrame) -> None:
    """Replace ``table`` (schema-qualified) with the contents of ``df``."""
    with connect(settings) as con:
        init_schema(con)
        con.register("_df", df)
        con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM _df")
        con.unregister("_df")

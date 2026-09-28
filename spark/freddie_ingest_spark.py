"""Spark / Databricks implementation of the Freddie Mac ingestion step.

The local pipeline uses DuckDB; this job does the same work at full-dataset scale (the
complete Freddie Mac release is ~50M loans and several billion monthly rows) on Databricks
Community Edition or any Spark 3.4+ cluster.

    # Databricks: upload the unzipped quarterly .txt files to a Volume or DBFS, then
    spark-submit spark/freddie_ingest_spark.py --input /Volumes/main/loanlens/raw \\
        --output /Volumes/main/loanlens/warehouse --format delta

    # Local test (pip install pyspark; needs Java 17):
    python spark/freddie_ingest_spark.py --input data/raw/freddie_txt --output /tmp/ll_spark --format parquet

Same guarantees as the DuckDB loader:
* layouts (Release 47 and legacy, detected per file) and file-name patterns come from
  loanlens.schemas, the single source of truth shared with the DuckDB loader;
* incremental: a period is (re)processed only if its files changed since the last run
  (fingerprint = size + modification time, kept in a small ingest-log table);
* idempotent: each period is written with a partition overwrite (Delta `replaceWhere` /
  dynamic partition overwrite for Parquet), so reruns never duplicate rows;
* quality gates before the write: missing keys, impossible values, duplicate keys and
  orphan performance records fail the batch and stop the job.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from loanlens.schemas import LAYOUTS, classify, detect_layout  # noqa: E402

SPARK_TYPES = {"INTEGER": T.IntegerType(), "DOUBLE": T.DoubleType(), "VARCHAR": T.StringType()}


class DataQualityError(RuntimeError):
    pass


def read_layout(spark: SparkSession, path: str, layout) -> DataFrame:
    # Read everything as strings, then cast - malformed numbers become nulls we can count.
    schema = T.StructType([T.StructField(n, T.StringType()) for n, _ in layout])
    raw = spark.read.csv(path, sep="|", header=False, schema=schema, mode="PERMISSIVE")
    cols = []
    for name, dtype in layout:
        c = F.trim(F.col(name))
        cols.append(c.cast(SPARK_TYPES[dtype]).alias(name))
        if dtype != "VARCHAR" and name != "net_sale_proceeds":
            cols.append((c.isNotNull() & (c != "") & c.cast(SPARK_TYPES[dtype]).isNull()).cast("int").alias(f"_bad_{name}"))
    df = raw.select(*cols)
    bad = [c for c in df.columns if c.startswith("_bad_")]
    return df.withColumn("_parse_errors", sum(F.col(c) for c in bad) if bad else F.lit(0)).drop(*bad)


def check(condition_count: int, message: str, failures: list[str]) -> None:
    if condition_count:
        failures.append(f"{message}: {condition_count:,} rows")


def validate(orig: DataFrame, perf: DataFrame, period: str) -> None:
    f: list[str] = []
    o = orig.withColumn("credit_score", F.when(F.col("credit_score") == 9999, None).otherwise(F.col("credit_score")))
    # 00-99 months delinquent, RA = REO acquisition, XX = not available
    check(o.filter(F.col("loan_sequence_number").isNull()).count(), "origination: missing loan id", f)
    check(o.groupBy("loan_sequence_number").count().filter("count > 1").count(), "origination: duplicate loan id", f)
    check(o.filter((F.col("credit_score") < 300) | (F.col("credit_score") > 850)).count(), "credit score outside 300-850", f)
    check(o.filter((F.col("orig_interest_rate") < 0.5) | (F.col("orig_interest_rate") > 20)).count(), "interest rate outside 0.5-20%", f)
    check(o.filter(F.col("_parse_errors") > 0).count(), "origination: unparseable numbers", f)
    check(perf.filter(F.col("loan_sequence_number").isNull() | F.col("monthly_reporting_period").isNull()).count(),
          "performance: missing keys", f)
    check(perf.groupBy("loan_sequence_number", "monthly_reporting_period").count().filter("count > 1").count(),
          "performance: duplicate loan-month", f)
    check(perf.filter(~F.col("current_loan_delinquency_status").rlike(r"^(\d{1,3}|RA|XX)$")).count(),
          "performance: invalid delinquency status", f)
    check(perf.join(orig.select("loan_sequence_number"), "loan_sequence_number", "left_anti").count(),
          "performance: records without an origination record", f)
    if f:
        raise DataQualityError(f"{period} failed validation:\n  - " + "\n  - ".join(f))


def write_partition(df: DataFrame, path: str, period: str, fmt: str, layout_version: str) -> None:
    df = (df.withColumn("source_period", F.lit(period)).withColumn("layout_version", F.lit(layout_version))
            .withColumn("loaded_at", F.current_timestamp()))
    writer = df.write.format(fmt).mode("overwrite").partitionBy("source_period")
    if fmt == "delta":
        writer = writer.option("replaceWhere", f"source_period = '{period}'")
    else:
        writer = writer.option("partitionOverwriteMode", "dynamic")
    writer.save(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--format", default="delta", choices=["delta", "parquet"])
    args = ap.parse_args()

    spark = SparkSession.builder.appName("loanlens-freddie-ingest").getOrCreate()
    spark.conf.set("spark.sql.sources.partitionOverwriteMode", "dynamic")
    # Databricks Volumes (/Volumes/...) and DBFS (/dbfs/...) are FUSE-mounted, so the
    # local filesystem API lists them too.
    files: dict[str, dict[str, Path]] = {}
    for p in Path(args.input).glob("*.txt"):
        if (hit := classify(p.name)):
            files.setdefault(hit[0], {})[hit[1]] = p
    periods = sorted(p for p, kinds in files.items() if len(kinds) == 2)

    log_path = f"{args.output}/_ingest_log"
    try:
        seen = {r["source_period"]: r["fingerprint"] for r in spark.read.format(args.format).load(log_path).collect()}
    except Exception:  # first run
        seen = {}

    loaded = []
    for period in periods:
        orig_file, perf_file = files[period]["origination"], files[period]["performance"]
        fingerprint = "-".join(f"{f.stat().st_size}:{int(f.stat().st_mtime)}" for f in (orig_file, perf_file))
        if seen.get(period) == fingerprint:
            print(f"{period}: unchanged, skipped")
            continue
        layouts = {}
        for kind, f in (("origination", orig_file), ("performance", perf_file)):
            with f.open() as fh:
                layouts[kind] = detect_layout(kind, fh.readline())
        orig = read_layout(spark, str(orig_file), LAYOUTS[("origination", layouts["origination"])]).cache()
        perf = read_layout(spark, str(perf_file), LAYOUTS[("performance", layouts["performance"])]).cache()
        validate(orig, perf, period)  # raises -> job fails -> scheduler alerts
        write_partition(orig.drop("_parse_errors"), f"{args.output}/origination", period, args.format, layouts["origination"])
        write_partition(perf.drop("_parse_errors"), f"{args.output}/performance", period, args.format, layouts["performance"])
        loaded.append((period, fingerprint))
        print(f"{period}: loaded {orig.count():,} loans, {perf.count():,} monthly records")
        orig.unpersist()
        perf.unpersist()

    if loaded:
        seen.update(dict(loaded))
        spark.createDataFrame(list(seen.items()), ["source_period", "fingerprint"]) \
            .write.format(args.format).mode("overwrite").save(log_path)
    print(f"done: {len(loaded)} loaded, {len(periods) - len(loaded)} skipped")


if __name__ == "__main__":
    main()

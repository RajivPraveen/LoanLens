# Runbook

## Environments

| Profile | Use | Data |
|---|---|---|
| `freddie` (default) | All reported results | Freddie Mac Single-Family Loan-Level Sample files in `data/raw/freddie/` |
| `tiny` | Automated tests and CI only | Small synthetic dataset in the Freddie Mac layout (the licensed files cannot ship with the repository) |

Select a profile with `LOANLENS_PROFILE` or `--profile`. Use `LOANLENS_WORKSPACE` to redirect all
generated output to another directory.

## Loading the Freddie Mac data

1. Register at [freddiemac.com](https://www.freddiemac.com/research/datasets/sf-loanlevel-dataset) and,
   under **Standard Dataset Download by Year**, download the *sample* file for every year
   (`sample_1999.zip` ... `sample_2026.zip`, 50,000 loans per full year). Skip the Non-Standard dataset.
2. Copy them, still zipped, to `data/raw/freddie/`. Release 47 (`sample_orig_YYYY.txt` +
   `sample_perf_YYYY.txt`), older sample releases (`_svcg_`) and the quarterly Standard files are all
   recognised, and each file's layout is detected from its field count.
3. `loanlens all`. `stress.as_of: latest` picks the last reporting month automatically. The
   train, calibration and test vintages are `modeling.*_years` in `config/loanlens.yaml`; test vintages
   need 24 months of performance.
4. For the full Standard release (about 50M loans), run ingestion on Databricks with
   `spark/freddie_ingest_spark.py` and point dbt at that output (for example `dbt-databricks`).

### Known quirks in the real files (handled)

| Quirk | Where | Handling |
|---|---|---|
| Loan terms of 30-36 and up to 513 months | 2 loans in 1999/2001; a few long terms | GX range 12-600 months |
| Current interest rate of 30-50% (about ten times the prior month) | 39 records, reporting months 2017-03/04 | Tolerated by the gate (`mostly` 99.99%), nulled in `stg_freddie__performance`, note rate used instead |
| Loans in Puerto Rico, Guam, U.S. Virgin Islands | 2,333 loans (0.17%) | Kept in `raw`, excluded from staging: FRED has no state-level macro series for them |
| Loss sign convention differs across releases | `actual_loss`, `mi_recoveries` | Detected per source period in staging |

## Routine operation

| Task | Command |
|---|---|
| Monthly refresh (new FRED data, new Freddie Mac release) | `loanlens all`, or let the Dagster schedule run |
| Add or replace one year | Drop the file in `data/raw/freddie`, then `loanlens ingest && loanlens transform`. Only that period loads, and the fact table rebuilds only that partition |
| Restated quarter from Freddie Mac | Replace the file. Its fingerprint changes, the period is replaced, and no duplicates appear |
| Force a reload | `loanlens ingest --force --period 2005` |
| Rebuild everything in dbt | `loanlens transform --full-refresh` |
| Check health | `loanlens status` (load history and DQ pass rate per suite) |
| Orchestrate | `make dagster`; enable the `new_freddie_files` sensor to run on arrival |

## When something fails

| Symptom | Meaning | Action |
|---|---|---|
| `DataQualityError: freddie_origination [...]` | A batch failed a Great Expectations check. Nothing from that period was loaded | Read `data/alerts/alert_*.json` and `artifacts/validation/*.json` for the failed checks and example values. Fix or replace the file, then rerun |
| `_has_origination` failed | Performance records without an origination record (orphans) | Usually a truncated or mismatched file pair; re-download |
| `_period_matches_file` failed | Loan ids don't belong to the quarter in the file name | Wrong file renamed; check the source |
| dbt test failure | A model invariant broke (for example roll rates not summing to one) | `artifacts/dbt_logs`; rerun `loanlens transform` after fixing |
| `Too few defaults in the calibration split` | The split years chosen have too few defaults to calibrate | Widen `calibration_years` |
| XGBoost `libomp.dylib` not found (macOS) | The OpenMP runtime is missing | `brew install libomp`, or `make fix-libomp-macos` |
| DuckDB lock error | Another process holds the warehouse (for example `dagster dev` plus the CLI) | Run one writer at a time; Dagster uses the in-process executor for this reason |

Alerts go to the log, to `data/alerts/`, and to `LOANLENS_ALERT_WEBHOOK` (Slack/Teams incoming
webhook) when set.

## Power BI

`loanlens report` writes `powerbi/data/*.parquet`. Follow `powerbi/README.md` to build the `.pbix`
(Windows). For live loan-level data: `docker compose up -d postgres` (set `LOANLENS_PG_PORT` if 5432
is taken), then `make publish`, then connect Power BI to PostgreSQL.

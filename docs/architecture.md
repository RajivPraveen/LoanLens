# Architecture and design decisions

## Layers

| Layer | Technology | Responsibility |
|---|---|---|
| Sources | Freddie Mac Single-Family Loan-Level files, FRED (a synthetic generator stands in for Freddie Mac in tests only) | Raw inputs, cached under `data/raw` |
| Ingestion | Python + DuckDB (local), PySpark (Databricks) | Fingerprint, parse, validate, load atomically |
| Quality | Great Expectations 1.x (batch gates), dbt tests (model contracts) | Stop bad data before it lands, and prove every model's invariants |
| Warehouse | DuckDB file (PostgreSQL copy for BI) | `raw` → `staging` → `core` → `analytics` → `reporting`; `meta`, `ml` |
| Transformation | dbt-duckdb | Star schema, incremental staging and fact, marts, 79 tests |
| Modelling | scikit-learn, XGBoost, lifelines, SHAP, NumPy | PD, survival, hazard/LGD, Monte Carlo, decision, fairness |
| Orchestration | Dagster (+ `loanlens` CLI, Makefile) | Asset lineage, schedule, sensor, retries, alerts |
| Serving | Parquet exports + Power BI kit, HTML dashboard, PostgreSQL, markdown reports | Consumption by the risk team |
| Delivery | Docker, docker compose, GitHub Actions | Reproducible runs and CI |

## Ingestion flow (`src/loanlens/ingest/freddie.py`)

```
discover files ─▶ group by period (2005Q1 or 2005 for the sample dataset)
  └─ for each period:
       fingerprint (SHA-256 of the zip or txt pair)
       already loaded with this fingerprint? ─ yes ─▶ skip
       BEGIN
         parse origination → type → validation view (sentinels nulled) → GX suite ─ fail ─▶ ROLLBACK, alert, stop
         DELETE rows of this period (raw.origination, raw.performance)
         INSERT origination
         stream performance in chunks → type → GX suite incl. referential check ─ fail ─▶ ROLLBACK, alert, stop
         INSERT each chunk
         log success + DQ results
       COMMIT
```

- **Incremental:** only new or changed periods are processed.
- **Idempotent:** a period is replaced as a unit inside one transaction, so a rerun or a restated
  file never duplicates rows. Tested in `tests/test_ingest.py`.
- **Chunked:** performance files stream in `ingest.chunk_rows` chunks, so memory is bounded
  regardless of file size.
- **Layout detection:** each file's layout (Release 47: 31/35 fields, or legacy: 32/≤32) is
  detected from its first line. Raw tables hold the union of both, with `layout_version`, and
  staging normalizes codes and loss signs.
- **Validation view:** documented sentinels (for example credit score `9999` = not available) are
  nulled before range checks, while `raw` keeps the delivered values untouched.
- **Alerts:** every failure is logged, written to `data/alerts/*.json` and posted to
  `LOANLENS_ALERT_WEBHOOK` if set. Dagster adds a run-failure sensor on top.

## dbt design (`dbt/`)

- `generate_schema_name` is overridden so schemas are `staging`, `core` and so on, not
  `main_core`, which gives stable names in BI tools.
- `stg_freddie__performance` and `fct_loan_monthly` are **incremental** (`delete+insert`,
  `unique_key = source_period`). Their watermark is `ingest_log.finished_at > max(staged_at / built_at)`,
  so a restated file replaces exactly its partition. Staging is a table rather than a view because at
  75M rows every downstream model and test would otherwise re-decode the raw data.
- The default definition lives in one macro (`is_credit_event`), used by every model.
- Tests:
  - generic: unique, not_null, relationships, accepted_values, plus custom `value_between`,
    `not_negative` and `dbt_utils_unique_combination` (package-free);
  - singular: roll rates sum to one, no activity after termination, one termination per loan, no
    performance before first payment, losses only on liquidations, monotone vintage curves, and the
    portfolio mart reconciling to the fact table.
- The `reporting` models are tagged `post_model` and built after the Python models write `ml.*`.
  This two-pass build keeps the DAG acyclic.

## Modelling decisions

| Decision | Why |
|---|---|
| Out-of-time vintage splits, never random | Random splits leak the economy (loans from the same months land in train and test) |
| Logistic recalibration rather than isotonic | Strictly monotone, so ranking metrics are unchanged, and it is robust with few calibration defaults |
| Monotone constraints in XGBoost (FICO, LTV, CLTV, DTI) | Behaviour consistent with credit policy; easier to defend in model validation |
| No state identity in the PD model | Fair-lending proxy risk; local economics enter through macro features |
| Separate origination PD and behavioural hazard models | An approval decision can only use application data; stress testing needs sensitivity to the future economy |
| Logistic hazard, not trees, for stress | Smooth, monotone extrapolation to unseen severities; inspectable coefficients |
| COVID months excluded from hazard development | Forbearance broke the unemployment-to-default link; including them taught the model that unemployment spikes are harmless (found in the backtest) |
| LGD priced at the liquidation date | Crisis losses come from prices that kept falling *after* default |
| Feature code shared between fitting and projection | `feature_matrix` works on pandas rows and on NumPy scenario x loan arrays, so they cannot drift apart |

## Performance notes

| Step (Freddie Mac Sample 1999-2026: 1.36M loans, 75M loan-months; 10-core laptop, 16 GB) | Time |
|---|---|
| Ingest with full GX validation (28 annual files, first load) | ~10 min |
| Ingest rerun, nothing changed (fingerprints match) | ~2 s |
| dbt build (first full build) / incremental rebuild with no new files | ~2.5 min / ~20 s |
| PD training (logistic + XGBoost, bootstrap CIs) + SHAP | ~2.5 min |
| Survival (KM, Aalen-Johansen, Cox, time-varying Cox) | ~15 s |
| Stress: hazard fits on 4.9M loan-months, 5,000 scenarios x 10,000 loans x 36 months, backtests | ~2.5 min |
| Decision, fairness, reports, dashboard, Power BI export | ~20 s |

DuckDB runs with a bounded memory limit (`LOANLENS_DUCKDB_MEMORY`, default 8GB) and spills to disk,
and the 75M-row staging model is incremental, so a 16 GB laptop handles the full sample.

The stress projection is memory-bandwidth-bound. It runs in float32 and chunks the scenarios, and
static features are summed once per loan rather than broadcast.

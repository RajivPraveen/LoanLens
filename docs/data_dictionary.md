# Data dictionary

The warehouse is a single DuckDB file (`data/warehouse/loanlens.duckdb`), optionally copied to
PostgreSQL with the same schema and table names.

| Schema | Built by | Contents |
|---|---|---|
| `raw` | `loanlens ingest` | Files exactly as delivered (typed, sentinel codes kept) |
| `meta` | `loanlens ingest` | Load log and data-quality results |
| `reference` | dbt seed | State reference data |
| `staging` | dbt (views) | Typed, decoded sources |
| `intermediate` | dbt | Per-loan outcome summary |
| `core` | dbt | Star schema: dimensions and the monthly fact |
| `analytics` | dbt | Business marts |
| `ml` | Python models | Scores, stress-test results, curves |
| `reporting` | dbt (after models) | Marts joining model outputs |

Grain is given for every table. **PK** is the primary key, enforced by dbt `unique` / `not_null` or combination tests.

---

## raw

### Layouts and conventions
Freddie Mac changed the file layout in **Release 47 (July 2026)**. The loader detects each file's
layout from its field count and stores the union of both layouts, so fields a file lacks are NULL:

| | Release 47 | Legacy (earlier releases) |
|---|---|---|
| Origination fields | 31: servicer name and MI cancellation removed, **VantageScore 4.0** added | 32 |
| Performance fields | 35: adds MI cancellation indicator, **servicer name**, bankruptcy cramdown costs | up to 32 |
| Loan identifier | `PYYQnXXXXXXX` (product, origination year and quarter, 7 digits) | `F1YYQnXXXXXX` |
| Postal code | 3 digits | `###00` |
| Delinquency status | `00`, `01`, ... (capped at 99), `RA`, `XX` = not available | `0`, `1`, ..., `RA` |
| Loss sign | documented as losses positive and recoveries negative | losses negative |

Because Freddie Mac's own Release 47 example file still reports losses as negatives, staging
**detects the sign convention per source period from the data** (the median liquidation
`actual_loss` should be a loss) and normalizes it. Downstream, `net_loss_amount` is always
positive for a loss and recoveries are positive amounts. `layout_version` (`r47` or `legacy`)
records the layout on every raw row.

### raw.origination - one row per loan (Freddie Mac origination file)
All origination fields of both layouts (see `src/loanlens/schemas.py`) plus load metadata. Key fields:

| Column | Type | Description |
|---|---|---|
| `loan_sequence_number` | varchar | **PK.** `F1YYQnXXXXXX`: origination year and quarter plus a sequence number |
| `credit_score` | int | Classic FICO at origination; `9999` = not available |
| `vantage_score` | int | VantageScore 4.0 (Release 47); `9999` = not available |
| `first_payment_date` | varchar | `YYYYMM` of the first scheduled payment |
| `orig_ltv`, `orig_cltv` | int | Original loan-to-value / combined LTV, %; up to 998 for 2018Q2+ and HARP loans; `999` = not available |
| `orig_dti` | int | Debt-to-income ratio, %; `999` = not available |
| `orig_upb` | double | Original unpaid principal balance, $ |
| `orig_interest_rate` | double | Note rate, % |
| `mi_pct` | int | Mortgage insurance coverage, %; `0` none, `999` not available |
| `occupancy_status` | varchar | `P` primary, `I` investment, `S` second home |
| `loan_purpose` | varchar | `P` purchase, `C` cash-out refi, `N` no-cash-out refi, `R` refi unspecified |
| `channel` | varchar | `R` retail, `B` broker, `C` correspondent, `T` TPO not specified |
| `property_state` | varchar | Two-letter state code |
| `property_type` | varchar | `SF` single-family, `CO` condo, `PU` PUD, `MH` manufactured, `CP` co-op |
| `orig_loan_term` | int | Months |
| `num_borrowers`, `num_units` | int | |
| `first_time_homebuyer_flag` | varchar | `Y` / `N` / `9` (not applicable, e.g. refinances) |
| `source_period`, `source_file`, `batch_id`, `loaded_at` | | Load metadata: which file and load produced the row |

### raw.performance - one row per loan per reporting month (Freddie Mac "time" file)
All performance fields of both layouts plus load metadata. **PK** (`loan_sequence_number`, `monthly_reporting_period`).

| Column | Description |
|---|---|
| `monthly_reporting_period` | `YYYYMM` |
| `current_actual_upb` | Balance at month end (0 on the termination record) |
| `current_loan_delinquency_status` | Months delinquent (`00`, `01`, ... capped at 99; legacy `0`, `1`, ...), `RA` (REO acquisition) or `XX` (not available) |
| `loan_age`, `remaining_months_to_legal_maturity` | Months |
| `modification_flag` | `Y` modified this month, `P` modified in a prior month |
| `zero_balance_code` | `01` prepaid/matured, `02` third-party sale, `03` short sale, `06` repurchase, `09` REO disposition, `15` note sale, `16` reperforming loan sale |
| `zero_balance_removal_upb` | Balance removed on termination |
| `net_sale_proceeds`, `mi_recoveries`, `non_mi_recoveries`, `total_expenses` (+ components), `delinquent_accrued_interest` | Liquidation cash flows (expenses reported as negatives) |
| `actual_loss` | Freddie Mac loss calculation, as delivered (sign convention depends on the release; see above) |
| `servicer_name`, `mi_cancellation_indicator`, `bankruptcy_cramdown_costs` | Release 47 monthly fields |
| `estimated_ltv` | Freddie's current mark-to-market LTV estimate |
| `borrower_assistance_status_code` | `F` forbearance, `R` repayment plan, `T` trial modification |
| `payment_deferral` | Payment deferral flag: `C` current period (legacy `Y`), `P` prior period |

### raw.fred_observations / raw.fred_series
`series_id`, `geo` (`US` or state), `measure` (`unemployment_rate`, `hpi`, `mortgage_rate_30y`),
`observation_date`, `value`. Series: `UNRATE`, `MORTGAGE30US` (weekly), `USSTHPI` (quarterly), and
per state `{ST}UR` (monthly) and `{ST}STHPI` (quarterly FHFA all-transactions index).

## meta

| Table | Grain | Columns |
|---|---|---|
| `meta.ingest_log` | one row per load attempt | `source_period`, `source_file`, `file_sha256`, `file_bytes`, `orig_rows`, `perf_rows`, `status` (`loaded`/`failed`), `batch_id`, `run_id`, `started_at`, `finished_at`, `message` |
| `meta.dq_results` | one row per expectation per validated batch | `run_id`, `batch_id`, `source_period`, `suite`, `expectation`, `column`, `success`, `element_count`, `unexpected_count`, `unexpected_percent`, `examples`, `validated_at` |

## reference.state_reference (seed)
`state_code` (PK), `state_name`, `census_region`, `census_division`, `judicial_foreclosure`,
`population_millions`, `price_factor` (relative house-price level, used by the generator), `zip3`.

---

## core - star schema

### core.dim_loan - one row per loan (PK `loan_id`)

| Group | Columns |
|---|---|
| Identity & vintage | `loan_id`, `source_period`, `vintage` (`2006Q3`), `vintage_year`, `vintage_quarter`, `orig_month` (first payment - 2 months), `first_payment_month`, `maturity_month` |
| Geography | `state_code`, `state_name`, `census_region`, `census_division`, `judicial_foreclosure`, `zip3` |
| Credit | `credit_score`, `fico_band` (`<620`, `620-679`, `680-719`, `720-759`, `760+`, `Missing`), `orig_ltv`, `orig_cltv`, `ltv_band` (`<=60`, `61-80`, `81-90`, `91-95`, `>95`), `orig_dti`, `dti_band`, `mi_pct` |
| Loan terms | `orig_upb`, `orig_interest_rate`, `orig_loan_term`, `loan_purpose`, `occupancy`, `channel`, `property_type`, `num_units`, `num_borrowers`, `is_first_time_homebuyer`, `is_super_conforming`, `seller_name`, `servicer_name` |
| Macro at origination | `market_rate_at_orig` (PMMS 30y), `rate_spread_at_orig` (note rate - market), `state_ur_at_orig`, `state_ur_change_12m_at_orig`, `state_hpi_yoy_at_orig`, `state_hpi_at_orig` |
| Outcome | `first_dq30_month`, `first_dq60_month`, `first_dq90_month`, `first_default_month`, `months_to_default`, `termination_type`, `zero_balance_code`, `termination_month`, `months_to_termination`, `is_liquidated`, `liquidation_upb` (balance removed at termination), `default_upb` (balance at the first credit event), `severity_base_upb`, `net_loss`, `loss_severity`, `mi_recoveries`, `ever_modified`, `ever_forbearance`, `max_dq_months`, `months_reported`, `last_period_month`, `data_end_month`, `months_observable` |
| Model label | `default_in_window` (credit event within 24 months of first payment), `window_fully_observed` |

`termination_type`: Active, Voluntary payoff, Matured, Third-party sale, Short sale, REO disposition,
Note sale, Reperforming loan sale, Repurchase.

### core.fct_loan_monthly - one row per loan per month (PK `loan_id`, `period_month`)
Incremental model: `delete+insert` on `source_period`, so it rebuilds only periods loaded since its last build.

| Column | Description |
|---|---|
| `loan_id`, `period_month`, `month_key` (YYYYMM), `state_code`, `macro_key` | Keys to `dim_loan`, `dim_date`, `dim_macro` |
| `source_period` | Incremental partition key |
| `loan_age` | Months |
| `current_upb` | Balance; `exposure_upb` = current balance, or removal balance on the termination month |
| `dq_months`, `dq_state`, `prev_dq_state` | `dq_state` in Current, 30, 60, 90, 120+, REO, Prepaid, Liquidated, Other exit |
| `is_dq30_plus`, `is_dq60_plus`, `is_dq90_plus`, `is_serious_dq_ex_forbearance`, `is_reo` | Delinquency flags |
| `in_forbearance`, `is_modified` | Loss-mitigation flags |
| `is_terminated`, `is_prepaid`, `is_liquidated`, `zero_balance_code` | Termination flags |
| `is_default_event` | True in the loan's first credit-event month |
| `net_loss_amount` | Positive = loss, recorded in the liquidation month |
| `mtm_ltv` | Mark-to-market LTV = original LTV x (balance / original balance) x (state HPI at origination / state HPI now) |
| `refi_incentive` | Current note rate - current 30y market rate (percentage points) |
| `estimated_ltv` | Freddie-reported ELTV |
| `built_at` | Build timestamp (incremental watermark) |

### core.dim_macro - one row per state per month (PK `macro_key` = `ST-YYYYMM`)
`state_unemployment_rate`, `state_hpi`, `us_unemployment_rate`, `us_hpi`, `mortgage_rate_30y`,
`state_ur_change_12m`, `state_hpi_yoy`, `us_ur_change_12m`, `us_hpi_yoy`. Quarterly HPI is carried
forward within the quarter; weekly mortgage rates are averaged to months.

### core.dim_date - one row per month (PK `month_key`)
`month`, `year`, `quarter`, `year_quarter`, `month_label`, `is_nber_recession`.

### core.dim_geography - one row per state (PK `state_code`)

---

## analytics - marts

| Mart | Grain | Main columns |
|---|---|---|
| `mart_portfolio_monthly` | month | active loans and balance; 30+/60+/90+ counts and rates (count and balance weighted); serious DQ excluding forbearance; forbearance and REO counts; prepaid and default events and balances; `smm`, `cpr`, `cdr`; `net_loss`; `annualised_loss_rate`; `avg_mtm_ltv` |
| `mart_vintage_curves` | vintage year x months since first payment | `cum_default_rate` (count), `cum_default_rate_upb`, `cum_loss_rate`, `cum_prepay_rate`, `is_fully_seasoned` |
| `mart_roll_rates` | month x from state x to state | `loans`, `upb`, `roll_rate`, `roll_rate_upb` |
| `mart_roll_rate_summary` | month | `current_to_30`, `dq30_to_60`, `dq60_to_90`, `dq90_to_120`, `dq30_cure`, `dq60_cure`, `current_to_prepaid` |
| `mart_risk_segments` | FICO band x LTV band x state (latest month, active loans) | loans, balance, `avg_mtm_ltv`, `dq30_plus_rate`, `serious_dq_rate`, `hist_default_rate_24m`, `hist_loss_severity` |
| `mart_loss_by_vintage` | vintage year | averages at origination, `lifetime_default_rate`, `default_rate_24m`, liquidations, `net_loss`, `loss_severity`, `cum_loss_rate`, `prepaid_share`, `modified_share`, `active_loans` |
| `mart_data_quality` | run x suite | `checks`, `checks_passed`, `pass_rate`, `unexpected_values`, `failed_checks` |
| `mart_ingest_history` | load attempt | load log with `duration_seconds` |

## ml - model outputs

| Table | Grain | Columns |
|---|---|---|
| `ml.pd_origination_scores` | loan | `pd_xgb` (calibrated 24m PD), `pd_lr`, `pd_xgb_raw`, `risk_driver_1..3` (top SHAP reasons), `split` |
| `ml.loan_scores` | active loan at the as-of month | `pd_12m` (baseline-scenario hazard model), `lgd`, `ead`, `expected_loss`, `risk_grade` (A-F), `pd_24m_origination`, risk drivers |
| `ml.stress_scenarios` | scenario | `scenario_id`, `scenario_type` (`monte_carlo`/`named`), `recession`, `peak_unemployment`, `unemployment_rise`, `hpi_trough_change`, `hpi_change_end`, `mortgage_rate_change_end`, `loss_amount`, `loss_rate`, `default_rate_upb`, `default_rate_loans`, `prepay_rate_upb` |
| `ml.stress_summary` | metric | EL, VaR99, ES99 and portfolio totals as key/value |
| `ml.stress_paths` | month x measure x path | national paths for named scenarios and Monte Carlo p5/p50/p95 |
| `ml.backtest_monthly` | month (2008-2010) | actual vs projected (in-sample, out-of-sample, naive) defaults |
| `ml.survival_curves` | curve x group x month | KM survival and Aalen-Johansen incidence curves |

## reporting - joins of marts and model outputs

| Table | Grain |
|---|---|
| `rpt_active_portfolio_risk` | active loan (latest month) with segment attributes, PD, LGD, EL, risk grade |
| `rpt_risk_segments_el` | FICO band x LTV band x state: balance, expected loss, EL rate, balance-weighted PD |
| `rpt_stress_loss_distribution` | scenario with its `loss_percentile` |

## Power BI export (`powerbi/data`)
Parquet copies of the dimensions, marts, reporting and `ml` tables, plus
`fct_portfolio_segment_monthly` (month x state x FICO band x LTV band), which is the loan-month fact
pre-aggregated to keep the file small.

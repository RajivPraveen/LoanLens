# Metric definitions

Every metric below is computed in exactly one place, named in brackets.

## Credit events

| Term | Definition |
|---|---|
| **Default (credit event)** | First month a loan is 90+ days delinquent *outside COVID forbearance*, enters REO (`RA`), or terminates with a loss-generating zero-balance code (02 third-party sale, 03 short sale, 09 REO disposition, 15 note sale, 16 reperforming loan sale). [`dbt/macros/bands.sql::is_credit_event`, `int_loan_outcomes.first_default_month`] |
| **Why exclude forbearance** | During the COVID-19 relief programme (2020-2021), loans in forbearance were reported as delinquent although borrowers were allowed to skip payments. Counting them as defaults would overstate credit risk; the same months are also excluded from the stress-model development sample. |
| **24-month default label** | `default_in_window` = default within 24 months of first payment (inclusive). A loan is only used for modelling if the full window is observed (`window_fully_observed`). Loans that prepay inside the window without defaulting are non-defaults. [`dim_loan`] |
| **Prepayment** | Zero-balance code 01 with more than one month to maturity (code 01 at maturity = *Matured*). |
| **Liquidation** | Termination with a loss-generating zero-balance code. |

## Delinquency and portfolio KPIs [`mart_portfolio_monthly`]

| Metric | Definition |
|---|---|
| Active loans / balance | Loans reporting in the month without a zero-balance code; sum of `current_upb` |
| 30+ / 60+ / 90+ DQ rate | Active loans with >= 1 / 2 / 3 months delinquent (or REO) / active loans. The `_upb` variants weight by balance. |
| Serious delinquency (SDQ) rate | 90+ DQ rate (industry convention; includes forbearance) |
| SDQ ex-forbearance | 90+ excluding loans flagged in forbearance |
| SMM | Prepaid balance in the month / (active + prepaid + liquidated balance) |
| **CPR** | 1 - (1 - SMM)^12 - annualised prepayment speed |
| **CDR** | 1 - (1 - monthly defaulted balance / balance at risk)^12 |
| Annualised loss rate | Monthly net loss / active balance x 12 |

## Vintage curves [`mart_vintage_curves`]

For origination year *v* and months since first payment *a*:

- **Cumulative default rate** = loans of *v* whose first default occurred at age <= *a* / loans originated in *v* (count). A balance-weighted version uses original balance.
- **Cumulative loss rate** = net losses on liquidations at age <= *a* / original balance of *v*.
- **Cumulative prepayment rate** = voluntary payoffs at age <= *a* / loans originated.
- `is_fully_seasoned` marks ages that every loan in the vintage has reached.

## Roll rates [`mart_roll_rates`]

Roll rate from state *i* to state *j* in month *t* = loans in *i* at *t-1* and in *j* at *t* /
loans in *i* at *t-1*. States: Current, 30, 60, 90, 120+ days, REO, and the terminal states Prepaid,
Liquidated and Other exit. Rows sum to 100% (dbt test `assert_roll_rates_sum_to_one`). *Cure* =
roll back to Current.

## Losses

| Metric | Definition |
|---|---|
| Net loss | Balance + delinquent interest + expenses - sale proceeds - MI and other recoveries (Freddie Mac `actual_loss`, sign-normalized so a loss is positive) |
| **Loss severity** | Net loss / balance at default (per liquidated loan; the larger of the balance at the first credit event and the balance removed at termination, because short-sale removal balances are often written-down residuals) [`int_loan_outcomes.loss_severity`] |
| **LGD (per default event)** | Eventual net loss / balance at default. Defaults that cure or are modified contribute 0, so LGD < severity. Measured on defaults with >= 36 months to resolve. [`stress/hazard.py::fit_lgd`] |
| EAD | Current unpaid balance |
| **Expected loss (EL)** | PD x LGD x EAD [`ml.loan_scores`] |

## Model metrics [`models/default_model.py`]

| Metric | Definition |
|---|---|
| PD (24m, origination) | Calibrated XGBoost probability of default within 24 months of first payment |
| PD (12m, current book) | Cumulative 12-month default probability from the monthly hazard model under the baseline scenario |
| AUC | Area under the ROC curve (ranking quality; 0.5 = random); 95% CI from 200 bootstrap resamples |
| PR-AUC | Average precision - informative when defaults are rare |
| KS | Max difference between the cumulative score distributions of defaulters and non-defaulters |
| Brier score | Mean squared error of predicted probabilities |
| **ECE** | Expected calibration error: across 10 equal-count PD bins, the average of abs(mean PD - observed rate), weighted by bin size |
| Precision / recall top 10% | Among the riskiest 10% of loans by PD: share that default / share of all defaults captured |
| Lift (top decile) | Precision in the top 10% / overall default rate |
| Concordance (Cox) | Harrell's C: probability that of two loans the one with the higher predicted hazard defaults first |

## Stress-testing metrics [`stress/engine.py`]

| Metric | Definition |
|---|---|
| Scenario loss rate | Projected cumulative credit loss over the horizon / current balance of the performing book |
| Expected loss (stress) | Mean scenario loss rate across Monte Carlo scenarios |
| **VaR 99 ("severe but plausible")** | 99th percentile of the scenario loss distribution |
| **Expected shortfall 99** | Mean loss of scenarios at or beyond the 99th percentile |
| Unemployment rise | Peak national unemployment over the horizon - today's rate |
| House-price trough | Minimum national HPI over the horizon / today's HPI - 1 |
| Backtest error | Projected / actual - 1, for 36-month cumulative defaults and losses of the loans performing at the backtest date |

## Decision and fairness metrics

| Metric | Definition |
|---|---|
| Projected profit (loan) | Balance x net margin x horizon x (1 - PD) - PD x LGD x balance [`decision/cutoff.py`] |
| Break-even PD | margin x horizon / (margin x horizon + LGD) |
| Approval rate | Share of applications with PD <= cutoff |
| Profit uplift | Realised profit of approved loans at the cutoff / realised profit when approving all - 1 (test vintages, actual default outcomes, modelled severity) |
| **Adverse impact ratio (AIR)** | Group approval rate / approval rate of the most-approved group in the same dimension; below 0.80 fails the four-fifths rule of thumb [`models/fairness.py`] |
| Calibration ratio | Group mean PD / group observed default rate |
| False decline rate | Share of non-defaulting loans that would be declined |

## Data quality [`mart_data_quality`]

- **Check pass rate** = passed expectations / evaluated expectations per run and suite.
- A batch loads only if **all** its checks pass.

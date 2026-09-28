# Assumptions and limitations

## 1. Data

**Scope of the real dataset.** Freddie Mac's loan-level data covers fixed-rate, fully amortising,
agency-conforming loans that Freddie Mac acquired. It excludes jumbo, FHA/VA, subprime-private-label
and portfolio loans, and it contains only *approved* loans. Conclusions do not transfer to other
mortgage segments without re-estimation.

**No protected-class attributes.** The dataset has no race, ethnicity, sex or age fields. The fairness
review covers geography, first-time-buyer status, borrower count and loan size only. A complete
fair-lending analysis needs HMDA data or BISG proxies.

**Sample, not the full population.** Results use Freddie Mac's Sample files: a random 50,000 loans
per origination year (1999-2025; 12,500 so far for 2026), 1.35M loans and 75M loan-months in total,
with performance through 2026-03. Portfolio-level rates are unbiased estimates of the full dataset's,
but thin segments (small states, rare products) are noisy.

**Geographic scope.** Loans in Puerto Rico, Guam and the U.S. Virgin Islands (2,333 loans, 0.17%) are loaded
into `raw` but excluded from modelling, because FRED has no state-level unemployment or house-price
series for them.

**Data corrections.** A handful of monthly records carry implausible current interest rates (30-50%,
reporting months 2017-03/04); they are replaced by the note rate. Loss and recovery sign conventions
are detected per source file. Everything else is used as delivered. See the quirks table in
[runbook.md](runbook.md).

**Macro data.** Monthly state unemployment (BLS via FRED) and quarterly FHFA state house-price indices
(interpolated to months) are the latest vintages, not the real-time data a lender would have seen at
the time, so history is slightly "cleaner" than it was in real time.

**Test data.** The automated tests and CI cannot ship the licensed Freddie Mac files, so they run on a
small synthetic dataset in the same layout (`src/loanlens/synthetic/`, `tiny` profile). It is never
used for reported results.

## 2. Modelling

- **Out-of-time evaluation.** Models train on 1999-2011 vintages, are recalibrated on 2012-2013, and
  are tested on 2014-2023 (the latest vintages with a full 24-month window). The recalibrated PD reflects the post-crisis regime; a new crisis would
  make it under-predict until it is recalibrated.
- **Origination PD sees no future economics.** Vintages whose first 24 months hit a shock (for
  example 2020) show a level gap. The macro-conditional hazard model handles post-origination
  conditions.
- **Features.** State or region identity is deliberately excluded from the PD model. Local economics
  enter only through state unemployment and house-price growth at origination.
- **Survival.** Kaplan-Meier treats the competing event as censoring (cause-specific). Cumulative
  incidence uses Aalen-Johansen. Cox models are fitted on samples of 40k loans (static) and 10k loans
  (time-varying, quarterly intervals). Proportional hazards is tested and only approximately holds.
- **Hazard and stress models:**
  - Logistic discrete-time hazards, with coefficients inspected for sign and size.
  - COVID forbearance months are excluded from development.
  - LGD is priced at an assumed 18-month liquidation lag.
  - Losses are expected values per loan (no idiosyncratic simulation).
  - The book runs off with no new originations.
  - A 10,000-loan random sample is scaled to the book.
- **Scenario generator.** Expansion dynamics are estimated on U.S. history since 1990, excluding
  recessions and the 18 months after them. Recession starts are drawn at 14% a year. Severity follows
  Beta(2, 3.5), giving an unemployment rise of 1.5-10 points and a house-price fall of up to about
  45% peak to trough. States follow estimated betas plus AR(1) noise. These are illustrative choices,
  not a forecast.
- **Backtest.** The out-of-sample backtest removes 2007-2010 from development; the in-sample version
  uses the production fit. Losses on actual defaults are compared at their eventual realised value.
- **Profit model.** Net margin of 0.50% a year (a guarantee-fee-style spread), a 2-year horizon
  matching the PD window, and severity by LTV band and MI from resolved historical defaults. No
  prepayment timing, funding or capital costs. Sensitivity is reported in the memo.

## 3. Interpretation

- Every relationship reported is an **association** in observational data, not a causal effect.
- Only approved loans are observed (**no reject inference**), so a cutoff's effect on applicants who
  were previously declined is unknown. A pilot is recommended.
- The stress distribution reflects **macro uncertainty only**; model uncertainty (see the
  out-of-sample backtest gap) means tail losses should be read as a lower bound.

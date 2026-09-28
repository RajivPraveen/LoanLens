"""Time-to-default and time-to-prepayment (competing risks).

Each loan is followed from its first payment until the first of: credit event (default),
voluntary payoff (prepayment), or censoring (still active at the data cut-off, matured,
repurchased). Default and prepayment compete - a loan that prepays can no longer default -
so cumulative incidence uses the Aalen-Johansen estimator rather than 1 - Kaplan-Meier.

Models:
- Kaplan-Meier survival (no default) by credit-score band.
- Aalen-Johansen cumulative incidence of default and prepayment by origination era.
- Cause-specific Cox proportional hazards models at origination, with a PH-assumption test.
- Time-varying Cox models where mark-to-market LTV, state unemployment and the refinance
  incentive evolve monthly - showing how the economy accelerates default and payoff.
"""

from __future__ import annotations

import json
import logging
import warnings

import numpy as np
import pandas as pd
from lifelines import AalenJohansenFitter, CoxPHFitter, CoxTimeVaryingFitter, KaplanMeierFitter
from lifelines.statistics import proportional_hazard_test

from loanlens import viz
from loanlens.config import Settings
from loanlens.models.data import load_loans
from loanlens.reporting.md import fmt_pct, table
from loanlens.warehouse import write_table

log = logging.getLogger(__name__)

ERAS = [(1990, 2004, "2000-2004"), (2005, 2008, "2005-2008 (pre-crisis)"),
        (2009, 2014, "2009-2014"), (2015, 2100, "2015+")]
FICO_ORDER = ["<620", "620-679", "680-719", "720-759", "760+"]
COX_COVARIATES = {
    "credit_score": "Credit score (+1 SD)", "orig_ltv": "LTV (+1 SD)", "orig_dti": "DTI (+1 SD)",
    "rate_spread_at_orig": "Rate spread (+1 SD)", "log_orig_upb": "Loan size (+1 SD)",
    "state_ur_at_orig": "State unemployment at orig. (+1 SD)", "investor": "Investment property",
    "cash_out": "Cash-out refinance", "single_borrower": "Single borrower",
    "short_term": "15-year term",
}
TV_COVARIATES = {
    "credit_score": "Credit score (+1 SD)", "mtm_ltv": "Mark-to-market LTV (+1 SD)",
    "state_unemployment_rate": "State unemployment (+1 SD)",
    "state_ur_change_12m": "Unemployment 12m change (+1 SD)",
    "refi_incentive": "Refinance incentive (+1 SD)",
}


def survival_frame(loans: pd.DataFrame) -> pd.DataFrame:
    df = loans.copy()
    prepay = (df["termination_type"] == "Voluntary payoff")
    default_t = df["months_to_default"]
    prepay_t = df["months_to_termination"].where(prepay)
    other_t = df["months_to_termination"].where(~prepay & df["months_to_termination"].notna())
    censor_t = df["months_observable"]
    times = pd.concat([default_t, prepay_t, other_t, censor_t], axis=1)
    times.columns = ["default", "prepay", "other", "censor"]
    df["duration"] = times.min(axis=1).clip(lower=1)
    first = times.idxmin(axis=1)
    df["event"] = np.select([first == "default", first == "prepay"], [1, 2], 0)
    df["era"] = "other"
    for lo, hi, name in ERAS:
        df.loc[df["vintage_year"].between(lo, hi), "era"] = name
    df["investor"] = (df["occupancy"] == "Investment").astype(float)
    df["cash_out"] = (df["loan_purpose"] == "Cash-out refinance").astype(float)
    df["single_borrower"] = (df["num_borrowers"] == 1).astype(float)
    df["short_term"] = (df["orig_loan_term"] <= 180).astype(float)
    return df


def _standardise(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if out[c].nunique() > 2:
            out[c] = (out[c] - out[c].mean()) / out[c].std()
    return out


def _cox(df: pd.DataFrame, event_code: int) -> tuple[CoxPHFitter, pd.DataFrame, float]:
    cols = list(COX_COVARIATES)
    data = _standardise(df[cols + ["duration", "event"]].dropna(), cols)
    data["E"] = (data.pop("event") == event_code).astype(int)
    cph = CoxPHFitter(penalizer=0.01).fit(data, duration_col="duration", event_col="E")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ph = proportional_hazard_test(cph, data, time_transform="rank")
    return cph, ph.summary, float(cph.concordance_index_)


def _time_varying_panel(settings: Settings, sample_ids: pd.Series) -> pd.DataFrame:
    ids = pd.DataFrame({"loan_id": sample_ids})
    from loanlens.warehouse import connect

    with connect(settings, read_only=True) as con:
        con.register("ids", ids)
        panel = con.execute("""
            select f.loan_id, f.loan_age, f.mtm_ltv, f.refi_incentive,
                   m.state_unemployment_rate, m.state_ur_change_12m,
                   f.is_default_event, f.is_prepaid, l.credit_score, l.first_default_month,
                   f.period_month
            from core.fct_loan_monthly f
            join ids using (loan_id)
            join core.dim_loan l using (loan_id)
            left join core.dim_macro m using (macro_key)
            where l.first_default_month is null or f.period_month <= l.first_default_month
        """).df()
    panel = panel.sort_values(["loan_id", "loan_age"])
    panel["q"] = (panel["loan_age"] - 1) // 3
    agg = panel.groupby(["loan_id", "q"]).agg(
        start=("loan_age", "min"), stop=("loan_age", "max"),
        credit_score=("credit_score", "first"), mtm_ltv=("mtm_ltv", "first"),
        refi_incentive=("refi_incentive", "first"),
        state_unemployment_rate=("state_unemployment_rate", "first"),
        state_ur_change_12m=("state_ur_change_12m", "first"),
        default=("is_default_event", "max"), prepay=("is_prepaid", "max"),
    ).reset_index()
    agg["start"] = agg["start"] - 1
    return agg.dropna()


def _tv_cox(panel: pd.DataFrame, event: str) -> CoxTimeVaryingFitter:
    cols = list(TV_COVARIATES)
    data = _standardise(panel[["loan_id", "start", "stop", event] + cols], cols)
    data = data.rename(columns={event: "E"})
    data["E"] = data["E"].astype(int)
    ctv = CoxTimeVaryingFitter(penalizer=0.01)
    ctv.fit(data, id_col="loan_id", start_col="start", stop_col="stop", event_col="E")
    return ctv


def _hr_table(model, labels: dict[str, str]) -> pd.DataFrame:
    s = model.summary
    return pd.DataFrame({
        "covariate": [labels[c] for c in s.index],
        "hazard_ratio": s["exp(coef)"].to_numpy(),
        "ci_low": s["exp(coef) lower 95%"].to_numpy(),
        "ci_high": s["exp(coef) upper 95%"].to_numpy(),
        "p": s["p"].to_numpy(),
    })


def run(settings: Settings) -> dict:
    cfg = settings["survival"]
    loans = survival_frame(load_loans(settings))
    rng_state = 0
    km_df = loans.sample(min(cfg["km_sample"], len(loans)), random_state=rng_state)
    fig_dir = settings.figures_dir
    curves = []

    # --- Kaplan-Meier: probability of no default by FICO band (competing events censored)
    fig, ax = viz.plt.subplots(figsize=(7, 4.4))
    km_rows = []
    for i, band in enumerate(FICO_ORDER):
        g = km_df[km_df["fico_band"] == band]
        if len(g) < 50:
            continue
        kmf = KaplanMeierFitter().fit(g["duration"], g["event"] == 1, label=band)
        sf = kmf.survival_function_[band]
        sf = sf[sf.index <= 120]
        ax.step(sf.index, sf.values, where="post", color=viz.SERIES[i], label=band)
        viz.label_line_end(ax, sf.index[-1], sf.values[-1], band)
        km_rows.append({"fico_band": band, "loans": len(g),
                        "survival_24m": float(kmf.predict(24)), "survival_60m": float(kmf.predict(60)),
                        "survival_120m": float(kmf.predict(120))})
        curves.append(pd.DataFrame({"curve": "km_no_default", "group": band,
                                    "month": sf.index, "value": sf.values}))
    viz.pct_axis(ax, "y", 0)
    ax.set_xlabel("Months since first payment")
    ax.set_ylabel("Share not yet defaulted")
    ax.set_title("Kaplan-Meier: time to default by credit score band")
    ax.legend(title="FICO band", loc="lower left", ncols=3)
    viz.save(fig, fig_dir / "survival_km_fico.png")

    # --- Aalen-Johansen cumulative incidence by era (small multiples: default | prepay)
    fig, axes = viz.plt.subplots(1, 2, figsize=(11, 4.2))
    cif_rows = []
    for i, (_, _, era) in enumerate(ERAS):
        g = km_df[km_df["era"] == era]
        if len(g) < 50:
            continue
        row = {"era": era, "loans": len(g)}
        for ax, code, name in [(axes[0], 1, "default"), (axes[1], 2, "prepay")]:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ajf = AalenJohansenFitter(calculate_variance=False).fit(
                    g["duration"], g["event"], event_of_interest=code)
            cif = ajf.cumulative_density_.iloc[:, 0]
            cif = cif[cif.index <= 120]
            ax.step(cif.index, cif.values, where="post", color=viz.SERIES[i], label=era)
            for horizon in (24, 60):
                row[f"{name}_cif_{horizon}m"] = float(cif[cif.index <= horizon].iloc[-1]) if (cif.index <= horizon).any() else np.nan
            curves.append(pd.DataFrame({"curve": f"cif_{name}", "group": era,
                                        "month": cif.index, "value": cif.values}))
        cif_rows.append(row)
    for ax, name in [(axes[0], "Cumulative default incidence"), (axes[1], "Cumulative prepayment incidence")]:
        viz.pct_axis(ax, "y", 0)
        ax.set_xlabel("Months since first payment")
        ax.set_title(name)
    axes[0].legend(title="Origination era", loc="upper left")
    viz.save(fig, fig_dir / "survival_cif_era.png")

    # --- Cause-specific Cox models at origination
    cox_df = loans.sample(min(cfg["cox_sample"], len(loans)), random_state=1)
    cox_def, ph_def, c_def = _cox(cox_df, 1)
    cox_pre, ph_pre, c_pre = _cox(cox_df, 2)
    hr_def, hr_pre = _hr_table(cox_def, COX_COVARIATES), _hr_table(cox_pre, COX_COVARIATES)

    # --- Time-varying Cox models with monthly macro covariates
    tv_ids = loans.sample(min(cfg["cox_tv_sample"], len(loans)), random_state=2)["loan_id"]
    panel = _time_varying_panel(settings, tv_ids)
    tv_def, tv_pre = _tv_cox(panel, "default"), _tv_cox(panel, "prepay")
    tv_hr_def, tv_hr_pre = _hr_table(tv_def, TV_COVARIATES), _hr_table(tv_pre, TV_COVARIATES)

    _forest(fig_dir / "survival_cox_hr.png", hr_def, hr_pre,
            "Cox hazard ratios at origination (log scale)")
    _forest(fig_dir / "survival_tv_cox_hr.png", tv_hr_def, tv_hr_pre,
            "Time-varying Cox: how the economy moves the hazards (log scale)")

    summary = {
        "loans": int(len(loans)),
        "events": {"default": int((loans["event"] == 1).sum()),
                   "prepay": int((loans["event"] == 2).sum()),
                   "censored": int((loans["event"] == 0).sum())},
        "km_by_fico": km_rows,
        "cif_by_era": cif_rows,
        "cox_default": {"concordance": c_def, "hazard_ratios": hr_def.to_dict("records"),
                        "ph_violations": int((ph_def["p"] < 0.01).sum())},
        "cox_prepay": {"concordance": c_pre, "hazard_ratios": hr_pre.to_dict("records"),
                       "ph_violations": int((ph_pre["p"] < 0.01).sum())},
        "tv_cox_default": {"hazard_ratios": tv_hr_def.to_dict("records"), "intervals": int(len(panel))},
        "tv_cox_prepay": {"hazard_ratios": tv_hr_pre.to_dict("records")},
    }
    out_dir = settings.artifacts_dir / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "survival_summary.json").write_text(json.dumps(summary, indent=2, default=float))
    write_table(settings, "ml.survival_curves", pd.concat(curves, ignore_index=True))
    _report(settings, summary, hr_def, hr_pre, tv_hr_def, tv_hr_pre, ph_def, ph_pre)
    return summary


def _forest(path, hr_def: pd.DataFrame, hr_pre: pd.DataFrame, title: str) -> None:
    labels = list(hr_def["covariate"])
    y = np.arange(len(labels))
    fig, ax = viz.plt.subplots(figsize=(8, 0.42 * len(labels) + 1.4))
    for i, (df, name, off) in enumerate([(hr_def, "Default", -0.15), (hr_pre, "Prepayment", 0.15)]):
        ax.errorbar(df["hazard_ratio"], y + off,
                    xerr=[df["hazard_ratio"] - df["ci_low"], df["ci_high"] - df["hazard_ratio"]],
                    fmt="o", color=viz.SERIES[i], markersize=6, elinewidth=1.5, capsize=0, label=name)
    ax.axvline(1, color=viz.AXIS, lw=1)
    ax.set_xscale("log")
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Hazard ratio (1 = no effect)")
    ax.set_title(title)
    ax.legend(loc="lower right")
    viz.save(fig, path)


def _report(settings, s, hr_def, hr_pre, tv_def, tv_pre, ph_def, ph_pre) -> None:
    hr_fmt = {"hazard_ratio": lambda v: f"{v:.2f}", "ci_low": lambda v: f"{v:.2f}",
              "ci_high": lambda v: f"{v:.2f}", "p": lambda v: "<0.001" if v < 0.001 else f"{v:.3f}"}
    km = pd.DataFrame(s["km_by_fico"])
    cif = pd.DataFrame(s["cif_by_era"])
    pct_cols = {c: (lambda v: fmt_pct(v, 1)) for c in cif.columns if "cif" in c}
    ev = s["events"]

    def lookup(tbl, name):
        row = tbl[tbl["covariate"] == name]
        return float(row["hazard_ratio"].iloc[0]) if len(row) else float("nan")

    text = f"""# Survival analysis: time to default and prepayment

*Generated by `loanlens survival` - profile `{settings.profile}`.*

{s['loans']:,} loans followed from first payment: **{ev['default']:,} defaults**, **{ev['prepay']:,}
prepayments**, {ev['censored']:,} censored (active at the data cut-off, matured or repurchased).
Default and prepayment are competing risks - a loan that pays off can no longer default - so
cumulative incidence is estimated with Aalen-Johansen, and Kaplan-Meier curves treat the
competing event as censoring (a cause-specific view).

## Kaplan-Meier: share of loans not yet defaulted

{table(km, {c: (lambda v: fmt_pct(v, 2)) for c in km.columns if "survival" in c} | {"loans": lambda v: f"{int(v):,}"})}

![KM by FICO](figures/survival_km_fico.png)

## Cumulative incidence by origination era (Aalen-Johansen)

{table(cif, pct_cols | {"loans": lambda v: f"{int(v):,}"})}

![CIF by era](figures/survival_cif_era.png)

Pre-crisis loans default several times more often than post-2009 loans, and every era shows
the refinance waves (2003, 2012, 2020-21) as steep steps in prepayment incidence. Prepayment
matters to an investor as much as default: it shortens the life of the asset and cuts the
interest income it earns.

## Cox proportional hazards at origination

Cause-specific models, continuous covariates standardised (hazard ratio per 1 SD).
Concordance: default **{s['cox_default']['concordance']:.3f}**, prepayment **{s['cox_prepay']['concordance']:.3f}**.

**Default hazard**

{table(hr_def, hr_fmt)}

**Prepayment hazard**

{table(hr_pre, hr_fmt)}

![Cox HRs](figures/survival_cox_hr.png)

*Proportional-hazards check* (Schoenfeld residual test, rank time transform): {s['cox_default']['ph_violations']}
of {len(ph_def)} default covariates and {s['cox_prepay']['ph_violations']} of {len(ph_pre)} prepayment covariates reject
PH at the 1% level. With this many loans small departures are detectable; the main one is that
origination attributes matter less as loans season and the economy takes over - which is why
the time-varying model below is the better description of risk.

## Time-varying Cox: how the economy moves the hazards

{s['tv_cox_default']['intervals']:,} loan-quarter intervals. Covariates update every quarter.

**Default hazard**

{table(tv_def, hr_fmt)}

**Prepayment hazard**

{table(tv_pre, hr_fmt)}

![TV Cox HRs](figures/survival_tv_cox_hr.png)

- A one-standard-deviation rise in mark-to-market LTV multiplies the default hazard by
  **{lookup(tv_def, 'Mark-to-market LTV (+1 SD)'):.2f}x**, and a 1 SD rise in state unemployment by
  **{lookup(tv_def, 'State unemployment (+1 SD)'):.2f}x**.
- A 1 SD larger refinance incentive multiplies the prepayment hazard by
  **{lookup(tv_pre, 'Refinance incentive (+1 SD)'):.2f}x**, while higher mark-to-market LTV
  suppresses prepayment ({lookup(tv_pre, 'Mark-to-market LTV (+1 SD)'):.2f}x) - underwater borrowers cannot refinance.

These are associations estimated from observational data, not causal effects.
"""
    (settings.reports_dir / "survival_report.md").write_text(text)

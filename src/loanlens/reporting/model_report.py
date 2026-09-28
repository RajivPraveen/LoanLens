"""reports/model_report.md - default model evaluation, figures and interpretation."""

from __future__ import annotations

import pandas as pd
from sklearn.metrics import roc_curve

from loanlens import viz
from loanlens.config import Settings
from loanlens.models.default_model import TARGET, calibration_table
from loanlens.reporting.md import fmt_pct, table


def _figures(settings: Settings, test: pd.DataFrame) -> None:
    y = test[TARGET].astype(int).to_numpy()
    fig_dir = settings.figures_dir

    # ROC: two series, legend + direct AUC labels
    fig, ax = viz.plt.subplots(figsize=(5.6, 5))
    for i, (col, name) in enumerate([("pd_lr", "Logistic regression"), ("pd_xgb", "XGBoost")]):
        fpr, tpr, _ = roc_curve(y, test[col])
        from sklearn.metrics import roc_auc_score

        ax.plot(fpr, tpr, color=viz.SERIES[i], label=f"{name} (AUC {roc_auc_score(y, test[col]):.3f})")
    ax.plot([0, 1], [0, 1], color=viz.DEEMPHASIS, lw=1)
    ax.set_xlabel("False positive rate")
    ax.set_ylabel("True positive rate")
    ax.set_title("ROC - out-of-time test vintages")
    ax.grid(axis="both")
    ax.legend(loc="lower right")
    viz.save(fig, fig_dir / "pd_roc.png")

    # Calibration: predicted vs observed by decile
    fig, ax = viz.plt.subplots(figsize=(5.6, 5))
    hi = 0
    for i, (col, name) in enumerate([("pd_lr", "Logistic regression (recalibrated)"),
                                     ("pd_xgb", "XGBoost (recalibrated)"),
                                     ("pd_xgb_raw", "XGBoost (raw)")]):
        cal = calibration_table(y, test[col].to_numpy())
        hi = max(hi, cal["mean_pd"].max(), cal["observed"].max())
        ax.plot(cal["mean_pd"], cal["observed"], marker="o", markersize=6,
                color=viz.SERIES[i], label=name)
    ax.plot([0, hi], [0, hi], color=viz.DEEMPHASIS, lw=1, label="Perfect calibration")
    viz.pct_axis(ax, "x", 1)
    viz.pct_axis(ax, "y", 1)
    ax.grid(axis="both")
    ax.set_xlabel("Mean predicted PD (decile)")
    ax.set_ylabel("Observed default rate")
    ax.set_title("Calibration - test vintages")
    ax.legend(loc="upper left")
    viz.save(fig, fig_dir / "pd_calibration.png")

    # Lift: observed default rate by XGBoost risk decile (single series)
    cal = calibration_table(y, test["pd_xgb"].to_numpy())
    fig, ax = viz.plt.subplots(figsize=(6.4, 4))
    ax.bar(cal["bin"] + 1, cal["observed"], color=viz.SERIES[0], width=0.7)
    ax.axhline(y.mean(), color=viz.INK_2, lw=1)
    ax.annotate(f"portfolio average {y.mean():.2%}", (0.6, y.mean()), xytext=(0, 4),
                textcoords="offset points", fontsize=9, color=viz.INK_2)
    top = cal.iloc[-1]
    ax.annotate(f"{top['observed']:.2%}", (10, top["observed"]), xytext=(0, 4),
                textcoords="offset points", ha="center", fontsize=9, color=viz.INK_2)
    viz.pct_axis(ax, "y", 1)
    ax.set_xticks(range(1, 11))
    ax.set_xlabel("Predicted risk decile (1 = lowest)")
    ax.set_ylabel("Observed default rate")
    ax.set_title("Risk ranking - observed 24m default rate by XGBoost decile")
    viz.save(fig, fig_dir / "pd_lift.png")


def write_model_report(settings: Settings, metrics: dict, test: pd.DataFrame) -> None:
    _figures(settings, test)
    t = metrics["test"]
    rows = []
    for key, name in [("logistic_regression", "Logistic regression (baseline)"),
                      ("xgboost", "XGBoost"),
                      ("logistic_regression_uncalibrated", "Logistic regression - raw"),
                      ("xgboost_uncalibrated", "XGBoost - raw")]:
        m = t[key]
        rows.append({"Model": name, "AUC": m["auc"], "PR-AUC": m["pr_auc"], "KS": m["ks"],
                     "Brier": m["brier"], "ECE": m["ece"], "Mean PD": m["mean_pd"],
                     "Precision top 10%": m["precision_top10"], "Recall top 10%": m["recall_top10"]})
    perf = pd.DataFrame(rows)
    pct = {c: (lambda v: fmt_pct(v, 2)) for c in ("Mean PD", "Precision top 10%", "Recall top 10%", "ECE")}
    pct["Brier"] = lambda v: f"{v:.5f}"

    split = pd.DataFrame([{"Split": k, "Vintages": f"{v['years'][0]}-{v['years'][1]}",
                           "Loans": f"{v['loans']:,}", "Defaults": f"{v['defaults']:,}",
                           "Default rate": v["default_rate"]} for k, v in metrics["split"].items()])
    by_year = pd.DataFrame(metrics["test_by_vintage"])
    shap_top = pd.DataFrame(metrics["shap_global"]).head(10)
    coefs = pd.DataFrame(metrics["lr_coefficients"]).head(10)
    ci_lr, ci_x = metrics["auc_ci95"]["logistic_regression"], metrics["auc_ci95"]["xgboost"]
    xgb, lr = t["xgboost"], t["logistic_regression"]
    diff = xgb["auc"] - lr["auc"]
    raw = t["xgboost_uncalibrated"]
    if abs(xgb["mean_pd"] - xgb["base_rate"]) <= abs(raw["mean_pd"] - xgb["base_rate"]):
        calib_note = "recalibration removes most of the level bias inherited from the crisis-heavy training data."
    else:
        calib_note = (f"here the raw scores are closer. The calibration vintages ({metrics['split']['calibration']['years'][0]}-"
                      f"{metrics['split']['calibration']['years'][1]}, default rate "
                      f"{fmt_pct(metrics['split']['calibration']['default_rate'])}) were unusually clean, while later "
                      f"vintages were riskier, so the recalibrated PDs **under-predict** the test level. Ranking is "
                      f"unaffected; before production use, recalibrate on the most recent fully observed vintages.")
    years = set(by_year["vintage_year"].astype(int))
    examples = [txt for yrs, txt in (({2019, 2020}, "loans originated in 2019-2020 ran into COVID"),
                                     ({2022, 2023}, "2022-2023 loans were written at higher debt-to-income ratios into a rate shock"))
                if yrs <= years]
    shock_note = f" ({'; '.join(examples)})" if examples else ""
    gaps = by_year.assign(gap=by_year["default_rate"] - by_year["mean_pd_xgb"]).nlargest(3, "gap")
    worst = ", ".join(f"{int(r.vintage_year)} ({fmt_pct(r.default_rate)} observed vs {fmt_pct(r.mean_pd_xgb)} predicted)"
                      for r in gaps.itertuples())

    text = f"""# Default model report

*Generated by `loanlens train` - profile `{settings.profile}`.*

**Target:** credit event within **{metrics['window_months']} months** of first payment - 90+ days
delinquent outside COVID forbearance, REO, or a loss-generating termination (short sale,
third-party sale, REO disposition, note sale).

## Out-of-time design

Loans are split by origination year, never at random, so the test set is strictly later
than anything the models saw. Only loans whose full {metrics['window_months']}-month window is
observed are used.

{table(split, {"Default rate": lambda v: fmt_pct(v, 2)})}

The training vintages include the 2005-2008 crisis cohorts, so their base rate is far above
the later vintages. Both models are **recalibrated** (logistic recalibration of the raw score)
on the calibration vintages so predicted PDs reflect the current regime; this is monotone
and leaves ranking metrics unchanged.

## Results on test vintages

{table(perf, pct)}

- **XGBoost AUC {xgb['auc']:.3f}** (95% bootstrap CI {ci_x[0]:.3f}-{ci_x[1]:.3f}) vs
  **logistic regression {lr['auc']:.3f}** ({ci_lr[0]:.3f}-{ci_lr[1]:.3f}): a difference of
  {diff:+.3f} AUC points.
- Observed test default rate {fmt_pct(xgb['base_rate'])} vs mean predicted PD
  {fmt_pct(xgb['mean_pd'])} (XGBoost, recalibrated) and {fmt_pct(raw['mean_pd'])} raw -
  {calib_note}
- The riskiest 10% of test loans capture {fmt_pct(xgb['recall_top10'], 1)} of defaults
  (lift {xgb['lift_top10']:.1f}x).

![ROC](figures/pd_roc.png)
![Calibration](figures/pd_calibration.png)
![Lift](figures/pd_lift.png)

## Stability by test vintage

{table(by_year, {"default_rate": lambda v: fmt_pct(v, 2), "mean_pd_xgb": lambda v: fmt_pct(v, 2),
                 "vintage_year": lambda v: str(int(v)), "loans": lambda v: f"{int(v):,}"})}

The largest level gaps are {worst}. Vintages whose first 24 months overlap a shock{shock_note}
default above prediction - an origination-only model cannot see post-origination economics. That is what the macro-conditional hazard model in the
stress-testing module is for.

## Explainability

Global drivers (mean |SHAP| on a test sample, one-hot columns summed back to the feature):

{table(shap_top, {"mean_abs_shap": lambda v: f"{v:.4f}"})}

![SHAP importance](figures/pd_shap_importance.png)
![SHAP beeswarm](figures/pd_shap_beeswarm.png)

Every scored loan also carries its top three risk-increasing drivers (`risk_driver_1..3`,
from TreeSHAP) - the basis for adverse-action style reason codes.

Largest logistic-regression coefficients (standardised numeric features):

{table(coefs, {"coefficient": lambda v: f"{v:+.3f}", "odds_ratio_per_sd": lambda v: f"{v:.2f}"})}

## Caveats

- Freddie Mac data only contains **approved, agency-conforming** loans; the model has never
  seen rejected applicants (no reject inference), so it ranks risk *within* an approved-like
  population.
- Results show associations, not causal effects.
- XGBoost is constrained to be monotone in credit score, LTV, CLTV and DTI so its behaviour
  is explainable and consistent with credit policy.
"""
    (settings.reports_dir / "model_report.md").write_text(text)

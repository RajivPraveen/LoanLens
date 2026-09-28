"""Fair-lending review of the origination PD model.

Freddie Mac's loan-level data contains no race, ethnicity, sex or age, so protected-class
outcomes cannot be measured directly (that requires HMDA data). The review therefore audits
the groups the data *does* identify and that fair-lending examiners also look at:

* geography - census region and the largest states (redlining / place-based disparity);
* first-time homebuyers vs repeat buyers (purchase loans only);
* single-borrower vs multi-borrower loans (related to marital status, an ECOA-protected basis);
* loan-size terciles (a proxy for borrower income and wealth).

For each group, on the out-of-time test vintages: discrimination (AUC), calibration (mean PD /
observed default rate), and decision impact at the recommended cutoff and at a stricter
"decline the riskiest 10%" policy - approval rate, adverse impact ratio (AIR, group approval
rate / most-approved group; < 0.80 fails the four-fifths rule of thumb), and the error rates
among loans that did not default (false decline rate) and that did (true decline rate).
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from loanlens import viz
from loanlens.config import Settings
from loanlens.reporting.md import fmt_pct, table

log = logging.getLogger(__name__)

TARGET = "default_in_window"


def _groups(df: pd.DataFrame, top_states: int = 8) -> dict[str, pd.Series]:
    size = pd.qcut(df["orig_upb"].rank(method="first"), 3, labels=["Smallest third", "Middle third", "Largest third"])
    fthb = df["is_first_time_homebuyer"].map({True: "First-time buyer", False: "Repeat buyer"})
    big_states = df["state_code"].value_counts().head(top_states).index
    return {
        "Census region": df["census_region"],
        "State (largest)": df["state_code"].where(df["state_code"].isin(big_states)),
        "First-time homebuyer (purchases)": fthb,
        "Number of borrowers": np.where(df["num_borrowers"] == 1, "Single borrower", "Two or more"),
        "Loan size": size.astype(str),
    }


def audit(df: pd.DataFrame, cutoffs: dict[str, float], min_group: int) -> pd.DataFrame:
    rows = []
    for dim, labels in _groups(df).items():
        labels = pd.Series(labels, index=df.index)
        for group, g in df.groupby(labels):
            if len(g) < min_group:
                continue
            y, p = g[TARGET].astype(int), g["pd_xgb"]
            row = {"dimension": dim, "group": group, "loans": len(g), "defaults": int(y.sum()),
                   "default_rate": y.mean(), "mean_pd": p.mean(),
                   "calibration_ratio": p.mean() / y.mean() if y.mean() > 0 else np.nan,
                   "auc": roc_auc_score(y, p) if 0 < y.sum() < len(y) else np.nan}
            for name, c in cutoffs.items():
                declined = p > c
                row[f"approval_{name}"] = 1 - declined.mean()
                row[f"false_decline_{name}"] = declined[y == 0].mean()
                row[f"true_decline_{name}"] = declined[y == 1].mean() if y.sum() else np.nan
            rows.append(row)
    out = pd.DataFrame(rows)
    for name in cutoffs:
        best = out.groupby("dimension")[f"approval_{name}"].transform("max")
        out[f"air_{name}"] = out[f"approval_{name}"] / best
    return out


def run(settings: Settings) -> dict:
    art = settings.artifacts_dir
    test = pd.read_parquet(art / "models" / "pd_test_predictions.parquet")
    cutoff_path = art / "decision" / "cutoff.json"
    recommended = json.loads(cutoff_path.read_text())["cutoff_pd"] if cutoff_path.exists() else float(np.quantile(test["pd_xgb"], 0.99))
    strict = float(np.quantile(test["pd_xgb"], 0.90))
    cutoffs = {"recommended": recommended, "strict": strict}
    cfg = settings["fairness"]
    res = audit(test, cutoffs, cfg["min_group_size"])
    overall_auc = roc_auc_score(test[TARGET].astype(int), test["pd_xgb"])
    # Fairness is about groups relative to each other: a level shift that affects every group
    # equally (the whole test period defaulting above prediction) is a calibration issue for the
    # model, not a disparity. Groups are flagged on calibration relative to the overall ratio.
    overall_calibration = float(test["pd_xgb"].mean() / test[TARGET].astype(int).mean())
    res["relative_calibration"] = res["calibration_ratio"] / overall_calibration

    flags = []
    for _, r in res.iterrows():
        for name in cutoffs:
            if r[f"air_{name}"] < cfg["air_threshold"]:
                flags.append(f"{r['dimension']} / {r['group']}: AIR {r[f'air_{name}']:.2f} under the {name} cutoff")
        rel = r["relative_calibration"]
        if not np.isnan(rel) and r["defaults"] >= 20 and not 0.67 <= rel <= 1.5:
            flags.append(f"{r['dimension']} / {r['group']}: relative calibration {rel:.2f} "
                         f"(calibration ratio {r['calibration_ratio']:.2f} vs {overall_calibration:.2f} overall; PD "
                         f"{'over' if rel > 1 else 'under'}-states this group's risk relative to others)")
        if not np.isnan(r["auc"]) and r["defaults"] >= 20 and overall_auc - r["auc"] > 0.05:
            flags.append(f"{r['dimension']} / {r['group']}: AUC {r['auc']:.3f} vs {overall_auc:.3f} overall")

    out_dir = art / "fairness"
    out_dir.mkdir(parents=True, exist_ok=True)
    res.to_csv(out_dir / "fairness_metrics.csv", index=False)
    _figure(settings, res)
    summary = {"cutoffs": cutoffs, "overall_auc": overall_auc, "overall_calibration": overall_calibration, "flags": flags,
               "min_air_recommended": float(res["air_recommended"].min()),
               "min_air_strict": float(res["air_strict"].min())}
    (out_dir / "fairness_summary.json").write_text(json.dumps(summary, indent=2, default=float))
    _report(settings, res, summary)
    log.info("Fairness review: %d flags; min AIR %.3f (recommended), %.3f (strict)",
             len(flags), summary["min_air_recommended"], summary["min_air_strict"])
    return summary


def _figure(settings: Settings, res: pd.DataFrame) -> None:
    dims = list(dict.fromkeys(res["dimension"]))
    fig, axes = viz.plt.subplots(1, 2, figsize=(11, 0.34 * len(res) + 1.8), sharey=True)
    labels = [f"{g}" for g in res["group"]]
    y = np.arange(len(res))
    # Calibration: mean PD vs observed default rate per group
    ax = axes[0]
    ax.hlines(y, res["default_rate"], res["mean_pd"], color=viz.DEEMPHASIS, lw=2)
    ax.scatter(res["default_rate"], y, color=viz.SERIES[0], s=36, label="Observed default rate", zorder=3)
    ax.scatter(res["mean_pd"], y, color=viz.SERIES[1], s=36, label="Mean predicted PD", zorder=3)
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    viz.pct_axis(ax, "x", 1)
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.set_title("Calibration by group")
    ax.legend(loc="lower right", fontsize=8)
    # AIR under the strict policy
    ax = axes[1]
    ax.barh(y, res["air_strict"], color=viz.SERIES[0], height=0.6)
    ax.axvline(0.8, color=viz.STATUS["critical"], lw=1)
    ax.annotate("four-fifths line", (0.8, len(res) - 0.5), xytext=(4, 0), textcoords="offset points",
                fontsize=8, color=viz.INK_2)
    ax.set_xlim(0.5, 1.02)
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.set_title("Adverse impact ratio - strict policy (decline riskiest 10%)")
    # dimension separators
    start = 0
    for d in dims:
        n = (res["dimension"] == d).sum()
        for a in axes:
            a.axhline(start - 0.5, color=viz.GRID, lw=0.8)
        axes[0].annotate(d, (0, start - 0.5), xycoords=("axes fraction", "data"), xytext=(0, 3),
                         textcoords="offset points", fontsize=8, color=viz.MUTED)
        start += n
    viz.save(fig, settings.figures_dir / "fairness_groups.png")


def _report(settings: Settings, res: pd.DataFrame, s: dict) -> None:
    view = res[["dimension", "group", "loans", "default_rate", "mean_pd", "calibration_ratio", "relative_calibration", "auc",
                "approval_recommended", "air_recommended", "approval_strict", "air_strict",
                "false_decline_strict"]]
    fmt = {c: (lambda v: fmt_pct(v, 2)) for c in ("default_rate", "mean_pd")}
    fmt |= {c: (lambda v: fmt_pct(v, 1)) for c in ("approval_recommended", "approval_strict", "false_decline_strict")}
    fmt |= {c: (lambda v: "–" if pd.isna(v) else f"{v:.2f}") for c in ("calibration_ratio", "relative_calibration", "air_recommended", "air_strict")}
    fmt |= {"auc": lambda v: "–" if pd.isna(v) else f"{v:.3f}", "loans": lambda v: f"{int(v):,}"}
    flags = "\n".join(f"- {f}" for f in s["flags"]) or "- None - no group breaches the thresholds."
    text = f"""# Fair-lending review: origination default model

*Generated by `loanlens fairness` - profile `{settings.profile}`. Out-of-time test vintages.*

## Scope and limits

Freddie Mac loan-level data has **no race, ethnicity, sex or age fields**, so outcomes for
ECOA/Fair Housing Act protected classes cannot be measured here; a full fair-lending analysis would join
HMDA data (or use BISG proxies) and include a less-discriminatory-alternative search. This review covers
the groups the data identifies: geography, first-time buyers, single vs multiple borrowers (related to
marital status, a protected basis) and loan size (an income/wealth proxy).

The model excludes state and region identity as features by design; local economic conditions enter only
through state unemployment and house-price growth at origination.

## Thresholds

- Adverse impact ratio (AIR) below **{settings['fairness']['air_threshold']:.2f}** (four-fifths rule of thumb).
- Relative calibration outside **0.67-1.50**, for groups with >= 20 defaults: the group's calibration ratio
  (mean PD / observed default rate) divided by the overall ratio ({s['overall_calibration']:.2f}). A shift that
  moves every group together is a model-level calibration issue (see the model report), not a disparity.
- Group AUC more than **0.05** below the overall AUC ({s['overall_auc']:.3f}).

Two decision policies are tested: the **recommended** cutoff (PD > {fmt_pct(s['cutoffs']['recommended'], 2)} declined) and a
**strict** policy declining the riskiest 10% (PD > {fmt_pct(s['cutoffs']['strict'], 2)}).

## Findings

{flags}

Minimum AIR: **{s['min_air_recommended']:.3f}** under the recommended cutoff, **{s['min_air_strict']:.3f}** under the strict policy.

{table(view, fmt)}

![Groups](figures/fairness_groups.png)

## Interpretation

- Under the recommended cutoff almost every applicant is approved, so approval-rate disparities are negligible.
- A strict policy concentrates declines where observed risk is higher (e.g. single-borrower and small-balance
  loans). Where a group's AIR approaches the four-fifths line, the disparity must be justified by business
  necessity (the calibration columns show whether the higher PD reflects genuinely higher observed defaults)
  and a less discriminatory alternative should be sought - for example risk-based pricing or compensating
  factors instead of a hard decline.
- Calibration within groups shows whether a group's risk is over- or under-stated; a model that systematically
  over-predicts risk for a group would unfairly penalise it even at a fixed cutoff.

Findings are associations in historical data, not evidence of intent, and are documented for model-risk review.
"""
    (settings.reports_dir / "fairness_review.md").write_text(text)

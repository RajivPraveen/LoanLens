"""reports/lending_memo.md - one-page approval-cutoff recommendation for decision makers."""

from __future__ import annotations

import json

import pandas as pd

from loanlens import viz
from loanlens.config import Settings
from loanlens.reporting.md import fmt_money, fmt_pct, table


def _figure(settings: Settings, curve: pd.DataFrame, cutoff: float) -> None:
    c = curve[curve["approval_rate"] >= 0.6].sort_values("approval_rate")
    fig, axes = viz.plt.subplots(1, 2, figsize=(10, 3.6))
    ax = axes[0]
    ax.plot(c["approval_rate"], c["profit"] / 1e6, color=viz.SERIES[0])
    best = curve.iloc[(curve["cutoff_pd"] - cutoff).abs().argmin()]
    ax.scatter([best["approval_rate"]], [best["profit"] / 1e6], s=60, color=viz.SERIES[1],
               edgecolors=viz.SURFACE, linewidths=2, zorder=3)
    ax.annotate(f"recommended: approve {best['approval_rate']:.1%}", (best["approval_rate"], best["profit"] / 1e6),
                xytext=(-8, -16), textcoords="offset points", ha="right", fontsize=9, color=viz.INK_2)
    viz.pct_axis(ax, "x", 0)
    ax.set_xlabel("Approval rate")
    ax.set_ylabel("Realised 2-year profit ($M)")
    ax.set_title("Profit after credit losses")
    ax = axes[1]
    ax.plot(c["approval_rate"], c["default_rate_approved"], color=viz.SERIES[0])
    ax.scatter([best["approval_rate"]], [best["default_rate_approved"]], s=60, color=viz.SERIES[1],
               edgecolors=viz.SURFACE, linewidths=2, zorder=3)
    viz.pct_axis(ax, "x", 0)
    viz.pct_axis(ax, "y", 2)
    ax.set_xlabel("Approval rate")
    ax.set_ylabel("24-month default rate of approved loans")
    ax.set_title("Credit quality of the approved book")
    viz.save(fig, settings.figures_dir / "memo_tradeoff.png")


def write_memo(settings: Settings, r: dict, curve: pd.DataFrame, expected_curve: pd.DataFrame) -> None:
    _figure(settings, curve, r["cutoff_pd"])
    t = r["test"]
    a = r["assumptions"]
    picks = [int((curve["approval_rate"] - target).abs().idxmin())
             for target in (1.0, 0.995, 0.99, 0.98, 0.95, 0.90)]
    picks.append(int((curve["cutoff_pd"] - r["cutoff_pd"]).abs().idxmin()))
    rows = curve.loc[sorted(set(picks))].sort_values("approval_rate", ascending=False)
    rows["Option"] = [("**Recommended**" if abs(c - r["cutoff_pd"]) < 1e-12 else
                       ("Approve all (today)" if ar >= 0.9999 else f"Approve {ar:.1%}"))
                      for c, ar in zip(rows["cutoff_pd"], rows["approval_rate"], strict=True)]
    view = rows[["Option", "cutoff_pd", "approval_rate", "default_rate_approved", "profit", "profit_vs_approve_all"]]
    view.columns = ["Option", "PD cutoff", "Approval rate", "Default rate (approved)", "Profit", "vs. approve all"]
    sens = pd.DataFrame(r["sensitivity"])
    sens_view = sens.rename(columns={"net_margin": "Net margin", "lgd_multiplier": "Severity x",
                                     "cutoff_pd": "Cutoff", "test_approval_rate": "Approval",
                                     "test_profit_uplift": "Profit uplift"})

    level_note = ""
    metrics_path = settings.artifacts_dir / "models" / "pd_metrics.json"
    if metrics_path.exists():
        xgb = json.loads(metrics_path.read_text())["test"]["xgboost"]
        if xgb["mean_pd"] < 0.8 * xgb["base_rate"]:
            level_note = (f" On the test vintages the PD averages {fmt_pct(xgb['mean_pd'])} against {fmt_pct(xgb['base_rate'])} "
                          "observed: the ranking holds, but the level is low, so the profit figures above (which use actual "
                          "outcomes) are reliable while the PD cutoff itself should be re-derived after recalibration.")
    data_note = ("*Figures below come from a synthetic test dataset, not from Freddie Mac data.*"
                 if settings["synthetic"] else
                 "*Based on the Freddie Mac Single-Family Loan-Level Sample (Release 47), with out-of-sample test vintages.*")
    text = f"""# Memo: approval cutoff for new mortgage originations

**To:** Credit Risk Committee  **From:** Portfolio Analytics (LoanLens)  **Re:** PD-based approval cutoff

{data_note}

**Recommendation.** Decline applications whose predicted 24-month default probability exceeds
**{fmt_pct(r['cutoff_pd'], 2)}**. On the out-of-sample test vintages this approves **{fmt_pct(t['approval_rate'], 1)}** of loans
(declines {t['declined_loans']:,} of {t['loans']:,}), removes **{fmt_pct(t['defaults_avoided_share'], 1)} of defaults**, lowers the
approved book's default rate from {fmt_pct(t['default_rate_all'])} to **{fmt_pct(t['default_rate_approved'])}**, and raises
2-year profit after credit losses by **{fmt_pct(t['profit_uplift'], 2)}** ({fmt_money(t['realised_profit'] - t['realised_profit_approve_all'], 'm')})
while giving up {fmt_pct(t['volume_given_up_upb_share'], 1)} of origination volume.

**Why.** A loan earns its net credit margin ({fmt_pct(a['net_margin_annual'], 2)} a year - a guarantee-fee-style spread
net of servicing and capital costs) while it performs and loses
its balance times loss severity when it defaults. Approving is profitable only while
PD < margin x horizon / (margin x horizon + severity) - about **{fmt_pct(r['breakeven_pd_at_mean_lgd'], 1)}** at average
severity. The calibrated model finds a thin tail of applications above that line; the rest of the book is
already profitable, so the right move is a targeted decline of the tail, not a broad tightening.

| Trade-off (test vintages) | | | | | |
|---|---:|---:|---:|---:|---:|
""" + "\n".join(
        "| " + " | ".join([
            str(row["Option"]), fmt_pct(row["PD cutoff"], 2), fmt_pct(row["Approval rate"], 1),
            fmt_pct(row["Default rate (approved)"], 2), fmt_money(row["Profit"], "m"),
            f"{row['vs. approve all'] * 100:+.2f}%"]) + " |"
        for _, row in view.iterrows()) + f"""

Columns: PD cutoff, approval rate, default rate of approved loans, realised 2-year profit, change vs approving all.
Tightening further (e.g. approving 90%) *reduces* profit: it declines many loans that would have been profitable.

![Trade-off](figures/memo_tradeoff.png)

**How robust is it?** The cutoff was chosen on {a['chosen_on']} and evaluated on {a['evaluated_on']} with their actual
default outcomes. Under lower margins or 1.5x loss severity the optimal cutoff falls and the uplift grows:

{table(sens_view, {"Net margin": lambda v: fmt_pct(v, 2), "Cutoff": lambda v: fmt_pct(v, 2), "Approval": lambda v: fmt_pct(v, 1),
                   "Profit uplift": lambda v: f"{v * 100:+.2f}%", "Severity x": lambda v: f"{v:.1f}x"})}

**Risks and caveats.** (1) The model has only seen approved agency loans - declined-applicant performance is
unknown (no reject inference), so the cutoff should be piloted before full rollout. (2) Profit uses a simple
margin and a 2-year horizon; prepayment timing and funding costs are not modelled. (3) A cutoff changes who
is approved - see the fair-lending review (`fairness_review.md`) for approval-rate impact by region and
borrower group before adoption. (4) Recalibrate quarterly: the PD level shifts with the economy.{level_note}

**Next steps.** Pilot the cutoff on one channel for a quarter, track declined-applicant outcomes where
available, and pair it with risk-based pricing for loans just below the line.
"""
    (settings.reports_dir / "lending_memo.md").write_text(text)

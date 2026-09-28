"""reports/stress_test_report.md - Monte Carlo stress test and 2008-2010 backtest."""

from __future__ import annotations

import numpy as np
import pandas as pd

from loanlens import viz
from loanlens.config import Settings
from loanlens.reporting.md import fmt_money, fmt_pct, table
from loanlens.stress.hazard import FEATURES
from loanlens.stress.scenarios import ScenarioSet

NAMED_COLORS = {"Baseline": viz.SERIES[2], "Adverse": viz.SERIES[3],
                "Severely adverse": viz.SERIES[1], "GFC replay (2008-2010)": viz.SERIES[6]}


def _figures(settings: Settings, results: pd.DataFrame, summary: dict, sc: ScenarioSet,
             backtest: dict) -> None:
    fig_dir = settings.figures_dir
    mc = results[results["scenario_type"] == "monte_carlo"]
    named = results[results["scenario_type"] == "named"].set_index("scenario_id")

    # 1. Loss distribution with EL / VaR / ES markers
    fig, ax = viz.plt.subplots(figsize=(8, 4.2))
    ax.hist(mc["loss_rate"], bins=60, color=viz.SERIES[0], edgecolor=viz.SURFACE, linewidth=0.8)
    ymax = ax.get_ylim()[1]
    for value, label in [(summary["expected_loss_rate"], "Expected loss"),
                         (summary["var99_loss_rate"], "99th percentile"),
                         (summary["es99_loss_rate"], "Expected shortfall 99%")]:
        ax.axvline(value, color=viz.INK_2, lw=1)
        ax.annotate(f"{label}\n{value:.2%}", (value, ymax * 0.92), xytext=(4, 0),
                    textcoords="offset points", fontsize=8.5, color=viz.INK_2, va="top")
    viz.pct_axis(ax, "x", 2)
    ax.set_xlabel(f"Cumulative credit loss over {summary['horizon_months']} months (% of current balance)")
    ax.set_ylabel("Scenarios")
    ax.set_title(f"Loss distribution across {summary['n_scenarios']:,} simulated economies")
    viz.save(fig, fig_dir / "stress_loss_distribution.png")

    # 2. Drivers: loss vs unemployment rise, colour = house-price trough (sequential)
    fig, ax = viz.plt.subplots(figsize=(7.5, 4.6))
    pts = ax.scatter(mc["unemployment_rise"], mc["loss_rate"], c=-mc["hpi_trough_change"],
                     cmap=viz.SEQ_CMAP, s=10, linewidths=0)
    cbar = fig.colorbar(pts, ax=ax, pad=0.02)
    cbar.set_label("House-price decline, peak to trough")
    cbar.ax.yaxis.set_major_formatter(viz.plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    cbar.outline.set_visible(False)
    for name, row in named.iterrows():
        ax.scatter(row["unemployment_rise"], row["loss_rate"], s=60, marker="D",
                   color=NAMED_COLORS.get(name, viz.INK), edgecolors=viz.SURFACE, linewidths=2, zorder=3)
        ax.annotate(name, (row["unemployment_rise"], row["loss_rate"]), xytext=(-8, 10),
                    textcoords="offset points", fontsize=8.5, color=viz.INK_2, ha="right",
                    bbox={"boxstyle": "round,pad=0.2", "fc": viz.SURFACE, "ec": "none", "alpha": 0.85})
    viz.pct_axis(ax, "y", 2)
    ax.grid(axis="both")
    ax.set_xlabel("Rise in national unemployment (percentage points, peak vs today)")
    ax.set_ylabel("Credit loss (% of balance)")
    ax.set_title("What drives the losses: unemployment and house prices together")
    viz.save(fig, fig_dir / "stress_drivers.png")

    # 3. Scenario fan charts (small multiples)
    is_mc = np.array([k == "monte_carlo" for k in sc.kind])
    t = np.arange(1, sc.us_ur.shape[1] + 1)
    fig, axes = viz.plt.subplots(1, 2, figsize=(11, 4))
    for ax, series, title, pct in [(axes[0], sc.us_ur, "National unemployment rate", False),
                                   (axes[1], sc.us_hpi_rel - 1, "National house prices vs today", True)]:
        lo, med, hi = np.percentile(series[is_mc], [5, 50, 95], axis=0)
        ax.fill_between(t, lo, hi, color=viz.SEQUENTIAL[1], linewidth=0, label="Monte Carlo 5-95%")
        ax.plot(t, med, color=viz.SERIES[0], label="Monte Carlo median")
        for i in np.flatnonzero(~is_mc):
            ax.plot(t, series[i], color=NAMED_COLORS.get(sc.names[i], viz.INK), lw=1.6, label=sc.names[i])
        ax.set_title(title)
        ax.set_xlabel("Months ahead")
        if pct:
            viz.pct_axis(ax, "y", 0)
        else:
            ax.yaxis.set_major_formatter(viz.plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    axes[0].legend(loc="upper left", fontsize=8)
    viz.save(fig, fig_dir / "stress_scenario_paths.png")

    # 4. Backtest monthly defaults
    m = backtest["monthly"]
    fig, ax = viz.plt.subplots(figsize=(8, 4.2))
    series = [("defaults", "Actual", viz.INK), ("projected_defaults_in_sample", "Model, fitted on all history", viz.SERIES[0]),
              ("projected_defaults", "Model, crisis excluded (out-of-sample)", viz.SERIES[1]),
              ("naive_defaults", "Naive through-the-cycle rate", viz.MUTED)]
    for col, name, color in series:
        ax.plot(m["month"], m[col], color=color, label=name, lw=2 if col != "naive_defaults" else 1.5)
    ax.set_ylabel("Defaults per month")
    ax.set_title(f"Backtest: the {backtest['summary']['as_of']} book through the real 2008-2010 economy")
    ax.legend(loc="upper left")
    viz.save(fig, fig_dir / "stress_backtest.png")


def write_stress_report(settings: Settings, results: pd.DataFrame, summary: dict,
                        sc: ScenarioSet, backtest: dict) -> None:
    _figures(settings, results, summary, sc, backtest)
    named = pd.DataFrame(summary["named"]).rename(columns={
        "scenario_id": "Scenario", "peak_unemployment": "Peak unemployment",
        "hpi_trough_change": "House-price trough", "loss_rate": "Credit loss",
        "default_rate_upb": "Defaulted balance"})
    dec = pd.DataFrame(summary["drivers"]["by_decile"])
    bt = summary["backtest"]
    bt_table = pd.DataFrame([
        {"Measure": "Default rate (loans)", "Actual": bt["actual_default_rate"],
         "Model, all history": bt["in_sample_default_rate"],
         "Model, crisis excluded": bt["projected_default_rate"], "Naive": bt["naive_default_rate"]},
        {"Measure": "Credit loss (% of balance)", "Actual": bt["actual_loss_rate"],
         "Model, all history": bt["in_sample_loss_rate"],
         "Model, crisis excluded": bt["projected_loss_rate"], "Naive": float("nan")},
    ])
    pct = lambda v: fmt_pct(v, 2)  # noqa: E731
    coef = pd.DataFrame({
        "feature": FEATURES,
        "default": [c["coefficient"] for c in summary["hazard_default"]["coefficients"]],
        "prepayment": [c["coefficient"] for c in summary["hazard_prepay"]["coefficients"]],
    })
    d = summary["drivers"]
    severe = next((r for r in summary["named"] if r["scenario_id"] == "Severely adverse"), None)

    text = f"""# Stress test: Monte Carlo credit-loss projection

*Generated by `loanlens stress` - profile `{settings.profile}`.*

**Portfolio:** {summary['portfolio_loans']:,} performing loans, {fmt_money(summary['portfolio_upb'], 'bn')} balance,
as of {summary['as_of']}. **Horizon:** {summary['horizon_months']} months. **Scenarios:** {summary['n_scenarios']:,}
simulated economies plus four named scenarios. Losses are projected on a {summary['sampled_loans']:,}-loan random
sample, scaled to the full book.

## Headline

| Measure | Loss rate | Loss amount |
|---|---:|---:|
| Expected loss (mean of all scenarios) | {pct(summary['expected_loss_rate'])} | {fmt_money(summary['expected_loss_amount'], 'm')} |
| Median scenario | {pct(summary['median_loss_rate'])} | |
| 95th percentile | {pct(summary['p95_loss_rate'])} | |
| **99th percentile (severe but plausible)** | **{pct(summary['var99_loss_rate'])}** | {fmt_money(summary['var99_loss_amount'], 'm')} |
| Expected shortfall, worst 1% | {pct(summary['es99_loss_rate'])} | |
| Severely adverse (CCAR-style) scenario | {pct(severe['loss_rate']) if severe else '–'} | |

{fmt_pct(summary['recession_share'], 0)} of simulated paths contain a recession. The worst 1% of scenarios average a
peak unemployment rate of {summary['tail_avg_peak_unemployment']:.1f}% and a national house-price fall of
{fmt_pct(-summary['tail_avg_hpi_trough'], 0)}.

![Loss distribution](figures/stress_loss_distribution.png)

## Named scenarios

{table(named, {c: pct for c in ('House-price trough', 'Credit loss', 'Defaulted balance')} | {'Peak unemployment': lambda v: f"{v:.1f}%"})}

![Scenario paths](figures/stress_scenario_paths.png)

## What drives the biggest losses

A linear fit of scenario loss on the macro summary explains **R² = {d['r2']:.2f}** of the variation. Each extra point
of peak unemployment adds about **{pct(d['coef_per_pt_unemployment'])}** of loss; each additional 10% house-price fall
adds **{pct(d['coef_per_10pct_hpi_decline'])}**. Losses are largest when both happen together - falling prices push
borrowers underwater exactly when job losses make them unable to pay, and the interaction terms in the hazard
model capture that compounding.

{table(dec, {'loss_rate': pct, 'hpi_trough_change': lambda v: fmt_pct(v, 1), 'recession_share': lambda v: fmt_pct(v, 0),
              'peak_unemployment': lambda v: f"{v:.1f}%", 'decile': lambda v: str(int(v))})}

![Drivers](figures/stress_drivers.png)

## Backtest: 2008-2010

The {bt['loans']:,} loans performing at {bt['as_of']} are projected through the **realised** state unemployment, house
prices and mortgage rates of the next {bt['horizon_months']} months, then compared with what actually happened.

{table(bt_table, {c: pct for c in ('Actual', 'Model, all history', 'Model, crisis excluded', 'Naive')})}

- **Fitted on all history** (the production model), the specification reproduces the crisis within
  {fmt_pct(abs(bt['in_sample_default_error']), 0)} on defaults and {fmt_pct(abs(bt['in_sample_loss_error']), 0)} on losses.
- **Out of sample** - every 2007-2010 observation removed before fitting - {_out_of_sample_text(bt)}

![Backtest](figures/stress_backtest.png)

## Method

1. **Hazard models.** Two discrete-time logistic models on {summary['hazard_default']['rows']:,} loan-months estimate the monthly
   probability of default ({summary['hazard_default']['events']:,} events, in-sample AUC {summary['hazard_default']['auc']:.3f}) and
   voluntary prepayment ({summary['hazard_prepay']['events']:,} events, AUC {summary['hazard_prepay']['auc']:.3f}) from borrower
   attributes, vintage cohort, loan age, mark-to-market LTV, state unemployment (level and 12-month change), the
   refinance incentive and stress interactions. COVID-forbearance months (2020-03 to 2021-12) are
   excluded from fitting because payment relief broke the usual unemployment-default link.
2. **LGD model.** Loss per default event (cures count as zero) regressed on LTV re-priced to the expected liquidation
   date (18 months after default, using the scenario's house-price path), mortgage insurance, judicial-foreclosure
   state and local unemployment. {summary['lgd']['n']:,} resolved defaults, mean LGD {pct(summary['lgd']['mean'])}.
3. **Scenarios.** National unemployment, house prices and mortgage rates are simulated from dynamics estimated on
   FRED history with a recession regime (annual start probability {fmt_pct(settings['stress']['recession_prob_annual'], 0)},
   random severity); states follow through estimated sensitivities (betas) plus idiosyncratic noise.
4. **Projection.** Each month, every loan's default and prepayment hazards are evaluated under the scenario;
   survival follows a competing-risks recursion, balances amortise, and expected default balance x LGD is summed.

Hazard coefficients (raw feature units):

{table(coef, {'default': lambda v: f"{v:+.3f}", 'prepayment': lambda v: f"{v:+.3f}"})}

## Limitations

- Expected-value projection per loan (no loan-level randomness) - the distribution reflects macro uncertainty, which
  dominates portfolio credit risk; idiosyncratic noise diversifies away across tens of thousands of loans.
- Losses are recognised at default (expected LGD), not at the later liquidation date.
- Scenario dynamics and the recession regime are simple and calibrated to U.S. history since 1990; they are not a
  forecast. No new originations are assumed over the horizon (run-off book).
- Freddie Mac loans only; associations, not causal effects.
"""
    (settings.reports_dir / "stress_test_report.md").write_text(text)


def _direction(err: float) -> str:
    return "over-predicts" if err > 0 else "under-predicts"


def _out_of_sample_text(bt: dict) -> str:
    """Backtest narrative that follows the sign of the errors rather than assuming one."""
    d_err, l_err, n_err = bt["default_rate_error"], bt["loss_rate_error"], bt["naive_error"]
    beats = abs(d_err) < abs(n_err)
    head = (f"the model {'captures the direction and timing of the crisis and ' if beats else ''}"
            f"{'beats' if beats else 'does not beat'} a through-the-cycle benchmark "
            f"({fmt_pct(bt['projected_default_rate'])} vs {fmt_pct(bt['naive_default_rate'])} projected against "
            f"{fmt_pct(bt['actual_default_rate'])} actual). It {_direction(d_err)} defaults by "
            f"{fmt_pct(abs(d_err), 0)} and {_direction(l_err)} losses by {fmt_pct(abs(l_err), 0)}.")
    if d_err > 0 and l_err > 0:
        tail = (" Without the crisis in the data, the hazard model carries its pre-crisis sensitivities to "
                "unemployment and negative equity into an economy far outside anything it was fitted on, and overshoots. "
                "The error is on the conservative side, but it is large: the gap between the two fits is a direct "
                "measure of **model uncertainty** in the tail, and the production model (fitted on all history, "
                "including the crisis) is the one to use.")
    elif d_err < 0 and l_err < 0:
        tail = (" Without the crisis in the data the model has almost no examples of deeply underwater borrowers in a "
                "rising-unemployment economy, so it extrapolates the compounding too weakly - the classic stress-model "
                "failure of 2008. **Treat tail losses as a lower bound**, and consider an overlay (e.g. scaling to the "
                "in-sample backtest) for capital planning.")
    else:
        tail = (" Default frequency and loss severity err in opposite directions, so the loss projection partly "
                "offsets; treat the out-of-sample gap as a measure of model uncertainty in the tail.")
    return head + tail

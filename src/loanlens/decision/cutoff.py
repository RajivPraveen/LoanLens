"""Profit-maximising approval cutoff on predicted default probability.

For each loan i with calibrated 24-month PD p_i, balance B_i and expected loss severity L_i:

    projected profit_i = B_i * m * H * (1 - p_i)  -  p_i * L_i * B_i

where m is the net annual margin on a performing loan and H the horizon in years (matching
the PD window). Approving a loan adds profit only while p_i < m*H / (m*H + L_i).

The cutoff is *chosen* on the calibration vintages and *evaluated* on the later test
vintages using their actual default outcomes, so the reported uplift is out-of-sample.
Severity L_i is the historical loss per default event for the loan's LTV band and MI
coverage (cures count as zero loss), measured on resolved defaults from training vintages.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from loanlens.config import Settings
from loanlens.models.data import load_loans, time_split
from loanlens.models.default_model import load_model
from loanlens.warehouse import query

log = logging.getLogger(__name__)


def severity_table(settings: Settings, max_year: int) -> pd.DataFrame:
    return query(settings, f"""
        select l.ltv_band, (coalesce(l.mi_pct, 0) > 0) as has_mi,
               count(*) as defaults,
               avg(greatest(l.net_loss, 0) / nullif(f.exposure_upb, 0)) as lgd
        from core.fct_loan_monthly f join core.dim_loan l using (loan_id)
        where f.is_default_event and l.vintage_year <= {max_year}
          and f.period_month <= l.data_end_month - interval 36 month and f.exposure_upb > 0
        group by all
    """)


def attach_economics(df: pd.DataFrame, sev: pd.DataFrame, margin: float, horizon: float) -> pd.DataFrame:
    out = df.copy()
    out["has_mi"] = out["mi_pct"].fillna(0) > 0
    out = out.merge(sev[["ltv_band", "has_mi", "lgd"]], on=["ltv_band", "has_mi"], how="left")
    out["lgd"] = out["lgd"].fillna(sev["lgd"].mean()).clip(0, 1)
    out["revenue_if_performing"] = out["orig_upb"] * margin * horizon
    out["expected_profit"] = out["revenue_if_performing"] * (1 - out["pd"]) - out["pd"] * out["lgd"] * out["orig_upb"]
    y = out["default_in_window"].astype(float)
    out["realised_profit"] = out["revenue_if_performing"] * (1 - y) - y * out["lgd"] * out["orig_upb"]
    return out


def curve(df: pd.DataFrame, grid: np.ndarray, profit_col: str) -> pd.DataFrame:
    rows = []
    total = df[profit_col].sum()
    for c in grid:
        ok = df["pd"] <= c
        rows.append({
            "cutoff_pd": float(c),
            "approval_rate": float(ok.mean()),
            "approved_loans": int(ok.sum()),
            "default_rate_approved": float(df.loc[ok, "default_in_window"].mean()) if ok.any() else np.nan,
            "expected_loss_rate": float((df.loc[ok, "pd"] * df.loc[ok, "lgd"] * df.loc[ok, "orig_upb"]).sum()
                                        / max(df.loc[ok, "orig_upb"].sum(), 1)),
            "profit": float(df.loc[ok, profit_col].sum()),
            "profit_vs_approve_all": float(df.loc[ok, profit_col].sum() / total - 1) if total else np.nan,
            "approved_upb": float(df.loc[ok, "orig_upb"].sum()),
        })
    return pd.DataFrame(rows)


def optimise(settings: Settings) -> dict:
    cfg = settings["modeling"]
    econ = settings["profit"]
    model = load_model(settings)
    loans = load_loans(settings)
    split = time_split(loans, cfg)
    sev = severity_table(settings, cfg["train_years"][1])
    margin, horizon = econ["net_margin_annual"], econ["horizon_years"]

    parts = {}
    for name in ("calibration", "test"):
        part = split[name].copy()
        part["pd"] = model.predict(part)["pd_xgb"].to_numpy()
        parts[name] = attach_economics(part, sev, margin, horizon)
    cal, test = parts["calibration"], parts["test"]

    # Candidate cutoffs: PD quantiles for approval rates 60-100%, finer in the top 2%.
    all_pd = np.concatenate([cal["pd"], test["pd"]])
    approval_grid = np.r_[np.linspace(0.60, 0.98, 77), np.linspace(0.9825, 1.0, 71)]
    grid = np.unique(np.r_[np.quantile(cal["pd"], approval_grid), all_pd.max() + 1e-9])
    cal_curve = curve(cal, grid, "expected_profit")
    best = cal_curve.loc[cal_curve["profit"].idxmax()]
    cutoff = float(best["cutoff_pd"])

    test_curve = curve(test, grid, "realised_profit")
    test_expected_curve = curve(test, grid, "expected_profit")
    at_cut = curve(test, np.array([cutoff]), "realised_profit").iloc[0]
    at_cut_expected = curve(test, np.array([cutoff]), "expected_profit").iloc[0]
    approve_all = curve(test, np.array([all_pd.max() + 1e-9]), "realised_profit").iloc[0]

    # Sensitivity of the recommendation to the economic assumptions.
    sens = []
    for m_ in (margin * 0.5, margin * (2 / 3), margin, margin * 4 / 3):
        for lgd_mult in (1.0, 1.5):
            sev_s = sev.assign(lgd=(sev["lgd"] * lgd_mult).clip(0, 1))
            c_ = attach_economics(cal.drop(columns=["lgd", "has_mi", "revenue_if_performing",
                                                    "expected_profit", "realised_profit"]), sev_s, m_, horizon)
            t_ = attach_economics(test.drop(columns=["lgd", "has_mi", "revenue_if_performing",
                                                     "expected_profit", "realised_profit"]), sev_s, m_, horizon)
            cc = curve(c_, grid, "expected_profit")
            b = cc.loc[cc["profit"].idxmax()]
            tt = curve(t_, np.array([b["cutoff_pd"]]), "realised_profit").iloc[0]
            sens.append({"net_margin": m_, "lgd_multiplier": lgd_mult, "cutoff_pd": b["cutoff_pd"],
                         "test_approval_rate": tt["approval_rate"],
                         "test_profit_uplift": tt["profit_vs_approve_all"]})

    result = {
        "assumptions": {"net_margin_annual": margin, "horizon_years": horizon,
                        "severity_by_ltv_band": sev.to_dict("records"),
                        "chosen_on": f"calibration vintages {cfg['calibration_years']}",
                        "evaluated_on": f"test vintages {cfg['test_years']}"},
        "cutoff_pd": cutoff,
        "breakeven_pd_at_mean_lgd": float(margin * horizon / (margin * horizon + test["lgd"].mean())),
        "test": {
            "loans": int(len(test)),
            "approval_rate": float(at_cut["approval_rate"]),
            "declined_loans": int(len(test) - at_cut["approved_loans"]),
            "default_rate_approved": float(at_cut["default_rate_approved"]),
            "default_rate_all": float(test["default_in_window"].mean()),
            "defaults_avoided_share": float(1 - test.loc[test["pd"] <= cutoff, "default_in_window"].sum()
                                            / max(test["default_in_window"].sum(), 1)),
            "realised_profit": float(at_cut["profit"]),
            "realised_profit_approve_all": float(approve_all["profit"]),
            "profit_uplift": float(at_cut["profit_vs_approve_all"]),
            "expected_profit_uplift": float(at_cut_expected["profit_vs_approve_all"]),
            "volume_given_up_upb_share": float(1 - at_cut["approved_upb"] / approve_all["approved_upb"]),
        },
        "sensitivity": sens,
    }
    out_dir = settings.artifacts_dir / "decision"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cutoff.json").write_text(json.dumps(result, indent=2, default=float))
    test_curve.to_csv(out_dir / "tradeoff_test.csv", index=False)
    cal_curve.to_csv(out_dir / "tradeoff_calibration.csv", index=False)

    from loanlens.reporting.memo import write_memo

    write_memo(settings, result, test_curve, test_expected_curve)
    log.info("Recommended cutoff PD <= %.2f%%: approve %.1f%%, profit %+.2f%% vs approve-all (test)",
             cutoff * 100, result["test"]["approval_rate"] * 100, result["test"]["profit_uplift"] * 100)
    return result

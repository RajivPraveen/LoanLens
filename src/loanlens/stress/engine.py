"""Monte Carlo stress test, current-book scoring and historical backtest.

Projection: for every scenario and month, each performing loan's default and prepayment
hazards are evaluated under the scenario's state unemployment, house prices (which move its
mark-to-market LTV) and mortgage rate (its refinance incentive). Survival is tracked as a
competing-risks recursion, balances amortise on schedule, and each month's expected default
balance is multiplied by a mark-to-market-LTV-driven LGD. The result is a distribution of
cumulative credit losses over the horizon.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from loanlens.config import Settings
from loanlens.stress.hazard import (
    LIQUIDATION_LAG,
    STATIC_SQL,
    HazardModel,
    LGDModel,
    clean_static,
    feature_matrix,
    fit_hazard,
    fit_lgd,
    load_panel,
)
from loanlens.stress.scenarios import (
    MacroHistory,
    ScenarioSet,
    load_history,
    realised_path,
    simulate,
)
from loanlens.warehouse import connect, write_table

log = logging.getLogger(__name__)


@dataclass
class Portfolio:
    frame: pd.DataFrame        # one row per (sampled) loan
    weight: np.ndarray         # loans represented by each row
    state_idx: np.ndarray
    total_upb: float
    total_loans: int


def load_portfolio(settings: Settings, as_of: str, states: list[str], sample: int | None,
                   seed: int = 3) -> Portfolio:
    with connect(settings, read_only=True) as con:
        df = con.execute(f"""
            select f.loan_id, f.current_upb as bal0, f.current_interest_rate as note_rate,
                   f.loan_age as age0, f.mtm_ltv as mtm0, f.dq_state,
                   {STATIC_SQL}
            from core.fct_loan_monthly f
            join core.dim_loan l using (loan_id)
            where f.period_month = date '{as_of}-01'
              and not f.is_terminated
              and f.current_upb > 0
              and (l.first_default_month is null or l.first_default_month > f.period_month)
        """).df()
    if df.empty:
        raise ValueError(f"No performing loans at {as_of}")
    df = clean_static(df.sort_values("loan_id", ignore_index=True))
    df["mtm0"] = df["mtm0"].fillna(df["orig_ltv"])
    df["remaining"] = np.maximum(df["orig_loan_term"] - df["age0"], 1)
    total_upb, total_loans = float(df["bal0"].sum()), len(df)
    if sample and len(df) > sample:
        df = df.sample(sample, random_state=seed).reset_index(drop=True)
    weight = np.full(len(df), total_loans / len(df))
    state_pos = {s: i for i, s in enumerate(states)}
    return Portfolio(df, weight, df["state_code"].map(state_pos).to_numpy(), total_upb, total_loans)


def _amortised(bal0, rate, remaining, t):
    r = rate / 1200.0
    gn = (1 + r) ** remaining
    gt = (1 + r) ** np.minimum(t, remaining)
    return bal0 * (gn - gt) / (gn - 1)


@dataclass
class Projection:
    loss: np.ndarray             # [S] expected credit loss ($, portfolio scale)
    default_upb: np.ndarray      # [S]
    default_loans: np.ndarray    # [S]
    prepay_upb: np.ndarray       # [S]
    monthly_default_loans: np.ndarray  # [S, T]
    monthly_loss: np.ndarray     # [S, T]
    loan_pd_12m: np.ndarray | None = None   # [N] for single-scenario runs
    loan_lgd_12m: np.ndarray | None = None


def project(p: Portfolio, sc: ScenarioSet, hd: HazardModel, hp: HazardModel, lgd: LGDModel,
            chunk: int = 250, keep_loan_detail: bool = False) -> Projection:
    f = p.frame
    f32 = lambda a: np.asarray(a, dtype=np.float32)  # noqa: E731
    # float32 end to end: halves memory traffic over the scenarios x loans arrays
    hd = HazardModel(np.float32(hd.intercept), hd.coef.astype(np.float32), hd.auc_in_sample, hd.events, hd.rows)
    hp = HazardModel(np.float32(hp.intercept), hp.coef.astype(np.float32), hp.auc_in_sample, hp.events, hp.rows)
    lgd = LGDModel(np.float32(lgd.intercept), lgd.coef.astype(np.float32), lgd.r2, lgd.mean_lgd, lgd.n)
    static = {k: f32(f[k]) for k in ("credit_score", "orig_dti", "orig_cltv", "orig_ltv",
                                     "investor", "cash_out", "single_borrower", "short_term",
                                     "orig_upb", "vintage_year")}
    bal0, note, rem, age0 = f32(f["bal0"]), f32(f["note_rate"]), f32(f["remaining"]), f32(f["age0"])
    mtm0, mi, jud = f32(f["mtm0"]), f32(f["mi_pct"]), f32(f["judicial"])
    w = f32(p.weight)
    si = p.state_idx
    S, T = sc.n, sc.us_ur.shape[1]
    out = {k: np.zeros(S) for k in ("loss", "default_upb", "default_loans", "prepay_upb")}
    m_def, m_loss = np.zeros((S, T)), np.zeros((S, T))
    pd12 = lgd12 = None

    for s0 in range(0, S, chunk):
        s1 = min(s0 + chunk, S)
        alive = np.ones((s1 - s0, len(f)), dtype=np.float32)
        cum_pd = np.zeros_like(alive)
        lgd_acc = np.zeros_like(alive)
        for t in range(T):
            bal_t = _amortised(bal0, note, rem, t + 1)
            ur = sc.st_ur[s0:s1, si, t]
            ur_lag = sc.st_ur_hist12[si, t][None, :] if t < 12 else sc.st_ur[s0:s1, si, t - 12]
            hpi = sc.st_hpi_rel[s0:s1, si, t]
            mtm = mtm0 * (bal_t / np.maximum(bal0, 1)) / hpi
            x = {**static, "loan_age": age0 + t + 1, "mtm_ltv": mtm, "ur": ur,
                 "ur_chg12": ur - ur_lag, "refi_incentive": note - sc.rate[s0:s1, t][:, None]}
            feats = feature_matrix(x)
            h_d, h_p = hd.hazard(feats=feats), hp.hazard(feats=feats)
            tot = h_d + h_p
            scale = np.where(tot > 0.99, 0.99 / tot, 1.0)
            pd_t, pp_t = alive * h_d * scale, alive * h_p * scale
            alive = alive - pd_t - pp_t
            hpi_liq = sc.st_hpi_rel[s0:s1, si, min(t + LIQUIDATION_LAG, T - 1)]
            lgd_t = lgd.predict(mtm * hpi / hpi_liq, mi, jud, ur)
            loss_t = pd_t * bal_t * lgd_t
            out["loss"][s0:s1] += loss_t @ w
            out["default_upb"][s0:s1] += (pd_t * bal_t) @ w
            out["default_loans"][s0:s1] += pd_t @ w
            out["prepay_upb"][s0:s1] += (pp_t * bal_t) @ w
            m_def[s0:s1, t] = pd_t @ w
            m_loss[s0:s1, t] = loss_t @ w
            if keep_loan_detail and t < 12:
                cum_pd += pd_t
                lgd_acc += pd_t * lgd_t
        if keep_loan_detail:
            pd12 = cum_pd[0]
            lgd12 = np.where(cum_pd[0] > 0, lgd_acc[0] / np.maximum(cum_pd[0], 1e-12), lgd.mean_lgd)
    return Projection(out["loss"], out["default_upb"], out["default_loans"], out["prepay_upb"],
                      m_def, m_loss, pd12, lgd12)


# ---- orchestration -----------------------------------------------------------------------

def fit_models(settings: Settings, exclude: tuple[str, str] | None = None) -> tuple[HazardModel, HazardModel, LGDModel]:
    n = settings["hazard"]["panel_sample_loans"]
    panel = load_panel(settings, n, exclude=exclude)
    hd = fit_hazard(panel, "is_default_event")
    hp = fit_hazard(panel, "is_prepay_event")
    lgd = fit_lgd(settings, exclude=exclude)
    log.info("Hazard models on %d loan-months: %d defaults (AUC %.3f), %d prepayments (AUC %.3f); "
             "LGD on %d defaults (mean %.1f%%)", hd.rows, hd.events, hd.auc_in_sample, hp.events,
             hp.auc_in_sample, lgd.n, lgd.mean_lgd * 100)
    return hd, hp, lgd


def resolve_as_of(settings: Settings) -> str:
    as_of = settings["stress"]["as_of"]
    if as_of != "latest":
        return as_of
    with connect(settings, read_only=True) as con:
        return str(con.execute("select max(period_month) from core.fct_loan_monthly").fetchone()[0])[:7]


def run_stress(settings: Settings) -> dict:
    cfg = {**settings["stress"], "as_of": resolve_as_of(settings)}
    hist = load_history(settings)
    hd, hp, lgd = fit_models(settings)
    sc = simulate(settings, hist, cfg["as_of"], cfg["horizon_months"], cfg["n_scenarios"],
                  cfg["seed"], cfg["recession_prob_annual"])
    port = load_portfolio(settings, cfg["as_of"], hist.states, cfg["portfolio_sample"])
    log.info("Projecting %d scenarios x %d loans (representing %d loans, $%.2fbn) over %d months",
             sc.n, len(port.frame), port.total_loans, port.total_upb / 1e9, cfg["horizon_months"])
    proj = project(port, sc, hd, hp, lgd, chunk=cfg["scenario_chunk"])

    end = hist.index_of(cfg["as_of"])
    results = sc.summary(hist.us_ur[end], hist.rate[end])
    results["loss_amount"] = proj.loss
    results["loss_rate"] = proj.loss / port.total_upb
    results["default_rate_upb"] = proj.default_upb / port.total_upb
    results["default_rate_loans"] = proj.default_loans / port.total_loans
    results["prepay_rate_upb"] = proj.prepay_upb / port.total_upb
    results["as_of"] = cfg["as_of"]

    mc = results[results["scenario_type"] == "monte_carlo"]
    losses = mc["loss_rate"].to_numpy()
    var99 = float(np.quantile(losses, 0.99))
    tail = mc[mc["loss_rate"] >= var99]
    summary = {
        "as_of": cfg["as_of"], "horizon_months": cfg["horizon_months"],
        "n_scenarios": int(len(mc)), "portfolio_loans": port.total_loans,
        "portfolio_upb": port.total_upb, "sampled_loans": int(len(port.frame)),
        "expected_loss_rate": float(losses.mean()),
        "median_loss_rate": float(np.median(losses)),
        "p95_loss_rate": float(np.quantile(losses, 0.95)),
        "var99_loss_rate": var99,
        "es99_loss_rate": float(tail["loss_rate"].mean()),
        "expected_loss_amount": float(mc["loss_amount"].mean()),
        "var99_loss_amount": float(np.quantile(mc["loss_amount"], 0.99)),
        "recession_share": float(mc["recession"].mean()),
        "tail_avg_peak_unemployment": float(tail["peak_unemployment"].mean()),
        "tail_avg_hpi_trough": float(tail["hpi_trough_change"].mean()),
        "named": results[results["scenario_type"] == "named"][
            ["scenario_id", "peak_unemployment", "hpi_trough_change", "loss_rate",
             "default_rate_upb"]].to_dict("records"),
        "drivers": _driver_analysis(mc),
        "hazard_default": {"auc": hd.auc_in_sample, "events": hd.events, "rows": hd.rows,
                           "coefficients": hd.coefficients().to_dict("records")},
        "hazard_prepay": {"auc": hp.auc_in_sample, "events": hp.events,
                          "coefficients": hp.coefficients().to_dict("records")},
        "lgd": {"n": lgd.n, "mean": lgd.mean_lgd, "r2": lgd.r2, "intercept": lgd.intercept,
                "coef": list(map(float, lgd.coef))},
    }

    backtest = run_backtest(settings, hist, full_models=(hd, hp, lgd))
    summary["backtest"] = backtest["summary"]

    scores = score_book(settings, hist, hd, hp, lgd, cfg["as_of"])
    summary["book_expected_loss_12m"] = float(scores["expected_loss"].sum())
    summary["book_pd_12m_upb_weighted"] = float(
        (scores["pd_12m"] * scores["ead"]).sum() / scores["ead"].sum())

    _write_outputs(settings, results, summary, sc, hist, end, backtest)
    from loanlens.reporting.stress_report import write_stress_report

    write_stress_report(settings, results, summary, sc, backtest)
    return summary


def _driver_analysis(mc: pd.DataFrame) -> dict:
    X = np.column_stack([np.ones(len(mc)), mc["unemployment_rise"], mc["hpi_trough_change"],
                         mc["mortgage_rate_change_end"]])
    y = mc["loss_rate"].to_numpy()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    r2 = 1 - ((y - X @ beta) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    corr = {c: float(np.corrcoef(mc[c], y)[0, 1]) for c in
            ("unemployment_rise", "hpi_trough_change", "mortgage_rate_change_end")}
    deciles = mc.assign(decile=pd.qcut(mc["loss_rate"].rank(method="first"), 10, labels=False) + 1)
    by_decile = deciles.groupby("decile").agg(
        loss_rate=("loss_rate", "mean"), peak_unemployment=("peak_unemployment", "mean"),
        hpi_trough_change=("hpi_trough_change", "mean"), recession_share=("recession", "mean"),
    ).reset_index()
    return {"r2": float(r2), "coef_per_pt_unemployment": float(beta[1]),
            "coef_per_10pct_hpi_decline": float(-beta[2] * 0.10),
            "correlations": corr, "by_decile": by_decile.to_dict("records")}


def score_book(settings: Settings, hist: MacroHistory, hd, hp, lgd, as_of: str) -> pd.DataFrame:
    """Baseline-scenario 12-month PD, LGD, EAD and expected loss for every performing loan."""
    port = load_portfolio(settings, as_of, hist.states, sample=None)
    port.weight = np.ones(len(port.frame))
    base = simulate(settings, hist, as_of, 12, 1, 0, 0.0, include_named=True)
    idx = base.names.index("Baseline")
    base = ScenarioSet([base.names[idx]], ["named"], base.us_ur[idx:idx + 1],
                       base.us_hpi_rel[idx:idx + 1], base.rate[idx:idx + 1],
                       base.st_ur[idx:idx + 1], base.st_hpi_rel[idx:idx + 1], base.st_ur_hist12,
                       base.recession[idx:idx + 1])
    proj = project(port, base, hd, hp, lgd, chunk=1, keep_loan_detail=True)
    f = port.frame
    scores = pd.DataFrame({
        "loan_id": f["loan_id"], "as_of_month": pd.Timestamp(as_of + "-01"),
        "pd_12m": proj.loan_pd_12m.astype(float), "lgd": proj.loan_lgd_12m.astype(float),
        "ead": f["bal0"].astype(float),
    })
    scores["expected_loss"] = scores["pd_12m"] * scores["lgd"] * scores["ead"]
    scores["risk_grade"] = pd.cut(scores["pd_12m"], [-1, 0.001, 0.0025, 0.005, 0.01, 0.025, 2],
                                  labels=["A", "B", "C", "D", "E", "F"]).astype(str)
    with connect(settings, read_only=True) as con:
        try:
            orig = con.execute("""select loan_id, pd_xgb as pd_24m_origination, risk_driver_1,
                                  risk_driver_2, risk_driver_3 from ml.pd_origination_scores""").df()
            scores = scores.merge(orig, on="loan_id", how="left")
        except Exception:  # PD model not trained yet
            pass
    write_table(settings, "ml.loan_scores", scores)
    return scores


def run_backtest(settings: Settings, hist: MacroHistory,
                 full_models: tuple[HazardModel, HazardModel, LGDModel] | None = None) -> dict:
    """Project the 2007-12 book through the realised 2008-2010 macro path and compare with
    what actually happened.

    * out-of-sample: models refitted with every 2007-2010 observation removed (a forecast);
    * in-sample: the production models fitted on all history (a specification check);
    * naive: a flat through-the-cycle monthly default rate from pre-2007 history.
    """
    cfg = settings["backtest"]
    window = (cfg["exclude_start"], cfg["exclude_end"])
    hd, hp, lgd = fit_models(settings, exclude=window)
    port = load_portfolio(settings, cfg["as_of"], hist.states, sample=None)
    port.weight = np.ones(len(port.frame))
    T = cfg["horizon_months"]
    path = realised_path(hist, cfg["as_of"], T)
    proj = project(port, path, hd, hp, lgd, chunk=1)
    full = project(port, path, *(full_models or fit_models(settings)), chunk=1)

    start = pd.Timestamp(cfg["as_of"] + "-01") + pd.DateOffset(months=1)
    end = start + pd.DateOffset(months=T - 1)
    ids = pd.DataFrame({"loan_id": port.frame["loan_id"]})
    with connect(settings, read_only=True) as con:
        con.register("ids", ids)
        actual = con.execute(f"""
            select f.period_month, count(*) as defaults, sum(f.exposure_upb) as default_upb,
                   sum(greatest(l.net_loss, 0)) as eventual_loss
            from core.fct_loan_monthly f join ids using (loan_id) join core.dim_loan l using (loan_id)
            where f.is_default_event
              and f.period_month between date '{start.date()}' and date '{end.date()}'
            group by 1 order by 1
        """).df()
        # Naive benchmark: through-the-cycle monthly default hazard from pre-window history.
        ttc = con.execute(f"""
            select avg(case when f.is_default_event then 1.0 else 0.0 end) as h
            from core.fct_loan_monthly f join core.dim_loan l using (loan_id)
            where f.period_month < date '{window[0]}-01'
              and (l.first_default_month is null or f.period_month <= l.first_default_month)
        """).fetchone()[0]
    months = pd.date_range(start, periods=T, freq="MS")
    monthly = pd.DataFrame({"month": months, "projected_defaults": proj.monthly_default_loans[0],
                            "projected_defaults_in_sample": full.monthly_default_loans[0]})
    monthly = monthly.merge(actual.rename(columns={"period_month": "month"}), on="month", how="left").fillna(0)
    alive = np.cumprod(np.full(T, 1 - ttc))
    monthly["naive_defaults"] = port.total_loans * np.r_[1, alive[:-1]] * ttc

    n = port.total_loans
    summary = {
        "as_of": cfg["as_of"], "horizon_months": T, "window_excluded": list(window),
        "loans": n, "upb": port.total_upb,
        "actual_default_rate": float(monthly["defaults"].sum() / n),
        "projected_default_rate": float(proj.default_loans[0] / n),
        "naive_default_rate": float(monthly["naive_defaults"].sum() / n),
        "actual_default_rate_upb": float(monthly["default_upb"].sum() / port.total_upb),
        "projected_default_rate_upb": float(proj.default_upb[0] / port.total_upb),
        "actual_loss_rate": float(monthly["eventual_loss"].sum() / port.total_upb),
        "projected_loss_rate": float(proj.loss[0] / port.total_upb),
        "in_sample_default_rate": float(full.default_loans[0] / n),
        "in_sample_loss_rate": float(full.loss[0] / port.total_upb),
        "hazard_auc_in_sample": hd.auc_in_sample,
    }
    summary["default_rate_error"] = summary["projected_default_rate"] / summary["actual_default_rate"] - 1
    summary["loss_rate_error"] = summary["projected_loss_rate"] / max(summary["actual_loss_rate"], 1e-12) - 1
    summary["naive_error"] = summary["naive_default_rate"] / summary["actual_default_rate"] - 1
    summary["in_sample_default_error"] = summary["in_sample_default_rate"] / summary["actual_default_rate"] - 1
    summary["in_sample_loss_error"] = summary["in_sample_loss_rate"] / max(summary["actual_loss_rate"], 1e-12) - 1
    return {"summary": summary, "monthly": monthly}


def _write_outputs(settings, results, summary, sc: ScenarioSet, hist, end, backtest) -> None:
    write_table(settings, "ml.stress_scenarios", results)
    flat = {k: v for k, v in summary.items() if isinstance(v, int | float | str)}
    write_table(settings, "ml.stress_summary",
                pd.DataFrame({"metric": list(flat), "value": [str(v) for v in flat.values()]}))
    # Paths for the dashboard: named scenarios + Monte Carlo percentile bands.
    months = pd.date_range(hist.months[end] + pd.DateOffset(months=1), periods=sc.us_ur.shape[1], freq="MS")
    rows = []
    mc = np.array([k == "monte_carlo" for k in sc.kind])
    for name, series in (("unemployment_rate", sc.us_ur), ("hpi_relative", sc.us_hpi_rel),
                         ("mortgage_rate", sc.rate)):
        for q in (5, 50, 95):
            rows.append(pd.DataFrame({"month": months, "measure": name, "path": f"Monte Carlo p{q}",
                                      "value": np.percentile(series[mc], q, axis=0)}))
        for i in np.flatnonzero(~mc):
            rows.append(pd.DataFrame({"month": months, "measure": name, "path": sc.names[i],
                                      "value": series[i]}))
    write_table(settings, "ml.stress_paths", pd.concat(rows, ignore_index=True))
    write_table(settings, "ml.backtest_monthly", backtest["monthly"])
    out = settings.artifacts_dir / "models" / "stress_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=float))

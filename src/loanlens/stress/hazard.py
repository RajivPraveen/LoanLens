"""Macro-conditional monthly hazard models (discrete-time logistic) and the LGD model.

For every not-yet-defaulted loan-month the default model estimates P(credit event this
month) and the prepayment model P(voluntary payoff this month) from borrower attributes,
loan age, mark-to-market LTV, state unemployment (level and 12-month change), the refinance
incentive, vintage cohort and stress interactions. COVID-forbearance months (2020-03 to
2021-12) are excluded from model development: payment relief broke the usual link between
unemployment and default, and a +10-point unemployment spike with almost no defaults would
otherwise teach the model that rising unemployment is harmless. Logistic regression is the standard
choice for regulatory stress models: every coefficient is inspectable and the response to
scenarios outside the historical range stays smooth and monotone.

The same ``feature_matrix`` function is used for fitting (pandas rows) and for projection
(numpy arrays broadcast over scenarios x loans), so the two can never drift apart.
Coefficients are converted back to raw feature units after fitting on standardised data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import roc_auc_score

from loanlens.config import Settings
from loanlens.warehouse import connect

FEATURES = [
    "fico_c", "fico_low", "dti_c", "cltv_gap", "investor", "cash_out", "single_borrower",
    "short_term", "log_upb_c", "age_early", "age_log", "mtm", "mtm_over80", "mtm_over100",
    "ur", "ur_up", "ur_down", "refi", "refi_pos",
    "vint_pre2004", "vint_2004", "vint_2005_2007", "vint_2008", "underwater_x_ur",
    "equity_stress_x_ur_up",
]
LIQUIDATION_LAG = 18  # months from default to property sale, typical of the data
FORBEARANCE_START, FORBEARANCE_END = pd.Timestamp("2020-03-01"), pd.Timestamp("2021-12-01")


def feature_matrix(x: dict) -> list:
    """Hazard features from raw inputs. Works on pandas Series or broadcastable arrays.

    Required keys: credit_score, orig_dti, orig_cltv, orig_ltv, investor, cash_out,
    single_borrower, short_term, orig_upb, vintage_year, loan_age, mtm_ltv, ur, ur_chg12,
    refi_incentive.
    """
    fico = x["credit_score"]
    mtm = np.clip(x["mtm_ltv"] / 100.0, 0.05, 2.5)
    age = np.maximum(x["loan_age"], 1)
    inc = x["refi_incentive"]
    vy = x["vintage_year"]
    ur_up = np.clip(x["ur_chg12"], 0, 8)
    return [
        (fico - 740) / 100.0,
        np.maximum(680 - fico, 0) / 100.0,
        (x["orig_dti"] - 35) / 10.0,
        (x["orig_cltv"] - x["orig_ltv"]) / 10.0,
        x["investor"],
        x["cash_out"],
        x["single_borrower"],
        x["short_term"],
        np.log(x["orig_upb"] / 200_000.0),
        np.exp(-age / 8.0),
        np.log(age),
        mtm,
        np.maximum(mtm - 0.8, 0),
        np.maximum(mtm - 1.0, 0),
        x["ur"],
        np.clip(x["ur_chg12"], 0, 8),
        np.clip(x["ur_chg12"], -8, 0),
        np.clip(inc, -3, 3),
        np.clip(inc, 0, 3),
        # vintage cohorts: underwriting quality not captured by the reported attributes
        (vy < 2004) * 1.0,
        (vy == 2004) * 1.0,
        ((vy >= 2005) & (vy <= 2007)) * 1.0,
        (vy == 2008) * 1.0,
        # stress interactions: being underwater hurts more when jobs are scarce
        np.maximum(mtm - 1.0, 0) * (x["ur"] - 5.0),
        np.maximum(mtm - 0.8, 0) * ur_up,
    ]


@dataclass
class HazardModel:
    intercept: float
    coef: np.ndarray        # raw-unit coefficients aligned with FEATURES
    auc_in_sample: float
    events: int
    rows: int

    def linear_predictor(self, x: dict | None = None, feats: list | None = None):
        """Pass raw inputs ``x`` or precomputed ``feature_matrix`` output ``feats``.

        Low-dimensional (per-loan) terms are summed first, then the full scenario x loan
        terms are accumulated in place - much faster than broadcasting every feature.
        """
        feats = feature_matrix(x) if feats is None else feats
        small = self.intercept
        big = None
        for c, f in zip(self.coef, feats, strict=True):
            if np.ndim(f) < 2:
                small = small + c * f
            elif big is None:
                big = c * f
            else:
                big += c * f
        return small if big is None else big + small

    def hazard(self, x: dict | None = None, feats: list | None = None):
        eta = self.linear_predictor(x, feats)
        return 1.0 / (1.0 + np.exp(-eta))

    def coefficients(self) -> pd.DataFrame:
        return pd.DataFrame({"feature": FEATURES, "coefficient": self.coef})


@dataclass
class LGDModel:
    intercept: float
    coef: np.ndarray
    r2: float
    mean_lgd: float
    n: int

    def predict(self, liq_ltv, mi_pct, judicial, ur):
        """``liq_ltv``: mark-to-market LTV re-priced to the expected liquidation date
        (``LIQUIDATION_LAG`` months after default) using the scenario's house-price path."""
        mtm = np.clip(liq_ltv / 100.0, 0.05, 2.5)
        pred = (self.intercept + self.coef[0] * mtm + self.coef[1] * np.maximum(mtm - 0.8, 0)
                + self.coef[2] * mi_pct / 100.0 + self.coef[3] * judicial
                + self.coef[4] * np.maximum(ur - 5.0, 0))
        return np.clip(pred, 0.0, 1.0)


# ---- data -------------------------------------------------------------------------------

STATIC_SQL = """
    l.credit_score, l.orig_dti, l.orig_cltv, l.orig_ltv, l.orig_upb, l.mi_pct,
    l.occupancy = 'Investment'                    as investor,
    l.loan_purpose = 'Cash-out refinance'         as cash_out,
    coalesce(l.num_borrowers, 2) = 1              as single_borrower,
    l.orig_loan_term <= 180                       as short_term,
    l.judicial_foreclosure                        as judicial,
    l.state_code, l.orig_interest_rate, l.orig_loan_term, l.first_payment_month,
    l.vintage_year
"""


def clean_static(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["credit_score"] = df["credit_score"].astype(float).fillna(740)
    df["orig_dti"] = df["orig_dti"].astype(float).fillna(35)
    df["orig_ltv"] = df["orig_ltv"].astype(float).fillna(75)
    df["orig_cltv"] = df["orig_cltv"].astype(float).fillna(df["orig_ltv"])
    df["mi_pct"] = df["mi_pct"].astype(float).fillna(0)
    df["vintage_year"] = df["vintage_year"].astype(float)
    for col in ("investor", "cash_out", "single_borrower", "short_term", "judicial"):
        df[col] = df[col].astype(float).fillna(0)
    return df


def load_panel(settings: Settings, n_loans: int, seed: int = 11,
               exclude: tuple[str, str] | None = None) -> pd.DataFrame:
    """Loan-month panel of not-yet-defaulted months for a random sample of loans."""
    with connect(settings, read_only=True) as con:
        all_ids = con.execute("select loan_id from core.dim_loan order by loan_id").df()["loan_id"]
        rng = np.random.default_rng(seed)
        ids = pd.DataFrame({"loan_id": rng.choice(all_ids, min(n_loans, len(all_ids)), replace=False)})
        con.register("ids", ids)
        panel = con.execute(f"""
            select f.loan_id, f.period_month, f.loan_age, f.mtm_ltv, f.refi_incentive,
                   m.state_unemployment_rate as ur, m.state_ur_change_12m as ur_chg12,
                   f.is_default_event,
                   f.is_prepaid and l.termination_type = 'Voluntary payoff' as is_prepay_event,
                   {STATIC_SQL}
            from core.fct_loan_monthly f
            join ids using (loan_id)
            join core.dim_loan l using (loan_id)
            left join core.dim_macro m using (macro_key)
            where (l.first_default_month is null or f.period_month <= l.first_default_month)
        """).df()
    panel = clean_static(panel.sort_values(["loan_id", "period_month"], ignore_index=True))
    panel = panel[~panel["period_month"].between(FORBEARANCE_START, FORBEARANCE_END)]
    panel = panel.dropna(subset=["mtm_ltv", "ur", "ur_chg12", "refi_incentive"])
    if exclude:
        lo, hi = pd.Timestamp(exclude[0] + "-01"), pd.Timestamp(exclude[1] + "-01")
        panel = panel[~panel["period_month"].between(lo, hi)]
    return panel


def fit_hazard(panel: pd.DataFrame, event_col: str, negative_share: float = 0.3,
               seed: int = 0) -> HazardModel:
    """Logistic hazard on event rows + a random share of non-event rows (weighted back)."""
    rng = np.random.default_rng(seed)
    y = panel[event_col].astype(bool).to_numpy()
    keep = y | (rng.random(len(y)) < negative_share)
    data = panel[keep]
    yk = y[keep].astype(int)
    weights = np.where(yk == 1, 1.0, 1.0 / negative_share)
    X = np.column_stack(feature_matrix({k: data[k].to_numpy(dtype=float) for k in _INPUTS}))
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    lr = LogisticRegression(C=1.0, max_iter=3000).fit(Z, yk, sample_weight=weights)
    coef = lr.coef_[0] / sd
    intercept = float(lr.intercept_[0] - np.sum(lr.coef_[0] * mu / sd))
    auc = float(roc_auc_score(yk, lr.decision_function(Z), sample_weight=weights))
    return HazardModel(intercept, coef, auc, int(yk.sum()), int(len(panel)))


_INPUTS = ["credit_score", "orig_dti", "orig_cltv", "orig_ltv", "investor", "cash_out",
           "single_borrower", "short_term", "orig_upb", "loan_age", "mtm_ltv", "ur",
           "ur_chg12", "refi_incentive", "vintage_year"]


def fit_lgd(settings: Settings, exclude: tuple[str, str] | None = None) -> LGDModel:
    """LGD per default event = eventual net loss / balance at default (cures give 0).

    Only defaults with at least 36 months to resolve before the data cut-off are used;
    ``exclude`` drops defaults inside a (backtest) window.
    """
    cutoff_clause = (f"and f.period_month not between date '{exclude[0]}-01' and date '{exclude[1]}-01'"
                     if exclude else "")
    with connect(settings, read_only=True) as con:
        df = con.execute(f"""
            select f.mtm_ltv * m.state_hpi / nullif(fwd.state_hpi, 0) as liq_ltv,
                   coalesce(l.mi_pct, 0) as mi_pct,
                   l.judicial_foreclosure::double as judicial,
                   m.state_unemployment_rate as ur,
                   greatest(l.net_loss, 0) / nullif(f.exposure_upb, 0) as lgd
            from core.fct_loan_monthly f
            join core.dim_loan l using (loan_id)
            left join core.dim_macro m using (macro_key)
            left join core.dim_macro fwd
                   on fwd.state_code = l.state_code
                  and fwd.month = f.period_month + interval {LIQUIDATION_LAG} month
            where f.is_default_event
              and f.period_month <= l.data_end_month - interval 36 month
              and f.exposure_upb > 0
              {cutoff_clause}
        """).df().dropna()
    df["lgd"] = df["lgd"].clip(0, 1.2)
    mtm = np.clip(df["liq_ltv"].to_numpy() / 100.0, 0.05, 2.5)
    X = np.column_stack([mtm, np.maximum(mtm - 0.8, 0), df["mi_pct"] / 100.0, df["judicial"],
                         np.maximum(df["ur"] - 5.0, 0)])
    reg = LinearRegression().fit(X, df["lgd"])
    return LGDModel(float(reg.intercept_), reg.coef_, float(reg.score(X, df["lgd"])),
                    float(df["lgd"].mean()), int(len(df)))

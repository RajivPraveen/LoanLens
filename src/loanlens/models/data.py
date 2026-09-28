"""Model-ready loan frame built from the warehouse star schema (core.dim_loan)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from loanlens.config import Settings
from loanlens.warehouse import query

NUMERIC_FEATURES = [
    "credit_score", "orig_ltv", "orig_cltv", "orig_dti", "orig_interest_rate",
    "rate_spread_at_orig", "log_orig_upb", "orig_loan_term", "num_borrowers", "num_units",
    "mi_pct", "state_ur_at_orig", "state_ur_change_12m_at_orig", "state_hpi_yoy_at_orig",
    "first_time_homebuyer", "super_conforming",
]
CATEGORICAL_FEATURES = ["loan_purpose", "occupancy", "channel", "property_type"]
# State/region identity is deliberately NOT a feature (fair-lending proxy risk); local
# economic conditions enter through state-level unemployment and house-price growth.

# Monotone constraints for the gradient-boosted model (+1: risk rises with the feature).
MONOTONE = {"credit_score": -1, "orig_ltv": 1, "orig_cltv": 1, "orig_dti": 1}

FEATURE_LABELS = {
    "credit_score": "Credit score", "orig_ltv": "LTV", "orig_cltv": "Combined LTV",
    "orig_dti": "Debt-to-income", "orig_interest_rate": "Note rate",
    "rate_spread_at_orig": "Rate spread vs market", "log_orig_upb": "Loan size (log)",
    "orig_loan_term": "Loan term", "num_borrowers": "Number of borrowers",
    "num_units": "Number of units", "mi_pct": "MI coverage",
    "state_ur_at_orig": "State unemployment at origination",
    "state_ur_change_12m_at_orig": "State unemployment 12m change",
    "state_hpi_yoy_at_orig": "State house-price growth", "first_time_homebuyer": "First-time buyer",
    "super_conforming": "Super-conforming", "loan_purpose": "Loan purpose",
    "occupancy": "Occupancy", "channel": "Channel", "property_type": "Property type",
}


def load_loans(settings: Settings) -> pd.DataFrame:
    df = query(settings, """
        select loan_id, vintage_year, vintage, first_payment_month, state_code, census_region,
               credit_score, fico_band, orig_ltv, ltv_band, orig_cltv, orig_dti, dti_band,
               orig_interest_rate, rate_spread_at_orig, orig_upb, orig_loan_term,
               num_borrowers, num_units, mi_pct, state_ur_at_orig, state_ur_change_12m_at_orig,
               state_hpi_yoy_at_orig, is_first_time_homebuyer, is_super_conforming,
               loan_purpose, occupancy, channel, property_type,
               default_in_window, window_fully_observed, months_to_default, first_default_month,
               termination_type, months_to_termination, months_observable, is_liquidated,
               liquidation_upb, net_loss, loss_severity
        from core.dim_loan
        order by loan_id  -- row order feeds XGBoost subsampling and every seeded sample
    """)
    df["log_orig_upb"] = np.log(df["orig_upb"])
    df["first_time_homebuyer"] = df["is_first_time_homebuyer"].map({True: 1.0, False: 0.0})
    df["super_conforming"] = df["is_super_conforming"].astype(float)
    df["mi_pct"] = df["mi_pct"].fillna(0.0)
    for col in NUMERIC_FEATURES:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    df["default_in_window"] = df["default_in_window"].astype(bool)
    return df


def time_split(df: pd.DataFrame, modeling_cfg: dict) -> dict[str, pd.DataFrame]:
    """Out-of-time split by origination year; only loans whose label window is observed."""
    observed = df[df["window_fully_observed"]]
    out = {}
    for name in ("train", "calibration", "test"):
        lo, hi = modeling_cfg[f"{name}_years"]
        out[name] = observed[observed["vintage_year"].between(lo, hi)].copy()
    return out

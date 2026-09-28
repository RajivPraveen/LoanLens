"""Origination probability-of-default (PD) model.

Question answered: at application time, how likely is this loan to become seriously
delinquent (90+ days, outside COVID forbearance, or a credit-event termination) within the
first 24 months after its first payment?

Design:
- Out-of-time split by vintage: train on older loans, recalibrate on the next vintages,
  evaluate on later, untouched vintages. No random splits, so no look-ahead leakage.
- Logistic regression is the interpretable baseline; XGBoost (with monotone constraints on
  credit score, LTV, CLTV and DTI) is the challenger. Every metric is reported for both.
- Logistic recalibration (Platt scaling on the logit) maps raw scores to probabilities that
  match observed default rates in the most recent calibration vintages. It is strictly
  monotone, so ranking metrics are unchanged.
- TreeSHAP explains global drivers and gives each loan its top three risk reasons.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
    roc_curve,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

from loanlens.config import Settings
from loanlens.models.data import (
    CATEGORICAL_FEATURES,
    FEATURE_LABELS,
    MONOTONE,
    NUMERIC_FEATURES,
    load_loans,
    time_split,
)
from loanlens.warehouse import write_table

log = logging.getLogger(__name__)
TARGET = "default_in_window"


# ---- building blocks -------------------------------------------------------------------

def _encoder() -> OneHotEncoder:
    return OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=0.005,
                         sparse_output=False)


def make_lr() -> Pipeline:
    prep = ColumnTransformer([
        ("num", Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True)),
                          ("scale", StandardScaler())]), NUMERIC_FEATURES),
        ("cat", _encoder(), CATEGORICAL_FEATURES),
    ], verbose_feature_names_out=False)
    return Pipeline([("prep", prep),
                     ("model", LogisticRegression(C=0.5, max_iter=2000))])


def make_xgb_prep() -> ColumnTransformer:
    prep = ColumnTransformer([
        ("num", "passthrough", NUMERIC_FEATURES),
        ("cat", _encoder(), CATEGORICAL_FEATURES),
    ], verbose_feature_names_out=False)
    return prep.set_output(transform="pandas")


class Recalibrator:
    """Platt scaling on the logit of a raw score: p = sigmoid(a + b * logit(raw))."""

    def fit(self, raw: np.ndarray, y: np.ndarray) -> Recalibrator:
        self.lr = LogisticRegression(C=1e6, max_iter=1000).fit(_logit(raw)[:, None], y)
        return self

    def transform(self, raw: np.ndarray) -> np.ndarray:
        return self.lr.predict_proba(_logit(raw)[:, None])[:, 1]


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


@dataclass
class PDModel:
    lr: Pipeline
    xgb_prep: ColumnTransformer
    xgb: XGBClassifier
    lr_calibrator: Recalibrator
    xgb_calibrator: Recalibrator
    window_months: int
    trained_at: str
    split_years: dict

    def raw_scores(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        lr = self.lr.predict_proba(df[NUMERIC_FEATURES + CATEGORICAL_FEATURES])[:, 1]
        xgb = self.xgb.predict_proba(self.xgb_prep.transform(df))[:, 1]
        return lr, xgb

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        lr, xgb = self.raw_scores(df)
        return pd.DataFrame({
            "pd_lr": self.lr_calibrator.transform(lr),
            "pd_xgb": self.xgb_calibrator.transform(xgb),
            "pd_xgb_raw": xgb,
        }, index=df.index)

    def reason_codes(self, df: pd.DataFrame, top: int = 3) -> pd.DataFrame:
        """Top risk-increasing drivers per loan from native TreeSHAP contributions."""
        X = self.xgb_prep.transform(df)
        import xgboost as xgb_lib

        contrib = self.xgb.get_booster().predict(xgb_lib.DMatrix(X), pred_contribs=True)[:, :-1]
        grouped = _group_contributions(pd.DataFrame(contrib, columns=X.columns, index=df.index))
        labels = np.array([FEATURE_LABELS.get(c, c) for c in grouped.columns])
        order = np.argsort(-grouped.to_numpy(), axis=1)[:, :top]
        values = np.take_along_axis(grouped.to_numpy(), order, axis=1)
        out = pd.DataFrame(index=df.index)
        for k in range(top):
            out[f"risk_driver_{k + 1}"] = np.where(values[:, k] > 0, labels[order[:, k]], None)
        return out


def _base_feature(col: str) -> str:
    for cat in CATEGORICAL_FEATURES:
        if col.startswith(cat + "_"):
            return cat
    return col


def _group_contributions(contrib: pd.DataFrame) -> pd.DataFrame:
    """Sum one-hot SHAP columns back to their source feature."""
    return contrib.T.groupby(_base_feature).sum().T


# ---- metrics -----------------------------------------------------------------------------

def ks_statistic(y, p) -> float:
    fpr, tpr, _ = roc_curve(y, p)
    return float(np.max(tpr - fpr))


def expected_calibration_error(y, p, bins: int = 10) -> float:
    edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    ece = 0.0
    for b in range(len(edges) - 1):
        mask = idx == b
        if mask.any():
            ece += mask.mean() * abs(p[mask].mean() - y[mask].mean())
    return float(ece)


def evaluate(y: np.ndarray, p: np.ndarray, top_share: float = 0.10) -> dict:
    y = np.asarray(y).astype(int)
    cutoff = np.quantile(p, 1 - top_share)
    flagged = p >= cutoff
    tp = (flagged & (y == 1)).sum()
    return {
        "n": int(len(y)),
        "defaults": int(y.sum()),
        "base_rate": float(y.mean()),
        "mean_pd": float(p.mean()),
        "auc": float(roc_auc_score(y, p)),
        "pr_auc": float(average_precision_score(y, p)),
        "ks": ks_statistic(y, p),
        "brier": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))),
        "ece": expected_calibration_error(y, p),
        f"precision_top{int(top_share * 100)}": float(tp / max(flagged.sum(), 1)),
        f"recall_top{int(top_share * 100)}": float(tp / max(y.sum(), 1)),
        "lift_top10": float((tp / max(flagged.sum(), 1)) / max(y.mean(), 1e-12)),
    }


def bootstrap_auc(y, p, n_boot: int = 200, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    y, p = np.asarray(y), np.asarray(p)
    aucs = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(y), len(y))
        if y[idx].min() != y[idx].max():
            aucs.append(roc_auc_score(y[idx], p[idx]))
    return float(np.percentile(aucs, 2.5)), float(np.percentile(aucs, 97.5))


def calibration_table(y, p, bins: int = 10) -> pd.DataFrame:
    df = pd.DataFrame({"y": np.asarray(y).astype(int), "p": p})
    df["bin"] = pd.qcut(df["p"].rank(method="first"), bins, labels=False)
    return df.groupby("bin").agg(mean_pd=("p", "mean"), observed=("y", "mean"),
                                 n=("y", "size"), defaults=("y", "sum")).reset_index()


# ---- training ----------------------------------------------------------------------------

def train(settings: Settings) -> dict:
    cfg = settings["modeling"]
    loans = load_loans(settings)
    split = time_split(loans, cfg)
    train_df, cal_df, test_df = split["train"], split["calibration"], split["test"]
    for name, part in split.items():
        log.info("%s: %d loans, default rate %.2f%%", name, len(part), part[TARGET].mean() * 100)
        if part[TARGET].sum() < 5:
            raise ValueError(f"Too few defaults in the {name} split to fit/evaluate the model")

    X_cols = NUMERIC_FEATURES + CATEGORICAL_FEATURES
    y_train = train_df[TARGET].astype(int).to_numpy()

    lr = make_lr().fit(train_df[X_cols], y_train)

    # XGBoost: early stopping on the latest training vintage, then refit on all training years.
    last_year = train_df["vintage_year"].max()
    fit_part, es_part = train_df[train_df["vintage_year"] < last_year], train_df[train_df["vintage_year"] == last_year]
    prep = make_xgb_prep().fit(train_df)
    feature_names = list(prep.get_feature_names_out())
    monotone = {f: MONOTONE[f] for f in feature_names if f in MONOTONE}
    xgb_params = {k: v for k, v in cfg["xgb"].items() if k != "early_stopping_rounds"}
    probe = XGBClassifier(**xgb_params, monotone_constraints=monotone, eval_metric="logloss",
                          early_stopping_rounds=cfg["xgb"]["early_stopping_rounds"],
                          tree_method="hist", n_jobs=-1, random_state=0)
    probe.fit(prep.transform(fit_part), fit_part[TARGET].astype(int),
              eval_set=[(prep.transform(es_part), es_part[TARGET].astype(int))], verbose=False)
    best_n = max(int(probe.best_iteration) + 1, 25)
    xgb_params["n_estimators"] = best_n
    xgb = XGBClassifier(**xgb_params, monotone_constraints=monotone, eval_metric="logloss",
                        tree_method="hist", n_jobs=-1, random_state=0)
    xgb.fit(prep.transform(train_df), y_train, verbose=False)
    log.info("XGBoost: %d trees (early-stopped on %d vintage)", best_n, last_year)

    lr_raw_cal = lr.predict_proba(cal_df[X_cols])[:, 1]
    xgb_raw_cal = xgb.predict_proba(prep.transform(cal_df))[:, 1]
    y_cal = cal_df[TARGET].astype(int).to_numpy()
    model = PDModel(
        lr=lr, xgb_prep=prep, xgb=xgb,
        lr_calibrator=Recalibrator().fit(lr_raw_cal, y_cal),
        xgb_calibrator=Recalibrator().fit(xgb_raw_cal, y_cal),
        window_months=cfg["default_window_months"],
        trained_at=datetime.now().isoformat(timespec="seconds"),
        split_years={k: cfg[f"{k}_years"] for k in ("train", "calibration", "test")},
    )

    # ---- evaluation on out-of-time test vintages
    test_pred = model.predict(test_df)
    y_test = test_df[TARGET].astype(int).to_numpy()
    lr_raw_test, _ = model.raw_scores(test_df)
    metrics = {
        "window_months": model.window_months,
        "split": {name: {"years": cfg[f"{name}_years"], "loans": int(len(part)),
                         "defaults": int(part[TARGET].sum()),
                         "default_rate": float(part[TARGET].mean())}
                  for name, part in split.items()},
        "xgb_trees": best_n,
        "test": {
            "logistic_regression": evaluate(y_test, test_pred["pd_lr"].to_numpy()),
            "xgboost": evaluate(y_test, test_pred["pd_xgb"].to_numpy()),
            "xgboost_uncalibrated": evaluate(y_test, test_pred["pd_xgb_raw"].to_numpy()),
            "logistic_regression_uncalibrated": evaluate(y_test, lr_raw_test),
        },
        "auc_ci95": {
            "logistic_regression": bootstrap_auc(y_test, test_pred["pd_lr"].to_numpy()),
            "xgboost": bootstrap_auc(y_test, test_pred["pd_xgb"].to_numpy()),
        },
    }
    by_year = []
    for year, g in test_df.assign(**test_pred).groupby("vintage_year"):
        if g[TARGET].nunique() == 2:
            by_year.append({"vintage_year": int(year), "loans": len(g),
                            "default_rate": float(g[TARGET].mean()),
                            "mean_pd_xgb": float(g["pd_xgb"].mean()),
                            "auc_lr": float(roc_auc_score(g[TARGET], g["pd_lr"])),
                            "auc_xgb": float(roc_auc_score(g[TARGET], g["pd_xgb"]))})
    metrics["test_by_vintage"] = by_year
    metrics["lr_coefficients"] = _lr_coefficients(lr)

    # ---- persist
    out_dir = settings.artifacts_dir / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out_dir / "pd_model.joblib")
    (out_dir / "pd_metrics.json").write_text(json.dumps(metrics, indent=2))

    keep = ["loan_id", "vintage_year", "state_code", "census_region", "fico_band", "ltv_band",
            "occupancy", "loan_purpose", "num_borrowers", "is_first_time_homebuyer",
            "orig_upb", "orig_ltv", "mi_pct", "credit_score", TARGET, "is_liquidated",
            "loss_severity", "months_to_default"]
    test_out = pd.concat([test_df[keep], test_pred, model.reason_codes(test_df)], axis=1)
    test_out.to_parquet(out_dir / "pd_test_predictions.parquet", index=False)

    # Score every loan (origination PD) for reporting.
    all_pred = model.predict(loans)
    scores = pd.concat([loans[["loan_id", "vintage_year"]], all_pred,
                        model.reason_codes(loans)], axis=1)
    scores["split"] = np.select(
        [loans["vintage_year"].between(*cfg["train_years"]),
         loans["vintage_year"].between(*cfg["calibration_years"]),
         loans["vintage_year"].between(*cfg["test_years"])],
        ["train", "calibration", "test"], "out_of_sample")
    write_table(settings, "ml.pd_origination_scores", scores)

    shap_summary = _shap_analysis(settings, model, test_df)
    metrics["shap_global"] = shap_summary
    (out_dir / "pd_metrics.json").write_text(json.dumps(metrics, indent=2))

    from loanlens.reporting.model_report import write_model_report

    write_model_report(settings, metrics, test_out)
    t = metrics["test"]
    log.info("Test AUC: XGBoost %.3f vs logistic %.3f", t["xgboost"]["auc"], t["logistic_regression"]["auc"])
    return metrics


def _lr_coefficients(lr: Pipeline) -> list[dict]:
    names = lr.named_steps["prep"].get_feature_names_out()
    coefs = lr.named_steps["model"].coef_[0]
    rows = [{"feature": n, "coefficient": float(c), "odds_ratio_per_sd": float(np.exp(c))}
            for n, c in zip(names, coefs, strict=True)]
    return sorted(rows, key=lambda r: -abs(r["coefficient"]))


def _shap_analysis(settings: Settings, model: PDModel, test_df: pd.DataFrame) -> list[dict]:
    import shap

    from loanlens import viz

    n = min(settings["modeling"]["shap_sample"], len(test_df))
    sample = test_df.sample(n, random_state=0)
    X = model.xgb_prep.transform(sample)
    explainer = shap.TreeExplainer(model.xgb)
    values = explainer.shap_values(X)
    grouped = _group_contributions(pd.DataFrame(values, columns=X.columns, index=sample.index))
    importance = grouped.abs().mean().sort_values(ascending=False)

    fig_dir = settings.figures_dir
    top = importance.head(12)[::-1]
    fig, ax = viz.plt.subplots(figsize=(7.5, 4.8))
    ax.barh([FEATURE_LABELS.get(c, c) for c in top.index], top.values, color=viz.SERIES[0],
            height=0.6)
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Mean |SHAP value| (log-odds of default)")
    ax.set_title("What drives predicted default risk (XGBoost, test vintages)")
    viz.save(fig, fig_dir / "pd_shap_importance.png")

    numeric = [c for c in importance.index if c in NUMERIC_FEATURES][:10]
    viz.plt.figure(figsize=(8, 5.2))
    shap.summary_plot(values[:, [list(X.columns).index(c) for c in numeric]],
                      X[numeric].rename(columns=FEATURE_LABELS), show=False, cmap=viz.DIV_CMAP,
                      plot_size=None)
    fig = viz.plt.gcf()
    fig.axes[0].set_title("SHAP values per loan - red = high feature value", loc="left")
    viz.save(fig, fig_dir / "pd_shap_beeswarm.png")

    return [{"feature": FEATURE_LABELS.get(k, k), "mean_abs_shap": float(v)}
            for k, v in importance.items()]


def load_model(settings: Settings) -> PDModel:
    return joblib.load(settings.artifacts_dir / "models" / "pd_model.joblib")

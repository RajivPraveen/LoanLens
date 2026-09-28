"""End-to-end pipeline on the tiny profile: dbt build + tests, models, stress test, reports."""

from __future__ import annotations

import json

import numpy as np
import pytest

from loanlens import pipeline
from loanlens.warehouse import query

pytestmark = pytest.mark.slow


def test_star_schema_reconciles_to_raw(built):
    raw = query(built, "select count(*) from raw.performance").iloc[0, 0]
    fact = query(built, "select count(*) from core.fct_loan_monthly").iloc[0, 0]
    loans = query(built, "select count(*) from core.dim_loan").iloc[0, 0]
    assert fact == raw
    assert loans == query(built, "select count(*) from raw.origination").iloc[0, 0]
    sums = query(built, """select period_month, from_state, sum(roll_rate) s
                           from analytics.mart_roll_rates group by all""")
    assert np.allclose(sums["s"], 1.0)


def test_incremental_dbt_rebuild_is_stable(built):
    before = query(built, "select count(*) from core.fct_loan_monthly").iloc[0, 0]
    pipeline.transform(built)  # nothing new loaded -> incremental no-op
    assert query(built, "select count(*) from core.fct_loan_monthly").iloc[0, 0] == before


def test_models_stress_and_reports(built):
    metrics = pipeline.train(built)
    for model in ("xgboost", "logistic_regression"):
        assert 0.5 <= metrics["test"][model]["auc"] <= 1.0
    scores = query(built, "select pd_xgb from ml.pd_origination_scores")
    assert scores["pd_xgb"].between(0, 1).all()

    pipeline.survival(built)
    summary = pipeline.stress(built)
    named = {r["scenario_id"]: r["loss_rate"] for r in summary["named"]}
    assert named["Severely adverse"] > named["Baseline"]
    assert summary["expected_loss_rate"] <= summary["var99_loss_rate"] <= summary["es99_loss_rate"] + 1e-12
    assert summary["backtest"]["actual_default_rate"] > 0

    decision = pipeline.decide(built)
    assert 0 < decision["test"]["approval_rate"] <= 1
    pipeline.fairness(built)
    pipeline.report(built)

    reports = built.reports_dir
    for name in ("model_report.md", "survival_report.md", "stress_test_report.md", "lending_memo.md",
                 "fairness_review.md", "results_summary.md", "dashboard.html"):
        assert (reports / name).exists(), name
    assert (built.powerbi_dir / "fct_portfolio_segment_monthly.parquet").exists()
    results = json.loads((reports / "results_summary.json").read_text())
    assert results["loans"] == query(built, "select count(*) from core.dim_loan").iloc[0, 0]

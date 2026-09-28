"""Power BI export: the star schema, marts and model outputs as Parquet (+ CSV for small tables).

Power BI Desktop imports the folder with the Power Query template in powerbi/. The 11.7M-row
loan-month fact is exported pre-aggregated (month x state x FICO band x LTV band) so the file
stays small; point Power BI at PostgreSQL (`loanlens publish-postgres`) for loan-level detail.
"""

from __future__ import annotations

import logging

from loanlens.config import Settings
from loanlens.warehouse import connect

log = logging.getLogger(__name__)

EXPORTS = {
    # name: SQL
    "dim_loan": """select loan_id, vintage, vintage_year, first_payment_month, state_code, census_region,
                          credit_score, fico_band, orig_ltv, ltv_band, orig_dti, dti_band, orig_upb,
                          orig_interest_rate, orig_loan_term, loan_purpose, occupancy, channel,
                          property_type, num_borrowers, is_first_time_homebuyer, termination_type,
                          first_default_month, months_to_default, is_liquidated, net_loss, loss_severity,
                          default_in_window, window_fully_observed
                   from core.dim_loan""",
    "dim_date": "select * from core.dim_date",
    "dim_geography": "select * from core.dim_geography",
    "dim_macro": "select * from core.dim_macro where month >= date '1999-01-01'",
    "fct_portfolio_segment_monthly": """
        select f.period_month, f.month_key, f.state_code, l.fico_band, l.ltv_band,
               count(*) filter (where not f.is_terminated)                          as active_loans,
               sum(f.current_upb) filter (where not f.is_terminated)                as active_upb,
               count(*) filter (where not f.is_terminated and f.is_dq30_plus)       as dq30_plus_loans,
               count(*) filter (where not f.is_terminated and f.is_dq90_plus)       as dq90_plus_loans,
               coalesce(sum(f.current_upb) filter (where not f.is_terminated and f.is_dq30_plus), 0) as dq30_plus_upb,
               count(*) filter (where f.is_default_event)                           as default_events,
               count(*) filter (where f.is_prepaid)                                 as prepaid_loans,
               coalesce(sum(f.exposure_upb) filter (where f.is_prepaid), 0)         as prepaid_upb,
               sum(f.net_loss_amount)                                               as net_loss
        from core.fct_loan_monthly f join core.dim_loan l using (loan_id)
        group by all""",
    "mart_portfolio_monthly": "select * from analytics.mart_portfolio_monthly",
    "mart_vintage_curves": "select * from analytics.mart_vintage_curves",
    "mart_roll_rates": "select * from analytics.mart_roll_rates",
    "mart_roll_rate_summary": "select * from analytics.mart_roll_rate_summary",
    "mart_risk_segments": "select * from analytics.mart_risk_segments",
    "mart_loss_by_vintage": "select * from analytics.mart_loss_by_vintage",
    "mart_data_quality": "select * from analytics.mart_data_quality",
    "mart_ingest_history": "select * from analytics.mart_ingest_history",
    "rpt_active_portfolio_risk": "select * from reporting.rpt_active_portfolio_risk",
    "rpt_risk_segments_el": "select * from reporting.rpt_risk_segments_el",
    "rpt_stress_loss_distribution": "select * from reporting.rpt_stress_loss_distribution",
    "stress_paths": "select * from ml.stress_paths",
    "stress_summary": "select * from ml.stress_summary",
    "backtest_monthly": "select * from ml.backtest_monthly",
    "survival_curves": "select * from ml.survival_curves",
}
CSV_MAX_ROWS = 50_000


def export_powerbi(settings: Settings) -> dict[str, int]:
    out = settings.powerbi_dir
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    with connect(settings, read_only=True) as con:
        for name, sql in EXPORTS.items():
            con.execute(f"COPY ({sql}) TO '{out / (name + '.parquet')}' (FORMAT parquet, COMPRESSION zstd)")
            n = con.execute(f"select count(*) from ({sql})").fetchone()[0]
            if n <= CSV_MAX_ROWS:
                con.execute(f"COPY ({sql}) TO '{out / (name + '.csv')}' (HEADER, DELIMITER ',')")
            counts[name] = int(n)
    log.info("Power BI export: %d tables to %s", len(counts), out)
    return counts

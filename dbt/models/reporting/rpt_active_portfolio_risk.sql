-- Active loans at the latest reporting month with model PD, LGD and expected loss.
with snapshot as (
    select max(period_month) as snapshot_month from {{ ref('fct_loan_monthly') }}
)

select
    f.loan_id,
    f.period_month                          as snapshot_month,
    l.vintage_year,
    l.state_code,
    l.census_region,
    l.fico_band,
    l.ltv_band,
    l.dti_band,
    l.loan_purpose,
    l.occupancy,
    f.current_upb,
    f.mtm_ltv,
    f.dq_state,
    f.refi_incentive,
    s.pd_12m,
    s.lgd,
    s.ead,
    s.expected_loss,
    s.risk_grade
from {{ ref('fct_loan_monthly') }} f
join snapshot on f.period_month = snapshot.snapshot_month
join {{ ref('dim_loan') }} l using (loan_id)
left join {{ source('ml', 'loan_scores') }} s using (loan_id)
where not f.is_terminated

-- Current-portfolio snapshot (latest reporting month) by credit score band, LTV band and state,
-- with the historical default experience of the same segment for context.
with snapshot as (
    select max(period_month) as snapshot_month from {{ ref('fct_loan_monthly') }}
),

active as (
    select f.*, l.fico_band, l.ltv_band, l.census_region, l.vintage_year
    from {{ ref('fct_loan_monthly') }} f
    join {{ ref('dim_loan') }} l using (loan_id)
    join snapshot s on f.period_month = s.snapshot_month
    where not f.is_terminated
),

history as (
    select
        fico_band, ltv_band, state_code,
        avg(case when default_in_window then 1.0 else 0.0 end) filter (where window_fully_observed)
                                                                  as hist_default_rate_24m,
        avg(loss_severity)                                        as hist_loss_severity
    from {{ ref('dim_loan') }}
    group by all
)

select
    a.fico_band,
    a.ltv_band,
    a.state_code,
    a.census_region,
    max(a.period_month)                                           as snapshot_month,
    count(*)                                                      as loans,
    sum(a.current_upb)                                            as upb,
    avg(a.mtm_ltv)                                                as avg_mtm_ltv,
    avg(case when a.is_dq30_plus then 1.0 else 0.0 end)           as dq30_plus_rate,
    avg(case when a.is_dq90_plus then 1.0 else 0.0 end)           as serious_dq_rate,
    sum(a.current_upb) filter (where a.is_dq30_plus)              as dq30_plus_upb,
    max(h.hist_default_rate_24m)                                  as hist_default_rate_24m,
    max(h.hist_loss_severity)                                     as hist_loss_severity
from active a
left join history h using (fico_band, ltv_band, state_code)
group by a.fico_band, a.ltv_band, a.state_code, a.census_region

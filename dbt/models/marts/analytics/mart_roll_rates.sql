-- Month-over-month transitions between delinquency states (a roll-rate matrix per month).
with f as (
    select * from {{ ref('fct_loan_monthly') }}
    where prev_dq_state is not null
)

select
    period_month,
    month_key,
    prev_dq_state                                                           as from_state,
    dq_state                                                                as to_state,
    count(*)                                                                as loans,
    sum(exposure_upb)                                                       as upb,
    count(*) / sum(count(*)) over (partition by period_month, prev_dq_state) as roll_rate,
    sum(exposure_upb) / nullif(sum(sum(exposure_upb)) over (partition by period_month, prev_dq_state), 0)
                                                                            as roll_rate_upb
from f
group by period_month, month_key, prev_dq_state, dq_state

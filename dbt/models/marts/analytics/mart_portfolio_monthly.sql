-- Portfolio KPIs by reporting month: size, delinquency, prepayment and default speeds, losses.
with f as (
    select * from {{ ref('fct_loan_monthly') }}
),

agg as (
    select
        period_month,
        month_key,
        count(*) filter (where not is_terminated)                            as active_loans,
        sum(current_upb) filter (where not is_terminated)                    as active_upb,
        count(*) filter (where not is_terminated and is_dq30_plus)           as dq30_plus_loans,
        count(*) filter (where not is_terminated and is_dq60_plus)           as dq60_plus_loans,
        count(*) filter (where not is_terminated and is_dq90_plus)           as dq90_plus_loans,
        count(*) filter (where not is_terminated and is_serious_dq_ex_forbearance) as serious_dq_ex_forb_loans,
        count(*) filter (where not is_terminated and in_forbearance)         as forbearance_loans,
        count(*) filter (where not is_terminated and is_reo)                 as reo_loans,
        coalesce(sum(current_upb) filter (where not is_terminated and is_dq30_plus), 0) as dq30_plus_upb,
        coalesce(sum(current_upb) filter (where not is_terminated and is_dq90_plus), 0) as dq90_plus_upb,
        count(*) filter (where is_prepaid)                                   as prepaid_loans,
        coalesce(sum(exposure_upb) filter (where is_prepaid), 0)             as prepaid_upb,
        count(*) filter (where is_default_event)                             as default_events,
        coalesce(sum(exposure_upb) filter (where is_default_event), 0)       as default_upb,
        count(*) filter (where is_liquidated)                                as liquidations,
        coalesce(sum(exposure_upb) filter (where is_liquidated), 0)          as liquidated_upb,
        sum(net_loss_amount)                                                 as net_loss,
        avg(mtm_ltv) filter (where not is_terminated)                        as avg_mtm_ltv
    from f
    group by all
)

select
    agg.*,
    dq30_plus_loans / nullif(active_loans, 0)                               as dq30_plus_rate,
    dq60_plus_loans / nullif(active_loans, 0)                               as dq60_plus_rate,
    dq90_plus_loans / nullif(active_loans, 0)                               as serious_dq_rate,
    serious_dq_ex_forb_loans / nullif(active_loans, 0)                      as serious_dq_rate_ex_forbearance,
    forbearance_loans / nullif(active_loans, 0)                             as forbearance_rate,
    dq30_plus_upb / nullif(active_upb, 0)                                   as dq30_plus_rate_upb,
    dq90_plus_upb / nullif(active_upb, 0)                                   as serious_dq_rate_upb,
    -- speeds: monthly rate over balance at risk, annualised
    prepaid_upb / nullif(active_upb + prepaid_upb + liquidated_upb, 0)      as smm,
    1 - power(1 - prepaid_upb / nullif(active_upb + prepaid_upb + liquidated_upb, 0), 12) as cpr,
    1 - power(1 - default_upb / nullif(active_upb + prepaid_upb + liquidated_upb, 0), 12) as cdr,
    net_loss / nullif(active_upb, 0) * 12                                   as annualised_loss_rate
from agg

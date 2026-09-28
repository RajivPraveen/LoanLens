-- Lifetime credit performance by origination year.
select
    vintage_year,
    count(*)                                                                as loans,
    sum(orig_upb)                                                           as orig_upb,
    avg(credit_score)                                                       as avg_credit_score,
    avg(orig_ltv)                                                           as avg_orig_ltv,
    avg(orig_dti)                                                           as avg_orig_dti,
    avg(orig_interest_rate)                                                 as avg_orig_rate,
    count(*) filter (where first_default_month is not null)                 as defaults,
    count(*) filter (where first_default_month is not null) / count(*)      as lifetime_default_rate,
    avg(case when default_in_window then 1.0 else 0.0 end)
        filter (where window_fully_observed)                                as default_rate_24m,
    count(*) filter (where is_liquidated)                                   as liquidations,
    coalesce(sum(liquidation_upb), 0)                                       as liquidation_upb,
    sum(net_loss)                                                           as net_loss,
    sum(net_loss) filter (where is_liquidated) / nullif(sum(severity_base_upb), 0) as loss_severity,
    sum(net_loss) / sum(orig_upb)                                           as cum_loss_rate,
    count(*) filter (where termination_type = 'Voluntary payoff') / count(*) as prepaid_share,
    count(*) filter (where ever_modified) / count(*)                        as modified_share,
    count(*) filter (where termination_type = 'Active')                     as active_loans
from {{ ref('dim_loan') }}
group by vintage_year

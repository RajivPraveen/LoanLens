-- Expected loss concentration by segment for the current book.
select
    fico_band,
    ltv_band,
    state_code,
    census_region,
    count(*)                                         as loans,
    sum(current_upb)                                 as upb,
    sum(expected_loss)                               as expected_loss,
    sum(expected_loss) / nullif(sum(current_upb), 0) as expected_loss_rate,
    avg(pd_12m)                                      as avg_pd_12m,
    sum(pd_12m * current_upb) / nullif(sum(current_upb), 0) as upb_weighted_pd_12m
from {{ ref('rpt_active_portfolio_risk') }}
group by all

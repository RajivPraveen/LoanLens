-- The headline roll rates as monthly time series (share of loans moving to the next bucket).
with r as (
    select * from {{ ref('mart_roll_rates') }}
)

select
    period_month,
    month_key,
    sum(roll_rate) filter (where from_state = 'Current' and to_state = '30')  as current_to_30,
    sum(roll_rate) filter (where from_state = '30' and to_state = '60')       as dq30_to_60,
    sum(roll_rate) filter (where from_state = '60' and to_state = '90')       as dq60_to_90,
    sum(roll_rate) filter (where from_state = '90' and to_state = '120+')     as dq90_to_120,
    sum(roll_rate) filter (where from_state = '30' and to_state = 'Current')  as dq30_cure,
    sum(roll_rate) filter (where from_state = '60' and to_state = 'Current')  as dq60_cure,
    sum(roll_rate) filter (where from_state = 'Current' and to_state = 'Prepaid') as current_to_prepaid,
    sum(loans) filter (where from_state = 'Current')                          as current_loans_start
from r
group by all

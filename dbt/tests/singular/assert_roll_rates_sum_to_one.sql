-- Transitions out of every state in every month must sum to 100%.
select period_month, from_state, sum(roll_rate) as total
from {{ ref('mart_roll_rates') }}
group by all
having abs(sum(roll_rate) - 1) > 1e-9

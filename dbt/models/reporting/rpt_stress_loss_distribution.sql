-- Monte Carlo scenario outcomes with their rank in the loss distribution.
select
    *,
    percent_rank() over (order by loss_rate) as loss_percentile
from {{ source('ml', 'stress_scenarios') }}

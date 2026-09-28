select loan_id, count(*) as terminations
from {{ ref('fct_loan_monthly') }}
where is_terminated
group by loan_id
having count(*) > 1

-- Once a loan has a zero-balance code it must not report again.
select f.loan_id, f.period_month, l.termination_month
from {{ ref('fct_loan_monthly') }} f
join {{ ref('dim_loan') }} l using (loan_id)
where l.termination_month is not null and f.period_month > l.termination_month

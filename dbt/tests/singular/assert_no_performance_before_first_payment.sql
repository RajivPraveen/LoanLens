select f.loan_id, f.period_month, l.first_payment_month
from {{ ref('fct_loan_monthly') }} f
join {{ ref('dim_loan') }} l using (loan_id)
-- the first record is at loan age 0, one month before the first payment is due
where f.period_month < l.first_payment_month - interval 1 month

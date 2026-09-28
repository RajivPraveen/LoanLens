select loan_id, period_month, net_loss_amount
from {{ ref('fct_loan_monthly') }}
where net_loss_amount <> 0 and not is_liquidated

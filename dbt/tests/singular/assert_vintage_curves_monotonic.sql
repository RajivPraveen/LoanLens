-- Cumulative default and prepayment curves can never decrease with age.
-- Cumulative *net* loss can dip slightly: 756 real liquidations (1999-2026 sample) ended in a
-- small net gain (recoveries above the balance), about 0.5% of loss dollars. A dip of more than
-- 1 basis point of original balance would still signal an error.
select *
from (
    select
        vintage_year,
        months_since_first_payment,
        cum_default_rate - lag(cum_default_rate) over w as d_default,
        cum_prepay_rate - lag(cum_prepay_rate) over w   as d_prepay,
        cum_loss_rate - lag(cum_loss_rate) over w       as d_loss
    from {{ ref('mart_vintage_curves') }}
    window w as (partition by vintage_year order by months_since_first_payment)
)
where d_default < -1e-12 or d_prepay < -1e-12 or d_loss < -1e-4

-- Cumulative default, loss and prepayment by origination year and months since first payment.
with loans as (
    select * from {{ ref('dim_loan') }}
),

vintages as (
    select
        vintage_year,
        count(*)                                                             as loans,
        sum(orig_upb)                                                        as orig_upb,
        datediff('month', max(first_payment_month), max(data_end_month)) + 1 as max_seasoned_age,
        datediff('month', min(first_payment_month), max(data_end_month)) + 1 as max_age
    from loans
    group by vintage_year
),

defaults as (
    select vintage_year, months_to_default as age, count(*) as n, sum(orig_upb) as upb
    from loans where months_to_default is not null group by all
),

losses as (
    select vintage_year, months_to_termination as age, sum(net_loss) as loss
    from loans where is_liquidated group by all
),

prepays as (
    select vintage_year, months_to_termination as age, count(*) as n
    from loans where termination_type = 'Voluntary payoff' group by all
),

spine as (
    select v.vintage_year, a.range as age
    from vintages v, range(1, 361) a
    where a.range <= v.max_age
)

select
    s.vintage_year,
    s.age                                                                    as months_since_first_payment,
    v.loans,
    v.orig_upb,
    sum(coalesce(d.n, 0)) over w                                             as cum_defaults,
    sum(coalesce(d.n, 0)) over w / v.loans                                   as cum_default_rate,
    sum(coalesce(d.upb, 0)) over w / v.orig_upb                              as cum_default_rate_upb,
    sum(coalesce(l.loss, 0)) over w / v.orig_upb                             as cum_loss_rate,
    sum(coalesce(p.n, 0)) over w / v.loans                                   as cum_prepay_rate,
    s.age <= v.max_seasoned_age                                              as is_fully_seasoned
from spine s
join vintages v using (vintage_year)
left join defaults d using (vintage_year, age)
left join losses l using (vintage_year, age)
left join prepays p using (vintage_year, age)
window w as (partition by s.vintage_year order by s.age)

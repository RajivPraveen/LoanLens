-- Month calendar with NBER recession flags for chart shading.
with bounds as (
    -- performance starts at loan age 0, one month before the first payment
    select cast(min(first_payment_month) - interval 1 month as date) as first_month, max(data_end_month) as last_month
    from {{ ref('stg_freddie__origination') }}
    cross join (select max(data_end_month) as data_end_month from {{ ref('int_loan_outcomes') }})
)

select
    cast(m.month as date)                                   as month,
    cast(strftime(m.month, '%Y%m') as integer)              as month_key,
    year(m.month)                                           as year,
    quarter(m.month)                                        as quarter,
    year(m.month) || 'Q' || quarter(m.month)                as year_quarter,
    strftime(m.month, '%b %Y')                              as month_label,
    (m.month between date '2001-03-01' and date '2001-11-01'
     or m.month between date '2007-12-01' and date '2009-06-01'
     or m.month between date '2020-02-01' and date '2020-04-01') as is_nber_recession
from bounds,
     unnest(generate_series(bounds.first_month, bounds.last_month, interval 1 month)) as m(month)

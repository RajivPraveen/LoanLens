-- Monthly geo x month macro panel. Weekly series are averaged, quarterly series carried
-- forward within the quarter and the latest value carried to the end of the spine.
-- (Mirrors loanlens.ingest.fred.monthly_panel.)
with obs as (
    select
        geo,
        measure,
        cast(date_trunc('month', observation_date) as date) as month,
        avg(value)                                          as value
    from {{ source('fred', 'fred_observations') }}
    where value is not null
    group by all
),

bounds as (
    select geo, measure, min(month) as first_month from obs group by all
),

spine as (
    select b.geo, b.measure, cast(m.month as date) as month
    from bounds b,
         unnest(generate_series(b.first_month, (select max(month) from obs), interval 1 month)) as m(month)
),

filled as (
    select
        s.geo,
        s.measure,
        s.month,
        last_value(o.value ignore nulls) over (
            partition by s.geo, s.measure order by s.month
            rows between unbounded preceding and current row
        ) as value
    from spine s
    left join obs o using (geo, measure, month)
)

select
    geo,
    month,
    max(case when measure = 'unemployment_rate' then value end) as unemployment_rate,
    max(case when measure = 'hpi' then value end)               as hpi,
    max(case when measure = 'mortgage_rate_30y' then value end) as mortgage_rate_30y
from filled
group by all

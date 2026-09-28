-- State x month macro conditions joined to national series.
with panel as (
    select * from {{ ref('stg_fred__macro_monthly') }}
),

us as (
    select month, unemployment_rate as us_unemployment_rate, hpi as us_hpi, mortgage_rate_30y
    from panel where geo = 'US'
),

st as (
    select geo as state_code, month, unemployment_rate as state_unemployment_rate, hpi as state_hpi
    from panel where geo <> 'US'
)

select
    st.state_code || '-' || strftime(st.month, '%Y%m')                         as macro_key,
    st.state_code,
    st.month,
    st.state_unemployment_rate,
    st.state_hpi,
    us.us_unemployment_rate,
    us.us_hpi,
    us.mortgage_rate_30y,
    st.state_unemployment_rate
        - lag(st.state_unemployment_rate, 12) over w                            as state_ur_change_12m,
    st.state_hpi / nullif(lag(st.state_hpi, 12) over w, 0) - 1                 as state_hpi_yoy,
    us.us_unemployment_rate
        - lag(us.us_unemployment_rate, 12) over w                               as us_ur_change_12m,
    us.us_hpi / nullif(lag(us.us_hpi, 12) over w, 0) - 1                       as us_hpi_yoy
from st
join us using (month)
window w as (partition by st.state_code order by st.month)

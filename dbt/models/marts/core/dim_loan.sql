-- Loan dimension: origination attributes, macro conditions at origination and the loan's
-- observed outcome. Grain: one row per loan.
with orig as (
    select * from {{ ref('stg_freddie__origination') }}
),

outcomes as (
    select * from {{ ref('int_loan_outcomes') }}
),

macro as (
    select * from {{ ref('dim_macro') }}
),

geo as (
    select * from {{ ref('state_reference') }}
),

-- Loans delivered without any performance record yet (the newest originations) are observed
-- up to the dataset's end month, with no event.
data_end as (
    select max(data_end_month) as data_end_month from outcomes
)

select
    o.loan_id,
    o.source_period,
    o.vintage,
    o.vintage_year,
    o.vintage_quarter,
    o.orig_month,
    -- Performance records start at loan age 0, the month before the first payment. For 9 loans
    -- the origination file's first-payment date is later than their performance history (a
    -- reset date); the performance history wins, so every duration below is consistent.
    least(o.first_payment_month, cast(x.first_period_month + interval 1 month as date))
                                                                as first_payment_month,
    o.maturity_month,
    o.state_code,
    g.state_name,
    g.census_region,
    g.census_division,
    g.judicial_foreclosure,
    o.zip3,
    o.credit_score,
    o.fico_band,
    o.orig_ltv,
    o.orig_cltv,
    o.ltv_band,
    o.orig_dti,
    o.dti_band,
    o.mi_pct,
    o.orig_upb,
    o.orig_interest_rate,
    o.orig_loan_term,
    o.loan_purpose,
    o.occupancy,
    o.channel,
    o.property_type,
    o.num_units,
    o.num_borrowers,
    o.is_first_time_homebuyer,
    o.is_super_conforming,
    o.seller_name,
    coalesce(x.current_servicer_name, o.orig_servicer_name)    as servicer_name,
    o.vantage_score,
    o.layout_version,
    -- macro at origination
    m.mortgage_rate_30y                                         as market_rate_at_orig,
    o.orig_interest_rate - m.mortgage_rate_30y                  as rate_spread_at_orig,
    m.state_unemployment_rate                                   as state_ur_at_orig,
    m.state_ur_change_12m                                       as state_ur_change_12m_at_orig,
    m.state_hpi_yoy                                             as state_hpi_yoy_at_orig,
    m.state_hpi                                                 as state_hpi_at_orig,
    -- outcome
    x.first_dq30_month,
    x.first_dq60_month,
    x.first_dq90_month,
    x.first_default_month,
    datediff('month', least(o.first_payment_month, cast(x.first_period_month + interval 1 month as date)), x.first_default_month) + 1  as months_to_default,
    x.termination_type,
    x.zero_balance_code,
    x.termination_month,
    datediff('month', least(o.first_payment_month, cast(x.first_period_month + interval 1 month as date)), x.termination_month) + 1    as months_to_termination,
    x.is_liquidated,
    x.liquidation_upb,
    x.default_upb,
    x.severity_base_upb,
    x.net_loss,
    x.loss_severity,
    x.mi_recoveries,
    x.ever_modified,
    x.ever_forbearance,
    x.max_dq_months,
    x.months_reported,
    x.last_period_month,
    d.data_end_month,
    datediff('month', least(o.first_payment_month, cast(x.first_period_month + interval 1 month as date)), d.data_end_month) + 1       as months_observable,
    -- Default label used by the PD model: credit event within the window after first payment.
    x.first_default_month is not null
        and datediff('month', least(o.first_payment_month, cast(x.first_period_month + interval 1 month as date)), x.first_default_month) + 1
            <= {{ var('default_window_months') }}                       as default_in_window,
    datediff('month', least(o.first_payment_month, cast(x.first_period_month + interval 1 month as date)), d.data_end_month) + 1
        >= {{ var('default_window_months') }}                           as window_fully_observed
from orig o
left join outcomes x using (loan_id)
left join geo g using (state_code)
cross join data_end d
left join macro m on m.state_code = o.state_code and m.month = o.orig_month

{{
    config(
        materialized='incremental',
        incremental_strategy='delete+insert',
        unique_key='source_period',
        on_schema_change='sync_all_columns'
    )
}}
-- Monthly performance fact. Grain: loan x reporting month.
-- Incremental by source period: only periods (re)loaded since the last build are rebuilt,
-- and delete+insert on source_period makes restated files replace, never duplicate.
with perf as (
    select * from {{ ref('stg_freddie__performance') }}
    {% if is_incremental() %}
    where source_period in (
        select source_period
        from {{ source('meta', 'ingest_log') }}
        where status = 'loaded'
          and finished_at > (select coalesce(max(built_at), timestamp '1900-01-01') from {{ this }})
    )
    {% endif %}
),

loans as (
    select loan_id, state_code, orig_upb, orig_ltv, orig_month, state_hpi_at_orig,
           first_default_month, orig_interest_rate
    from {{ ref('dim_loan') }}
),

macro as (
    select macro_key, state_code, month, state_hpi, mortgage_rate_30y
    from {{ ref('dim_macro') }}
),

enriched as (
    select
        p.loan_id,
        p.source_period,
        p.period_month,
        cast(strftime(p.period_month, '%Y%m') as integer)                     as month_key,
        l.state_code,
        l.state_code || '-' || strftime(p.period_month, '%Y%m')               as macro_key,
        p.loan_age,
        p.current_upb,
        -- balance at risk this month: current UPB, or UPB removed on termination
        case when p.zero_balance_code is not null then p.zero_balance_removal_upb
             else p.current_upb end                                           as exposure_upb,
        p.dq_months,
        p.dq_state,
        lag(p.dq_state) over (partition by p.loan_id order by p.period_month) as prev_dq_state,
        coalesce(p.dq_months >= 1 or p.is_reo, false)                         as is_dq30_plus,
        coalesce(p.dq_months >= 2 or p.is_reo, false)                         as is_dq60_plus,
        coalesce(p.dq_months >= 3 or p.is_reo, false)                         as is_dq90_plus,
        coalesce((p.dq_months >= 3 and not p.in_forbearance) or p.is_reo, false) as is_serious_dq_ex_forbearance,
        p.is_reo,
        p.in_forbearance,
        p.is_modified,
        coalesce(p.current_interest_rate, l.orig_interest_rate)              as current_interest_rate,
        p.zero_balance_code,
        p.zero_balance_code is not null                                       as is_terminated,
        coalesce(p.zero_balance_code = '01', false)                           as is_prepaid,
        coalesce(p.zero_balance_code in ('02', '03', '09', '15', '16'), false) as is_liquidated,
        coalesce(p.period_month = l.first_default_month, false)               as is_default_event,
        coalesce(p.net_loss_amount, 0)                                        as net_loss_amount,
        p.estimated_ltv,
        l.orig_ltv * (case when p.zero_balance_code is not null then p.zero_balance_removal_upb
                           else p.current_upb end / nullif(l.orig_upb, 0))
            * (l.state_hpi_at_orig / nullif(m.state_hpi, 0))                  as mtm_ltv,
        coalesce(p.current_interest_rate, l.orig_interest_rate) - m.mortgage_rate_30y as refi_incentive,
        now()                                                                 as built_at
    from perf p
    join loans l using (loan_id)
    left join macro m on m.state_code = l.state_code and m.month = p.period_month
)

select * from enriched

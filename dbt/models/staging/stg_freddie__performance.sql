{{
    config(
        materialized='incremental',
        incremental_strategy='delete+insert',
        unique_key='source_period',
        on_schema_change='sync_all_columns'
    )
}}
-- Typed monthly performance with decoded delinquency and termination fields.
-- A table, not a view: at 75M rows every downstream model and test would otherwise re-decode
-- the raw file. Incremental by source period, like fct_loan_monthly.
with src as (
    select * from {{ source('freddie', 'performance') }}
    {% if is_incremental() %}
    where source_period in (
        select source_period
        from {{ source('meta', 'ingest_log') }}
        where status = 'loaded'
          and finished_at > (select coalesce(max(staged_at), timestamp '1900-01-01') from {{ this }})
    )
    {% endif %}
),

-- Loss-field sign convention, detected per source period from the data itself.
-- Before Release 47 Freddie Mac reported losses as negatives; the Release 47 notes say losses
-- become positive (recoveries negative), but Freddie's own R47 example file still uses
-- negatives. A liquidation normally produces a loss, so the sign of the median liquidation
-- "actual loss" identifies the convention; MI recoveries are normalised the same way.
conventions as (
    select
        source_period,
        case when median(actual_loss) filter (
                 where zero_balance_code in ('02', '03', '09', '15', '16') and actual_loss <> 0) < 0
             then -1 else 1 end                                          as loss_sign,
        case when median(mi_recoveries) filter (where mi_recoveries <> 0) < 0
             then -1 else 1 end                                          as recovery_sign
    from src
    group by source_period
)

select
    loan_sequence_number                                         as loan_id,
    source_period,
    {{ yyyymm_to_date('monthly_reporting_period') }}             as period_month,
    current_actual_upb                                           as current_upb,
    current_loan_delinquency_status                              as dq_status_raw,
    try_cast(current_loan_delinquency_status as integer)         as dq_months,
    current_loan_delinquency_status = 'RA'                       as is_reo,
    loan_age,
    remaining_months_to_legal_maturity                           as remaining_months,
    modification_flag in ('Y', 'P')                              as is_modified,
    modification_flag = 'Y'                                      as modified_this_month,
    nullif(zero_balance_code, '')                                as zero_balance_code,
    case when zero_balance_effective_date is not null
         then {{ yyyymm_to_date('zero_balance_effective_date') }} end as zero_balance_month,
    -- A few records (reporting months 2017-03/04) carry rates of 30-50%, roughly ten times the
    -- loan's rate the month before - a data-entry error. Implausible rates become NULL; the
    -- fact table falls back to the note rate at origination.
    case when current_interest_rate between 0 and 20 then current_interest_rate end
                                                                 as current_interest_rate,
    coalesce(current_non_interest_bearing_upb, 0)                as deferred_upb,
    coalesce(zero_balance_removal_upb, 0)                        as zero_balance_removal_upb,
    mi_recoveries * c.recovery_sign                              as mi_recoveries,       -- positive = cash recovered
    abs(try_cast(net_sale_proceeds as double))                   as net_sale_proceeds,
    non_mi_recoveries * c.recovery_sign                          as non_mi_recoveries,
    abs(total_expenses)                                          as total_expenses,
    abs(delinquent_accrued_interest)                             as delinquent_accrued_interest,
    actual_loss * c.loss_sign                                    as net_loss_amount,     -- positive = loss
    case when estimated_ltv between 1 and 998 then estimated_ltv end as estimated_ltv,
    coalesce(borrower_assistance_status_code = 'F', false)       as in_forbearance,
    nullif(borrower_assistance_status_code, '')                  as borrower_assistance_status,
    case
        when zero_balance_code = '01' then 'Prepaid'
        when zero_balance_code in ('02', '03', '09', '15', '16') then 'Liquidated'
        when zero_balance_code is not null then 'Other exit'
        when current_loan_delinquency_status = 'RA' then 'REO'
        when try_cast(current_loan_delinquency_status as integer) = 0 then 'Current'
        when try_cast(current_loan_delinquency_status as integer) = 1 then '30'
        when try_cast(current_loan_delinquency_status as integer) = 2 then '60'
        when try_cast(current_loan_delinquency_status as integer) = 3 then '90'
        when try_cast(current_loan_delinquency_status as integer) >= 4 then '120+'
        else 'Unknown'                                          -- 'XX' = not available
    end                                                          as dq_state,
    nullif(servicer_name, '')                                    as servicer_name,
    layout_version,
    batch_id,
    now()                                                        as staged_at
from src
left join conventions c using (source_period)

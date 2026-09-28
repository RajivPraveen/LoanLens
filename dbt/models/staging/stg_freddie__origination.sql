-- Typed, decoded origination attributes. Freddie Mac sentinel codes become NULL here.
with src as (
    select * from {{ source('freddie', 'origination') }}
),

decoded as (
    select
        loan_sequence_number                                            as loan_id,
        source_period,
        -- Origination quarter: from the loan identifier when it carries one (Release 47
        -- "PYYQnXXXXXXX", legacy "F1YYQnXXXXXX"), otherwise from the first payment date
        -- (loans typically close two months before their first payment).
        case
            when regexp_matches(loan_sequence_number, '^[A-Z][0-9]{2}Q[1-4][0-9]{7}$')
                then {{ two_digit_year('substr(loan_sequence_number, 2, 2)') }}
            when regexp_matches(loan_sequence_number, '^F1[0-9]{2}Q[1-4][0-9]{6}$')
                then {{ two_digit_year('substr(loan_sequence_number, 3, 2)') }}
            else year({{ yyyymm_to_date('first_payment_date') }} - interval 2 month)
        end                                                             as vintage_year,
        case
            when regexp_matches(loan_sequence_number, '^[A-Z][0-9]{2}Q[1-4][0-9]{7}$')
                then cast(substr(loan_sequence_number, 5, 1) as int)
            when regexp_matches(loan_sequence_number, '^F1[0-9]{2}Q[1-4][0-9]{6}$')
                then cast(substr(loan_sequence_number, 6, 1) as int)
            else quarter({{ yyyymm_to_date('first_payment_date') }} - interval 2 month)
        end                                                             as vintage_quarter,
        {{ yyyymm_to_date('first_payment_date') }}                      as first_payment_month,
        {{ yyyymm_to_date('maturity_date') }}                           as maturity_month,
        case when credit_score between 300 and 850 then credit_score end as credit_score,
        case when vantage_score between 300 and 850 then vantage_score end as vantage_score,
        case first_time_homebuyer_flag when 'Y' then true when 'N' then false end
                                                                        as is_first_time_homebuyer,
        nullif(msa, '')                                                 as msa,
        case when mi_pct between 0 and 55 then mi_pct end               as mi_pct,
        case when num_units between 1 and 4 then num_units end          as num_units,
        case occupancy_status when 'P' then 'Primary' when 'I' then 'Investment'
                              when 'S' then 'Second home' else 'Unknown' end as occupancy,
        case when orig_cltv between 1 and 998 then orig_cltv end        as orig_cltv,
        case when orig_dti between 0 and 65 then orig_dti end           as orig_dti,
        orig_upb,
        case when orig_ltv between 1 and 998 then orig_ltv end          as orig_ltv,
        orig_interest_rate,
        case channel when 'R' then 'Retail' when 'B' then 'Broker'
                     when 'C' then 'Correspondent' when 'T' then 'TPO not specified'
                     else 'Unknown' end                                 as channel,
        ppm_flag = 'Y'                                                  as has_prepayment_penalty,
        amortization_type,
        property_state                                                  as state_code,
        case property_type when 'SF' then 'Single-family' when 'CO' then 'Condo'
                           when 'PU' then 'PUD' when 'MH' then 'Manufactured'
                           when 'CP' then 'Co-op' else 'Unknown' end    as property_type,
        substr(postal_code, 1, 3)                                       as zip3,
        case loan_purpose when 'P' then 'Purchase' when 'C' then 'Cash-out refinance'
                          when 'N' then 'No cash-out refinance' when 'R' then 'Refinance'
                          else 'Unknown' end                            as loan_purpose,
        orig_loan_term,
        case when num_borrowers between 1 and 10 then num_borrowers end as num_borrowers,
        seller_name,
        nullif(servicer_name, '')                                       as orig_servicer_name,  -- legacy layout only
        coalesce(super_conforming_flag = 'Y', false)                    as is_super_conforming,
        coalesce(harp_indicator = 'Y', false)                           as is_harp,
        layout_version,
        batch_id,
        loaded_at
    from src
)

select
    *,
    -- Closing is typically two months before the first payment.
    cast(first_payment_month - interval 2 month as date)                as orig_month,
    vintage_year || 'Q' || vintage_quarter                              as vintage,
    {{ fico_band('credit_score') }}                                     as fico_band,
    {{ ltv_band('orig_ltv') }}                                          as ltv_band,
    {{ dti_band('orig_dti') }}                                          as dti_band
from decoded
-- Scope: the 50 states and DC. Loans in Puerto Rico, Guam and the U.S. Virgin Islands (~0.2%
-- of the sample) are kept in raw but excluded here: FRED has no house-price or unemployment
-- series for them, and every model conditions on local economics.
where state_code in (select state_code from {{ ref('state_reference') }})

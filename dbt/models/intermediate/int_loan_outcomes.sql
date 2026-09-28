-- One row per loan summarising its observed life: first delinquency milestones, the first
-- credit event (the platform's default definition), termination and realised loss.
with perf as (
    select * from {{ ref('stg_freddie__performance') }}
),

data_end as (
    select max(period_month) as data_end_month from perf
),

per_loan as (
    select
        loan_id,
        min(period_month) filter (where dq_months >= 1)                     as first_dq30_month,
        min(period_month) filter (where dq_months >= 2)                     as first_dq60_month,
        min(period_month) filter (where dq_months >= 3)                     as first_dq90_month,
        min(period_month) filter (
            where {{ is_credit_event('dq_months', 'is_reo', 'in_forbearance', 'zero_balance_code') }}
        )                                                                   as first_default_month,
        -- balance at the first credit event (loss-severity denominator)
        arg_min(current_upb, period_month) filter (
            where {{ is_credit_event('dq_months', 'is_reo', 'in_forbearance', 'zero_balance_code') }}
              and current_upb > 0
        )                                                                   as default_upb,
        min(period_month)                                                   as first_period_month,
        max(period_month)                                                   as last_period_month,
        count(*)                                                            as months_reported,
        max(zero_balance_code)                                              as zero_balance_code,
        max(period_month) filter (where zero_balance_code is not null)      as termination_month,
        max(remaining_months) filter (where zero_balance_code is not null)  as remaining_months_at_termination,
        max(zero_balance_removal_upb) filter (where zero_balance_code is not null) as termination_upb,
        coalesce(sum(net_loss_amount), 0)                                   as net_loss,
        coalesce(sum(mi_recoveries), 0)                                     as mi_recoveries,
        bool_or(is_modified)                                                as ever_modified,
        bool_or(in_forbearance)                                             as ever_forbearance,
        max(dq_months)                                                      as max_dq_months,
        arg_max(servicer_name, period_month) filter (where servicer_name is not null)
                                                                            as current_servicer_name
    from perf
    group by loan_id
)

select
    p.*,
    d.data_end_month,
    case
        when p.zero_balance_code is null then 'Active'
        when p.zero_balance_code = '01' and coalesce(p.remaining_months_at_termination, 99) <= 1 then 'Matured'
        when p.zero_balance_code = '01' then 'Voluntary payoff'
        when p.zero_balance_code = '02' then 'Third-party sale'
        when p.zero_balance_code = '03' then 'Short sale'
        when p.zero_balance_code = '09' then 'REO disposition'
        when p.zero_balance_code = '15' then 'Note sale'
        when p.zero_balance_code = '16' then 'Reperforming loan sale'
        when p.zero_balance_code = '06' then 'Repurchase'
        else 'Other'
    end                                                                     as termination_type,
    p.zero_balance_code in ('02', '03', '09', '15', '16')                   as is_liquidated,
    case when p.zero_balance_code in ('02', '03', '09', '15', '16')
         then p.termination_upb end                                         as liquidation_upb,
    -- Severity = net loss / balance at default. The balance removed at termination is not used:
    -- on short sales it is often a written-down residual of a few dollars or cents.
    case when p.zero_balance_code in ('02', '03', '09', '15', '16')
         then greatest(coalesce(p.default_upb, 0), coalesce(p.termination_upb, 0)) end
                                                                            as severity_base_upb,
    case when p.zero_balance_code in ('02', '03', '09', '15', '16')
              and greatest(coalesce(p.default_upb, 0), coalesce(p.termination_upb, 0)) > 0
         then p.net_loss / greatest(coalesce(p.default_upb, 0), coalesce(p.termination_upb, 0))
    end                                                                     as loss_severity
from per_loan p
cross join data_end d

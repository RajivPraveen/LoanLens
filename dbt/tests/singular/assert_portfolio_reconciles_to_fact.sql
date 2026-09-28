-- The mart must reconcile exactly to the fact table (no rows lost or double-counted).
with fact as (
    select count(*) filter (where not is_terminated) as active from {{ ref('fct_loan_monthly') }}
),
mart as (
    select sum(active_loans) as active from {{ ref('mart_portfolio_monthly') }}
)
select * from fact, mart where fact.active <> mart.active

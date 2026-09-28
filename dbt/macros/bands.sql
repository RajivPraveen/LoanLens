{% macro fico_band(col) -%}
    case
        when {{ col }} is null then 'Missing'
        when {{ col }} < 620 then '<620'
        when {{ col }} < 680 then '620-679'
        when {{ col }} < 720 then '680-719'
        when {{ col }} < 760 then '720-759'
        else '760+'
    end
{%- endmacro %}

{% macro ltv_band(col) -%}
    case
        when {{ col }} is null then 'Missing'
        when {{ col }} <= 60 then '<=60'
        when {{ col }} <= 80 then '61-80'
        when {{ col }} <= 90 then '81-90'
        when {{ col }} <= 95 then '91-95'
        else '>95'
    end
{%- endmacro %}

{% macro dti_band(col) -%}
    case
        when {{ col }} is null then 'Missing'
        when {{ col }} <= 30 then '<=30'
        when {{ col }} <= 36 then '31-36'
        when {{ col }} <= 43 then '37-43'
        when {{ col }} <= 50 then '44-50'
        else '>50'
    end
{%- endmacro %}

{% macro yyyymm_to_date(col) -%}
    cast(strptime({{ col }} || '01', '%Y%m%d') as date)
{%- endmacro %}

{# Credit event: 90+ days delinquent outside COVID forbearance, REO, or a loss-generating
   zero-balance code. This is the "default" definition used across the platform. #}
{% macro is_credit_event(dq_months, is_reo, in_forbearance, zb_code) -%}
    (({{ dq_months }} >= 3 and not coalesce({{ in_forbearance }}, false))
     or coalesce({{ is_reo }}, false)
     or {{ zb_code }} in ('02', '03', '09', '15', '16'))
{%- endmacro %}

{# Two-digit year from a loan identifier: 90-99 -> 1990s, otherwise 2000s. #}
{% macro two_digit_year(expr) -%}
    (case when cast({{ expr }} as int) >= 90 then 1900 else 2000 end + cast({{ expr }} as int))
{%- endmacro %}

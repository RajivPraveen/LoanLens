-- Data-quality pass rates per pipeline run and suite (Great Expectations results).
select
    run_id,
    suite,
    min(validated_at)                                       as validated_at,
    count(distinct batch_id)                                as batches,
    count(*)                                                as checks,
    sum(case when success then 1 else 0 end)                as checks_passed,
    avg(case when success then 1.0 else 0.0 end)            as pass_rate,
    coalesce(sum(unexpected_count), 0)                      as unexpected_values,
    string_agg(distinct case when not success then expectation || '(' || "column" || ')' end, '; ')
                                                            as failed_checks
from {{ source('meta', 'dq_results') }}
group by all

select
    source_period,
    source_file,
    status,
    orig_rows,
    perf_rows,
    file_bytes,
    batch_id,
    run_id,
    started_at,
    finished_at,
    datediff('second', started_at, finished_at)             as duration_seconds,
    message
from {{ source('meta', 'ingest_log') }}

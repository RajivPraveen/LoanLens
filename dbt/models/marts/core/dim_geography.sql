select
    state_code,
    state_name,
    census_region,
    census_division,
    judicial_foreclosure,
    population_millions
from {{ ref('state_reference') }}

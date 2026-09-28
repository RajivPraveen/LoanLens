"""Freddie Mac Single-Family Loan-Level Dataset file layouts.

Both files are pipe-delimited text with no header. Two layouts are supported and detected
per file from the number of fields:

* **Release 47 (July 2026 onward)** - 31 origination fields, 35 monthly performance fields.
  Servicer name and the MI cancellation indicator moved to the monthly file, VantageScore 4.0
  was added, loan identifiers are ``PYYQnXXXXXXX``, and the sign convention for loss fields
  flipped: actual loss / expenses are positive, recoveries and gains negative.
  (Freddie Mac "SFLLD Disclosure Changes Effective July 2026" and file_layout_july_2026.xlsx.)
* **Legacy (before Release 47)** - 32 origination fields (older releases have fewer trailing
  columns and are padded), 32 or fewer performance fields; losses reported as negatives.

The raw warehouse tables hold the union of both layouts plus ``layout_version``; dbt staging
normalises sign conventions and codes so everything downstream is layout-independent.
Sentinel codes such as 9999 (credit score not available) are kept as-is in ``raw``.
"""

from __future__ import annotations

import re

R47, LEGACY = "r47", "legacy"

ORIGINATION_LAYOUT: list[tuple[str, str]] = [  # Release 47, 31 fields
    ("credit_score", "INTEGER"),              # Classic FICO
    ("first_payment_date", "VARCHAR"),
    ("first_time_homebuyer_flag", "VARCHAR"),
    ("maturity_date", "VARCHAR"),
    ("msa", "VARCHAR"),
    ("mi_pct", "INTEGER"),
    ("num_units", "INTEGER"),
    ("occupancy_status", "VARCHAR"),
    ("orig_cltv", "INTEGER"),
    ("orig_dti", "INTEGER"),
    ("orig_upb", "DOUBLE"),
    ("orig_ltv", "INTEGER"),
    ("orig_interest_rate", "DOUBLE"),
    ("channel", "VARCHAR"),
    ("ppm_flag", "VARCHAR"),
    ("amortization_type", "VARCHAR"),
    ("property_state", "VARCHAR"),
    ("property_type", "VARCHAR"),
    ("postal_code", "VARCHAR"),
    ("loan_sequence_number", "VARCHAR"),      # Loan Identifier
    ("loan_purpose", "VARCHAR"),
    ("orig_loan_term", "INTEGER"),
    ("num_borrowers", "INTEGER"),
    ("seller_name", "VARCHAR"),
    ("super_conforming_flag", "VARCHAR"),
    ("pre_harp_loan_sequence_number", "VARCHAR"),
    ("program_indicator", "VARCHAR"),         # Special Eligibility Program
    ("harp_indicator", "VARCHAR"),
    ("property_valuation_method", "VARCHAR"),
    ("interest_only_indicator", "VARCHAR"),
    ("vantage_score", "INTEGER"),             # VantageScore 4.0
]

ORIGINATION_LAYOUT_LEGACY: list[tuple[str, str]] = [  # before Release 47, 32 fields
    *ORIGINATION_LAYOUT[:24],
    ("servicer_name", "VARCHAR"),
    *ORIGINATION_LAYOUT[24:30],
    ("mi_cancellation_indicator", "VARCHAR"),
]

PERFORMANCE_LAYOUT_LEGACY: list[tuple[str, str]] = [  # before Release 47, up to 32 fields
    ("loan_sequence_number", "VARCHAR"),
    ("monthly_reporting_period", "VARCHAR"),
    ("current_actual_upb", "DOUBLE"),
    ("current_loan_delinquency_status", "VARCHAR"),
    ("loan_age", "INTEGER"),
    ("remaining_months_to_legal_maturity", "INTEGER"),
    ("defect_settlement_date", "VARCHAR"),
    ("modification_flag", "VARCHAR"),
    ("zero_balance_code", "VARCHAR"),
    ("zero_balance_effective_date", "VARCHAR"),
    ("current_interest_rate", "DOUBLE"),
    ("current_non_interest_bearing_upb", "DOUBLE"),
    ("ddlpi", "VARCHAR"),
    ("mi_recoveries", "DOUBLE"),
    ("net_sale_proceeds", "VARCHAR"),  # numeric, or 'C' (covered) / 'U' (unknown)
    ("non_mi_recoveries", "DOUBLE"),
    ("total_expenses", "DOUBLE"),
    ("legal_costs", "DOUBLE"),
    ("maintenance_preservation_costs", "DOUBLE"),
    ("taxes_insurance", "DOUBLE"),
    ("miscellaneous_expenses", "DOUBLE"),
    ("actual_loss", "DOUBLE"),
    ("cumulative_modification_cost", "DOUBLE"),
    ("step_modification_flag", "VARCHAR"),     # Interest Rate Step Indicator
    ("payment_deferral", "VARCHAR"),           # Payment Deferral Flag (R47: C/P)
    ("estimated_ltv", "INTEGER"),
    ("zero_balance_removal_upb", "DOUBLE"),
    ("delinquent_accrued_interest", "DOUBLE"),
    ("delinquency_due_to_disaster", "VARCHAR"),
    ("borrower_assistance_status_code", "VARCHAR"),  # Borrower Assistance Plan
    ("current_month_modification_cost", "DOUBLE"),
    ("interest_bearing_upb", "DOUBLE"),
]

PERFORMANCE_LAYOUT: list[tuple[str, str]] = [  # Release 47, 35 fields
    *PERFORMANCE_LAYOUT_LEGACY,
    ("mi_cancellation_indicator", "VARCHAR"),
    ("servicer_name", "VARCHAR"),
    ("bankruptcy_cramdown_costs", "DOUBLE"),
]

LAYOUTS = {
    ("origination", R47): ORIGINATION_LAYOUT,
    ("origination", LEGACY): ORIGINATION_LAYOUT_LEGACY,
    ("performance", R47): PERFORMANCE_LAYOUT,
    ("performance", LEGACY): PERFORMANCE_LAYOUT_LEGACY,
}


def _union(*layouts) -> list[tuple[str, str]]:
    seen, out = set(), []
    for layout in layouts:
        for name, dtype in layout:
            if name not in seen:
                seen.add(name)
                out.append((name, dtype))
    return out


# Warehouse raw tables: union of both layouts (fields missing from a file's layout are NULL).
RAW_ORIGINATION = _union(ORIGINATION_LAYOUT, ORIGINATION_LAYOUT_LEGACY)
RAW_PERFORMANCE = _union(PERFORMANCE_LAYOUT, PERFORMANCE_LAYOUT_LEGACY)
ORIGINATION_COLUMNS = [name for name, _ in ORIGINATION_LAYOUT]
PERFORMANCE_COLUMNS = [name for name, _ in PERFORMANCE_LAYOUT]


def detect_layout(kind: str, first_line: str) -> str:
    """Release 47 files have 31 (origination) / 35 (performance) fields; anything else is legacy."""
    n = first_line.rstrip("\r\n").count("|") + 1
    if kind == "origination":
        if n == 31:
            return R47
        if n <= 32:
            return LEGACY
    else:
        if n == 35:
            return R47
        if n <= 32:
            return LEGACY
    raise ValueError(f"Unrecognised {kind} layout: {n} fields per line")


# Columns appended by the loader to every raw row.
LOAD_METADATA = [
    ("source_period", "VARCHAR"),
    ("source_file", "VARCHAR"),
    ("layout_version", "VARCHAR"),
    ("batch_id", "VARCHAR"),
    ("loaded_at", "TIMESTAMP"),
]

VALID_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "PR", "GU", "VI",
}

# Zero balance codes: 01 prepaid or matured, 02 third-party sale, 03 short sale / charge-off,
# 06 repurchase (legacy), 09 REO disposition, 15 whole loan sale, 16 reperforming loan
# securitization, 96 defect prior to a credit event.
ZERO_BALANCE_CODES = {"01", "02", "03", "06", "09", "15", "16", "96"}
CREDIT_EVENT_ZB_CODES = {"02", "03", "09", "15", "16"}


def raw_table_ddl(table: str, layout: list[tuple[str, str]]) -> str:
    cols = ",\n    ".join(f"{name} {dtype}" for name, dtype in [*layout, *LOAD_METADATA])
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {cols}\n)"


# (regex on a file/member name, kind). The captured group is the source period.
MEMBER_PATTERNS = [
    (re.compile(r"(?:^|/)orig_(\d{4}Q[1-4])\.txt$", re.I), "origination"),
    (re.compile(r"(?:^|/)perf_(\d{4}Q[1-4])\.txt$", re.I), "performance"),
    (re.compile(r"(?:^|/)historical_data_time_(\d{4}Q[1-4])\.txt$", re.I), "performance"),
    (re.compile(r"(?:^|/)historical_data_(\d{4}Q[1-4])\.txt$", re.I), "origination"),
    (re.compile(r"(?:^|/)sample_orig_(\d{4})\.txt$", re.I), "origination"),
    (re.compile(r"(?:^|/)sample_(?:svcg|perf)_(\d{4})\.txt$", re.I), "performance"),
]


def classify(name: str) -> tuple[str, str] | None:
    """(period, kind) for a recognised file or zip-member name."""
    for pattern, kind in MEMBER_PATTERNS:
        m = pattern.search(name)
        if m:
            return m.group(1).upper(), kind
    return None

"""Great Expectations suites that gate every batch before it reaches the warehouse.

Validation runs on a *validation view* of each batch in which documented Freddie Mac
sentinel codes (e.g. credit score 9999 = not available) are nulled, so the range checks only
see real values. The raw table keeps the file's values untouched. Ranges and code lists follow
the official enumerations (SFLLD file layout and disclosure notes, Release 47).
"""

from __future__ import annotations

import great_expectations.expectations as gxe

from loanlens.schemas import VALID_STATES, ZERO_BALANCE_CODES

YYYYMM = r"^(19|20)\d{2}(0[1-9]|1[0-2])$"

# column -> sentinel values meaning "not available"
ORIGINATION_SENTINELS = {
    "credit_score": [9999],
    "vantage_score": [9999],
    "mi_pct": [999],
    "orig_dti": [999],
    "orig_ltv": [999],
    "orig_cltv": [999],
    "num_units": [99],
    "num_borrowers": [99],
}
PERFORMANCE_SENTINELS = {"estimated_ltv": [999, 9999]}


def origination_suite() -> list:
    return [
        gxe.ExpectTableRowCountToBeBetween(min_value=1),
        *[gxe.ExpectColumnValuesToNotBeNull(column=c) for c in (
            "loan_sequence_number", "first_payment_date", "orig_upb", "orig_interest_rate",
            "property_state", "orig_loan_term")],
        gxe.ExpectColumnValuesToBeUnique(column="loan_sequence_number"),
        gxe.ExpectColumnValuesToMatchRegex(column="loan_sequence_number", regex=r"^[A-Z0-9]{12}$"),
        gxe.ExpectColumnValuesToMatchRegex(column="first_payment_date", regex=YYYYMM),
        gxe.ExpectColumnValuesToBeBetween(column="credit_score", min_value=300, max_value=850),
        gxe.ExpectColumnValuesToBeBetween(column="vantage_score", min_value=300, max_value=850),
        # 6-105% (LTV) / 6-200% (CLTV) up to 2018Q1; 1-998% afterwards and for HARP loans
        gxe.ExpectColumnValuesToBeBetween(column="orig_ltv", min_value=1, max_value=998),
        gxe.ExpectColumnValuesToBeBetween(column="orig_cltv", min_value=1, max_value=998),
        gxe.ExpectColumnValuesToBeBetween(column="orig_dti", min_value=0, max_value=65),
        gxe.ExpectColumnValuesToBeBetween(column="orig_interest_rate", min_value=0.5, max_value=20),
        gxe.ExpectColumnValuesToBeBetween(column="orig_upb", min_value=1_000, max_value=5_000_000),
        # observed 30-513 months in the 1999-2026 files (a few short balloon-like terms)
        gxe.ExpectColumnValuesToBeBetween(column="orig_loan_term", min_value=12, max_value=600),
        gxe.ExpectColumnValuesToBeBetween(column="mi_pct", min_value=0, max_value=55),
        gxe.ExpectColumnValuesToBeBetween(column="num_units", min_value=1, max_value=4),
        gxe.ExpectColumnValuesToBeBetween(column="num_borrowers", min_value=1, max_value=10),
        gxe.ExpectColumnValuesToBeInSet(column="property_state", value_set=sorted(VALID_STATES)),
        gxe.ExpectColumnValuesToBeInSet(column="loan_purpose", value_set=["P", "C", "N", "R", "9"]),
        gxe.ExpectColumnValuesToBeInSet(column="occupancy_status", value_set=["P", "I", "S", "9"]),
        gxe.ExpectColumnValuesToBeInSet(column="channel", value_set=["R", "B", "C", "T", "9"]),
        gxe.ExpectColumnValuesToBeInSet(column="first_time_homebuyer_flag", value_set=["Y", "N", "9"]),
        gxe.ExpectColumnValuesToBeInSet(column="amortization_type", value_set=["FRM", "ARM"]),
        gxe.ExpectColumnValuesToBeInSet(column="property_type", value_set=["SF", "CO", "PU", "MH", "CP", "99"]),
        # first payment 0-15 months after the start of the file's origination period
        gxe.ExpectColumnValuesToBeInSet(column="_period_matches_file", value_set=[True], mostly=0.98),
        gxe.ExpectColumnValuesToBeBetween(column="_parse_errors", max_value=0),
    ]


def performance_suite() -> list:
    return [
        gxe.ExpectTableRowCountToBeBetween(min_value=1),
        *[gxe.ExpectColumnValuesToNotBeNull(column=c) for c in (
            "loan_sequence_number", "monthly_reporting_period",
            "current_loan_delinquency_status", "loan_age")],
        gxe.ExpectCompoundColumnsToBeUnique(
            column_list=["loan_sequence_number", "monthly_reporting_period"]),
        gxe.ExpectColumnValuesToMatchRegex(column="monthly_reporting_period", regex=YYYYMM),
        gxe.ExpectColumnValuesToMatchRegex(column="current_loan_delinquency_status",
                                           regex=r"^(\d{1,3}|RA|XX)$"),
        gxe.ExpectColumnValuesToBeBetween(column="current_actual_upb", min_value=0, max_value=5_000_000),
        # a handful of mis-keyed rates (30-50%, reporting months 2017-03/04) are tolerated here
        # and nulled in dbt staging; more than 1 in 10,000 fails the batch
        gxe.ExpectColumnValuesToBeBetween(column="current_interest_rate", min_value=0, max_value=20,
                                          mostly=0.9999),
        gxe.ExpectColumnValuesToBeBetween(column="loan_age", min_value=-12, max_value=600),
        gxe.ExpectColumnValuesToBeBetween(column="estimated_ltv", min_value=0, max_value=998),
        gxe.ExpectColumnValuesToBeBetween(column="actual_loss", min_value=-5_000_000, max_value=5_000_000),
        gxe.ExpectColumnValuesToBeInSet(column="zero_balance_code", value_set=sorted(ZERO_BALANCE_CODES)),
        gxe.ExpectColumnValuesToBeInSet(column="modification_flag", value_set=["Y", "P"]),
        gxe.ExpectColumnValuesToBeInSet(column="borrower_assistance_status_code",
                                        value_set=["F", "R", "T", "7", "9"]),
        # Referential integrity: every performance record belongs to a loan in the batch.
        gxe.ExpectColumnValuesToBeInSet(column="_has_origination", value_set=[True]),
        gxe.ExpectColumnValuesToBeBetween(column="_parse_errors", max_value=0),
    ]


def fred_suite() -> list:
    return [
        gxe.ExpectTableRowCountToBeBetween(min_value=1),
        gxe.ExpectColumnValuesToNotBeNull(column="series_id"),
        gxe.ExpectColumnValuesToNotBeNull(column="observation_date"),
        gxe.ExpectColumnValuesToNotBeNull(column="value", mostly=0.99),
        gxe.ExpectCompoundColumnsToBeUnique(column_list=["series_id", "observation_date"]),
        gxe.ExpectColumnValuesToBeInSet(column="measure",
                                        value_set=["unemployment_rate", "hpi", "mortgage_rate_30y"]),
        gxe.ExpectColumnValuesToBeBetween(column="_rate_value", min_value=0, max_value=40),
        gxe.ExpectColumnValuesToBeBetween(column="_hpi_value", min_value=1),
    ]


SUITES = {
    "freddie_origination": origination_suite,
    "freddie_performance": performance_suite,
    "fred_observations": fred_suite,
}

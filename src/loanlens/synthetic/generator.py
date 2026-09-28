"""Synthetic loan-level data in the Freddie Mac Single-Family Loan-Level Dataset layout.

The real dataset requires a (free) registration, so the platform ships with a generator that
writes files in the current Release 47 layout (``historical_data_YYYYQn.zip`` holding
``orig_YYYYQn.txt`` and ``perf_YYYYQn.txt``: 31 and 35 pipe-delimited fields). Replace the files in ``data/raw/freddie`` with the real
downloads and every downstream step runs unchanged.

Loan behaviour is simulated month by month with a delinquency state machine whose transition
hazards depend on borrower attributes and on the *real* FRED history for the loan's state:
unemployment level and change, house-price moves (mark-to-market LTV) and the refinance
incentive against the 30-year mortgage rate. Policy periods are modelled explicitly: loss
mitigation modifications after 2009 (HAMP), the foreclosure backlog of 2009-2013, and COVID
forbearance with a foreclosure moratorium in 2020-2021. The coefficients are hand-set to give
realistic magnitudes; they are documented in docs/assumptions.md, not estimated.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from loanlens.config import Settings
from loanlens.ingest.fred import load_observations, monthly_panel
from loanlens.reference import (
    index_to_yyyymm,
    month_index,
    parse_quarter,
    quarter_range,
    resolve_states,
    state_reference,
)
from loanlens.schemas import ORIGINATION_COLUMNS, PERFORMANCE_COLUMNS

log = logging.getLogger(__name__)

# Baseline one-unit conforming loan limits (FHFA).
CONFORMING_LIMITS = {
    1999: 240_000, 2000: 252_700, 2001: 275_000, 2002: 300_700, 2003: 322_700, 2004: 333_700,
    2005: 359_650, 2006: 417_000, 2017: 424_100, 2018: 453_100, 2019: 484_350, 2020: 510_400,
    2021: 548_250, 2022: 647_200, 2023: 726_200, 2024: 766_550, 2025: 806_500,
}
SELLERS = ["OTHER"] * 6 + [
    "Cardinal Home Lending", "Harborview Mortgage", "Summit Point Bank", "Northgate Financial",
    "Prairie State Mortgage", "Bayline Bank, N.A.", "Keystone Residential",
]
SERVICERS = ["OTHER"] * 5 + [
    "Cardinal Home Lending", "Summit Point Bank", "Lakeshore Servicing", "Bayline Bank, N.A.",
]

COVID_START, COVID_END = month_index("2020-03"), month_index("2021-12")
FORBEARANCE_ENTRY_END = month_index("2021-03")
MORATORIUM_START, MORATORIUM_END = month_index("2020-03"), month_index("2021-07")
HAMP_START = month_index("2009-04")
BACKLOG_START, BACKLOG_END = month_index("2009-01"), month_index("2013-12")
HARP_START, HARP_END = month_index("2009-04"), month_index("2018-12")
RPL_SALES_START = month_index("2014-01")


def conforming_limit(year: int) -> float:
    return CONFORMING_LIMITS[max(y for y in CONFORMING_LIMITS if y <= year)]


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def scheduled_balance(upb, annual_rate, term, paid):
    """Remaining balance of a fully amortising loan after ``paid`` payments."""
    r = annual_rate / 1200.0
    growth_n = (1.0 + r) ** term
    growth_k = (1.0 + r) ** np.minimum(paid, term)
    return upb * (growth_n - growth_k) / (growth_n - 1.0)


@dataclass
class Macro:
    start_idx: int
    states: list[str]
    ur: np.ndarray            # [state, month] unemployment rate, %
    hpi: np.ndarray           # [state, month] FHFA all-transactions HPI
    mortgage_rate: np.ndarray # [month] Freddie Mac PMMS 30y, %
    nat_hpi: np.ndarray       # [month]

    def col(self, idx):
        return idx - self.start_idx


def build_macro(settings: Settings) -> Macro:
    syn = settings["synthetic"]
    states = resolve_states(syn.get("states", "all"))
    panel = monthly_panel(load_observations(settings), syn["performance_end"])
    months = pd.period_range("1990-01", syn["performance_end"], freq="M")

    def matrix(geo_list, measure):
        wide = panel.pivot_table(index="month", columns="geo", values=measure)
        wide = wide.reindex(index=months).ffill().bfill()
        return wide[geo_list].to_numpy().T

    return Macro(
        start_idx=month_index(str(months[0])),
        states=states,
        ur=matrix(states, "unemployment_rate"),
        hpi=matrix(states, "hpi"),
        mortgage_rate=matrix(["US"], "mortgage_rate_30y")[0],
        nat_hpi=matrix(["US"], "hpi")[0],
    )


def era_params(year: int) -> dict:
    """Underwriting regime by origination year (score mix, DTI caps, layered risk)."""
    if year <= 2003:
        return dict(fico_mu=716, fico_sd=50, tail=0.07, dti_mu=34, dti_sd=10, dti_cap=64,
                    hi_ltv=0.20, piggy=0.05, inv=0.07, broker=0.25, shift=-0.40)
    if year == 2004:
        return dict(fico_mu=718, fico_sd=51, tail=0.08, dti_mu=36, dti_sd=11, dti_cap=64,
                    hi_ltv=0.22, piggy=0.12, inv=0.09, broker=0.27, shift=-0.10)
    if year <= 2007:
        return dict(fico_mu=716, fico_sd=53, tail=0.10, dti_mu=37, dti_sd=11, dti_cap=64,
                    hi_ltv=0.25, piggy=0.20, inv=0.11, broker=0.28, shift=0.20)
    if year == 2008:
        return dict(fico_mu=738, fico_sd=46, tail=0.05, dti_mu=36, dti_sd=10, dti_cap=60,
                    hi_ltv=0.15, piggy=0.04, inv=0.08, broker=0.18, shift=0.15)
    if year <= 2012:
        return dict(fico_mu=764, fico_sd=36, tail=0.01, dti_mu=32, dti_sd=9, dti_cap=50,
                    hi_ltv=0.07, piggy=0.01, inv=0.06, broker=0.10, shift=-0.60)
    if year <= 2019:
        return dict(fico_mu=752, fico_sd=41, tail=0.02, dti_mu=34, dti_sd=9, dti_cap=50,
                    hi_ltv=0.17, piggy=0.02, inv=0.07, broker=0.12, shift=-0.15)
    return dict(fico_mu=756, fico_sd=41, tail=0.02, dti_mu=35, dti_sd=9, dti_cap=50,
                hi_ltv=0.20, piggy=0.02, inv=0.06, broker=0.13, shift=-0.05)


def _origination(rng, quarter: str, n: int, macro: Macro) -> dict[str, np.ndarray]:
    year, q = parse_quarter(quarter)
    era = era_params(year)
    ref = state_reference().set_index("state_code").loc[macro.states]
    weights = ref["population_millions"].to_numpy()
    state_idx = rng.choice(len(macro.states), n, p=weights / weights.sum())

    orig_m = month_index(f"{year}-{3 * (q - 1) + 1:02d}") + rng.integers(0, 3, n)
    fp_m = orig_m + rng.choice([1, 2], n, p=[0.3, 0.7])
    c0 = macro.col(month_index(f"{year}-{3 * (q - 1) + 1:02d}"))
    mkt_now = macro.mortgage_rate[c0]
    mkt_36 = macro.mortgage_rate[max(c0 - 36, 0):c0].mean()
    refi_share = 0.12 + 0.72 * sigmoid(1.6 * (mkt_36 - mkt_now) + 0.2)

    purpose = np.where(rng.random(n) < refi_share,
                       np.where(rng.random(n) < 0.45, "C", "N"), "P")
    is_purchase = purpose == "P"

    # Credit score: era distribution plus a thin near-prime tail.
    fico = rng.normal(era["fico_mu"], era["fico_sd"], n)
    tail = rng.random(n) < era["tail"]
    fico[tail] = rng.normal(640, 35, tail.sum())
    fico = np.clip(np.round(fico), 520, 840).astype(int)

    # LTV: purchase mixture with point masses at 80/90/95/97; refinances are lower.
    comps = np.array([0.28, 0.30, 0.10, 0.12, era["hi_ltv"] * 0.6, era["hi_ltv"] * 0.4])
    comp = rng.choice(6, n, p=comps / comps.sum())
    ltv_p = np.select(
        [comp == 0, comp == 1, comp == 2, comp == 3, comp == 4],
        [rng.uniform(35, 79, n), 80, rng.integers(81, 90, n), 90, 95], 97,
    )
    cash_cap = 85 if year < 2009 else 80
    ltv_r = 20 + 75 * rng.beta(5, 2.2, n)
    ltv_r = np.where(purpose == "C", np.minimum(ltv_r, cash_cap), ltv_r)
    ltv = np.round(np.where(is_purchase, ltv_p, ltv_r)).astype(int)
    cltv = ltv.copy()
    piggy = is_purchase & (ltv == 80) & (rng.random(n) < era["piggy"])
    cltv[piggy] = rng.choice([90, 95, 100], piggy.sum(), p=[0.4, 0.3, 0.3])
    mi_pct = np.select([ltv <= 80, ltv <= 85, ltv <= 90, ltv <= 95], [0, 12, 25, 30], 35)

    dti = np.clip(np.round(rng.normal(era["dti_mu"], era["dti_sd"], n)), 5, era["dti_cap"]).astype(int)

    inv = rng.random(n) < era["inv"]
    second = ~inv & (rng.random(n) < 0.045)
    occupancy = np.where(inv, "I", np.where(second, "S", "P"))
    units = np.where(inv & (rng.random(n) < 0.12), rng.choice([2, 3, 4], n, p=[0.6, 0.2, 0.2]),
                     np.where(rng.random(n) < 0.015, 2, 1))
    prop_type = rng.choice(["SF", "PU", "CO", "MH"], n, p=[0.68, 0.21, 0.10, 0.01])
    tpo = rng.random(n)
    channel = np.where(tpo < era["broker"], "B", np.where(tpo < era["broker"] + 0.37, "C", "R"))
    if year >= 2020:
        channel = np.where(rng.random(n) < 0.03, "T", channel)
    fthb_rate = 0.45 if year >= 2020 else 0.33
    fthb = np.where(is_purchase, np.where(rng.random(n) < fthb_rate, "Y", "N"), "9")
    n_borrowers = np.where(rng.random(n) < (0.55 if year < 2010 else 0.47), 2, 1)
    term = np.where(
        is_purchase,
        rng.choice([360, 180, 240], n, p=[0.90, 0.08, 0.02]),
        rng.choice([360, 180, 240], n, p=[0.70, 0.25, 0.05]),
    )

    # Balance scales with national house prices and the state's price level.
    price = ref["price_factor"].to_numpy()[state_idx]
    median = 150_000 * macro.nat_hpi[c0] / macro.nat_hpi[macro.col(month_index("2000-01"))]
    upb = median * price * np.exp(rng.normal(0, 0.45, n)) * np.where(is_purchase, 1.0, 0.92)
    limit = conforming_limit(year)
    high_cost = (price >= 1.3) & (year >= 2008)
    upb = np.clip(upb, 25_000, np.where(high_cost, limit * 1.5, limit))
    upb = np.round(upb / 1000) * 1000
    super_conf = np.where(upb > limit, "Y", "")

    spread = (0.10 + 0.004 * (740 - fico) + 0.15 * (ltv > 80) + 0.40 * inv
              + 0.12 * (purpose == "C") - 0.55 * (term == 180) - 0.30 * (term == 240)
              + rng.normal(0, 0.18, n))
    mkt_orig = macro.mortgage_rate[macro.col(orig_m)]
    rate = np.maximum(np.round((mkt_orig + spread) / 0.125) * 0.125, 1.75)

    # Unobserved risk (e.g. income documentation quality) - makes the models imperfect.
    frailty = rng.normal(0, 0.45, n) + era["shift"]

    missing_fico = rng.random(n) < 0.003
    missing_dti = rng.random(n) < (0.03 if year < 2010 else 0.01)
    zip3 = ref["zip3"].to_numpy()[state_idx].astype(int) + rng.integers(0, 6, n)
    seq = np.arange(1, n + 1)

    return dict(
        loan_id=np.array([f"F{year % 100:02d}Q{q}{s:07d}" for s in seq]),  # PYYQnXXXXXXX
        state_idx=state_idx, orig_m=orig_m, fp_m=fp_m, maturity_m=fp_m + term - 1,
        fico=fico, fico_reported=np.where(missing_fico, 9999, fico),
        ltv=ltv, cltv=cltv, mi_pct=mi_pct, dti=dti, dti_reported=np.where(missing_dti, 999, dti),
        upb=upb, rate=rate, term=term, purpose=purpose, occupancy=occupancy, units=units,
        prop_type=prop_type, channel=channel, fthb=fthb, n_borrowers=n_borrowers,
        super_conf=super_conf, frailty=frailty, zip3=zip3,
        judicial=ref["judicial_foreclosure"].to_numpy()[state_idx].astype(bool),
        seller=rng.choice(SELLERS, n), servicer=rng.choice(SERVICERS, n),
    )


def _loss_fields(bal, months_dq, rate, loan, idx, hpi_ratio, code, m):
    """Liquidation cash flows in the Release 47 sign convention: losses and expenses are
    positive, recoveries and proceeds negative."""
    value = loan["upb"][idx] / (loan["ltv"][idx] / 100.0) * hpi_ratio
    crisis = BACKLOG_START <= m <= BACKLOG_END
    discount = np.select([code == "09", code == "03"], [0.36 if crisis else 0.28, 0.10], 0.03)
    proceeds = value * (1 - discount)
    proceeds = np.where(code == "03", np.minimum(proceeds, 0.95 * bal), proceeds)
    proceeds = np.where(code == "15", 0.70 * bal, np.where(code == "16", 0.88 * bal, proceeds))
    proceeds = np.minimum(proceeds, 1.02 * bal)
    accrued = bal * rate / 1200.0 * months_dq
    note_sale = np.isin(code, ["15", "16"])
    expenses = bal * np.where(note_sale, 0.005, 0.025 + 0.0035 * months_dq)
    gross = bal + accrued + expenses - proceeds
    mi = np.minimum(loan["mi_pct"][idx] / 100.0 * (bal + accrued), np.maximum(gross, 0))
    non_mi = 0.02 * np.maximum(gross - mi, 0)
    net_loss = gross - mi - non_mi
    return dict(
        mi_recoveries=-mi, net_sale_proceeds=-proceeds, non_mi_recoveries=-non_mi,
        total_expenses=expenses, legal_costs=0.35 * expenses,
        maintenance_preservation_costs=0.25 * expenses, taxes_insurance=0.35 * expenses,
        miscellaneous_expenses=0.05 * expenses, actual_loss=net_loss,
        delinquent_accrued_interest=accrued,
    )


def _simulate(rng, loan: dict, macro: Macro, end_m: int) -> pd.DataFrame:
    n = len(loan["loan_id"])
    si = loan["state_idx"]
    hpi_orig = macro.hpi[si, macro.col(loan["orig_m"])]
    inv = loan["occupancy"] == "I"
    cashout = loan["purpose"] == "C"
    broker = loan["channel"] == "B"
    short_term = loan["term"] <= 180
    single = loan["n_borrowers"] == 1

    dq = np.zeros(n, int)
    alive = np.ones(n, bool)
    reo = np.zeros(n, bool)
    reo_dq = np.zeros(n, int)
    reo_bal = np.zeros(n)
    modified = np.zeros(n, bool)
    mod_m = np.full(n, -1)
    forb = np.zeros(n, bool)
    deferred = np.zeros(n, bool)   # ever had a payment deferral
    impaired = np.zeros(n, bool)   # ever 60+ days late: shut out of refinancing
    burnout = np.zeros(n)          # months spent with a sizeable refinance incentive
    cur_rate = loan["rate"].astype(float).copy()

    records: list[dict] = []
    for m in range(int(loan["fp_m"].min()), end_m + 1):
        act = alive & (loan["fp_m"] <= m)
        if not act.any():
            if not alive.any():
                break
            continue
        c = macro.col(m)
        age = m - loan["fp_m"] + 1
        # End of COVID forbearance: most borrowers exit through a payment deferral
        # (missed payments moved to the end of the loan); the rest stay delinquent.
        deferral = np.zeros(n, bool)
        if m == COVID_END + 1:
            deferral = act & forb & (rng.random(n) < 0.7)
            dq[deferral] = 0
            deferred |= deferral
            forb[:] = False
        ur = macro.ur[si, c]
        ur12 = ur - macro.ur[si, c - 12]
        hpi_ratio = macro.hpi[si, c] / hpi_orig
        mkt = macro.mortgage_rate[c]
        paid_before = np.maximum(age - 1 - dq, 0)
        bal = scheduled_balance(loan["upb"], loan["rate"], loan["term"], paid_before)
        bal = np.where(reo, reo_bal, bal)
        mtm = loan["ltv"] / 100.0 * (bal / loan["upb"]) / hpi_ratio
        season = np.exp(-age / 8.0)

        x_roll = (-6.1 + 0.019 * (720 - loan["fico"]) + 2.0 * np.maximum(mtm - 0.8, 0)
                  + 1.8 * np.maximum(mtm - 1.0, 0) + 0.035 * (loan["dti"] - 35)
                  + 0.02 * (loan["cltv"] - loan["ltv"])
                  + 0.11 * (ur - 5.5) + 0.20 * np.clip(ur12, 0, 6) + 0.35 * inv + 0.20 * single
                  + 0.15 * cashout + 0.15 * broker - 0.30 * short_term + loan["frailty"]
                  - 1.2 * season + 0.8 * modified)
        underwater_pen = 1.25 if HARP_START <= m <= HARP_END else 2.5
        raw_incentive = cur_rate - mkt
        burnout += raw_incentive > 0.75
        incentive = np.clip(raw_incentive, -1.0, 2.0)
        tight_credit = 1.0 if month_index("2008-06") <= m <= month_index("2013-12") else 0.0
        x_prep = (-4.9 + 1.3 * incentive + 0.5 * np.log(loan["upb"] / 200_000)
                  + 0.004 * (loan["fico"] - 720) - underwater_pen * np.maximum(mtm - 0.9, 0)
                  - np.minimum(0.02 * burnout, 0.8) - 1.0 * modified - 1.2 * impaired
                  - 0.6 * tight_credit * (loan["fico"] < 700) - 1.5 * np.exp(-age / 6.0))
        p_roll, p_prep = sigmoid(x_roll), sigmoid(x_prep)

        u = rng.random(n)
        new_dq = dq.copy()
        zb = np.full(n, "", dtype=object)

        # --- current loans -------------------------------------------------------------
        cur = act & ~reo & (dq == 0)
        repurchase = cur & (age <= 36) & (u < 0.00015)
        prepay = cur & ~repurchase & (u < p_prep)
        roll = cur & ~repurchase & ~prepay & (u < p_prep + p_roll)
        zb[repurchase] = "06"
        zb[prepay] = "01"
        new_dq[roll] = 1
        in_covid = COVID_START <= m <= COVID_END
        forb |= roll & (COVID_START <= m <= FORBEARANCE_ENTRY_END)

        # --- delinquent loans ----------------------------------------------------------
        dql = act & ~reo & (dq >= 1)
        k = dq
        underwater = np.maximum(mtm - 1.0, 0)
        cure_base = np.select([k == 1, k == 2, k <= 5], [0.60, 0.35, 0.10], 0.02)
        p_cure = cure_base * np.exp(-0.9 * underwater - 0.08 * np.maximum(ur - 6, 0)
                                    + 0.005 * (loan["fico"] - 720))
        p_cure = np.where(forb & (m >= month_index("2020-07")), 0.12, p_cure)
        p_stay = np.where(forb, 0.0, np.select([k == 1, k == 2], [0.20, 0.15], 0.10))
        mod_rate = 0.025 if HAMP_START <= m <= month_index("2014-12") else (0.015 if m > HAMP_START else 0.0)
        p_mod = np.where((k >= 3) & ~forb, mod_rate, 0.0)
        in_moratorium = MORATORIUM_START <= m <= MORATORIUM_END
        liq_base = np.where(loan["judicial"], 0.05, 0.09)
        if BACKLOG_START <= m <= BACKLOG_END:
            liq_base = liq_base * 0.55
        p_liq = np.where((k >= 6) & ~in_moratorium, liq_base, 0.0)
        p_dq_prepay = np.where(k <= 2, 0.3 * p_prep, 0.0)
        edges = np.cumsum([p_cure, p_stay, p_mod, p_liq, p_dq_prepay], axis=0)
        cured = dql & (u < edges[0])
        stayed = dql & ~cured & (u < edges[1])
        modded = dql & ~cured & ~stayed & (u < edges[2])
        liquid = dql & ~cured & ~stayed & ~modded & (u < edges[3])
        dq_prepay = dql & ~cured & ~stayed & ~modded & ~liquid & (u < edges[4])
        rolled = dql & ~(cured | stayed | modded | liquid | dq_prepay)
        new_dq[cured | modded] = 0
        new_dq[rolled] = dq[rolled] + 1
        impaired |= new_dq >= 2
        zb[dq_prepay] = "01"
        modified |= modded
        mod_m[modded] = m
        cur_rate[modded] = np.maximum(cur_rate[modded] - 2.0, 2.0)
        forb &= ~cured

        # Liquidation route: REO (then disposition), short sale, third-party or note sale.
        u2 = rng.random(n)
        p_short = 0.20 + 0.20 * (mtm > 1.0)
        p_note = 0.10 if m >= RPL_SALES_START else 0.0
        route = np.where(u2 < p_short, "03", np.where(u2 < p_short + 0.12, "02",
                         np.where(u2 < p_short + 0.12 + p_note,
                                  np.where(rng.random(n) < 0.5, "15", "16"), "REO")))
        to_reo = liquid & (route == "REO")
        direct = liquid & (route != "REO")
        zb[direct] = route[direct]
        reo_dq[to_reo] = dq[to_reo]
        reo_bal[to_reo] = bal[to_reo]
        dispose = act & reo & (u < 0.2)
        zb[dispose] = "09"

        # --- maturity ------------------------------------------------------------------
        paid_now = np.maximum(age - new_dq, 0)
        matured = act & ~reo & (zb == "") & (new_dq == 0) & (paid_now >= loan["term"])
        zb[matured] = "01"
        if not in_covid:
            forb &= new_dq > 0
        if m > COVID_END:
            forb[:] = False

        reo |= to_reo
        terminated = act & (zb != "")
        rec_idx = np.flatnonzero(act)
        paid_now = np.maximum(age - new_dq, 0)
        bal_now = scheduled_balance(loan["upb"], loan["rate"], loan["term"], paid_now)
        status = np.where(reo & ~terminated, "RA", np.char.zfill(np.minimum(new_dq, 99).astype(str), 2))
        status = np.where(zb == "09", "RA", status)
        credit = terminated & np.isin(zb, ["02", "03", "09", "15", "16"])
        removal_upb = np.where(terminated, np.where(credit | reo, bal, bal_now), 0.0)
        current_upb = np.where(terminated, 0.0, np.where(reo, bal, bal_now))
        eltv = np.clip(np.round(loan["ltv"] * (current_upb / loan["upb"]) / hpi_ratio), 0, 998)

        rec = dict(
            idx=rec_idx,
            period=np.full(rec_idx.size, m),
            current_actual_upb=np.round(current_upb[rec_idx], 2),
            current_loan_delinquency_status=status[rec_idx],
            loan_age=age[rec_idx],
            remaining_months_to_legal_maturity=(loan["maturity_m"] - m)[rec_idx],
            modification_flag=np.where(mod_m == m, "Y", np.where(modified, "P", ""))[rec_idx],
            zero_balance_code=zb[rec_idx],
            zero_balance_effective_date=np.where(terminated, index_to_yyyymm(m), 0)[rec_idx],
            current_interest_rate=cur_rate[rec_idx],
            ddlpi=index_to_yyyymm(m - np.where(reo, reo_dq, new_dq))[rec_idx],
            estimated_ltv=eltv[rec_idx].astype(int),
            zero_balance_removal_upb=removal_upb[rec_idx],
            borrower_assistance_status_code=np.where(forb, "F", "")[rec_idx],
            step_modification_flag=np.where(modified, "N", "")[rec_idx],
            payment_deferral=np.where(deferral, "C", np.where(deferred, "P", ""))[rec_idx],
            mi_cancellation_indicator=np.where(loan["mi_pct"] > 0, "N", "7")[rec_idx],
            servicer_name=loan["servicer"][rec_idx],
        )
        cidx = np.flatnonzero(credit)
        if cidx.size:
            months_dq = np.where(zb[cidx] == "09", reo_dq[cidx], dq[cidx]).astype(float)
            loss = _loss_fields(bal[cidx], months_dq, cur_rate[cidx], loan, cidx,
                                hpi_ratio[cidx], zb[cidx], m)
            pos = np.searchsorted(rec_idx, cidx)
            for key, values in loss.items():
                col = np.full(rec_idx.size, np.nan)
                col[pos] = values
                rec[key] = col
        records.append(rec)

        dq = np.where(reo, dq, new_dq)
        alive &= ~terminated

    return _records_to_frame(records, loan)


def _records_to_frame(records: list[dict], loan: dict) -> pd.DataFrame:
    keys = {k for rec in records for k in rec}
    cols = {}
    total = sum(rec["idx"].size for rec in records)
    for key in keys:
        parts = []
        for rec in records:
            if key in rec:
                parts.append(rec[key])
            else:
                parts.append(np.full(rec["idx"].size, np.nan))
        cols[key] = np.concatenate(parts) if total else np.array([])
    df = pd.DataFrame(cols)
    df["loan_sequence_number"] = loan["loan_id"][df["idx"].to_numpy()]
    df = df.sort_values(["idx", "period"], kind="stable").reset_index(drop=True)
    df["monthly_reporting_period"] = index_to_yyyymm(df["period"].to_numpy()).astype(str)
    return df


def _format_origination(loan: dict, macro: Macro) -> pd.DataFrame:
    n = len(loan["loan_id"])
    states = np.array(macro.states)[loan["state_idx"]]
    blank = np.full(n, "")
    out = pd.DataFrame({
        "credit_score": loan["fico_reported"],
        "first_payment_date": index_to_yyyymm(loan["fp_m"]),
        "first_time_homebuyer_flag": loan["fthb"],
        "maturity_date": index_to_yyyymm(loan["maturity_m"]),
        "msa": blank,
        "mi_pct": loan["mi_pct"],
        "num_units": loan["units"],
        "occupancy_status": loan["occupancy"],
        "orig_cltv": loan["cltv"],
        "orig_dti": loan["dti_reported"],
        "orig_upb": loan["upb"].astype(int),
        "orig_ltv": loan["ltv"],
        "orig_interest_rate": np.char.mod("%.3f", loan["rate"]),
        "channel": loan["channel"],
        "ppm_flag": "N",
        "amortization_type": "FRM",
        "property_state": states,
        "property_type": loan["prop_type"],
        "postal_code": np.char.mod("%03d", loan["zip3"]),
        "loan_sequence_number": loan["loan_id"],
        "loan_purpose": loan["purpose"],
        "orig_loan_term": loan["term"],
        "num_borrowers": loan["n_borrowers"],
        "seller_name": loan["seller"],
        "super_conforming_flag": np.where(loan["super_conf"] == "Y", "Y", "N"),
        "pre_harp_loan_sequence_number": blank,
        "program_indicator": blank,
        "harp_indicator": "N",
        "property_valuation_method": "7",
        "interest_only_indicator": "N",
        "vantage_score": 9999,
    })
    return out[ORIGINATION_COLUMNS]


def _format_performance(perf: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=perf.index)
    for col in PERFORMANCE_COLUMNS:
        out[col] = perf[col] if col in perf else np.nan
    out["current_interest_rate"] = np.char.mod("%.3f", perf["current_interest_rate"].to_numpy())
    out["zero_balance_effective_date"] = perf["zero_balance_effective_date"].where(
        perf["zero_balance_effective_date"] > 0).astype("Int64")
    out["current_non_interest_bearing_upb"] = 0.0
    out["interest_bearing_upb"] = perf["current_actual_upb"]
    out["delinquency_due_to_disaster"] = ""
    out["defect_settlement_date"] = ""
    for col in ("net_sale_proceeds",):
        if col in perf:
            out[col] = perf[col].map(lambda v: "" if pd.isna(v) else f"{v:.2f}")
    return out


def generate_quarter(quarter: str, macro: Macro, loans_per_quarter: int, seed: int,
                     end_month: str, out_dir: Path) -> Path:
    year, q = parse_quarter(quarter)
    rng = np.random.default_rng([seed, year, q])
    loan = _origination(rng, quarter, loans_per_quarter, macro)
    perf = _simulate(rng, loan, macro, month_index(end_month))
    orig_txt = _format_origination(loan, macro).to_csv(sep="|", header=False, index=False)
    perf_txt = _format_performance(perf).to_csv(sep="|", header=False, index=False,
                                                 float_format="%.2f", na_rep="")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"historical_data_{quarter}.zip"
    tmp = path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"orig_{quarter}.txt", orig_txt)   # Release 47 file naming
        zf.writestr(f"perf_{quarter}.txt", perf_txt)
    tmp.replace(path)
    return path


def _worker(args):
    return generate_quarter(*args)


def generate(settings: Settings, quarters: list[str] | None = None, overwrite: bool = False,
             workers: int | None = None) -> list[Path]:
    syn = settings["synthetic"]
    if not syn:
        raise RuntimeError(f"Profile {settings.profile!r} has synthetic generation disabled")
    macro = build_macro(settings)
    quarters = quarters or quarter_range(syn["first_quarter"], syn["last_quarter"])
    out_dir = settings.raw_freddie_dir
    todo = [q for q in quarters
            if overwrite or not (out_dir / f"historical_data_{q}.zip").exists()]
    log.info("Synthetic: %d quarters requested, %d to generate", len(quarters), len(todo))
    jobs = [(q, macro, syn["loans_per_quarter"], syn["seed"], syn["performance_end"], out_dir)
            for q in todo]
    workers = workers or min(8, os.cpu_count() or 1)
    if workers == 1 or len(jobs) <= 1:
        return [_worker(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_worker, jobs))


def read_zip_member(path: Path, performance: bool) -> io.StringIO:
    """Convenience for tests / notebooks: return one member of a quarterly zip as text."""
    with zipfile.ZipFile(path) as zf:
        from loanlens.ingest.freddie import classify

        kind = "performance" if performance else "origination"
        name = next(n for n in zf.namelist() if (hit := classify(n)) and hit[1] == kind)
        return io.StringIO(zf.read(name).decode())

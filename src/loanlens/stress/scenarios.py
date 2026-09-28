"""Macro scenario generation for stress testing.

National paths (unemployment rate, house-price index, 30y mortgage rate) are simulated
monthly from the portfolio's as-of date:

* **Expansion dynamics** estimated from FRED history: unemployment mean-reverts with
  persistent AR(1) changes; house-price log growth follows an AR(1) around its long-run mean;
  the mortgage rate is a slowly mean-reverting random walk.
* **Recession regime**: each month a recession starts with the hazard implied by
  ``recession_prob_annual``. Its severity s ~ Beta(2, 3.5) sets the unemployment rise
  (1.5 to 10 points, peaking after 10-20 months), a correlated house-price decline
  (up to ~30% peak-to-trough over 2-3 years) and Fed easing of mortgage rates.

State paths are the national path passed through state sensitivities estimated from history
(betas of state unemployment / house-price changes on the national changes, e.g. sand states
have house-price betas above 1), plus idiosyncratic AR(1) noise.

Named deterministic scenarios (baseline, adverse, severely adverse, GFC replay) use the same
state mapping so they are directly comparable with the simulated distribution.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from loanlens.config import Settings
from loanlens.warehouse import query


@dataclass
class MacroHistory:
    months: pd.DatetimeIndex
    states: list[str]
    us_ur: np.ndarray            # [T]
    us_hpi: np.ndarray           # [T]
    rate: np.ndarray             # [T]
    st_ur: np.ndarray            # [state, T]
    st_hpi: np.ndarray           # [state, T]

    def index_of(self, month: str) -> int:
        return int(self.months.get_loc(pd.Timestamp(month + "-01")))


def load_history(settings: Settings) -> MacroHistory:
    df = query(settings, """
        select state_code, month, state_unemployment_rate, state_hpi, us_unemployment_rate,
               us_hpi, mortgage_rate_30y
        from core.dim_macro where month >= date '1990-01-01' order by month, state_code
    """)
    df["month"] = pd.to_datetime(df["month"])
    wide = lambda col: df.pivot(index="month", columns="state_code", values=col).ffill().bfill()  # noqa: E731
    ur, hpi = wide("state_unemployment_rate"), wide("state_hpi")
    nat = df.groupby("month")[["us_unemployment_rate", "us_hpi", "mortgage_rate_30y"]].first().ffill().bfill()
    return MacroHistory(
        months=pd.DatetimeIndex(ur.index), states=list(ur.columns),
        us_ur=nat["us_unemployment_rate"].to_numpy(), us_hpi=nat["us_hpi"].to_numpy(),
        rate=nat["mortgage_rate_30y"].to_numpy(),
        st_ur=ur.to_numpy().T, st_hpi=hpi.to_numpy().T,
    )


@dataclass
class ScenarioSet:
    """Paths for months 1..T after the as-of month. Levels for UR/rate, HPI relative to as-of."""
    names: list[str]
    kind: list[str]                  # 'monte_carlo' or 'named'
    us_ur: np.ndarray                # [S, T]
    us_hpi_rel: np.ndarray           # [S, T]
    rate: np.ndarray                 # [S, T]
    st_ur: np.ndarray                # [S, state, T]
    st_hpi_rel: np.ndarray           # [S, state, T]
    st_ur_hist12: np.ndarray         # [state, 12] state UR for the 12 months up to as-of
    recession: np.ndarray            # [S] bool

    @property
    def n(self) -> int:
        return len(self.names)

    def summary(self, as_of_ur: float, as_of_rate: float) -> pd.DataFrame:
        return pd.DataFrame({
            "scenario_id": self.names,
            "scenario_type": self.kind,
            "recession": self.recession,
            "peak_unemployment": self.us_ur.max(axis=1),
            "unemployment_rise": self.us_ur.max(axis=1) - as_of_ur,
            "hpi_trough_change": self.us_hpi_rel.min(axis=1) - 1.0,
            "hpi_change_end": self.us_hpi_rel[:, -1] - 1.0,
            "mortgage_rate_change_end": self.rate[:, -1] - as_of_rate,
        })


# ---- estimation ---------------------------------------------------------------------------

@dataclass
class Dynamics:
    ur_phi: float
    ur_kappa: float
    ur_star: float
    ur_sigma: float
    hpi_mu: float
    hpi_rho: float
    hpi_sigma: float
    rate_sigma: float
    rate_kappa: float
    rate_star: float
    st_ur_beta: np.ndarray
    st_ur_sigma: np.ndarray
    st_hpi_beta: np.ndarray
    st_hpi_alpha: np.ndarray
    st_hpi_sigma: np.ndarray


RECESSIONS = [("2001-03", "2001-11"), ("2007-12", "2009-06"), ("2020-02", "2020-04")]


def estimate_dynamics(h: MacroHistory, end: int) -> Dynamics:
    months = h.months[: end + 1]
    in_rec = np.zeros(len(months), bool)
    for a, b in RECESSIONS:
        # exclude recessions and the 18 months after them from "expansion" estimates
        in_rec |= (months >= pd.Timestamp(a + "-01")) & (months <= pd.Timestamp(b + "-01") + pd.DateOffset(months=18))
    exp = ~in_rec

    u = h.us_ur[: end + 1]
    du = np.diff(u)
    X = np.column_stack([du[:-1], 5.0 - u[1:-1]])
    mask = exp[2:]
    coef, *_ = np.linalg.lstsq(X[mask], du[1:][mask], rcond=None)
    resid = du[1:][mask] - X[mask] @ coef
    ur_phi, ur_kappa = float(np.clip(coef[0], 0, 0.8)), float(np.clip(coef[1], 0.002, 0.05))

    g = np.diff(np.log(h.us_hpi[: end + 1]))
    # quarterly series are forward-filled monthly: aggregate to 3-month growth, rescale
    g3 = np.array([g[i:i + 3].sum() for i in range(0, len(g) - 2, 3)]) / 3.0
    hpi_mu = float(np.mean(g3))
    hpi_rho = float(np.clip(np.corrcoef(g3[:-1], g3[1:])[0, 1], 0, 0.95)) ** (1 / 3)
    hpi_sigma = float(np.std(g3) * np.sqrt(1 - hpi_rho ** 2) / np.sqrt(3))

    r = h.rate[: end + 1]
    dr = np.diff(r)

    # State sensitivities from 12-month changes
    def beta(st, nat):
        x, y = nat - nat.mean(), st - st.mean(axis=1, keepdims=True)
        b = (y @ x) / (x @ x)
        res = y - np.outer(b, x)
        return b, res.std(axis=1)

    ur12 = h.st_ur[:, 12: end + 1] - h.st_ur[:, : end + 1 - 12]
    nat_ur12 = h.us_ur[12: end + 1] - h.us_ur[: end + 1 - 12]
    st_ur_beta, st_ur_res = beta(ur12, nat_ur12)
    hp12 = np.log(h.st_hpi[:, 12: end + 1] / h.st_hpi[:, : end + 1 - 12])
    nat_hp12 = np.log(h.us_hpi[12: end + 1] / h.us_hpi[: end + 1 - 12])
    st_hpi_beta, st_hpi_res = beta(hp12, nat_hp12)
    st_hpi_alpha = hp12.mean(axis=1) - st_hpi_beta * nat_hp12.mean()

    return Dynamics(
        ur_phi=ur_phi, ur_kappa=ur_kappa, ur_star=5.0, ur_sigma=float(resid.std()),
        hpi_mu=hpi_mu, hpi_rho=hpi_rho, hpi_sigma=hpi_sigma,
        rate_sigma=float(dr.std()), rate_kappa=0.01, rate_star=float(r[-240:].mean()),
        st_ur_beta=np.clip(st_ur_beta, 0.3, 2.0), st_ur_sigma=st_ur_res / np.sqrt(12),
        st_hpi_beta=np.clip(st_hpi_beta, 0.2, 2.5), st_hpi_alpha=st_hpi_alpha / 12.0,
        st_hpi_sigma=st_hpi_res / np.sqrt(12),
    )


# ---- simulation ---------------------------------------------------------------------------

def _recession_shape(T: int, onset: np.ndarray, peak_after: np.ndarray, size: np.ndarray,
                     decay_half_life: float) -> np.ndarray:
    """Hump-shaped shock: smooth ramp to `size` over `peak_after` months, then decay."""
    t = np.arange(1, T + 1)[None, :]
    rel = t - onset[:, None]
    ramp = np.clip(rel / peak_after[:, None], 0, 1)
    ramp = 0.5 - 0.5 * np.cos(np.pi * ramp)
    after = np.clip(rel - peak_after[:, None], 0, None)
    decay = 0.5 ** (after / decay_half_life)
    return size[:, None] * ramp * decay * (rel > 0)


def simulate(settings: Settings, h: MacroHistory, as_of: str, horizon: int, n: int,
             seed: int, recession_prob_annual: float, include_named: bool = True) -> ScenarioSet:
    end = h.index_of(as_of)
    d = estimate_dynamics(h, end)
    rng = np.random.default_rng(seed)
    T, S = horizon, n

    # --- recession regime
    lam = max(1 - (1 - recession_prob_annual) ** (1 / 12), 1e-12)
    onset = rng.geometric(lam, S).astype(float)          # month the recession starts
    recession = onset <= T - 3
    sev = rng.beta(2.0, 3.5, S)
    ur_rise = np.where(recession, 1.5 + 8.5 * sev, 0.0)
    ur_peak_after = rng.uniform(10, 20, S)
    hpi_drop = np.where(recession, np.clip((0.02 + 0.30 * sev ** 1.2) * rng.uniform(0.6, 1.4, S), 0, 0.45), 0.0)
    hpi_trough_after = rng.uniform(22, 36, S)
    rate_cut = np.where(recession, (0.5 + 1.5 * sev) * rng.uniform(0.5, 1.2, S), 0.0)

    ur_shock = _recession_shape(T, onset, ur_peak_after, ur_rise, 24.0)
    hpi_shock = _recession_shape(T, onset + 3, hpi_trough_after, np.log(1 - hpi_drop + 1e-9) * -1, 60.0)
    rate_shock = _recession_shape(T, onset, np.full(S, 12.0), rate_cut, 36.0)

    # --- expansion dynamics
    u0, r0 = h.us_ur[end], h.rate[end]
    du_prev = np.full(S, h.us_ur[end] - h.us_ur[end - 1])
    g_prev = np.full(S, np.log(h.us_hpi[end] / h.us_hpi[end - 3]) / 3)
    u, r, logp = np.full(S, u0), np.full(S, r0), np.zeros(S)
    base_u, base_r, base_p = np.empty((S, T)), np.empty((S, T)), np.empty((S, T))
    for t in range(T):
        du = d.ur_phi * du_prev + d.ur_kappa * (d.ur_star - u) + rng.normal(0, d.ur_sigma, S)
        u = np.clip(u + du, 2.5, 25)
        du_prev = du
        g = d.hpi_mu + d.hpi_rho * (g_prev - d.hpi_mu) + rng.normal(0, d.hpi_sigma, S)
        logp = logp + g
        g_prev = g
        r = np.clip(r + d.rate_kappa * (d.rate_star - r) + rng.normal(0, d.rate_sigma, S), 1.5, 15)
        base_u[:, t], base_r[:, t], base_p[:, t] = u, r, logp

    us_ur = np.clip(base_u + ur_shock, 2.5, 25)
    us_hpi_rel = np.exp(base_p - hpi_shock)
    rate = np.clip(base_r - rate_shock, 1.5, 15)
    names = [f"MC{i:05d}" for i in range(S)]
    kind = ["monte_carlo"] * S

    if include_named:
        named = named_scenarios(h, end, T)
        names += list(named)
        kind += ["named"] * len(named)
        us_ur = np.vstack([us_ur, [v[0] for v in named.values()]])
        us_hpi_rel = np.vstack([us_hpi_rel, [v[1] for v in named.values()]])
        rate = np.vstack([rate, [v[2] for v in named.values()]])
        recession = np.concatenate([recession, [k != "Baseline" for k in named]])

    return _to_states(h, end, d, names, kind, us_ur, us_hpi_rel, rate, recession, rng)


def _to_states(h, end, d, names, kind, us_ur, us_hpi_rel, rate, recession, rng) -> ScenarioSet:
    S, T = us_ur.shape
    n_st = len(h.states)
    u0 = h.us_ur[end]
    st_u0 = h.st_ur[:, end]
    noise_u = np.zeros((S, n_st))
    noise_p = np.zeros((S, n_st))
    st_ur = np.empty((S, n_st, T), dtype=np.float32)
    st_hpi = np.empty((S, n_st, T), dtype=np.float32)
    log_nat = np.log(us_hpi_rel)
    log_nat_prev = np.zeros(S)
    log_st = np.zeros((S, n_st))
    is_named = np.array([k == "named" for k in kind])
    for t in range(T):
        scale = np.where(is_named, 0.0, 1.0)[:, None]  # named scenarios are deterministic
        noise_u = 0.9 * noise_u + scale * rng.normal(0, 1, (S, n_st)) * d.st_ur_sigma
        noise_p = 0.8 * noise_p + scale * rng.normal(0, 1, (S, n_st)) * d.st_hpi_sigma
        st_ur[:, :, t] = np.clip(st_u0 + d.st_ur_beta * (us_ur[:, t] - u0)[:, None] + noise_u, 1.5, 30)
        g_nat = log_nat[:, t] - log_nat_prev
        log_nat_prev = log_nat[:, t]
        log_st = log_st + d.st_hpi_beta * g_nat[:, None] + noise_p * 0.3
        st_hpi[:, :, t] = np.exp(log_st)
    hist12 = h.st_ur[:, end - 11: end + 1]
    return ScenarioSet(names, kind, us_ur, us_hpi_rel, rate, st_ur, st_hpi, hist12, np.asarray(recession))


def named_scenarios(h: MacroHistory, end: int, T: int) -> dict[str, tuple]:
    """Deterministic scenarios: (US unemployment path, US HPI relative path, rate path)."""
    u0, r0 = h.us_ur[end], h.rate[end]
    t = np.arange(1, T + 1)

    def hump(size, peak, half_life=30):
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.clip(t / peak, 0, 1))
        return size * ramp * 0.5 ** (np.clip(t - peak, 0, None) / half_life)

    out = {
        "Baseline": (np.full(T, u0) + (5.0 - u0) * (1 - 0.97 ** t), np.exp(0.003 * t), np.full(T, r0)),
        "Adverse": (u0 + hump(3.0, 12), np.exp(-hump(0.12, 24, 60)) * np.exp(0.001 * t), r0 - hump(0.75, 12)),
        "Severely adverse": (u0 + hump(np.maximum(10.0 - u0, 4.0), 18), np.exp(-hump(0.29, 27, 90)),
                             r0 - hump(1.5, 12)),
    }
    # GFC replay: the realised 2007-12 -> 2010-12 national changes applied from today.
    gfc = h.index_of("2007-12")
    if gfc + T < len(h.months):
        out["GFC replay (2008-2010)"] = (
            u0 + (h.us_ur[gfc + 1: gfc + T + 1] - h.us_ur[gfc]),
            h.us_hpi[gfc + 1: gfc + T + 1] / h.us_hpi[gfc],
            r0 + (h.rate[gfc + 1: gfc + T + 1] - h.rate[gfc]),
        )
    return out


def realised_path(h: MacroHistory, as_of: str, horizon: int) -> ScenarioSet:
    """The actual history after `as_of`, packaged as a one-scenario set (for backtesting)."""
    end = h.index_of(as_of)
    sl = slice(end + 1, end + horizon + 1)
    return ScenarioSet(
        names=["Realised"], kind=["realised"],
        us_ur=h.us_ur[sl][None], us_hpi_rel=(h.us_hpi[sl] / h.us_hpi[end])[None],
        rate=h.rate[sl][None],
        st_ur=h.st_ur[:, sl][None].astype(np.float32),
        st_hpi_rel=(h.st_hpi[:, sl] / h.st_hpi[:, end: end + 1])[None].astype(np.float32),
        st_ur_hist12=h.st_ur[:, end - 11: end + 1], recession=np.array([True]),
    )

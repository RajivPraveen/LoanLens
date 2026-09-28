"""Fast unit tests: configuration, layouts, FRED processing, generator invariants, economics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from loanlens.config import load_settings
from loanlens.reference import month_index, quarter_range
from loanlens.schemas import (
    ORIGINATION_COLUMNS,
    ORIGINATION_LAYOUT,
    PERFORMANCE_COLUMNS,
    raw_table_ddl,
)


def test_profiles_merge_and_validate(tmp_path):
    tiny = load_settings("tiny", tmp_path)
    real = load_settings("freddie", tmp_path)
    assert real["synthetic"] is None and tiny["synthetic"]["loans_per_quarter"] > 0
    assert tiny["modeling"]["xgb"]["n_estimators"] < real["modeling"]["xgb"]["n_estimators"]
    assert tiny["modeling"]["xgb"]["max_depth"] == real["modeling"]["xgb"]["max_depth"]  # inherited
    with pytest.raises(ValueError):
        load_settings("nope", tmp_path)


def test_time_splits_do_not_overlap():
    for profile in ("freddie", "tiny"):
        m = load_settings(profile)["modeling"]
        assert m["train_years"][1] < m["calibration_years"][0] <= m["calibration_years"][1] < m["test_years"][0]


def test_freddie_layouts():
    from loanlens.schemas import LEGACY, ORIGINATION_LAYOUT_LEGACY, R47, detect_layout

    assert len(ORIGINATION_COLUMNS) == 31 and len(PERFORMANCE_COLUMNS) == 35   # Release 47
    assert len(ORIGINATION_LAYOUT_LEGACY) == 32
    assert ORIGINATION_COLUMNS[19] == "loan_sequence_number" and ORIGINATION_COLUMNS[-1] == "vantage_score"
    assert PERFORMANCE_COLUMNS[33] == "servicer_name"
    assert detect_layout("origination", "|".join(["x"] * 31)) == R47
    assert detect_layout("origination", "|".join(["x"] * 32)) == LEGACY
    assert detect_layout("performance", "|".join(["x"] * 35) + "\n") == R47
    assert detect_layout("performance", "|".join(["x"] * 26)) == LEGACY
    ddl = raw_table_ddl("raw.origination", ORIGINATION_LAYOUT)
    assert "source_period VARCHAR" in ddl and "layout_version VARCHAR" in ddl


def test_month_helpers():
    assert quarter_range("2004Q3", "2005Q2") == ["2004Q3", "2004Q4", "2005Q1", "2005Q2"]
    assert month_index("2000-01") - month_index("1999-12") == 1
    assert month_index("200501") == month_index("2005-01")


def test_monthly_panel_aggregates_and_fills():
    from loanlens.ingest.fred import monthly_panel

    obs = pd.DataFrame({
        "observation_date": pd.to_datetime(["2020-01-03", "2020-01-10", "2020-02-07", "2020-01-01", "2020-04-01"]),
        "value": [3.0, 4.0, 5.0, 100.0, 110.0],
        "geo": ["US", "US", "US", "CA", "CA"],
        "measure": ["mortgage_rate_30y"] * 3 + ["hpi"] * 2,
    })
    panel = monthly_panel(obs, "2020-06").set_index(["geo", "month"])
    assert panel.loc[("US", pd.Period("2020-01", "M")), "mortgage_rate_30y"] == 3.5     # weekly averaged
    assert panel.loc[("CA", pd.Period("2020-03", "M")), "hpi"] == 100.0                 # quarterly carried
    assert panel.loc[("CA", pd.Period("2020-06", "M")), "hpi"] == 110.0                 # carried to end


def test_scheduled_balance_amortises_to_zero():
    from loanlens.synthetic.generator import scheduled_balance

    bal = scheduled_balance(np.array([200_000.0]), np.array([6.0]), np.array([360]), np.array([0, 180, 360]))
    assert bal[0] == pytest.approx(200_000)
    assert 0 < bal[1] < 200_000
    assert bal[2] == pytest.approx(0, abs=1e-6)


def test_generator_quarter_invariants(fresh_settings):
    from loanlens.synthetic.generator import build_macro, generate_quarter, read_zip_member

    macro = build_macro(fresh_settings)
    path = generate_quarter("2006Q2", macro, 120, 7, "2018-12", fresh_settings.raw_freddie_dir)
    orig = pd.read_csv(read_zip_member(path, False), sep="|", header=None, names=ORIGINATION_COLUMNS, dtype=str)
    perf = pd.read_csv(read_zip_member(path, True), sep="|", header=None, names=PERFORMANCE_COLUMNS, dtype=str)
    assert len(orig) == 120 and orig["loan_sequence_number"].str.fullmatch(r"F06Q2\d{7}").all()
    assert not perf.duplicated(["loan_sequence_number", "monthly_reporting_period"]).any()
    assert set(perf["loan_sequence_number"]) <= set(orig["loan_sequence_number"])
    assert (perf["current_actual_upb"].astype(float) >= 0).all()
    # a loan never reports after its zero-balance record, and has at most one
    perf["period"] = perf["monthly_reporting_period"].astype(int)
    zb = perf.dropna(subset=["zero_balance_code"])
    assert not zb["loan_sequence_number"].duplicated().any()
    last = perf.groupby("loan_sequence_number")["period"].max()
    assert (zb.set_index("loan_sequence_number")["period"] == last.loc[zb["loan_sequence_number"]]).all()
    # deterministic
    again = generate_quarter("2006Q2", macro, 120, 7, "2018-12", fresh_settings.raw_freddie_dir / "again")
    assert read_zip_member(path, True).read() == read_zip_member(again, True).read()


def test_profit_curve_prefers_breakeven_cutoff():
    from loanlens.decision.cutoff import attach_economics, curve

    rng = np.random.default_rng(0)
    n = 20_000
    df = pd.DataFrame({"pd": rng.beta(0.5, 60, n), "orig_upb": 200_000.0, "mi_pct": 0.0, "ltv_band": "61-80"})
    df["default_in_window"] = rng.random(n) < df["pd"]
    sev = pd.DataFrame({"ltv_band": ["61-80"], "has_mi": [False], "lgd": [0.25]})
    econ = attach_economics(df, sev, margin=0.005, horizon=2)
    grid = np.quantile(df["pd"], np.linspace(0.5, 1.0, 201))
    c = curve(econ, grid, "expected_profit")
    best = c.loc[c["profit"].idxmax(), "cutoff_pd"]
    breakeven = 0.01 / (0.01 + 0.25)
    assert best == pytest.approx(breakeven, rel=0.15)
    assert c["profit"].max() >= c.iloc[-1]["profit"]  # never worse than approving everyone


def test_fairness_air_is_relative_to_best_group():
    from loanlens.models.fairness import audit

    rng = np.random.default_rng(1)
    n = 6_000
    df = pd.DataFrame({
        "pd_xgb": rng.uniform(0, 0.05, n), "orig_upb": rng.uniform(1e5, 5e5, n),
        "census_region": rng.choice(["Midwest", "West"], n), "state_code": "CA",
        "is_first_time_homebuyer": rng.choice([True, False], n), "num_borrowers": rng.choice([1, 2], n),
    })
    df.loc[df["census_region"] == "West", "pd_xgb"] *= 2  # West looks riskier
    df["default_in_window"] = rng.random(n) < df["pd_xgb"]
    res = audit(df, {"recommended": 0.04}, min_group=100)
    region = res[res["dimension"] == "Census region"].set_index("group")
    assert region.loc["Midwest", "air_recommended"] == pytest.approx(1.0)
    assert region.loc["West", "air_recommended"] < 1.0


def test_hazard_feature_matrix_broadcasts():
    from loanlens.stress.hazard import FEATURES, feature_matrix

    n, s = 5, 3
    static = {k: np.full(n, v) for k, v in dict(credit_score=720.0, orig_dti=35.0, orig_cltv=80.0, orig_ltv=80.0,
                                                  investor=0.0, cash_out=0.0, single_borrower=1.0, short_term=0.0,
                                                  orig_upb=200_000.0, vintage_year=2006.0, loan_age=24.0).items()}
    dyn = {k: np.full((s, n), v) for k, v in dict(mtm_ltv=95.0, ur=8.0, ur_chg12=3.0, refi_incentive=0.5).items()}
    feats = feature_matrix({**static, **dyn})
    assert len(feats) == len(FEATURES)
    shapes = {np.broadcast(*feats).shape}
    assert shapes == {(s, n)}


FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "freddie_r47_example"


@pytest.mark.skipif(not FIXTURES.exists(), reason="Freddie Mac example files not present")
def test_parses_official_release47_example_files():
    """Freddie Mac's published Release 47 example records parse cleanly and pass every check."""
    from loanlens.ingest.freddie import Member, Source, _peek_layout, _read, _type, _validation_view
    from loanlens.quality.suites import ORIGINATION_SENTINELS, PERFORMANCE_SENTINELS
    from loanlens.quality.validate import validate
    from loanlens.schemas import LAYOUTS, R47, RAW_ORIGINATION, RAW_PERFORMANCE

    src = Source("2000Q3", {"origination": Member(FIXTURES / "origination_sample_file.txt"),
                            "performance": Member(FIXTURES / "performance_sample_file.txt")})
    for kind, raw, sentinels, suite, flag in [
        ("origination", RAW_ORIGINATION, ORIGINATION_SENTINELS, "freddie_origination", "_period_matches_file"),
        ("performance", RAW_PERFORMANCE, PERFORMANCE_SENTINELS, "freddie_performance", "_has_origination"),
    ]:
        assert _peek_layout(src, kind) == R47
        layout = LAYOUTS[(kind, R47)]
        with src.open_member(kind == "performance") as fh:
            df = _type(next(_read(fh, layout, None)), layout, raw)
        assert df["_parse_errors"].sum() == 0
        view = _validation_view(df, sentinels)
        view[flag] = True  # the example files use placeholder ids/dates across files
        assert validate(view, suite, "example").success
    assert df["servicer_name"].notna().all()  # servicer moved to the monthly file in R47


def test_legacy_layout_loads_with_union_columns(fresh_settings):
    """A pre-Release-47 file (32 origination fields, servicer at position 25) still loads."""
    import zipfile

    from loanlens.ingest.freddie import ingest_freddie
    from loanlens.warehouse import query

    s = fresh_settings
    orig = "|".join(["750", "200603", "N", "203602", "", "0", "1", "P", "80", "35", "200000", "80",
                     "6.500", "R", "N", "FRM", "CA", "SF", "90000", "F106Q1000001", "P", "360", "2",
                     "SELLER A", "SERVICER B", "", "", "9", "", "9", "N", "7"]) + "\n"
    perf = "\n".join("|".join(["F106Q1000001", f"2006{m:02d}", "199000.00", "0", str(m - 3), str(363 - m),
                                "", "", "", "", "6.500", "0.00", "", "", "", "", "", "", "", "", "",
                                "", "", "", "", "80", "", "", "", "", "", ""]) for m in (3, 4, 5)) + "\n"
    s.raw_freddie_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(s.raw_freddie_dir / "historical_data_2006Q1.zip", "w") as zf:
        zf.writestr("historical_data_2006Q1.txt", orig)
        zf.writestr("historical_data_time_2006Q1.txt", perf)
    [result] = ingest_freddie(s)
    assert result.status == "loaded" and result.perf_rows == 3
    row = query(s, "select servicer_name, vantage_score, layout_version from raw.origination").iloc[0]
    assert row["servicer_name"] == "SERVICER B" and row["layout_version"] == "legacy"


def test_file_name_patterns():
    from loanlens.schemas import classify

    assert classify("sample_orig_2005.txt") == ("2005", "origination")
    assert classify("sample_perf_2005.txt") == ("2005", "performance")      # Release 47 sample
    assert classify("sample_svcg_2005.txt") == ("2005", "performance")      # older releases
    assert classify("historical_data_2005Q1.txt") == ("2005Q1", "origination")
    assert classify("historical_data_time_2005Q1.txt") == ("2005Q1", "performance")
    assert classify("perf_2005q1.txt") == ("2005Q1", "performance")
    assert classify("readme.txt") is None

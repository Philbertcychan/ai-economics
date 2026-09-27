"""Tests for the Nebius model in ``companies/nebius.py``.

Built from the committed data (``data/disclosed/NBIS.csv`` and ``assumptions/NBIS.csv``),
offline. The checks are the same three a finance reviewer makes: the actual columns are the
company's own figures, the forecast balances roll forward through the flows on the sheet, and
the Excel formulas say what the Python said.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from xlsx_eval import assert_formulas_recompute

from companies.assumptions import load_assumptions
from companies.nebius import (
    DRIVER_FORMULAS,
    FORECAST_QUARTERS,
    OPENING_BALANCES,
    OUTPUT_FORMULA_FROM_FORECAST,
    Nebius,
    _previous,
)
from data import ASSUMPTIONS_DIR, DISCLOSED_DIR


def built() -> Nebius:
    model = Nebius()
    model.load_data()
    model.build()
    return model


def test_previous_quarter() -> None:
    assert _previous("2025Q1") == "2024Q4"
    assert _previous("2026Q3") == "2026Q2"


def test_history_ties_to_disclosed_figures() -> None:
    model = built()
    d, o = model.drivers, model.outputs
    assert list(d.columns) == list(o.columns)
    actuals = [c for c in d.columns if c.endswith("A")]
    estimates = [c for c in d.columns if c.endswith("E")]
    assert actuals[0] == "2025Q1A" and len(estimates) == FORECAST_QUARTERS
    kpi = model.disclosed.set_index(["kpi", "period"])["value"]
    for period in actuals:
        q = period[:-1]
        assert d.loc["revenue_usd_m", period] == kpi["revenue_usd_m", q]
        assert d.loc["adjusted_ebitda_usd_m", period] == kpi["adjusted_ebitda_usd_m", q]
        assert d.loc["cfo_usd_m", period] == kpi["operating_cash_flow_usd_m", q]
        assert d.loc["capex_usd_m", period] == kpi["capex_usd_m", q]
        assert d.loc["arr_end_usd_m", period] == kpi["arr_usd_m", q]
        assert d.loc["cash_end_usd_m", period] == kpi["cash_usd_m", q]
        assert d.loc["debt_end_usd_m", period] == pytest.approx(
            kpi["debt_current_usd_m", q] + kpi["debt_non_current_usd_m", q]
        )
        assert d.loc["deferred_revenue_end_usd_m", period] == pytest.approx(
            kpi["deferred_revenue_current_usd_m", q] + kpi["deferred_revenue_non_current_usd_m", q]
        )
        # The cash-flow identity holds in actuals through the plugs.
        assert d.loc["cash_end_usd_m", period] == pytest.approx(
            (kpi["cash_usd_m", _previous(q)])
            + d.loc["cfo_usd_m", period]
            - d.loc["capex_usd_m", period]
            + d.loc["debt_raised_usd_m", period]
            + d.loc["financing_other_usd_m", period]
        )
    # The one disclosed active-power figure is on the sheet; June 2026 is the register's.
    assert d.loc["active_power_mw_end", "2025Q4A"] == kpi["active_power_mw", "2025Q4"]
    register = load_assumptions("NBIS").set_index("name")["value"]
    assert d.loc["active_power_mw_end", actuals[-1]] == register["active_power_mw_2026q2"]
    # Calibration lines are plain ratios of disclosed figures.
    assert o.loc["adjusted_ebitda_margin", "2026Q2A"] == pytest.approx(236.2 / 582.3)
    assert o.loc["revenue_to_midpoint_arr", "2026Q2A"] == pytest.approx(574.9 / ((1900 + 3000) / 8))
    assert o.loc["arr_per_active_mw_usd_m", "2025Q4A"] == pytest.approx(1250 / 170)
    assert math.isnan(o.loc["ebitda_per_mw_year_usd_m", "2026Q2A"])
    # Formula bookkeeping: every driver formula starts at the first estimate; the plugs have
    # no formula; the per-MW outputs start at the first estimate too.
    pasted = {"other_deferred_revenue_movements_usd_m", "financing_other_usd_m"}
    assert set(DRIVER_FORMULAS) == set(d.index) - pasted
    assert set(d.attrs["formula_starts"]) == set(DRIVER_FORMULAS)
    assert set(d.attrs["formula_starts"].values()) == {estimates[0]}
    assert set(o.attrs["formulas"]) == set(o.index)
    assert set(o.attrs["formula_starts"]) == OUTPUT_FORMULA_FROM_FORECAST
    frames = model.to_frames()
    assert "sensitivities" in frames and "disclosed" in frames


def test_forecast_roll_forwards_tie() -> None:
    model = built()
    d, o = model.drivers, model.outputs
    a = load_assumptions("NBIS").set_index("name")["value"]
    cols = list(d.columns)
    estimates = [c for c in cols if c.endswith("E")]
    for prev, cur in zip(cols[:-1], cols[1:], strict=True):
        assert d.loc["deferred_revenue_end_usd_m", cur] == pytest.approx(
            d.loc["deferred_revenue_end_usd_m", prev]
            + d.loc["deferred_revenue_change_usd_m", cur]
            + d.loc["other_deferred_revenue_movements_usd_m", cur]
        )
        assert d.loc["debt_end_usd_m", cur] == pytest.approx(
            d.loc["debt_end_usd_m", prev] + d.loc["debt_raised_usd_m", cur]
        )
        assert d.loc["cash_end_usd_m", cur] == pytest.approx(
            d.loc["cash_end_usd_m", prev]
            + d.loc["cfo_usd_m", cur]
            - d.loc["capex_usd_m", cur]
            + d.loc["debt_raised_usd_m", cur]
            + d.loc["financing_other_usd_m", cur]
            + d.loc["funding_required_usd_m", cur]
        )
    for prev, cur in zip(cols[-len(estimates) - 1 : -1], estimates, strict=True):
        added = a["mw_added_per_quarter"]
        assert d.loc["active_power_mw_end", cur] == d.loc["active_power_mw_end", prev] + added
        assert d.loc["arr_end_usd_m", cur] == pytest.approx(
            d.loc["arr_end_usd_m", prev] + added * a["arr_per_new_mw_year_usd_m"]
        )
        assert d.loc["revenue_usd_m", cur] == pytest.approx(
            (d.loc["arr_end_usd_m", prev] + d.loc["arr_end_usd_m", cur])
            / 8
            * a["revenue_to_midpoint_arr"]
            / a["ai_cloud_share_of_revenue"]
        )
        assert d.loc["cfo_usd_m", cur] == pytest.approx(
            d.loc["adjusted_ebitda_usd_m", cur]
            - d.loc["interest_on_debt_usd_m", cur]
            + d.loc["deferred_revenue_change_usd_m", cur]
            + d.loc["other_operating_cash_usd_m", cur]
        )
        assert d.loc["cash_end_usd_m", cur] >= a["minimum_cash_usd_bn"] * 1000 - 1e-6
        assert o.loc["prepayments_share_of_capex", cur] == pytest.approx(
            a["prepayment_share_of_capex"]
        )
        assert o.loc["payback_years_per_mw_net_of_prepayment", cur] == pytest.approx(
            a["capex_per_mw_usd_m"]
            * (1 - a["prepayment_share_of_capex"])
            / o.loc["ebitda_per_mw_year_usd_m", cur]
        )
    assert o.loc["cumulative_funding_required_usd_m", estimates[-1]] == pytest.approx(
        sum(d.loc["funding_required_usd_m", c] for c in estimates)
    )
    # The ARR check against guidance is on the sheet for the reader to judge.
    assert o.loc["arr_guidance_ye2026_low_usd_m", estimates[0]] == 7000
    assert 5000 < d.loc["arr_end_usd_m", "2026Q4E"] < 15000


def test_sensitivities_move_the_right_way() -> None:
    model = built()
    s = model.extra_frames["sensitivities"]
    base = s.loc["base case"]
    assert math.isnan(base["value"])
    more_mw = s.loc["mw_added_per_quarter = 300"]
    assert more_mw["arr_end_2026_usd_m"] > base["arr_end_2026_usd_m"]
    assert more_mw["revenue_final_year_usd_bn"] > base["revenue_final_year_usd_bn"]
    dearer = s.loc["capex_per_mw_usd_m = 35"]
    assert dearer["external_funding_usd_m"] >= base["external_funding_usd_m"]
    assert dearer["payback_net_of_prepayment_end"] > base["payback_net_of_prepayment_end"]
    # Rows without a range are not varied.
    assert not any(index.startswith("disclosed_payback_months") for index in s.index)


def test_workbook_formulas_recompute_to_the_python_values(tmp_path: Path) -> None:
    model = built()
    path = model.to_xlsx(tmp_path / "NBIS.xlsx")
    assert_formulas_recompute(path, {"Drivers": model.drivers, "Outputs": model.outputs})


def test_guards(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError, match=r"data/disclosed/NBIS\.csv"):
        Nebius().build()  # load_data() not called: no disclosed KPIs
    no_register = Nebius(assumptions_dir=tmp_path)
    no_register.load_data()
    with pytest.raises(NotImplementedError, match="no assumptions register"):
        no_register.build()
    # The opening active-power figure is named for its quarter; drop it and the build refuses.
    register = (ASSUMPTIONS_DIR / "NBIS.csv").read_text(encoding="utf-8").splitlines()
    kept = [line for line in register if not line.startswith("active_power_mw_2026q2,")]
    (tmp_path / "NBIS.csv").write_text("\n".join(kept) + "\n", encoding="utf-8")
    stale = Nebius(assumptions_dir=tmp_path)
    stale.load_data()
    with pytest.raises(ValueError, match="active_power_mw_2026q2"):
        stale.build()
    # A gap in the disclosed quarters is an error, not a silently wrong @prev reference.
    disclosed = (DISCLOSED_DIR / "NBIS.csv").read_text(encoding="utf-8").splitlines()
    gapped = [line for line in disclosed if not line.startswith("2025Q3,revenue_usd_m,")]
    ddir = tmp_path / "disclosed"
    ddir.mkdir()
    (ddir / "NBIS.csv").write_text("\n".join(gapped) + "\n", encoding="utf-8")
    broken = Nebius(disclosed_dir=ddir)
    broken.load_data()
    with pytest.raises(ValueError, match="not contiguous"):
        broken.build()
    assert set(OPENING_BALANCES) <= set(DRIVER_FORMULAS)

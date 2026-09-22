"""CoreWeave (CRWV) - the first neocloud in the repository.

Version 1: the quarterly HISTORY, one column per reported quarter. It answers "what has a unit
of CoreWeave's capacity actually earned, and how fast does it pay back", two ways:

* per MW of active power - uses only reported and disclosed figures, no assumptions;
* per GPU-hour - converts MW to GPUs with one assumption (kW per GPU) and runs the shared
  engine, so CoreWeave can be compared with anything else built on that engine.

Inputs come from three places, all visible on the workbook:

* ``self.reported``     revenue and capex, from SEC's structured data;
* ``self.disclosed``    active power, contracted power, backlog, adjusted EBITDA, from the
                        company's own releases (``data/disclosed/CRWV.csv``);
* the assumptions register (``assumptions/CRWV.csv``).

Capacity is averaged over each quarter (opening and closing MW) because revenue is earned on
the fleet that was running, not on the fleet at quarter end. The forecast - capacity ramp,
contracts, capex, debt, cash - is the next version and will add ``E`` columns to these sheets.

How the workbook is made traceable: every line is computed twice. Python computes the value,
and the same arithmetic is declared as an Excel formula template, so a finance reader can click
a cell and follow it back. A test re-evaluates the formulas against the Python values.
"""

from __future__ import annotations

import calendar
import dataclasses
import math

import pandas as pd

from companies.assumptions import engine_inputs, load_assumptions, to_inputs_frame
from companies.base import BaseCompanyModel
from data.disclosed import disclosed_series
from engine.unit_economics import breakdown, capital_recovery_factor

MODE = "rental"  # CoreWeave sells GPU-hours, not tokens (S-1 p.95)
PER_HOUR = "USD/GPU-hour"

DRIVER_FORMULAS: dict[str, str] = {
    "active_power_mw_added": "={active_power_mw_end}-{active_power_mw_end@prev}",
    "active_power_mw_avg": "=({active_power_mw_end@prev}+{active_power_mw_end})/2",
    "gpus_installed_avg": "={active_power_mw_avg}*1000/{in.power_draw_kw}",
    "gpu_hours_m": "={gpus_installed_avg}*{hours_in_quarter}/1000000",
    "cumulative_capex_usd_m": "={cumulative_capex_usd_m@prev}+{capex_usd_m}",
    "cumulative_mw_added": "={cumulative_mw_added@prev}+{active_power_mw_added}",
    # IF guards the zero-rate case, where the annuity formula would divide by zero.
    "capital_recovery_factor": (
        "=IF({in.financing_rate}=0,1/{in.depreciation_years},"
        "{in.financing_rate}/(1-(1+{in.financing_rate})^-{in.depreciation_years}))"
    ),
}
DRIVER_ROWS: dict[str, tuple[str, str]] = {
    # item: (label, unit). Rows without a formula above are reported or disclosed facts.
    "active_power_mw_end": ("Active power, end of quarter", "MW"),
    "active_power_mw_added": ("Active power added", "MW"),
    "active_power_mw_avg": ("Active power, average", "MW"),
    "contracted_power_gw": ("Contracted power", "GW"),
    "revenue_backlog_usd_bn": ("Revenue backlog", "USD bn"),
    "hours_in_quarter": ("Hours in the quarter", "hours"),
    "gpus_installed_avg": ("GPUs installed, average (from MW and kW per GPU)", "GPUs"),
    "gpu_hours_m": ("Installed GPU-hours", "millions"),
    "revenue_usd_m": ("Revenue", "USD m"),
    "adjusted_ebitda_usd_m": ("Adjusted EBITDA", "USD m"),
    "capex_usd_m": ("Capital expenditure", "USD m"),
    "cumulative_capex_usd_m": ("Capital expenditure, cumulative", "USD m"),
    "cumulative_mw_added": ("Active power added, cumulative", "MW"),
    "capital_recovery_factor": ("Capital recovery factor (share of price due each year)", "ratio"),
}

OUTPUT_FORMULAS: dict[str, str] = {
    "revenue_per_mw_year_usd_m": "={drivers.revenue_usd_m}*4/{drivers.active_power_mw_avg}",
    "ebitda_per_mw_year_usd_m": (
        "={drivers.adjusted_ebitda_usd_m}*4/{drivers.active_power_mw_avg}"
    ),
    "adjusted_ebitda_margin": "={drivers.adjusted_ebitda_usd_m}/{drivers.revenue_usd_m}",
    "capex_per_mw_added_usd_m": ("={drivers.cumulative_capex_usd_m}/{drivers.cumulative_mw_added}"),
    "payback_years_per_mw": "={capex_per_mw_added_usd_m}/{ebitda_per_mw_year_usd_m}",
    "backlog_years_of_revenue": (
        "={drivers.revenue_backlog_usd_bn}*1000/({drivers.revenue_usd_m}*4)"
    ),
    "revenue_per_gpu_hour": "={drivers.revenue_usd_m}/{drivers.gpu_hours_m}",
    "cash_cost_per_gpu_hour": (
        "=({drivers.revenue_usd_m}-{drivers.adjusted_ebitda_usd_m})/{drivers.gpu_hours_m}"
    ),
    "cash_margin_per_gpu_hour": "={revenue_per_gpu_hour}-{cash_cost_per_gpu_hour}",
    "capital_charge_per_gpu_hour": (
        "={in.chip_cost}*(1-{in.residual_value_share}"
        "/(1+{in.financing_rate})^{in.depreciation_years})"
        "*{drivers.capital_recovery_factor}/8760"
    ),
    "margin_per_gpu_hour": "={cash_margin_per_gpu_hour}-{capital_charge_per_gpu_hour}",
    "payback_years_per_gpu": (
        '=IF({cash_margin_per_gpu_hour}>0,{in.chip_cost}/({cash_margin_per_gpu_hour}*8760),"never")'
    ),
}
OUTPUT_ROWS: dict[str, tuple[str, str]] = {
    "revenue_per_mw_year_usd_m": ("Revenue per MW, annualised", "USD m"),
    "ebitda_per_mw_year_usd_m": ("Adjusted EBITDA per MW, annualised", "USD m"),
    "adjusted_ebitda_margin": ("Adjusted EBITDA margin", "%"),
    "capex_per_mw_added_usd_m": ("Capex per MW added, cumulative since the first quarter", "USD m"),
    "payback_years_per_mw": ("Payback per MW: capex per MW / EBITDA per MW", "years"),
    "backlog_years_of_revenue": ("Backlog, years of current revenue", "years"),
    "revenue_per_gpu_hour": ("Revenue per installed GPU-hour", PER_HOUR),
    "cash_cost_per_gpu_hour": ("Cash cost per installed GPU-hour", PER_HOUR),
    "cash_margin_per_gpu_hour": ("Cash margin per installed GPU-hour", PER_HOUR),
    "capital_charge_per_gpu_hour": ("Capital charge per installed GPU-hour", PER_HOUR),
    "margin_per_gpu_hour": ("Fully loaded margin per installed GPU-hour", PER_HOUR),
    "payback_years_per_gpu": ("Payback per GPU (engine)", "years"),
    "disclosed_payback_years": ("Payback, as disclosed by the company", "years"),
}


def hours_in_quarter(period: str) -> int:
    """Wall-clock hours in a calendar quarter such as ``2025Q1`` (2,160 in a 90-day quarter)."""
    year, quarter = int(period[:4]), int(period[5])
    months = range(3 * quarter - 2, 3 * quarter + 1)
    return 24 * sum(calendar.monthrange(year, month)[1] for month in months)


def _is_next_quarter(previous: str, current: str) -> bool:
    """True when ``current`` (``2025Q2``) directly follows ``previous`` (``2025Q1``)."""
    py, pq, cy, cq = int(previous[:4]), int(previous[5]), int(current[:4]), int(current[5])
    return (cy, cq) == ((py, pq + 1) if pq < 4 else (py + 1, 1))


def _finite(value: float) -> float:
    """Infinity (a unit that never pays back) has no cell value; the formula shows 'never'."""
    return value if math.isfinite(value) else math.nan


class CoreWeave(BaseCompanyModel):
    """CoreWeave, Inc. - GPU cloud. Version 1: quarterly history per MW and per GPU-hour."""

    ticker = "CRWV"
    # name, cik and layer are copied from data.edgar.COMPANIES["CRWV"] by BaseCompanyModel.

    def _quarterly_usd_m(self, concept: str) -> pd.Series:
        series = self.reported_series(concept, "Q")
        values = series["val"].to_numpy(dtype="float64") / 1e6
        return pd.Series(values, index=series["period"].to_numpy(), dtype="float64")

    def build(self) -> None:
        """Turn reported facts, disclosed KPIs and assumptions into the three model frames."""
        register = load_assumptions(self.ticker, self.assumptions_dir)
        if register is None:
            raise NotImplementedError(
                f"{self.ticker}: no assumptions register at {self.assumptions_dir}; "
                "the model cannot run without one"
            )
        power = disclosed_series(self.disclosed, "active_power_mw")
        ebitda = disclosed_series(self.disclosed, "adjusted_ebitda_usd_m")
        revenue, capex = self._quarterly_usd_m("revenue"), self._quarterly_usd_m("capex")
        # A quarter is modelled when its opening and closing capacity, revenue, EBITDA and capex
        # are all known. The quarter before the first one only supplies opening capacity.
        periods = [
            current
            for previous, current in zip(power.index[:-1], power.index[1:], strict=True)
            if _is_next_quarter(previous, current)
            and all(current in series.index for series in (ebitda, revenue, capex))
        ]
        if not periods:
            raise NotImplementedError(
                f"{self.ticker}: no quarter has capacity, revenue, EBITDA and capex together; "
                "run scripts/refresh.py and check data/disclosed/CRWV.csv"
            )
        base = engine_inputs(register)
        self.inputs = to_inputs_frame(register)
        contracted = disclosed_series(self.disclosed, "contracted_power_gw")
        backlog = disclosed_series(self.disclosed, "revenue_backlog_usd_bn")
        crf = capital_recovery_factor(base.financing_rate, base.depreciation_years)
        payback_row = register.loc[register["name"] == "disclosed_cash_payback_years", "value"]

        drivers = pd.DataFrame(index=list(DRIVER_ROWS), columns=periods, dtype="float64")
        outputs = pd.DataFrame(index=list(OUTPUT_ROWS), columns=periods, dtype="float64")
        cumulative_capex = cumulative_mw = 0.0
        for period in periods:
            opening = power.iloc[power.index.get_loc(period) - 1]
            closing, hours = power[period], hours_in_quarter(period)
            average = (opening + closing) / 2
            gpu_hours_m = average * 1000 / base.power_draw_kw * hours / 1e6
            cumulative_capex += capex[period]
            cumulative_mw += closing - opening
            drivers[period] = pd.Series(
                {
                    "active_power_mw_end": closing,
                    "active_power_mw_added": closing - opening,
                    "active_power_mw_avg": average,
                    "contracted_power_gw": contracted.get(period, math.nan),
                    "revenue_backlog_usd_bn": backlog.get(period, math.nan),
                    "hours_in_quarter": hours,
                    "gpus_installed_avg": average * 1000 / base.power_draw_kw,
                    "gpu_hours_m": gpu_hours_m,
                    "revenue_usd_m": revenue[period],
                    "adjusted_ebitda_usd_m": ebitda[period],
                    "capex_usd_m": capex[period],
                    "cumulative_capex_usd_m": cumulative_capex,
                    "cumulative_mw_added": cumulative_mw,
                    "capital_recovery_factor": crf,
                }
            )
            # The engine sees the quarter as one GPU renting at the revenue it actually earned
            # per installed hour, with every cost above adjusted EBITDA as its cash cost.
            unit = breakdown(
                dataclasses.replace(
                    base,
                    utilization=1.0,
                    price_per_gpu_hour=revenue[period] / gpu_hours_m,
                    other_opex_per_gpu_hour=(revenue[period] - ebitda[period]) / gpu_hours_m,
                    electricity_price_kwh=0.0,
                    facility_cost_per_kw_month=0.0,
                ),
                MODE,
            )
            ebitda_per_mw = ebitda[period] * 4 / average
            capex_per_mw = cumulative_capex / cumulative_mw if cumulative_mw else math.nan
            outputs[period] = pd.Series(
                {
                    "revenue_per_mw_year_usd_m": revenue[period] * 4 / average,
                    "ebitda_per_mw_year_usd_m": ebitda_per_mw,
                    "adjusted_ebitda_margin": ebitda[period] / revenue[period],
                    "capex_per_mw_added_usd_m": capex_per_mw,
                    "payback_years_per_mw": capex_per_mw / ebitda_per_mw,
                    "backlog_years_of_revenue": backlog.get(period, math.nan)
                    * 1000
                    / (revenue[period] * 4),
                    "revenue_per_gpu_hour": unit["revenue_per_gpu_hour"],
                    "cash_cost_per_gpu_hour": unit["cash_cost_per_gpu_hour"],
                    "cash_margin_per_gpu_hour": unit["cash_margin_per_gpu_hour"],
                    "capital_charge_per_gpu_hour": unit["capital_cost_per_gpu_hour"],
                    "margin_per_gpu_hour": unit["margin_per_gpu_hour"],
                    "payback_years_per_gpu": _finite(unit["payback_months"] / 12.0),
                    "disclosed_payback_years": float(payback_row.iloc[0])
                    if not payback_row.empty
                    else math.nan,
                }
            )

        output_formulas = dict(OUTPUT_FORMULAS)
        if payback_row.empty:
            outputs = outputs.drop(index="disclosed_payback_years")
        else:
            output_formulas["disclosed_payback_years"] = "={in.disclosed_cash_payback_years}"
        drivers.attrs = {
            "labels": {k: v[0] for k, v in DRIVER_ROWS.items()},
            "units": {k: v[1] for k, v in DRIVER_ROWS.items()},
            "formulas": dict(DRIVER_FORMULAS),
        }
        outputs.attrs = {
            "labels": {k: OUTPUT_ROWS[k][0] for k in outputs.index},
            "units": {k: OUTPUT_ROWS[k][1] for k in outputs.index},
            "formulas": {k: v for k, v in output_formulas.items() if k in outputs.index},
        }
        self.drivers, self.outputs = drivers, outputs

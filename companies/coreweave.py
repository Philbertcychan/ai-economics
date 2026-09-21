"""CoreWeave (CRWV) - the first neocloud in the repository.

Version 0 of the model is the UNIT-ECONOMICS view only: what one installed GPU costs and earns
per hour on fleet averages, taken from the S-1 (``assumptions/CRWV.csv``). It is one period
wide on purpose. The operating model - capacity, contracts, capex, debt and cash by quarter -
is the next step (TODO.md, Stage 1) and will add period columns to the same sheets.

How the workbook is made traceable: every line below is computed twice. Python computes the
value with the engine, and the same arithmetic is declared as an Excel formula template, so a
finance reader can click a cell and follow it back to the blue inputs. A test checks that the
two agree, which is what keeps the formulas honest.
"""

from __future__ import annotations

import math

import pandas as pd

from companies.assumptions import engine_inputs, load_assumptions, to_inputs_frame
from companies.base import BaseCompanyModel
from engine.unit_economics import breakdown, capital_recovery_factor

# The quarter the register's derived figures are measured on (Q4 2024, the last in the S-1).
BASIS_PERIOD = "2024Q4"
MODE = "rental"  # CoreWeave sells GPU-hours, not tokens (S-1 p.95)
PER_HOUR = "USD/GPU-hour"

DRIVER_FORMULAS: dict[str, str] = {
    # IF guards the zero-rate case, where the annuity formula would divide by zero.
    "capital_recovery_factor": (
        "=IF({in.financing_rate}=0,1/{in.depreciation_years},"
        "{in.financing_rate}/(1-(1+{in.financing_rate})^-{in.depreciation_years}))"
    ),
    "capital_cost_per_gpu_hour": (
        "={in.chip_cost}*(1-{in.residual_value_share}"
        "/(1+{in.financing_rate})^{in.depreciation_years})*{capital_recovery_factor}/8760"
    ),
    "energy_cost_per_gpu_hour": (
        "={in.power_draw_kw}*{in.pue}*{in.electricity_price_kwh}"
        "*({in.utilization}+(1-{in.utilization})*{in.idle_power_share})"
    ),
    "facility_cost_per_gpu_hour": "={in.facility_cost_per_kw_month}*{in.power_draw_kw}*12/8760",
    "other_opex_per_gpu_hour": "={in.other_opex_per_gpu_hour}",
    "cash_cost_per_gpu_hour": (
        "={energy_cost_per_gpu_hour}+{facility_cost_per_gpu_hour}+{other_opex_per_gpu_hour}"
    ),
    "cost_per_gpu_hour": "={cash_cost_per_gpu_hour}+{capital_cost_per_gpu_hour}",
    "revenue_per_gpu_hour": "={in.price_per_gpu_hour}*{in.utilization}",
}
DRIVER_LABELS: dict[str, str] = {
    "capital_recovery_factor": "Capital recovery factor (share of price due each year)",
    "capital_cost_per_gpu_hour": "Capital charge",
    "energy_cost_per_gpu_hour": "Energy",
    "facility_cost_per_gpu_hour": "Data-centre space",
    "other_opex_per_gpu_hour": "Other operating cost",
    "cash_cost_per_gpu_hour": "Cash cost",
    "cost_per_gpu_hour": "Fully loaded cost",
    "revenue_per_gpu_hour": "Revenue",
}

OUTPUT_FORMULAS: dict[str, str] = {
    "cash_margin_per_gpu_hour": (
        "={drivers.revenue_per_gpu_hour}-{drivers.cash_cost_per_gpu_hour}"
    ),
    "cash_margin_share": "={cash_margin_per_gpu_hour}/{drivers.revenue_per_gpu_hour}",
    "margin_per_gpu_hour": "={drivers.revenue_per_gpu_hour}-{drivers.cost_per_gpu_hour}",
    "breakeven_price_per_gpu_hour": "={drivers.cost_per_gpu_hour}/{in.utilization}",
    "payback_years": (
        '=IF({cash_margin_per_gpu_hour}>0,{in.chip_cost}/({cash_margin_per_gpu_hour}*8760),"never")'
    ),
}
OUTPUT_LABELS: dict[str, str] = {
    "cash_margin_per_gpu_hour": "Cash margin",
    "cash_margin_share": "Cash margin, share of revenue",
    "margin_per_gpu_hour": "Fully loaded margin",
    "breakeven_price_per_gpu_hour": "Break-even rental price",
    "payback_years": "Payback, model",
    "disclosed_payback_years": "Payback, as disclosed by the company",
    "payback_gap_years": "Payback gap, model less disclosed",
}
OUTPUT_UNITS: dict[str, str] = {
    "cash_margin_per_gpu_hour": PER_HOUR,
    "cash_margin_share": "%",
    "margin_per_gpu_hour": PER_HOUR,
    "breakeven_price_per_gpu_hour": PER_HOUR,
    "payback_years": "years",
    "disclosed_payback_years": "years",
    "payback_gap_years": "years",
}


def _finite(value: float) -> float:
    """Infinity (a GPU that never pays back) has no cell value; the formula shows 'never'."""
    return value if math.isfinite(value) else math.nan


class CoreWeave(BaseCompanyModel):
    """CoreWeave, Inc. - GPU cloud. Version 0: unit economics on fleet averages."""

    ticker = "CRWV"
    # name, cik and layer are copied from data.edgar.COMPANIES["CRWV"] by BaseCompanyModel.

    def build(self) -> None:
        """Turn the assumptions register into the inputs, drivers and outputs frames."""
        register = load_assumptions(self.ticker, self.assumptions_dir)
        if register is None:
            raise NotImplementedError(
                f"{self.ticker}: no assumptions register at {self.assumptions_dir}; "
                "the model cannot run without one"
            )
        inputs = engine_inputs(register)
        values = breakdown(inputs, MODE)
        self.inputs = to_inputs_frame(register)

        driver_values = {
            "capital_recovery_factor": capital_recovery_factor(
                inputs.financing_rate, inputs.depreciation_years
            ),
            **{name: values[name] for name in DRIVER_FORMULAS if name in values},
        }
        drivers = pd.DataFrame({BASIS_PERIOD: [driver_values[k] for k in DRIVER_FORMULAS]})
        drivers.index = pd.Index(list(DRIVER_FORMULAS))
        drivers.attrs = {
            "labels": dict(DRIVER_LABELS),
            "units": {
                k: ("ratio" if k == "capital_recovery_factor" else PER_HOUR)
                for k in DRIVER_FORMULAS
            },
            "formulas": dict(DRIVER_FORMULAS),
        }

        revenue = values["revenue_per_gpu_hour"]
        output_values = {
            "cash_margin_per_gpu_hour": values["cash_margin_per_gpu_hour"],
            "cash_margin_share": values["cash_margin_per_gpu_hour"] / revenue
            if revenue
            else math.nan,
            "margin_per_gpu_hour": values["margin_per_gpu_hour"],
            "breakeven_price_per_gpu_hour": _finite(values["breakeven_price_per_gpu_hour"]),
            "payback_years": _finite(values["payback_months"] / 12.0),
        }
        formulas = dict(OUTPUT_FORMULAS)
        # The company's own payback figure is a comparison line, shown only when the register
        # carries it, so that the gap between model and disclosure sits on the workbook itself.
        disclosed = register.loc[register["name"] == "disclosed_cash_payback_years", "value"]
        if not disclosed.empty:
            output_values["disclosed_payback_years"] = float(disclosed.iloc[0])
            output_values["payback_gap_years"] = (
                output_values["payback_years"] - output_values["disclosed_payback_years"]
            )
            formulas["disclosed_payback_years"] = "={in.disclosed_cash_payback_years}"
            formulas["payback_gap_years"] = "={payback_years}-{disclosed_payback_years}"
        outputs = pd.DataFrame({BASIS_PERIOD: list(output_values.values())})
        outputs.index = pd.Index(list(output_values))
        outputs.attrs = {
            "labels": {k: OUTPUT_LABELS[k] for k in output_values},
            "units": {k: OUTPUT_UNITS[k] for k in output_values},
            "formulas": formulas,
        }
        self.drivers, self.outputs = drivers, outputs

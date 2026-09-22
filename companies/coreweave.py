"""CoreWeave (CRWV) - the first neocloud in the repository.

Six reported quarters (``A`` columns) and ten forecast quarters (``E`` columns) on the same
sheets, the way a finance model is laid out: reported facts hard-coded, everything else
computed.

What the model answers
----------------------
* What has a unit of capacity earned, per MW of active power (no assumptions) and per
  installed GPU-hour (one assumption, kW per GPU, plus the shared engine)?
* How fast does a GPU pay back, with and without the customer prepayments the company nets
  off in its own figure?
* If contracts keep being signed and capacity keeps going live at the assumed pace, what
  happens to revenue, cash, debt and the need for outside money?

Where the numbers come from
---------------------------
* ``self.reported``   revenue, capex, cash from operations, cash, receivables, debt principal,
                      interest on debt, debt raised and repaid, deferred revenue and its
                      movements - SEC structured data;
* ``self.disclosed``  active power, contracted power, revenue backlog, adjusted EBITDA and the
                      scheduled principal repayments - the company's releases and notes
                      (``data/disclosed/CRWV.csv``);
* the assumptions register (``assumptions/CRWV.csv``) for everything else.

The forecast chain, one column per quarter::

    contracted MW signed (assumption) -> bookings (rate x term) -> backlog; prepayments
    MW going live (assumption) -> active power, averaged -> revenue (rate per MW-year) -> EBITDA
    MW going live x capex per MW -> capex -> net debt raised (share of capex); maturities refinanced
    EBITDA - interest + change in deferred revenue - receivables build + other -> cash from ops
    cash + CFO - capex + net debt raised -> cash; shortfall below the minimum -> external funding

Capacity is averaged over each quarter because revenue is earned on the fleet that was
running, not the fleet at quarter end. External funding is equity-like: it bears no interest
and is not repaid, so it does not enter debt or leverage.

Two things the model does not do, and says so: prepaid revenue is recognised as a flat share
of the balance rather than by contract vintage, and capex goes live in the quarter it is spent.

How the workbook is made traceable: every computed line is written twice. Python computes the
value, and the same arithmetic is declared as an Excel formula template, so a finance reader can
click a cell and follow it back to the blue inputs. A test re-evaluates every formula in every
column against the Python value. In actual columns the debt, deferred-revenue and cash
roll-forwards tie to the reported balances through explicit "other movements" rows.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime as dt
import logging
import math

import pandas as pd

from companies.assumptions import engine_inputs, load_assumptions, to_inputs_frame
from companies.base import BaseCompanyModel
from data.disclosed import disclosed_series
from engine.unit_economics import (
    HOURS_PER_YEAR,
    GPUEconomicsInputs,
    breakdown,
    capital_recovery_factor,
)

log = logging.getLogger(__name__)

MODE = "rental"  # CoreWeave sells GPU-hours, not tokens (S-1 p.95)
FORECAST_QUARTERS = 10
PER_HOUR = "USD/GPU-hour"
# A maturity ladder is "as of" a balance-sheet date; one filed more than this long after the
# last actual quarter belongs to a later quarter and would double count repayments.
LADDER_MAX_AGE_DAYS = 75

ASSUMPTIONS_NEEDED = (
    "mw_added_per_quarter",
    "mw_contracted_per_quarter",
    "revenue_per_mw_year_usd_m",
    "adjusted_ebitda_margin",
    "capex_per_mw_usd_m",
    "contract_years",
    "prepayment_share_of_tcv",
    "prepaid_revenue_recognised_share_per_quarter",
    "cost_of_debt",
    "receivables_share_of_quarterly_revenue",
    "other_operating_cash_share_of_revenue",
    "debt_share_of_capex",
    "minimum_cash_usd_bn",
    "disclosed_cash_payback_years",
)

# ---- Drivers: (label, unit). Order is the order on the sheet. -------------------------------
DRIVER_ROWS: dict[str, tuple[str, str]] = {
    "active_power_mw_end": ("Active power, end of quarter", "MW"),
    "active_power_mw_added": ("Active power added (went live)", "MW"),
    "active_power_mw_avg": ("Active power, average", "MW"),
    "contracted_power_mw_added": ("Contracted power added (signed)", "MW"),
    "contracted_power_gw": ("Contracted power, end of quarter", "GW"),
    "pipeline_gw": ("Contracted but not yet active", "GW"),
    "hours_in_quarter": ("Hours in the quarter", "hours"),
    "gpus_installed_avg": ("GPUs installed, average (from MW and kW per GPU)", "GPUs"),
    "gpu_hours_m": ("Installed GPU-hours", "millions"),
    "revenue_usd_m": ("Revenue", "USD m"),
    "adjusted_ebitda_usd_m": ("Adjusted EBITDA", "USD m"),
    "bookings_tcv_usd_bn": ("Contracts signed (contracted MW added x rate x term)", "USD bn"),
    "revenue_backlog_usd_bn": ("Revenue backlog", "USD bn"),
    "capex_usd_m": ("Capital expenditure", "USD m"),
    "cumulative_capex_usd_m": ("Capital expenditure, cumulative since {first}", "USD m"),
    "cumulative_mw_added": ("Active power added, cumulative since {first}", "MW"),
    "prepayments_received_usd_m": ("Customer prepayments received", "USD m"),
    "prepaid_revenue_recognised_usd_m": ("Prepaid revenue recognised (non-cash)", "USD m"),
    "deferred_revenue_change_usd_m": ("Change in deferred revenue (cash flow statement)", "USD m"),
    "other_deferred_revenue_movements_usd_m": (
        "Other deferred revenue movements (plug to reported balance)",
        "USD m",
    ),
    "deferred_revenue_end_usd_m": ("Deferred revenue, end of quarter", "USD m"),
    "receivables_end_usd_m": ("Receivables, end of quarter", "USD m"),
    "receivables_build_usd_m": ("Receivables build (cash tied up in unpaid invoices)", "USD m"),
    "interest_on_debt_usd_m": ("Interest on debt", "USD m"),
    "other_operating_cash_usd_m": ("Other operating cash items", "USD m"),
    "cfo_usd_m": ("Cash from operations", "USD m"),
    "debt_repaid_usd_m": ("Debt repaid (scheduled principal in estimates)", "USD m"),
    "debt_drawn_usd_m": ("Debt raised, gross", "USD m"),
    "other_debt_movements_usd_m": ("Other debt movements (plug to reported principal)", "USD m"),
    "debt_principal_end_usd_m": ("Debt principal, end of quarter", "USD m"),
    "other_financing_usd_m": ("Other financing and investing (plug to reported cash)", "USD m"),
    "funding_required_usd_m": (
        "External funding required to hold minimum cash (equity-like, no interest)",
        "USD m",
    ),
    "cash_end_usd_m": ("Cash, end of quarter", "USD m"),
    "capital_recovery_factor": ("Capital recovery factor (share of price due each year)", "ratio"),
}

# Formula templates. A row in FORMULA_FROM_FORECAST is a reported fact in the actual columns
# and only becomes a formula from the first estimate column; every other template applies to
# all columns. Rows with no template are pasted: the repayment schedule, and plugs that are
# zero in estimates.
DRIVER_FORMULAS: dict[str, str] = {
    "active_power_mw_end": "={active_power_mw_end@prev}+{in.mw_added_per_quarter}",
    "active_power_mw_added": "={active_power_mw_end}-{active_power_mw_end@prev}",
    "active_power_mw_avg": "=({active_power_mw_end@prev}+{active_power_mw_end})/2",
    "contracted_power_mw_added": "=({contracted_power_gw}-{contracted_power_gw@prev})*1000",
    "contracted_power_gw": "={contracted_power_gw@prev}+{in.mw_contracted_per_quarter}/1000",
    "pipeline_gw": "={contracted_power_gw}-{active_power_mw_end}/1000",
    "gpus_installed_avg": "={active_power_mw_avg}*1000/{in.power_draw_kw}",
    "gpu_hours_m": "={gpus_installed_avg}*{hours_in_quarter}/1000000",
    "revenue_usd_m": (
        "={in.revenue_per_mw_year_usd_m}*{active_power_mw_avg}*{hours_in_quarter}/8760"
    ),
    "adjusted_ebitda_usd_m": "={revenue_usd_m}*{in.adjusted_ebitda_margin}",
    "bookings_tcv_usd_bn": (
        "={contracted_power_mw_added}*{in.revenue_per_mw_year_usd_m}*{in.contract_years}/1000"
    ),
    "revenue_backlog_usd_bn": (
        "={revenue_backlog_usd_bn@prev}+{bookings_tcv_usd_bn}-{revenue_usd_m}/1000"
    ),
    "capex_usd_m": "={in.capex_per_mw_usd_m}*{active_power_mw_added}",
    "cumulative_capex_usd_m": "={cumulative_capex_usd_m@prev}+{capex_usd_m}",
    "cumulative_mw_added": "={cumulative_mw_added@prev}+{active_power_mw_added}",
    "prepayments_received_usd_m": "={bookings_tcv_usd_bn}*1000*{in.prepayment_share_of_tcv}",
    "prepaid_revenue_recognised_usd_m": (
        "={deferred_revenue_end_usd_m@prev}*{in.prepaid_revenue_recognised_share_per_quarter}"
    ),
    "deferred_revenue_change_usd_m": (
        "={prepayments_received_usd_m}-{prepaid_revenue_recognised_usd_m}"
    ),
    "deferred_revenue_end_usd_m": (
        "={deferred_revenue_end_usd_m@prev}+{deferred_revenue_change_usd_m}"
        "+{other_deferred_revenue_movements_usd_m}"
    ),
    "receivables_end_usd_m": (
        "={receivables_end_usd_m@prev}+({revenue_usd_m}-{revenue_usd_m@prev})"
        "*{in.receivables_share_of_quarterly_revenue}"
    ),
    "receivables_build_usd_m": "={receivables_end_usd_m}-{receivables_end_usd_m@prev}",
    "interest_on_debt_usd_m": (
        "=({debt_principal_end_usd_m@prev}+{debt_principal_end_usd_m})/2*{in.cost_of_debt}/4"
    ),
    "other_operating_cash_usd_m": "={revenue_usd_m}*{in.other_operating_cash_share_of_revenue}",
    "cfo_usd_m": (
        "={adjusted_ebitda_usd_m}-{interest_on_debt_usd_m}+{deferred_revenue_change_usd_m}"
        "-{receivables_build_usd_m}+{other_operating_cash_usd_m}"
    ),
    "debt_drawn_usd_m": "={capex_usd_m}*{in.debt_share_of_capex}+{debt_repaid_usd_m}",
    "debt_principal_end_usd_m": (
        "={debt_principal_end_usd_m@prev}+{debt_drawn_usd_m}-{debt_repaid_usd_m}"
        "+{other_debt_movements_usd_m}"
    ),
    "funding_required_usd_m": (
        "=MAX(0,{in.minimum_cash_usd_bn}*1000-({cash_end_usd_m@prev}+{cfo_usd_m}"
        "-{capex_usd_m}+{debt_drawn_usd_m}-{debt_repaid_usd_m}+{other_financing_usd_m}))"
    ),
    "cash_end_usd_m": (
        "={cash_end_usd_m@prev}+{cfo_usd_m}-{capex_usd_m}+{debt_drawn_usd_m}"
        "-{debt_repaid_usd_m}+{other_financing_usd_m}+{funding_required_usd_m}"
    ),
    "capital_recovery_factor": (
        "=IF({in.financing_rate}=0,1/{in.depreciation_years},"
        "{in.financing_rate}/(1-(1+{in.financing_rate})^-{in.depreciation_years}))"
    ),
}
FORMULA_FROM_FORECAST: frozenset[str] = frozenset(
    {
        "active_power_mw_end",
        "contracted_power_gw",
        "revenue_usd_m",
        "adjusted_ebitda_usd_m",
        "bookings_tcv_usd_bn",
        "revenue_backlog_usd_bn",
        "capex_usd_m",
        "prepayments_received_usd_m",
        "prepaid_revenue_recognised_usd_m",
        "deferred_revenue_change_usd_m",
        "deferred_revenue_end_usd_m",
        "receivables_end_usd_m",
        "interest_on_debt_usd_m",
        "other_operating_cash_usd_m",
        "cfo_usd_m",
        "debt_drawn_usd_m",
        "debt_principal_end_usd_m",
        "funding_required_usd_m",
        "cash_end_usd_m",
    }
)
# Rows the forecast rolls forward from; the last actual must have them all.
OPENING_BALANCES = (
    "active_power_mw_end",
    "contracted_power_gw",
    "revenue_backlog_usd_bn",
    "revenue_usd_m",
    "deferred_revenue_end_usd_m",
    "receivables_end_usd_m",
    "debt_principal_end_usd_m",
    "cash_end_usd_m",
)

# ---- Outputs -------------------------------------------------------------------------------
OUTPUT_ROWS: dict[str, tuple[str, str]] = {
    "revenue_per_mw_year_usd_m": ("Revenue per MW, annualised", "USD m"),
    "ebitda_per_mw_year_usd_m": ("Adjusted EBITDA per MW, annualised", "USD m"),
    "adjusted_ebitda_margin": ("Adjusted EBITDA margin", "%"),
    "capex_per_mw_added_usd_m": ("Capex per MW added, cumulative since {first}", "USD m"),
    "capex_per_gpu_usd": ("Capex per GPU added (capex per MW x kW per GPU)", "USD"),
    "payback_years_per_mw": ("Payback per MW: capex per MW / EBITDA per MW", "years"),
    "backlog_years_of_revenue": ("Backlog, years of annualised current revenue", "years"),
    "revenue_per_gpu_hour": ("Revenue per installed GPU-hour", PER_HOUR),
    "cash_cost_per_gpu_hour": ("Cash cost per installed GPU-hour", PER_HOUR),
    "cash_margin_per_gpu_hour": ("Cash margin per installed GPU-hour", PER_HOUR),
    "capital_charge_per_gpu_hour": ("Capital charge per installed GPU-hour", PER_HOUR),
    "margin_per_gpu_hour": ("Fully loaded margin per installed GPU-hour", PER_HOUR),
    "tcv_per_gpu_usd": ("Contract value per GPU (rate x hours x term)", "USD"),
    "prepayment_per_gpu_usd": ("Prepayment per GPU", "USD"),
    "payback_years_per_gpu": ("Payback per GPU, gross of prepayment", "years"),
    "payback_years_company_definition": (
        "Payback per GPU, net of prepayment (company's definition)",
        "years",
    ),
    "payback_years_strict_cash": (
        "Payback per GPU, strict cash timing (prepayment credited at contract end)",
        "years",
    ),
    "disclosed_payback_years": ("Payback, as disclosed by the company", "years"),
    "free_cash_flow_usd_m": ("Free cash flow (CFO less capex)", "USD m"),
    "net_debt_usd_m": ("Net debt", "USD m"),
    "net_debt_to_ebitda": ("Net debt / annualised adjusted EBITDA", "x"),
    "interest_cover": ("Adjusted EBITDA / interest on debt", "x"),
    "cumulative_funding_required_usd_m": (
        "External funding required, cumulative over estimate quarters",
        "USD m",
    ),
}
OUTPUT_FORMULAS: dict[str, str] = {
    "revenue_per_mw_year_usd_m": "={drivers.revenue_usd_m}*4/{drivers.active_power_mw_avg}",
    "ebitda_per_mw_year_usd_m": (
        "={drivers.adjusted_ebitda_usd_m}*4/{drivers.active_power_mw_avg}"
    ),
    "adjusted_ebitda_margin": "={drivers.adjusted_ebitda_usd_m}/{drivers.revenue_usd_m}",
    "capex_per_mw_added_usd_m": ("={drivers.cumulative_capex_usd_m}/{drivers.cumulative_mw_added}"),
    "capex_per_gpu_usd": "={capex_per_mw_added_usd_m}*1000*{in.power_draw_kw}",
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
    "tcv_per_gpu_usd": "={revenue_per_gpu_hour}*8760*{in.contract_years}",
    "prepayment_per_gpu_usd": "={tcv_per_gpu_usd}*{in.prepayment_share_of_tcv}",
    "payback_years_per_gpu": (
        '=IF({cash_margin_per_gpu_hour}>0,{in.chip_cost}/({cash_margin_per_gpu_hour}*8760),"never")'
    ),
    "payback_years_company_definition": (
        "=IF({cash_margin_per_gpu_hour}>0,"
        '({in.chip_cost}-{prepayment_per_gpu_usd})/({cash_margin_per_gpu_hour}*8760),"never")'
    ),
    # The prepayment is cash at signing and is credited against the FINAL months of the
    # contract (S-1 p.96). Until that window the GPU bills its full revenue, so cash payback is
    # (price - prepayment) / margin if that lands before the window. Otherwise the window pays
    # costs without billing, and the deficit left at contract end is repaid at the plain
    # margin. The window is prepayment / annual revenue, in years, capped at the term.
    "payback_years_strict_cash": (
        '=IF({cash_margin_per_gpu_hour}<=0,"never",'
        "IF(({in.chip_cost}-{prepayment_per_gpu_usd})/({cash_margin_per_gpu_hour}*8760)"
        "<={in.contract_years}-MIN({in.contract_years},"
        "{prepayment_per_gpu_usd}/({revenue_per_gpu_hour}*8760)),"
        "({in.chip_cost}-{prepayment_per_gpu_usd})/({cash_margin_per_gpu_hour}*8760),"
        "{in.contract_years}+({in.chip_cost}-{prepayment_per_gpu_usd}"
        "-{cash_margin_per_gpu_hour}*8760*({in.contract_years}-MIN({in.contract_years},"
        "{prepayment_per_gpu_usd}/({revenue_per_gpu_hour}*8760)))"
        "+{cash_cost_per_gpu_hour}*8760*MIN({in.contract_years},"
        "{prepayment_per_gpu_usd}/({revenue_per_gpu_hour}*8760)))"
        "/({cash_margin_per_gpu_hour}*8760)))"
    ),
    "disclosed_payback_years": "={in.disclosed_cash_payback_years}",
    "free_cash_flow_usd_m": "={drivers.cfo_usd_m}-{drivers.capex_usd_m}",
    "net_debt_usd_m": "={drivers.debt_principal_end_usd_m}-{drivers.cash_end_usd_m}",
    "net_debt_to_ebitda": "={net_debt_usd_m}/({drivers.adjusted_ebitda_usd_m}*4)",
    "interest_cover": "={drivers.adjusted_ebitda_usd_m}/{drivers.interest_on_debt_usd_m}",
    "cumulative_funding_required_usd_m": (
        "={cumulative_funding_required_usd_m@prev}+{drivers.funding_required_usd_m}"
    ),
}

SENSITIVITY_COLUMNS = (
    "value",
    "external_funding_usd_m",
    "minimum_cash_usd_m",
    "net_debt_to_ebitda_end",
    "payback_company_definition_end",
    "revenue_final_year_usd_bn",
)


def hours_in_quarter(period: str) -> int:
    """Wall-clock hours in a calendar quarter such as ``2025Q1`` (2,160 in a 90-day quarter)."""
    year, quarter = int(period[:4]), int(period[5])
    months = range(3 * quarter - 2, 3 * quarter + 1)
    return 24 * sum(calendar.monthrange(year, month)[1] for month in months)


def quarter_end(period: str) -> dt.date:
    """Last day of a calendar quarter such as ``2026Q2`` -> 2026-06-30."""
    year, quarter = int(period[:4]), int(period[5])
    month = 3 * quarter
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def next_quarter(period: str) -> str:
    """``2025Q4`` -> ``2026Q1``."""
    year, quarter = int(period[:4]), int(period[5])
    return f"{year + 1}Q1" if quarter == 4 else f"{year}Q{quarter + 1}"


def _is_next_quarter(previous: str, current: str) -> bool:
    """True when ``current`` (``2025Q2``) directly follows ``previous`` (``2025Q1``)."""
    return next_quarter(previous) == current


def _finite(value: float) -> float:
    """Infinity (a unit that never pays back) has no cell value; the formula shows 'never'."""
    return value if math.isfinite(value) else math.nan


def payback_with_prepayment(
    chip_cost: float,
    revenue_per_hour: float,
    cash_margin_per_hour: float,
    prepayment: float,
    contract_years: float,
) -> tuple[float, float]:
    """(company's definition, strict cash timing) payback in years for one GPU.

    The company nets the prepayment off the investment and divides by EBITDA. The strict
    version follows the cash as the S-1 describes it: the prepayment arrives at signing and is
    credited against the final months of the contract. Until that window the GPU bills its
    full revenue, so the two definitions agree whenever the GPU pays back before the window.
    Inside the window nothing is billed while costs continue; whatever deficit is left at the
    end of the contract is repaid at the plain margin afterwards.
    """
    margin_per_year = cash_margin_per_hour * HOURS_PER_YEAR
    if margin_per_year <= 0:
        return math.inf, math.inf
    company = (chip_cost - prepayment) / margin_per_year
    revenue_per_year = revenue_per_hour * HOURS_PER_YEAR
    window = min(contract_years, prepayment / revenue_per_year) if revenue_per_year else 0.0
    if company <= contract_years - window:
        return company, company
    cost_per_year = revenue_per_year - margin_per_year
    deficit_at_end = (
        chip_cost
        - prepayment
        - margin_per_year * (contract_years - window)
        + cost_per_year * window
    )
    return company, contract_years + deficit_at_end / margin_per_year


def _scheduled_repayment(due: pd.Series, period: str, last_actual: str) -> float:
    """Principal due in a forecast quarter from the maturity ladder, spread evenly by year.

    The ladder gives one figure per calendar year. The first year is "remainder of the year"
    after the last reported quarter, so it is spread over the quarters that remain.
    """
    year = period[:4]
    if year not in due.index:
        return 0.0
    quarters_in_year = 4
    if year == last_actual[:4]:
        quarters_in_year = 4 - int(last_actual[5])
    return float(due[year]) / quarters_in_year


class CoreWeave(BaseCompanyModel):
    """CoreWeave, Inc. - GPU cloud: quarterly history and a ten-quarter forecast."""

    ticker = "CRWV"
    # name, cik and layer are copied from data.edgar.COMPANIES["CRWV"] by BaseCompanyModel.

    def _quarterly_usd_m(self, concept: str) -> pd.Series:
        series = self.reported_series(concept, "Q")
        values = series["val"].to_numpy(dtype="float64") / 1e6
        return pd.Series(values, index=series["period"].to_numpy(), dtype="float64")

    # -- build --------------------------------------------------------------------------

    def build(self) -> None:
        """Turn reported facts, disclosed KPIs and assumptions into the model frames.

        Also runs one-at-a-time sensitivities over every register row that has a range and
        publishes them as an extra sheet.
        """
        register = load_assumptions(self.ticker, self.assumptions_dir)
        if register is None:
            raise NotImplementedError(
                f"{self.ticker}: no assumptions register at {self.assumptions_dir}; "
                "the model cannot run without one"
            )
        a = register.set_index("name")["value"].astype("float64")
        missing = [name for name in ASSUMPTIONS_NEEDED if name not in a.index]
        if missing:
            raise ValueError(f"{self.ticker}: assumptions register is missing {missing}")
        self.inputs = to_inputs_frame(register)
        data = self._gather()

        columns, first_estimate = self._compute(a, engine_inputs(register), data)
        first_actual = next(iter(columns))[:-1]
        self.drivers = self._frame(columns, DRIVER_ROWS)
        self.drivers.attrs = {
            "labels": {k: v[0].format(first=first_actual) for k, v in DRIVER_ROWS.items()},
            "units": {k: v[1] for k, v in DRIVER_ROWS.items()},
            "formulas": dict(DRIVER_FORMULAS),
            "formula_starts": {k: first_estimate for k in FORMULA_FROM_FORECAST},
        }
        self.outputs = self._frame(columns, OUTPUT_ROWS)
        self.outputs.attrs = {
            "labels": {k: v[0].format(first=first_actual) for k, v in OUTPUT_ROWS.items()},
            "units": {k: v[1] for k, v in OUTPUT_ROWS.items()},
            "formulas": dict(OUTPUT_FORMULAS),
        }
        self.extra_frames["sensitivities"] = self._sensitivities(register, a, data)

    @staticmethod
    def _frame(columns: dict[str, dict[str, float]], rows: dict) -> pd.DataFrame:
        return pd.DataFrame(
            {col: [vals[k] for k in rows] for col, vals in columns.items()},
            index=list(rows),
            dtype="float64",
        )

    def _gather(self) -> dict:
        """Everything the computation reads, pulled once so sensitivities can reuse it."""
        rep = {
            concept: self._quarterly_usd_m(concept)
            for concept in (
                "revenue",
                "capex",
                "cfo",
                "cash",
                "receivables",
                "debt_principal",
                "interest_expense_debt",
                "debt_proceeds",
                "debt_repayments",
                "deferred_revenue",
                "deferred_revenue_change",
                "deferred_revenue_recognised",
            )
        }
        ladder = (
            self.disclosed[self.disclosed["kpi"] == "debt_principal_due_usd_m"]
            if self.disclosed is not None
            else None
        )
        return {
            "rep": rep,
            "power": disclosed_series(self.disclosed, "active_power_mw"),
            "ebitda": disclosed_series(self.disclosed, "adjusted_ebitda_usd_m"),
            "contracted": disclosed_series(self.disclosed, "contracted_power_gw"),
            "backlog": disclosed_series(self.disclosed, "revenue_backlog_usd_bn"),
            "due": disclosed_series(self.disclosed, "debt_principal_due_usd_m"),
            "ladder_filed": (
                max(ladder["filed"]) if ladder is not None and not ladder.empty else None
            ),
        }

    def _actual_quarters(self, data: dict) -> list[str]:
        """The reported quarters the model can treat as actuals, checked for gaps."""
        rep, power, ebitda = data["rep"], data["power"], data["ebitda"]
        # A quarter is an actual when its opening and closing capacity, revenue, EBITDA and
        # capex are all known. The quarter before the first only supplies opening capacity.
        actual = [
            current
            for previous, current in zip(power.index[:-1], power.index[1:], strict=True)
            if _is_next_quarter(previous, current)
            and all(current in s.index for s in (ebitda, rep["revenue"], rep["capex"]))
        ]
        if not actual:
            raise NotImplementedError(
                f"{self.ticker}: no quarter has capacity, revenue, EBITDA and capex together; "
                "run scripts/refresh.py and check data/disclosed/CRWV.csv"
            )
        for previous, current in zip(actual[:-1], actual[1:], strict=True):
            # A gap would make the @prev formulas on the sheet refer to the wrong quarter.
            if not _is_next_quarter(previous, current):
                raise ValueError(
                    f"{self.ticker}: actual quarters are not contiguous ({previous} then "
                    f"{current}); a KPI is missing from data/disclosed/CRWV.csv in between"
                )
        latest_reported = max(rep["revenue"].index) if len(rep["revenue"]) else None
        if latest_reported and latest_reported > actual[-1]:
            log.warning(
                "%s: revenue is reported for %s but its operating KPIs are not in "
                "data/disclosed/CRWV.csv, so that quarter is forecast rather than actual",
                self.ticker,
                latest_reported,
            )
        return actual

    def _check_ladder(self, data: dict, last_actual: str) -> None:
        """The maturity ladder must be the one filed for the last actual quarter."""
        filed = data["ladder_filed"]
        if filed is None:
            raise ValueError(f"{self.ticker}: no debt maturity ladder in data/disclosed/CRWV.csv")
        filed_date = dt.date.fromisoformat(str(filed))
        end = quarter_end(last_actual)
        if not (end < filed_date <= end + dt.timedelta(days=LADDER_MAX_AGE_DAYS)):
            raise ValueError(
                f"{self.ticker}: the debt maturity ladder was filed {filed_date}, which is not "
                f"the filing for {last_actual}; update debt_principal_due_usd_m in "
                "data/disclosed/CRWV.csv"
            )

    def _compute(
        self, a: pd.Series, base: GPUEconomicsInputs, data: dict
    ) -> tuple[dict[str, dict[str, float]], str]:
        """All actual and forecast columns for one set of assumptions."""
        rep, power = data["rep"], data["power"]
        ebitda, contracted, backlog, due = (
            data["ebitda"],
            data["contracted"],
            data["backlog"],
            data["due"],
        )
        actual = self._actual_quarters(data)
        self._check_ladder(data, actual[-1])
        crf = capital_recovery_factor(base.financing_rate, base.depreciation_years)
        columns: dict[str, dict[str, float]] = {}
        cumulative_capex = cumulative_mw = cumulative_funding = 0.0

        def get(series: pd.Series, period: str) -> float:
            return float(series.get(period, math.nan))

        # ---- actual quarters ---------------------------------------------------------------
        for period in actual:
            previous = power.index[power.index.get_loc(period) - 1]
            opening, closing = power[previous], power[period]
            hours = hours_in_quarter(period)
            capex = rep["capex"][period]
            cumulative_capex += capex
            cumulative_mw += closing - opening
            recognised = get(rep["deferred_revenue_recognised"], period)
            change = get(rep["deferred_revenue_change"], period)
            cfo = get(rep["cfo"], period)
            interest = get(rep["interest_expense_debt"], period)
            drawn = get(rep["debt_proceeds"], period)
            repaid = get(rep["debt_repayments"], period)
            receivables = get(rep["receivables"], period)
            build = receivables - get(rep["receivables"], previous)
            deferred = get(rep["deferred_revenue"], period)
            debt = get(rep["debt_principal"], period)
            cash = get(rep["cash"], period)
            d = {
                "active_power_mw_end": closing,
                "active_power_mw_added": closing - opening,
                "active_power_mw_avg": (opening + closing) / 2,
                "contracted_power_mw_added": (get(contracted, period) - get(contracted, previous))
                * 1000,
                "contracted_power_gw": get(contracted, period),
                "pipeline_gw": get(contracted, period) - closing / 1000,
                "hours_in_quarter": hours,
                "revenue_usd_m": rep["revenue"][period],
                "adjusted_ebitda_usd_m": ebitda[period],
                "bookings_tcv_usd_bn": math.nan,
                "revenue_backlog_usd_bn": get(backlog, period),
                "capex_usd_m": capex,
                "cumulative_capex_usd_m": cumulative_capex,
                "cumulative_mw_added": cumulative_mw,
                "prepayments_received_usd_m": change + recognised,
                "prepaid_revenue_recognised_usd_m": recognised,
                "deferred_revenue_change_usd_m": change,
                # Reported balances move by more than the cash-flow lines say (rounding of the
                # disclosed balance, non-cash additions). The plugs make the sheet tie.
                "other_deferred_revenue_movements_usd_m": (
                    deferred - get(rep["deferred_revenue"], previous) - change
                ),
                "deferred_revenue_end_usd_m": deferred,
                "receivables_end_usd_m": receivables,
                "receivables_build_usd_m": build,
                "interest_on_debt_usd_m": interest,
                "other_operating_cash_usd_m": cfo - (ebitda[period] - interest + change - build),
                "cfo_usd_m": cfo,
                "debt_repaid_usd_m": repaid,
                "debt_drawn_usd_m": drawn,
                "other_debt_movements_usd_m": (
                    debt - get(rep["debt_principal"], previous) - drawn + repaid
                ),
                "debt_principal_end_usd_m": debt,
                "other_financing_usd_m": (
                    cash - get(rep["cash"], previous) - cfo + capex - drawn + repaid
                ),
                "funding_required_usd_m": 0.0,
                "cash_end_usd_m": cash,
                "capital_recovery_factor": crf,
            }
            columns[f"{period}A"] = self._with_derived(d, base, a)

        last = actual[-1]
        prev = columns[f"{last}A"]
        blank = [name for name in OPENING_BALANCES if math.isnan(prev[name])]
        if blank:
            # Excel treats a blank cell as 0, so a missing opening balance would silently
            # zero every roll-forward that follows.
            raise ValueError(
                f"{self.ticker}: the last actual quarter {last} lacks {blank}; the forecast "
                "cannot roll forward from a blank"
            )

        # ---- forecast quarters -------------------------------------------------------------
        period = last
        for _ in range(FORECAST_QUARTERS):
            period = next_quarter(period)
            hours = hours_in_quarter(period)
            added = float(a["mw_added_per_quarter"])
            signed = float(a["mw_contracted_per_quarter"])
            closing = prev["active_power_mw_end"] + added
            average = (prev["active_power_mw_end"] + closing) / 2
            contracted_end = prev["contracted_power_gw"] + signed / 1000
            revenue = float(a["revenue_per_mw_year_usd_m"]) * average * hours / HOURS_PER_YEAR
            ebitda_e = revenue * float(a["adjusted_ebitda_margin"])
            bookings = signed * float(a["revenue_per_mw_year_usd_m"]) * float(a["contract_years"])
            received = bookings * float(a["prepayment_share_of_tcv"])
            recognised = prev["deferred_revenue_end_usd_m"] * float(
                a["prepaid_revenue_recognised_share_per_quarter"]
            )
            change = received - recognised
            receivables = prev["receivables_end_usd_m"] + (revenue - prev["revenue_usd_m"]) * float(
                a["receivables_share_of_quarterly_revenue"]
            )
            build = receivables - prev["receivables_end_usd_m"]
            capex = float(a["capex_per_mw_usd_m"]) * added
            repaid = _scheduled_repayment(due, period, last)
            # The debt share is calibrated NET of repayments, so maturities are refinanced.
            drawn = capex * float(a["debt_share_of_capex"]) + repaid
            debt_end = prev["debt_principal_end_usd_m"] + drawn - repaid
            interest = (
                (prev["debt_principal_end_usd_m"] + debt_end) / 2 * float(a["cost_of_debt"]) / 4
            )
            other = revenue * float(a["other_operating_cash_share_of_revenue"])
            cfo = ebitda_e - interest + change - build + other
            other_financing = 0.0
            before_funding = prev["cash_end_usd_m"] + cfo - capex + drawn - repaid + other_financing
            funding = max(0.0, float(a["minimum_cash_usd_bn"]) * 1000 - before_funding)
            cumulative_capex += capex
            cumulative_mw += added
            cumulative_funding += funding
            d = {
                "active_power_mw_end": closing,
                "active_power_mw_added": added,
                "active_power_mw_avg": average,
                "contracted_power_mw_added": signed,
                "contracted_power_gw": contracted_end,
                "pipeline_gw": contracted_end - closing / 1000,
                "hours_in_quarter": hours,
                "revenue_usd_m": revenue,
                "adjusted_ebitda_usd_m": ebitda_e,
                "bookings_tcv_usd_bn": bookings / 1000,
                "revenue_backlog_usd_bn": prev["revenue_backlog_usd_bn"]
                + bookings / 1000
                - revenue / 1000,
                "capex_usd_m": capex,
                "cumulative_capex_usd_m": cumulative_capex,
                "cumulative_mw_added": cumulative_mw,
                "prepayments_received_usd_m": received,
                "prepaid_revenue_recognised_usd_m": recognised,
                "deferred_revenue_change_usd_m": change,
                "other_deferred_revenue_movements_usd_m": 0.0,
                "deferred_revenue_end_usd_m": prev["deferred_revenue_end_usd_m"] + change,
                "receivables_end_usd_m": receivables,
                "receivables_build_usd_m": build,
                "interest_on_debt_usd_m": interest,
                "other_operating_cash_usd_m": other,
                "cfo_usd_m": cfo,
                "debt_repaid_usd_m": repaid,
                "debt_drawn_usd_m": drawn,
                "other_debt_movements_usd_m": 0.0,
                "debt_principal_end_usd_m": debt_end,
                "other_financing_usd_m": other_financing,
                "funding_required_usd_m": funding,
                "cash_end_usd_m": before_funding + funding,
                "capital_recovery_factor": crf,
            }
            columns[f"{period}E"] = self._with_derived(d, base, a, cumulative_funding)
            prev = columns[f"{period}E"]
        return columns, f"{next_quarter(last)}E"

    @staticmethod
    def _with_derived(
        d: dict[str, float],
        base: GPUEconomicsInputs,
        a: pd.Series,
        cumulative_funding: float = 0.0,
    ) -> dict[str, float]:
        """Add the output lines that follow from one quarter's drivers."""
        gpus = d["active_power_mw_avg"] * 1000 / base.power_draw_kw
        gpu_hours_m = gpus * d["hours_in_quarter"] / 1e6
        d["gpus_installed_avg"], d["gpu_hours_m"] = gpus, gpu_hours_m
        revenue, ebitda = d["revenue_usd_m"], d["adjusted_ebitda_usd_m"]
        # The engine sees the quarter as one GPU renting at the revenue it actually earned per
        # installed hour, with every cost above adjusted EBITDA as its cash cost. Utilisation,
        # electricity and facility cost are pinned because they are already inside those two
        # figures; the register says so on the three rows.
        unit = breakdown(
            dataclasses.replace(
                base,
                utilization=1.0,
                price_per_gpu_hour=revenue / gpu_hours_m,
                other_opex_per_gpu_hour=(revenue - ebitda) / gpu_hours_m,
                electricity_price_kwh=0.0,
                facility_cost_per_kw_month=0.0,
            ),
            MODE,
        )
        margin = unit["cash_margin_per_gpu_hour"]
        tcv = unit["revenue_per_gpu_hour"] * HOURS_PER_YEAR * float(a["contract_years"])
        prepayment = tcv * float(a["prepayment_share_of_tcv"])
        company, strict = payback_with_prepayment(
            base.chip_cost,
            unit["revenue_per_gpu_hour"],
            margin,
            prepayment,
            float(a["contract_years"]),
        )
        ebitda_per_mw = ebitda * 4 / d["active_power_mw_avg"]
        capex_per_mw = (
            d["cumulative_capex_usd_m"] / d["cumulative_mw_added"]
            if d["cumulative_mw_added"]
            else math.nan
        )
        interest = d["interest_on_debt_usd_m"]
        d.update(
            {
                "revenue_per_mw_year_usd_m": revenue * 4 / d["active_power_mw_avg"],
                "ebitda_per_mw_year_usd_m": ebitda_per_mw,
                "adjusted_ebitda_margin": ebitda / revenue,
                "capex_per_mw_added_usd_m": capex_per_mw,
                "capex_per_gpu_usd": capex_per_mw * 1000 * base.power_draw_kw,
                "payback_years_per_mw": capex_per_mw / ebitda_per_mw,
                "backlog_years_of_revenue": d["revenue_backlog_usd_bn"] * 1000 / (revenue * 4),
                "revenue_per_gpu_hour": unit["revenue_per_gpu_hour"],
                "cash_cost_per_gpu_hour": unit["cash_cost_per_gpu_hour"],
                "cash_margin_per_gpu_hour": margin,
                "capital_charge_per_gpu_hour": unit["capital_cost_per_gpu_hour"],
                "margin_per_gpu_hour": unit["margin_per_gpu_hour"],
                "tcv_per_gpu_usd": tcv,
                "prepayment_per_gpu_usd": prepayment,
                "payback_years_per_gpu": _finite(unit["payback_months"] / 12.0),
                "payback_years_company_definition": _finite(company),
                "payback_years_strict_cash": _finite(strict),
                "disclosed_payback_years": float(a["disclosed_cash_payback_years"]),
                "free_cash_flow_usd_m": d["cfo_usd_m"] - d["capex_usd_m"],
                "net_debt_usd_m": d["debt_principal_end_usd_m"] - d["cash_end_usd_m"],
                "net_debt_to_ebitda": (d["debt_principal_end_usd_m"] - d["cash_end_usd_m"])
                / (ebitda * 4),
                "interest_cover": ebitda / interest if interest else math.nan,
                "cumulative_funding_required_usd_m": cumulative_funding,
            }
        )
        return d

    # -- sensitivities ------------------------------------------------------------------

    def _sensitivities(self, register: pd.DataFrame, a: pd.Series, data: dict) -> pd.DataFrame:
        """One-at-a-time: each register row with a range at its low and high, rest held.

        Reports what a finance reader asks first: does the plan need outside money, how low
        does cash go, where does leverage end, and what the per-GPU payback becomes.
        """
        rows: dict[str, dict[str, float]] = {"base case": self._summary_of(a, register, data, a)}
        ranged = register[register["low"].notna() | register["high"].notna()]
        for record in ranged.itertuples(index=False):
            for bound in ("low", "high"):
                value = float(getattr(record, bound))
                if math.isnan(value) or value == a[record.name]:
                    continue
                trial = a.copy()
                trial[record.name] = value
                rows[f"{record.name} = {value:g}"] = self._summary_of(trial, register, data, a)
        return pd.DataFrame.from_dict(rows, orient="index", columns=list(SENSITIVITY_COLUMNS))

    def _summary_of(
        self, trial: pd.Series, register: pd.DataFrame, data: dict, base_a: pd.Series
    ) -> dict[str, float]:
        engine_rows = register.copy()
        engine_rows["value"] = engine_rows["name"].map(trial).astype("float64")
        columns, _ = self._compute(trial, engine_inputs(engine_rows), data)
        estimates = [c for c in columns if c.endswith("E")]
        last = columns[estimates[-1]]
        final_year = estimates[-1][:4]
        changed = [n for n in trial.index if trial[n] != base_a[n]]
        return {
            "value": float(trial[changed[0]]) if changed else math.nan,
            "external_funding_usd_m": last["cumulative_funding_required_usd_m"],
            "minimum_cash_usd_m": min(columns[c]["cash_end_usd_m"] for c in estimates),
            "net_debt_to_ebitda_end": last["net_debt_to_ebitda"],
            "payback_company_definition_end": last["payback_years_company_definition"],
            "revenue_final_year_usd_bn": sum(
                columns[c]["revenue_usd_m"] for c in estimates if c.startswith(final_year)
            )
            / 1000,
        }

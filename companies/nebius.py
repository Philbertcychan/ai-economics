"""Nebius Group (NBIS) - the second neocloud, a foreign private issuer.

Six reported quarters (``A`` columns, Q1 2025 to Q2 2026) and ten forecast quarters (``E``
columns) on the same sheets, on the CoreWeave pattern: reported facts hard-coded, everything
else computed and declared as an Excel formula.

Why the data path differs from CoreWeave
----------------------------------------
Nebius files a 20-F once a year and furnishes results as exhibits to a Form 6-K each quarter.
6-Ks carry no XBRL, so SEC's structured data is annual only. Every quarterly figure here
therefore comes from the results exhibits themselves, read into ``data/disclosed/NBIS.csv``
with a quote check per row (revenue, adjusted EBITDA, cash from operations, capex, cash, debt,
deferred revenue, ARR, AI cloud revenue, power). ``self.reported`` is loaded but not used.

What the model answers
----------------------
* How has the business scaled per quarter, and how does ARR (the last month annualised)
  translate into the next quarter's revenue?
* If capacity keeps going live at the assumed pace and new MW earn what the company says new
  contracts earn, where does ARR land against the 7 to 9 USD bn guidance, and what do
  revenue, EBITDA, capex, prepayments, cash and the need for outside money look like?
* What does a MW of new capacity pay back, gross and net of the customer prepayment, against
  the company's own 1 year 10 months?

The forecast chain, one column per quarter::

    MW going live (assumption) -> active power -> ARR (+ new MW x ARR per new MW)
    ARR, averaged over the quarter -> AI cloud revenue -> group revenue -> EBITDA (margin)
    MW going live x capex per MW -> capex -> prepayments (share of capex) -> deferred revenue
    EBITDA - interest + change in deferred revenue + other (share of capex) -> cash from ops
    cash + CFO - capex + debt raised (share of capex) -> cash; shortfall below minimum -> funding

Active power is disclosed once (about 170 MW at December 2025). The June 2026 figure the
forecast rolls forward from is an assumption inferred from ARR, named for its quarter
(``active_power_mw_2026q2``) so that a later quarter of actuals cannot silently reuse it.

Two things the model does not do, and says so: prepaid revenue is recognised as a flat share
of the balance, and capex goes live in the quarter it is spent.
"""

from __future__ import annotations

import logging
import math

import pandas as pd

from companies.assumptions import load_assumptions, to_inputs_frame
from companies.base import BaseCompanyModel
from companies.coreweave import next_quarter
from data.disclosed import disclosed_series

log = logging.getLogger(__name__)

FORECAST_QUARTERS = 10

ASSUMPTIONS_NEEDED = (
    "mw_added_per_quarter",
    "arr_per_new_mw_year_usd_m",
    "revenue_to_midpoint_arr",
    "ai_cloud_share_of_revenue",
    "adjusted_ebitda_margin",
    "capex_per_mw_usd_m",
    "prepayment_share_of_capex",
    "prepaid_revenue_recognised_share_per_quarter",
    "other_operating_cash_share_of_capex",
    "cost_of_debt",
    "debt_share_of_capex",
    "minimum_cash_usd_bn",
    "disclosed_payback_months",
    "arr_guidance_ye2026_low_usd_m",
    "arr_guidance_ye2026_high_usd_m",
    "revenue_guidance_2026_low_usd_m",
    "revenue_guidance_2026_high_usd_m",
)

# ---- Drivers: (label, unit). Order is the order on the sheet. -------------------------------
DRIVER_ROWS: dict[str, tuple[str, str]] = {
    "active_power_mw_end": (
        "Active power, end of quarter (disclosed Dec 2025; June 2026 inferred from ARR)",
        "MW",
    ),
    "active_power_mw_added": ("Active power added (went live)", "MW"),
    "active_power_mw_avg": ("Active power, average", "MW"),
    "arr_end_usd_m": ("Annualised run-rate revenue (ARR), end of quarter", "USD m"),
    "ai_cloud_revenue_usd_m": ("AI cloud revenue", "USD m"),
    "revenue_usd_m": ("Revenue, group", "USD m"),
    "adjusted_ebitda_usd_m": ("Adjusted EBITDA, group", "USD m"),
    "capex_usd_m": ("Capital expenditure", "USD m"),
    "prepayments_received_usd_m": ("Customer prepayments received", "USD m"),
    "prepaid_revenue_recognised_usd_m": ("Prepaid revenue recognised (non-cash)", "USD m"),
    "deferred_revenue_change_usd_m": ("Change in deferred revenue (cash flow statement)", "USD m"),
    "other_deferred_revenue_movements_usd_m": (
        "Other deferred revenue movements (plug to reported balance)",
        "USD m",
    ),
    "deferred_revenue_end_usd_m": ("Deferred revenue, end of quarter", "USD m"),
    "interest_on_debt_usd_m": ("Interest on debt (inside other items in actuals)", "USD m"),
    "other_operating_cash_usd_m": (
        "Other operating cash items (actuals: everything between EBITDA plus deferred "
        "revenue and reported cash from operations)",
        "USD m",
    ),
    "cfo_usd_m": ("Cash from operations", "USD m"),
    "debt_raised_usd_m": ("Debt raised, net (actuals: change in the reported balance)", "USD m"),
    "debt_end_usd_m": ("Debt, end of quarter", "USD m"),
    "financing_other_usd_m": (
        "Equity raised and other investing and financing (plug to reported cash)",
        "USD m",
    ),
    "funding_required_usd_m": (
        "External funding required to hold minimum cash (equity-like, no interest)",
        "USD m",
    ),
    "cash_end_usd_m": ("Cash and cash equivalents, end of quarter", "USD m"),
}

# Every driver template starts at the first estimate column: the actual columns are the
# company's own figures, pasted. Rows with no template are plugs that are zero in estimates.
DRIVER_FORMULAS: dict[str, str] = {
    "active_power_mw_end": "={active_power_mw_end@prev}+{in.mw_added_per_quarter}",
    "active_power_mw_added": "={active_power_mw_end}-{active_power_mw_end@prev}",
    "active_power_mw_avg": "=({active_power_mw_end@prev}+{active_power_mw_end})/2",
    "arr_end_usd_m": (
        "={arr_end_usd_m@prev}+{active_power_mw_added}*{in.arr_per_new_mw_year_usd_m}"
    ),
    "ai_cloud_revenue_usd_m": (
        "=({arr_end_usd_m@prev}+{arr_end_usd_m})/8*{in.revenue_to_midpoint_arr}"
    ),
    "revenue_usd_m": "={ai_cloud_revenue_usd_m}/{in.ai_cloud_share_of_revenue}",
    "adjusted_ebitda_usd_m": "={revenue_usd_m}*{in.adjusted_ebitda_margin}",
    "capex_usd_m": "={in.capex_per_mw_usd_m}*{active_power_mw_added}",
    "prepayments_received_usd_m": "={capex_usd_m}*{in.prepayment_share_of_capex}",
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
    "interest_on_debt_usd_m": "=({debt_end_usd_m@prev}+{debt_end_usd_m})/2*{in.cost_of_debt}/4",
    "other_operating_cash_usd_m": "={capex_usd_m}*{in.other_operating_cash_share_of_capex}",
    "cfo_usd_m": (
        "={adjusted_ebitda_usd_m}-{interest_on_debt_usd_m}+{deferred_revenue_change_usd_m}"
        "+{other_operating_cash_usd_m}"
    ),
    "debt_raised_usd_m": "={capex_usd_m}*{in.debt_share_of_capex}",
    "debt_end_usd_m": "={debt_end_usd_m@prev}+{debt_raised_usd_m}",
    "funding_required_usd_m": (
        "=MAX(0,{in.minimum_cash_usd_bn}*1000-({cash_end_usd_m@prev}+{cfo_usd_m}"
        "-{capex_usd_m}+{debt_raised_usd_m}+{financing_other_usd_m}))"
    ),
    "cash_end_usd_m": (
        "={cash_end_usd_m@prev}+{cfo_usd_m}-{capex_usd_m}+{debt_raised_usd_m}"
        "+{financing_other_usd_m}+{funding_required_usd_m}"
    ),
}
# Rows the forecast rolls forward from; the last actual must have them all.
OPENING_BALANCES = (
    "active_power_mw_end",
    "arr_end_usd_m",
    "deferred_revenue_end_usd_m",
    "debt_end_usd_m",
    "cash_end_usd_m",
)

# ---- Outputs -------------------------------------------------------------------------------
OUTPUT_ROWS: dict[str, tuple[str, str]] = {
    "revenue_growth_qoq": ("Revenue growth, quarter on quarter", "%"),
    "adjusted_ebitda_margin": ("Adjusted EBITDA margin", "%"),
    "ai_cloud_share_of_revenue": ("AI cloud share of group revenue", "%"),
    "revenue_to_midpoint_arr": (
        "AI cloud revenue / one quarter of average ARR (calibration)",
        "ratio",
    ),
    "arr_per_active_mw_usd_m": ("ARR per active MW, fleet average", "USD m"),
    "arr_guidance_ye2026_low_usd_m": ("ARR guidance for end 2026, low", "USD m"),
    "arr_guidance_ye2026_high_usd_m": ("ARR guidance for end 2026, high", "USD m"),
    "revenue_guidance_2026_low_usd_m": ("Revenue guidance for 2026, low (full year)", "USD m"),
    "revenue_guidance_2026_high_usd_m": ("Revenue guidance for 2026, high (full year)", "USD m"),
    "ebitda_per_mw_year_usd_m": ("Adjusted EBITDA per average active MW, annualised", "USD m"),
    "payback_years_per_mw": ("Payback per new MW: capex per MW / EBITDA per MW", "years"),
    "payback_years_per_mw_net_of_prepayment": (
        "Payback per new MW, net of the customer prepayment",
        "years",
    ),
    "disclosed_payback_years": ("Payback, as disclosed by the company (Q2 2026 deals)", "years"),
    "prepayments_share_of_capex": (
        "Prepayments / capex (actuals: change in deferred revenue / capex)",
        "%",
    ),
    "free_cash_flow_usd_m": ("Free cash flow (CFO less capex)", "USD m"),
    "net_debt_usd_m": ("Net debt (negative is net cash)", "USD m"),
    "net_debt_to_ebitda": ("Net debt / annualised adjusted EBITDA", "x"),
    "cumulative_funding_required_usd_m": (
        "External funding required, cumulative over estimate quarters",
        "USD m",
    ),
}
OUTPUT_FORMULAS: dict[str, str] = {
    "revenue_growth_qoq": "={drivers.revenue_usd_m}/{drivers.revenue_usd_m@prev}-1",
    "adjusted_ebitda_margin": "={drivers.adjusted_ebitda_usd_m}/{drivers.revenue_usd_m}",
    "ai_cloud_share_of_revenue": "={drivers.ai_cloud_revenue_usd_m}/{drivers.revenue_usd_m}",
    "revenue_to_midpoint_arr": (
        "={drivers.ai_cloud_revenue_usd_m}"
        "/(({drivers.arr_end_usd_m@prev}+{drivers.arr_end_usd_m})/8)"
    ),
    "arr_per_active_mw_usd_m": "={drivers.arr_end_usd_m}/{drivers.active_power_mw_end}",
    "arr_guidance_ye2026_low_usd_m": "={in.arr_guidance_ye2026_low_usd_m}",
    "arr_guidance_ye2026_high_usd_m": "={in.arr_guidance_ye2026_high_usd_m}",
    "revenue_guidance_2026_low_usd_m": "={in.revenue_guidance_2026_low_usd_m}",
    "revenue_guidance_2026_high_usd_m": "={in.revenue_guidance_2026_high_usd_m}",
    "ebitda_per_mw_year_usd_m": (
        "={drivers.adjusted_ebitda_usd_m}*4/{drivers.active_power_mw_avg}"
    ),
    "payback_years_per_mw": "={in.capex_per_mw_usd_m}/{ebitda_per_mw_year_usd_m}",
    "payback_years_per_mw_net_of_prepayment": (
        "={in.capex_per_mw_usd_m}*(1-{in.prepayment_share_of_capex})/{ebitda_per_mw_year_usd_m}"
    ),
    "disclosed_payback_years": "={in.disclosed_payback_months}/12",
    "prepayments_share_of_capex": "={drivers.prepayments_received_usd_m}/{drivers.capex_usd_m}",
    "free_cash_flow_usd_m": "={drivers.cfo_usd_m}-{drivers.capex_usd_m}",
    "net_debt_usd_m": "={drivers.debt_end_usd_m}-{drivers.cash_end_usd_m}",
    "net_debt_to_ebitda": "={net_debt_usd_m}/({drivers.adjusted_ebitda_usd_m}*4)",
    "cumulative_funding_required_usd_m": (
        "={cumulative_funding_required_usd_m@prev}+{drivers.funding_required_usd_m}"
    ),
}
# Outputs whose inputs are blank in the actual columns (per-MW lines, prepayments): their
# formulas start with the estimates, so the actual columns show blanks rather than #DIV/0!.
OUTPUT_FORMULA_FROM_FORECAST: frozenset[str] = frozenset(
    {
        "ebitda_per_mw_year_usd_m",
        "payback_years_per_mw",
        "payback_years_per_mw_net_of_prepayment",
        "prepayments_share_of_capex",
    }
)

SENSITIVITY_COLUMNS = (
    "value",
    "arr_end_2026_usd_m",
    "external_funding_usd_m",
    "minimum_cash_usd_m",
    "net_debt_to_ebitda_end",
    "payback_net_of_prepayment_end",
    "revenue_final_year_usd_bn",
)

# Disclosed KPIs an actual quarter must have; the opening balances must also exist for the
# quarter before it.
_QUARTER_KPIS = (
    "revenue_usd_m",
    "adjusted_ebitda_usd_m",
    "operating_cash_flow_usd_m",
    "capex_usd_m",
    "arr_usd_m",
    "cash_usd_m",
    "debt_current_usd_m",
    "debt_non_current_usd_m",
    "deferred_revenue_current_usd_m",
    "deferred_revenue_non_current_usd_m",
)
_OPENING_KPIS = (
    "cash_usd_m",
    "debt_current_usd_m",
    "debt_non_current_usd_m",
    "deferred_revenue_current_usd_m",
    "deferred_revenue_non_current_usd_m",
)


class Nebius(BaseCompanyModel):
    """Nebius Group N.V. - GPU cloud (20-F / 6-K filer): quarterly history and a forecast."""

    ticker = "NBIS"
    # name, cik and layer are copied from data.edgar.COMPANIES["NBIS"] by BaseCompanyModel.

    # -- build --------------------------------------------------------------------------

    def build(self) -> None:
        """Turn disclosed KPIs and assumptions into the model frames, plus sensitivities."""
        if self.disclosed is None:
            raise NotImplementedError(
                f"{self.ticker}: no disclosed KPIs loaded; call load_data() and check "
                "data/disclosed/NBIS.csv (built by files/filing-evidence/NBIS)"
            )
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

        columns, first_estimate = self._compute(a, data)
        self.drivers = self._frame(columns, DRIVER_ROWS)
        self.drivers.attrs = {
            "labels": {k: v[0] for k, v in DRIVER_ROWS.items()},
            "units": {k: v[1] for k, v in DRIVER_ROWS.items()},
            "formulas": dict(DRIVER_FORMULAS),
            "formula_starts": {k: first_estimate for k in DRIVER_FORMULAS},
        }
        self.outputs = self._frame(columns, OUTPUT_ROWS)
        self.outputs.attrs = {
            "labels": {k: v[0] for k, v in OUTPUT_ROWS.items()},
            "units": {k: v[1] for k, v in OUTPUT_ROWS.items()},
            "formulas": dict(OUTPUT_FORMULAS),
            "formula_starts": {k: first_estimate for k in OUTPUT_FORMULA_FROM_FORECAST},
        }
        self.extra_frames["sensitivities"] = self._sensitivities(register, a, data)

    @staticmethod
    def _frame(columns: dict[str, dict[str, float]], rows: dict) -> pd.DataFrame:
        return pd.DataFrame(
            {col: [vals[k] for k in rows] for col, vals in columns.items()},
            index=list(rows),
            dtype="float64",
        )

    def _gather(self) -> dict[str, pd.Series]:
        """Every disclosed series the computation reads, pulled once."""
        kpis = set(_QUARTER_KPIS) | {
            "ai_cloud_revenue_usd_m",
            "active_power_mw",
            "deferred_revenue_change_usd_m",
        }
        return {kpi: disclosed_series(self.disclosed, kpi) for kpi in sorted(kpis)}

    def _actual_quarters(self, data: dict[str, pd.Series]) -> list[str]:
        """The disclosed quarters the model can treat as actuals, checked for gaps."""
        revenue = data["revenue_usd_m"]
        actual = [
            period
            for period in revenue.index
            if all(period in data[k].index for k in _QUARTER_KPIS)
            and all(_previous(period) in data[k].index for k in _OPENING_KPIS)
        ]
        if not actual:
            raise NotImplementedError(
                f"{self.ticker}: no quarter has revenue, EBITDA, cash flow, capex, ARR and "
                "balance-sheet KPIs together with opening balances; check data/disclosed/NBIS.csv"
            )
        for previous, current in zip(actual[:-1], actual[1:], strict=True):
            if next_quarter(previous) != current:
                raise ValueError(
                    f"{self.ticker}: actual quarters are not contiguous ({previous} then "
                    f"{current}); a KPI is missing from data/disclosed/NBIS.csv in between"
                )
        return actual

    def _compute(
        self, a: pd.Series, data: dict[str, pd.Series]
    ) -> tuple[dict[str, dict[str, float]], str]:
        """All actual and forecast columns for one set of assumptions."""
        actual = self._actual_quarters(data)
        last = actual[-1]
        opening_power = f"active_power_mw_{last.lower()}"
        if opening_power not in a.index:
            # Named for its quarter so a new quarter of actuals cannot reuse a stale figure.
            raise ValueError(
                f"{self.ticker}: assumptions/NBIS.csv needs {opening_power} (active power at the "
                f"end of {last}, inferred from ARR) for the forecast to roll forward from"
            )

        def get(kpi: str, period: str) -> float:
            return float(data[kpi].get(period, math.nan))

        columns: dict[str, dict[str, float]] = {}
        cumulative_funding = 0.0
        # ---- actual quarters ---------------------------------------------------------------
        for period in actual:
            previous = _previous(period)
            revenue, ebitda = get("revenue_usd_m", period), get("adjusted_ebitda_usd_m", period)
            cfo, capex = get("operating_cash_flow_usd_m", period), get("capex_usd_m", period)
            deferred = get("deferred_revenue_current_usd_m", period) + get(
                "deferred_revenue_non_current_usd_m", period
            )
            deferred_prev = get("deferred_revenue_current_usd_m", previous) + get(
                "deferred_revenue_non_current_usd_m", previous
            )
            change = get("deferred_revenue_change_usd_m", period)
            if math.isnan(change):
                # The early releases had no cash flow statement; the balance moved by a few
                # USD m, so the change in the balance stands in for the cash-flow line.
                change = deferred - deferred_prev
            debt = get("debt_current_usd_m", period) + get("debt_non_current_usd_m", period)
            debt_prev = get("debt_current_usd_m", previous) + get(
                "debt_non_current_usd_m", previous
            )
            cash, cash_prev = get("cash_usd_m", period), get("cash_usd_m", previous)
            power = get("active_power_mw", period)
            if period == last:
                power = float(a[opening_power])
            d = {
                "active_power_mw_end": power,
                "active_power_mw_added": math.nan,
                "active_power_mw_avg": math.nan,
                "arr_end_usd_m": get("arr_usd_m", period),
                "ai_cloud_revenue_usd_m": get("ai_cloud_revenue_usd_m", period),
                "revenue_usd_m": revenue,
                "adjusted_ebitda_usd_m": ebitda,
                "capex_usd_m": capex,
                "prepayments_received_usd_m": math.nan,
                "prepaid_revenue_recognised_usd_m": math.nan,
                "deferred_revenue_change_usd_m": change,
                "other_deferred_revenue_movements_usd_m": deferred - deferred_prev - change,
                "deferred_revenue_end_usd_m": deferred,
                "interest_on_debt_usd_m": math.nan,
                "other_operating_cash_usd_m": cfo - ebitda - change,
                "cfo_usd_m": cfo,
                "debt_raised_usd_m": debt - debt_prev,
                "debt_end_usd_m": debt,
                "financing_other_usd_m": cash - cash_prev - cfo + capex - (debt - debt_prev),
                "funding_required_usd_m": 0.0,
                "cash_end_usd_m": cash,
            }
            columns[f"{period}A"] = self._with_derived(
                d, a, get("revenue_usd_m", previous), get("arr_usd_m", previous)
            )

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
            added = float(a["mw_added_per_quarter"])
            closing = prev["active_power_mw_end"] + added
            arr = prev["arr_end_usd_m"] + added * float(a["arr_per_new_mw_year_usd_m"])
            ai_cloud = (prev["arr_end_usd_m"] + arr) / 8 * float(a["revenue_to_midpoint_arr"])
            revenue = ai_cloud / float(a["ai_cloud_share_of_revenue"])
            ebitda = revenue * float(a["adjusted_ebitda_margin"])
            capex = float(a["capex_per_mw_usd_m"]) * added
            received = capex * float(a["prepayment_share_of_capex"])
            recognised = prev["deferred_revenue_end_usd_m"] * float(
                a["prepaid_revenue_recognised_share_per_quarter"]
            )
            change = received - recognised
            raised = capex * float(a["debt_share_of_capex"])
            debt_end = prev["debt_end_usd_m"] + raised
            interest = (prev["debt_end_usd_m"] + debt_end) / 2 * float(a["cost_of_debt"]) / 4
            other = capex * float(a["other_operating_cash_share_of_capex"])
            cfo = ebitda - interest + change + other
            financing_other = 0.0
            before_funding = prev["cash_end_usd_m"] + cfo - capex + raised + financing_other
            funding = max(0.0, float(a["minimum_cash_usd_bn"]) * 1000 - before_funding)
            cumulative_funding += funding
            d = {
                "active_power_mw_end": closing,
                "active_power_mw_added": added,
                "active_power_mw_avg": (prev["active_power_mw_end"] + closing) / 2,
                "arr_end_usd_m": arr,
                "ai_cloud_revenue_usd_m": ai_cloud,
                "revenue_usd_m": revenue,
                "adjusted_ebitda_usd_m": ebitda,
                "capex_usd_m": capex,
                "prepayments_received_usd_m": received,
                "prepaid_revenue_recognised_usd_m": recognised,
                "deferred_revenue_change_usd_m": change,
                "other_deferred_revenue_movements_usd_m": 0.0,
                "deferred_revenue_end_usd_m": prev["deferred_revenue_end_usd_m"] + change,
                "interest_on_debt_usd_m": interest,
                "other_operating_cash_usd_m": other,
                "cfo_usd_m": cfo,
                "debt_raised_usd_m": raised,
                "debt_end_usd_m": debt_end,
                "financing_other_usd_m": financing_other,
                "funding_required_usd_m": funding,
                "cash_end_usd_m": before_funding + funding,
            }
            columns[f"{period}E"] = self._with_derived(
                d, a, prev["revenue_usd_m"], prev["arr_end_usd_m"], cumulative_funding
            )
            prev = columns[f"{period}E"]
        return columns, f"{next_quarter(last)}E"

    @staticmethod
    def _with_derived(
        d: dict[str, float],
        a: pd.Series,
        revenue_prev: float,
        arr_prev: float,
        cumulative_funding: float = 0.0,
    ) -> dict[str, float]:
        """Add the output lines that follow from one quarter's drivers."""
        revenue, ebitda = d["revenue_usd_m"], d["adjusted_ebitda_usd_m"]
        ebitda_per_mw = ebitda * 4 / d["active_power_mw_avg"]
        capex_per_mw = float(a["capex_per_mw_usd_m"])
        d.update(
            {
                "revenue_growth_qoq": revenue / revenue_prev - 1,
                "adjusted_ebitda_margin": ebitda / revenue,
                "ai_cloud_share_of_revenue": d["ai_cloud_revenue_usd_m"] / revenue,
                "revenue_to_midpoint_arr": d["ai_cloud_revenue_usd_m"]
                / ((arr_prev + d["arr_end_usd_m"]) / 8),
                "arr_per_active_mw_usd_m": d["arr_end_usd_m"] / d["active_power_mw_end"],
                "arr_guidance_ye2026_low_usd_m": float(a["arr_guidance_ye2026_low_usd_m"]),
                "arr_guidance_ye2026_high_usd_m": float(a["arr_guidance_ye2026_high_usd_m"]),
                "revenue_guidance_2026_low_usd_m": float(a["revenue_guidance_2026_low_usd_m"]),
                "revenue_guidance_2026_high_usd_m": float(a["revenue_guidance_2026_high_usd_m"]),
                "ebitda_per_mw_year_usd_m": ebitda_per_mw,
                "payback_years_per_mw": capex_per_mw / ebitda_per_mw,
                "payback_years_per_mw_net_of_prepayment": capex_per_mw
                * (1 - float(a["prepayment_share_of_capex"]))
                / ebitda_per_mw,
                "disclosed_payback_years": float(a["disclosed_payback_months"]) / 12,
                "prepayments_share_of_capex": (
                    d["prepayments_received_usd_m"] / d["capex_usd_m"]
                    if not math.isnan(d["prepayments_received_usd_m"])
                    else d["deferred_revenue_change_usd_m"] / d["capex_usd_m"]
                ),
                "free_cash_flow_usd_m": d["cfo_usd_m"] - d["capex_usd_m"],
                "net_debt_usd_m": d["debt_end_usd_m"] - d["cash_end_usd_m"],
                "net_debt_to_ebitda": (d["debt_end_usd_m"] - d["cash_end_usd_m"]) / (ebitda * 4),
                "cumulative_funding_required_usd_m": cumulative_funding,
            }
        )
        return d

    # -- sensitivities ------------------------------------------------------------------

    def _sensitivities(
        self, register: pd.DataFrame, a: pd.Series, data: dict[str, pd.Series]
    ) -> pd.DataFrame:
        """One-at-a-time: each register row with a range at its low and high, rest held."""
        rows: dict[str, dict[str, float]] = {"base case": self._summary_of(a, data, a)}
        ranged = register[register["low"].notna() | register["high"].notna()]
        for record in ranged.itertuples(index=False):
            for bound in ("low", "high"):
                value = float(getattr(record, bound))
                if math.isnan(value) or value == a[record.name]:
                    continue
                trial = a.copy()
                trial[record.name] = value
                rows[f"{record.name} = {value:g}"] = self._summary_of(trial, data, a)
        return pd.DataFrame.from_dict(rows, orient="index", columns=list(SENSITIVITY_COLUMNS))

    def _summary_of(
        self, trial: pd.Series, data: dict[str, pd.Series], base_a: pd.Series
    ) -> dict[str, float]:
        columns, _ = self._compute(trial, data)
        estimates = [c for c in columns if c.endswith("E")]
        last = columns[estimates[-1]]
        final_year = estimates[-1][:4]
        changed = [n for n in trial.index if trial[n] != base_a[n]]
        return {
            "value": float(trial[changed[0]]) if changed else math.nan,
            "arr_end_2026_usd_m": columns["2026Q4E"]["arr_end_usd_m"]
            if "2026Q4E" in columns
            else math.nan,
            "external_funding_usd_m": last["cumulative_funding_required_usd_m"],
            "minimum_cash_usd_m": min(columns[c]["cash_end_usd_m"] for c in estimates),
            "net_debt_to_ebitda_end": last["net_debt_to_ebitda"],
            "payback_net_of_prepayment_end": last["payback_years_per_mw_net_of_prepayment"],
            "revenue_final_year_usd_bn": sum(
                columns[c]["revenue_usd_m"] for c in estimates if c.startswith(final_year)
            )
            / 1000,
        }


def _previous(period: str) -> str:
    """``2025Q1`` -> ``2024Q4``."""
    year, quarter = int(period[:4]), int(period[5])
    return f"{year - 1}Q4" if quarter == 1 else f"{year}Q{quarter - 1}"

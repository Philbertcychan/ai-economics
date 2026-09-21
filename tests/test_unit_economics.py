"""Tests for ``engine/unit_economics.py``.

Three kinds of test:

1. ``TestInputs``: validation, unit labels, descriptions, ``to_dict``.
2. ``TestReferenceCases``: two worked cases whose expected numbers were computed by hand, with
   plain arithmetic, outside the engine. They are literals on purpose: a test that recomputes
   the formula it is testing can never fail.
3. ``TestBehaviour``: properties that must hold for any inputs (more utilisation never raises
   unit cost, a GPU that loses cash never pays back, and so on). These protect the meaning of
   the model rather than one number.

The two cases use round, synthetic values. They are not estimates for any real GPU; real values
live in ``assumptions/*.csv``.
"""

import dataclasses
import math
from dataclasses import replace

import pytest

from engine.unit_economics import (
    HOURS_PER_MONTH,
    HOURS_PER_YEAR,
    INPUT_DESCRIPTIONS,
    INPUT_FIELDS,
    INPUT_UNITS,
    MODES,
    GPUEconomicsInputs,
    breakdown,
    breakeven_price_per_gpu_hour,
    capital_cost_per_gpu_hour,
    capital_recovery_factor,
    cash_cost_per_gpu_hour,
    cash_margin_per_gpu_hour,
    cost_per_gpu_hour,
    cost_per_m_tokens,
    energy_cost_per_gpu_hour,
    facility_cost_per_gpu_hour,
    margin_per_gpu_hour,
    payback_months,
    revenue_per_gpu_hour,
    tokens_per_gpu_hour,
)

# Case 1: the nine required inputs only, selling tokens.
TOKENS_CASE = GPUEconomicsInputs(
    chip_cost=32_000,
    power_draw_kw=1.0,
    pue=1.25,
    electricity_price_kwh=0.08,
    utilization=0.6,
    tokens_per_sec=2_500,
    price_per_m_tokens=0.50,
    depreciation_years=5,
    financing_rate=0.08,
)

# Case 2: every refinement switched on, renting the hour out.
RENTAL_CASE = replace(
    TOKENS_CASE,
    utilization=0.9,
    price_per_gpu_hour=2.50,
    facility_cost_per_kw_month=150.0,
    other_opex_per_gpu_hour=0.10,
    idle_power_share=0.3,
    residual_value_share=0.10,
)

REL = 1e-9


class TestInputs:
    def test_field_order_and_tables_agree(self) -> None:
        assert tuple(INPUT_UNITS) == INPUT_FIELDS == tuple(INPUT_DESCRIPTIONS)
        assert INPUT_FIELDS[:9] == (
            "chip_cost",
            "power_draw_kw",
            "pue",
            "electricity_price_kwh",
            "utilization",
            "tokens_per_sec",
            "price_per_m_tokens",
            "depreciation_years",
            "financing_rate",
        )
        assert all(text.strip() for text in INPUT_DESCRIPTIONS.values())

    def test_units_the_exporter_formats_by(self) -> None:
        # scripts/export_xlsx.py keys number formats off these exact strings.
        assert INPUT_UNITS["chip_cost"] == "USD"
        assert INPUT_UNITS["utilization"] == INPUT_UNITS["idle_power_share"] == "share"
        assert INPUT_UNITS["financing_rate"] == "decimal"
        assert INPUT_UNITS["price_per_gpu_hour"] == "USD/GPU-hour"

    def test_refinements_default_to_off(self) -> None:
        assert TOKENS_CASE.price_per_gpu_hour == 0.0
        assert TOKENS_CASE.facility_cost_per_kw_month == 0.0
        assert TOKENS_CASE.other_opex_per_gpu_hour == 0.0
        assert TOKENS_CASE.idle_power_share == 1.0
        assert TOKENS_CASE.residual_value_share == 0.0

    def test_to_dict_round_trips(self) -> None:
        as_dict = RENTAL_CASE.to_dict()
        assert tuple(as_dict) == INPUT_FIELDS
        assert GPUEconomicsInputs(**as_dict) == RENTAL_CASE

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            TOKENS_CASE.utilization = 0.9  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("utilization", -0.01),
            ("utilization", 1.01),
            ("idle_power_share", 1.5),
            ("residual_value_share", -0.1),
            ("pue", 0.99),
            ("depreciation_years", 0),
            ("tokens_per_sec", -1),
            ("chip_cost", -1),
            ("power_draw_kw", -0.1),
            ("electricity_price_kwh", -0.01),
            ("price_per_m_tokens", -1),
            ("financing_rate", -0.01),
            ("price_per_gpu_hour", -1),
            ("facility_cost_per_kw_month", -1),
            ("other_opex_per_gpu_hour", -0.5),
        ],
    )
    def test_rejects_out_of_range(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            replace(TOKENS_CASE, **{field: value})

    @pytest.mark.parametrize("field", INPUT_FIELDS)
    def test_rejects_nan(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            replace(TOKENS_CASE, **{field: math.nan})

    def test_boundaries_are_allowed(self) -> None:
        replace(TOKENS_CASE, utilization=0.0)
        rental_only = replace(RENTAL_CASE, tokens_per_sec=0.0, price_per_m_tokens=0.0)
        assert math.isinf(cost_per_m_tokens(rental_only))
        assert margin_per_gpu_hour(rental_only, "rental") == pytest.approx(
            margin_per_gpu_hour(RENTAL_CASE, "rental")
        )
        replace(TOKENS_CASE, utilization=1.0, pue=1.0, financing_rate=0.0, chip_cost=0.0)


class TestCapitalRecoveryFactor:
    def test_mortgage_formula(self) -> None:
        assert capital_recovery_factor(0.08, 5) == pytest.approx(0.2504564545668364, rel=REL)

    def test_zero_rate_is_straight_line(self) -> None:
        assert capital_recovery_factor(0.0, 5) == pytest.approx(0.2, rel=REL)

    def test_cheaper_than_interest_on_the_full_price(self) -> None:
        # The alternative convention, depreciation plus interest on the whole price, charges
        # 1/5 + 0.08 = 0.28. The annuity is lower because the balance owed falls over time.
        assert capital_recovery_factor(0.08, 5) < 1 / 5 + 0.08

    def test_level_payments_repay_the_loan_exactly(self) -> None:
        balance, rate, payment = 1.0, 0.08, capital_recovery_factor(0.08, 5)
        for _ in range(5):
            balance = balance * (1 + rate) - payment
        assert balance == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.parametrize(("rate", "years"), [(-0.01, 5), (0.08, 0), (0.08, -1)])
    def test_rejects_bad_arguments(self, rate: float, years: float) -> None:
        with pytest.raises(ValueError):
            capital_recovery_factor(rate, years)


class TestReferenceCases:
    def test_tokens_case(self) -> None:
        i = TOKENS_CASE
        assert capital_cost_per_gpu_hour(i) == pytest.approx(0.9149094230752015, rel=REL)
        assert energy_cost_per_gpu_hour(i) == pytest.approx(0.10, rel=REL)
        assert facility_cost_per_gpu_hour(i) == 0.0
        assert cash_cost_per_gpu_hour(i) == pytest.approx(0.10, rel=REL)
        assert cost_per_gpu_hour(i) == pytest.approx(1.0149094230752016, rel=REL)
        assert tokens_per_gpu_hour(i) == pytest.approx(5_400_000, rel=REL)
        assert cost_per_m_tokens(i) == pytest.approx(0.18794618945837066, rel=REL)
        assert revenue_per_gpu_hour(i) == pytest.approx(2.70, rel=REL)
        assert cash_margin_per_gpu_hour(i) == pytest.approx(2.60, rel=REL)
        assert margin_per_gpu_hour(i) == pytest.approx(1.6850905769247986, rel=REL)
        assert payback_months(i) == pytest.approx(16.859852476290833, rel=REL)

    def test_rental_case(self) -> None:
        i = RENTAL_CASE
        assert capital_cost_per_gpu_hour(i) == pytest.approx(0.8526422250599188, rel=REL)
        assert energy_cost_per_gpu_hour(i) == pytest.approx(0.093, rel=REL)
        assert facility_cost_per_gpu_hour(i) == pytest.approx(0.2054794520547945, rel=REL)
        assert cash_cost_per_gpu_hour(i) == pytest.approx(0.3984794520547945, rel=REL)
        assert cost_per_gpu_hour(i) == pytest.approx(1.2511216771147133, rel=REL)
        assert revenue_per_gpu_hour(i, "rental") == pytest.approx(2.25, rel=REL)
        assert cash_margin_per_gpu_hour(i, "rental") == pytest.approx(1.8515205479452055, rel=REL)
        assert margin_per_gpu_hour(i, "rental") == pytest.approx(0.9988783228852867, rel=REL)
        assert breakeven_price_per_gpu_hour(i) == pytest.approx(1.390135196794126, rel=REL)
        assert payback_months(i, "rental") == pytest.approx(23.67546851532617, rel=REL)

    def test_constants(self) -> None:
        assert HOURS_PER_YEAR == 8760
        assert HOURS_PER_MONTH * 12 == HOURS_PER_YEAR
        assert MODES == ("tokens", "rental")


class TestBehaviour:
    def test_more_utilisation_lowers_token_cost(self) -> None:
        low, high = replace(TOKENS_CASE, utilization=0.4), replace(TOKENS_CASE, utilization=0.8)
        assert cost_per_m_tokens(high) < cost_per_m_tokens(low)

    @pytest.mark.parametrize(
        "field",
        [
            "electricity_price_kwh",
            "pue",
            "chip_cost",
            "financing_rate",
            "facility_cost_per_kw_month",
            "other_opex_per_gpu_hour",
            "power_draw_kw",
        ],
    )
    def test_each_cost_input_raises_cost(self, field: str) -> None:
        base = RENTAL_CASE
        higher = replace(base, **{field: getattr(base, field) * 1.5 + 0.01})
        assert cost_per_gpu_hour(higher) > cost_per_gpu_hour(base)

    def test_longer_life_and_residual_value_lower_the_capital_charge(self) -> None:
        base = capital_cost_per_gpu_hour(TOKENS_CASE)
        assert capital_cost_per_gpu_hour(replace(TOKENS_CASE, depreciation_years=6)) < base
        assert capital_cost_per_gpu_hour(replace(TOKENS_CASE, residual_value_share=0.2)) < base

    def test_idle_power_only_matters_when_idle(self) -> None:
        busy = replace(RENTAL_CASE, utilization=1.0)
        assert energy_cost_per_gpu_hour(busy) == pytest.approx(
            energy_cost_per_gpu_hour(replace(busy, idle_power_share=1.0)), rel=REL
        )
        idle_cheap = replace(RENTAL_CASE, utilization=0.5, idle_power_share=0.2)
        idle_full = replace(idle_cheap, idle_power_share=1.0)
        assert energy_cost_per_gpu_hour(idle_cheap) < energy_cost_per_gpu_hour(idle_full)

    def test_margin_is_token_spread_times_volume(self) -> None:
        i = TOKENS_CASE
        spread = i.price_per_m_tokens - cost_per_m_tokens(i)
        assert margin_per_gpu_hour(i) == pytest.approx(
            spread * tokens_per_gpu_hour(i) / 1e6, rel=1e-9
        )

    def test_breakeven_price_gives_zero_margin(self) -> None:
        at_breakeven = replace(
            RENTAL_CASE, price_per_gpu_hour=breakeven_price_per_gpu_hour(RENTAL_CASE)
        )
        assert margin_per_gpu_hour(at_breakeven, "rental") == pytest.approx(0.0, abs=1e-12)

    def test_nothing_sold_means_infinite_cost_and_no_payback(self) -> None:
        unsold = replace(RENTAL_CASE, utilization=0.0)
        assert math.isinf(cost_per_m_tokens(unsold))
        assert math.isinf(breakeven_price_per_gpu_hour(unsold))
        assert math.isinf(payback_months(unsold, "rental"))

    def test_zero_or_negative_cash_margin_never_pays_back(self) -> None:
        free = replace(TOKENS_CASE, price_per_m_tokens=0.0)
        assert math.isinf(payback_months(free))
        exactly_zero = replace(TOKENS_CASE, price_per_m_tokens=0.0, electricity_price_kwh=0.0)
        assert cash_margin_per_gpu_hour(exactly_zero) == 0.0
        assert math.isinf(payback_months(exactly_zero))

    def test_price_below_cost_gives_negative_margin_but_can_still_pay_back(self) -> None:
        # Covers its running costs, not its capital: pays back eventually, never earns its keep.
        thin = replace(TOKENS_CASE, price_per_m_tokens=0.10)
        assert margin_per_gpu_hour(thin) < 0 < cash_margin_per_gpu_hour(thin)
        assert math.isfinite(payback_months(thin))

    def test_payback_ignores_financing_by_design(self) -> None:
        # Operators quote payback before financing; the cost of money is in the margin instead.
        assert payback_months(replace(TOKENS_CASE, financing_rate=0.20)) == pytest.approx(
            payback_months(TOKENS_CASE), rel=REL
        )
        assert margin_per_gpu_hour(replace(TOKENS_CASE, financing_rate=0.20)) < (
            margin_per_gpu_hour(TOKENS_CASE)
        )

    def test_unknown_mode_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            revenue_per_gpu_hour(TOKENS_CASE, "subscriptions")

    @pytest.mark.parametrize("mode", MODES)
    def test_breakdown_matches_the_functions_and_adds_up(self, mode: str) -> None:
        b = breakdown(RENTAL_CASE, mode)
        assert b["cost_per_gpu_hour"] == pytest.approx(
            b["capital_cost_per_gpu_hour"] + b["cash_cost_per_gpu_hour"], rel=REL
        )
        assert b["cash_cost_per_gpu_hour"] == pytest.approx(
            b["energy_cost_per_gpu_hour"]
            + b["facility_cost_per_gpu_hour"]
            + b["other_opex_per_gpu_hour"],
            rel=REL,
        )
        assert b["margin_per_gpu_hour"] == pytest.approx(
            b["revenue_per_gpu_hour"] - b["cost_per_gpu_hour"], rel=REL
        )
        assert b["payback_months"] == payback_months(RENTAL_CASE, mode)

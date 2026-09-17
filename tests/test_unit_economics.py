"""Tests for ``engine/unit_economics.py``.

Two kinds of test live here and they behave differently on purpose:

1. Plumbing tests (``TestInputs``) cover validation, unit labels, descriptions and
   ``to_dict``. They pass today and must keep passing.
2. Engine tests (``TestReferenceCase``, ``TestInvariants``) pin the reference definitions
   written in the engine docstrings. Each is decorated with ``pending(...)`` naming the engine
   functions it calls. While any of those still raises ``NotImplementedError`` the test is a
   strict xfail, so CI stays green; once all of them are written the marker switches itself
   off and the test runs for real. The three functions can therefore land one at a time, in
   any order, with nothing to delete by hand in between.

Philbert: to see the real failures while implementing the engine, run

    uv run pytest tests/test_unit_economics.py --runxfail

Once all three functions are written, ``pending`` and ``_is_stub`` below are dead code and can
be deleted together with the decorators. The numbers in ``EXPECTED`` come from the reference
definitions, and the engine docstrings repeat them as a worked "Reference case". If you revise
a definition, revise the pinned number here and the worked example in that function's
docstring in the same commit, and say why in ``log.md``.
"""

import dataclasses
import math
from collections.abc import Callable
from dataclasses import replace

import pytest

from engine.unit_economics import (
    HOURS_PER_MONTH,
    HOURS_PER_YEAR,
    INPUT_DESCRIPTIONS,
    INPUT_FIELDS,
    INPUT_UNITS,
    GPUEconomicsInputs,
    cost_per_m_tokens,
    margin_per_gpu_hour,
    payback_months,
)

# The reference case the engine docstrings work through. Synthetic values chosen so the
# reference definitions are easy to check by hand; they are not estimates for any real GPU.
REFERENCE = GPUEconomicsInputs(
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

# Hand-checked outputs of the reference definitions for REFERENCE. Intermediate quantities are
# listed too so the invariant tests can pin identities without re-deriving any formula.
EXPECTED = {
    "energy_cost_per_hour": 0.10,
    "capital_cost_per_hour": 1.0228311,
    "tokens_per_hour": 5_400_000,
    "cost_per_m_tokens": 0.2079317,
    "revenue_per_hour": 2.70,
    "margin_per_gpu_hour": 1.5771690,
    "cash_contribution_per_hour": 2.3077626,
    "payback_months": 18.99485,
}
REL = 1e-4

EngineFunction = Callable[[GPUEconomicsInputs], float]


def _is_stub(func: EngineFunction) -> bool:
    """True while ``func`` is still the scaffold stub that raises ``NotImplementedError``."""
    try:
        func(REFERENCE)
    except NotImplementedError:
        return True
    except Exception:
        # A half-written body is not a stub. Swallowing its error keeps collection alive (the
        # plumbing tests must still run) and leaves the unmarked test to report the failure.
        return False
    return False


def pending(*funcs: EngineFunction) -> pytest.MarkDecorator:
    """Strict xfail for a test that calls ``funcs``, active only while one of them is a stub.

    Decided per test, not per class or module, so the engine functions can be implemented one
    at a time: a test starts running for real as soon as everything it calls exists. A strict
    marker that stayed on after that would turn every newly passing test into a red XPASS.
    """
    return pytest.mark.xfail(
        condition=any(_is_stub(func) for func in funcs),
        raises=NotImplementedError,
        strict=True,
        reason="TODO(philbert): implement engine/unit_economics.py",
    )


class TestInputs:
    """Validation, labels and serialisation of ``GPUEconomicsInputs`` - complete plumbing."""

    def test_hour_constants(self) -> None:
        # Both peers and the payback definition assume these exact conventions.
        assert HOURS_PER_YEAR == 8760
        assert HOURS_PER_MONTH == 730

    def test_reference_case_is_valid(self) -> None:
        assert REFERENCE.chip_cost == 32_000
        assert REFERENCE.utilization == 0.6

    def test_to_dict_round_trips_in_field_order(self) -> None:
        as_dict = REFERENCE.to_dict()
        assert list(as_dict) == list(INPUT_FIELDS)
        assert GPUEconomicsInputs(**as_dict) == REFERENCE

    def test_units_and_descriptions_cover_every_field_in_order(self) -> None:
        # The xlsx Inputs sheet zips fields, units and descriptions row by row, so the three
        # sequences must agree exactly, including order.
        assert list(INPUT_UNITS) == list(INPUT_FIELDS)
        assert list(INPUT_DESCRIPTIONS) == list(INPUT_FIELDS)
        assert all(isinstance(text, str) and text.strip() for text in INPUT_DESCRIPTIONS.values())

    def test_unit_labels_are_pinned_verbatim(self) -> None:
        # Pinned verbatim: the exporter's number formats key off "USD", "share" and "decimal".
        assert INPUT_UNITS == {
            "chip_cost": "USD",
            "power_draw_kw": "kW",
            "pue": "ratio",
            "electricity_price_kwh": "USD/kWh",
            "utilization": "share",
            "tokens_per_sec": "tokens/s",
            "price_per_m_tokens": "USD/M tokens",
            "depreciation_years": "years",
            "financing_rate": "decimal",
        }

    def test_inputs_are_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            REFERENCE.chip_cost = 1.0  # type: ignore[misc]

    @pytest.mark.parametrize("field", INPUT_FIELDS)
    def test_negative_value_is_rejected_and_names_the_field(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            replace(REFERENCE, **{field: -1.0})

    @pytest.mark.parametrize("field", INPUT_FIELDS)
    def test_nan_is_rejected(self, field: str) -> None:
        # NaN passes naive `x < 0` checks and would silently poison every output.
        with pytest.raises(ValueError, match=field):
            replace(REFERENCE, **{field: math.nan})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("utilization", 1.01),  # a share of hours cannot exceed 100%
            ("utilization", -0.01),
            ("pue", 0.99),  # facility power cannot be less than IT power
            ("depreciation_years", 0.0),  # divides the capital charge
            ("tokens_per_sec", 0.0),  # divides the cost per token
        ],
    )
    def test_out_of_range_value_is_rejected(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            replace(REFERENCE, **{field: value})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("utilization", 0.0),
            ("utilization", 1.0),
            ("pue", 1.0),
            ("chip_cost", 0.0),
            ("electricity_price_kwh", 0.0),
            ("price_per_m_tokens", 0.0),
            ("financing_rate", 0.0),
        ],
    )
    def test_boundary_values_are_accepted(self, field: str, value: float) -> None:
        # Zeros and the edges of the ranges are legitimate scenarios (free power, unfinanced
        # chips, an idle fleet), not input errors.
        assert getattr(replace(REFERENCE, **{field: value}), field) == value


class TestPendingMarker:
    """``pending`` decides which engine tests count, so it is pinned like any other plumbing."""

    @staticmethod
    def _stub(inputs: GPUEconomicsInputs) -> float:
        raise NotImplementedError

    @staticmethod
    def _written(inputs: GPUEconomicsInputs) -> float:
        return 1.0

    @staticmethod
    def _half_written(inputs: GPUEconomicsInputs) -> float:
        raise ZeroDivisionError

    def test_only_not_implemented_counts_as_a_stub(self) -> None:
        assert _is_stub(self._stub)
        assert not _is_stub(self._written)
        assert not _is_stub(self._half_written)

    def test_marker_is_active_only_while_a_function_is_a_stub(self) -> None:
        waiting = pending(self._written, self._stub)
        assert waiting.name == "xfail" and waiting.kwargs["condition"] is True
        assert waiting.kwargs["strict"] is True
        assert waiting.kwargs["raises"] is NotImplementedError
        assert pending(self._written, self._half_written).kwargs["condition"] is False


class TestReferenceCase:
    """The three headline numbers for REFERENCE, pinned to the reference definitions."""

    @pending(cost_per_m_tokens)
    def test_cost_per_m_tokens(self) -> None:
        assert cost_per_m_tokens(REFERENCE) == pytest.approx(EXPECTED["cost_per_m_tokens"], rel=REL)

    @pending(margin_per_gpu_hour)
    def test_margin_per_gpu_hour(self) -> None:
        assert margin_per_gpu_hour(REFERENCE) == pytest.approx(
            EXPECTED["margin_per_gpu_hour"], rel=REL
        )

    @pending(payback_months)
    def test_payback_months(self) -> None:
        assert payback_months(REFERENCE) == pytest.approx(EXPECTED["payback_months"], rel=REL)


class TestInvariants:
    """Directional and structural properties any sane revision of the definitions must keep."""

    @pending(cost_per_m_tokens)
    def test_higher_utilization_lowers_cost(self) -> None:
        busier = replace(REFERENCE, utilization=0.9)
        assert cost_per_m_tokens(busier) < cost_per_m_tokens(REFERENCE)

    @pending(cost_per_m_tokens)
    def test_higher_electricity_price_raises_cost(self) -> None:
        dearer_power = replace(REFERENCE, electricity_price_kwh=0.16)
        assert cost_per_m_tokens(dearer_power) > cost_per_m_tokens(REFERENCE)

    @pending(cost_per_m_tokens)
    def test_higher_pue_raises_cost(self) -> None:
        leakier_facility = replace(REFERENCE, pue=1.6)
        assert cost_per_m_tokens(leakier_facility) > cost_per_m_tokens(REFERENCE)

    @pending(cost_per_m_tokens, payback_months)
    def test_higher_chip_cost_raises_cost_and_payback(self) -> None:
        dearer_chip = replace(REFERENCE, chip_cost=64_000)
        assert cost_per_m_tokens(dearer_chip) > cost_per_m_tokens(REFERENCE)
        assert payback_months(dearer_chip) > payback_months(REFERENCE)

    @pending(payback_months)
    def test_zero_price_means_infinite_payback(self) -> None:
        # Giving tokens away never repays the chip; the payback_months docstring asks for
        # inf, not an error.
        result = payback_months(replace(REFERENCE, price_per_m_tokens=0.0))
        assert math.isinf(result) and result > 0

    @pending(payback_months)
    def test_zero_cash_contribution_means_infinite_payback(self) -> None:
        # The boundary itself: no revenue, free power and no interest make the contribution
        # exactly 0.0, where a `< 0` guard would fall through to a division by zero. The
        # payback_months docstring puts the cut-off at `<= 0`.
        idle = replace(
            REFERENCE, price_per_m_tokens=0.0, electricity_price_kwh=0.0, financing_rate=0.0
        )
        result = payback_months(idle)
        assert math.isinf(result) and result > 0

    @pending(cost_per_m_tokens, margin_per_gpu_hour)
    def test_price_below_cost_gives_negative_margin(self) -> None:
        # Half the fully loaded cost per token, whatever that turns out to be.
        underpriced = replace(REFERENCE, price_per_m_tokens=0.5 * cost_per_m_tokens(REFERENCE))
        assert margin_per_gpu_hour(underpriced) < 0

    @pending(cost_per_m_tokens, margin_per_gpu_hour)
    @pytest.mark.parametrize(
        ("inputs", "tokens_per_hour"),
        [
            (REFERENCE, EXPECTED["tokens_per_hour"]),
            (replace(REFERENCE, utilization=0.3), 2_700_000),  # half the busy hours
        ],
    )
    def test_margin_equals_per_token_spread_times_volume(
        self, inputs: GPUEconomicsInputs, tokens_per_hour: float
    ) -> None:
        # Margin per GPU-hour and cost per million tokens are two views of one number: the
        # margin must equal (price - cost) per million tokens times the millions produced.
        spread = inputs.price_per_m_tokens - cost_per_m_tokens(inputs)
        assert margin_per_gpu_hour(inputs) == pytest.approx(spread * tokens_per_hour / 1e6, rel=REL)

    @pending(cost_per_m_tokens)
    def test_zero_chip_cost_leaves_only_energy_cost(self) -> None:
        # With no chip to depreciate or finance, the only cost is 0.10 USD/h of electricity
        # spread over 5.4 million tokens an hour.
        free_chip = replace(REFERENCE, chip_cost=0.0)
        energy_only = EXPECTED["energy_cost_per_hour"] / (EXPECTED["tokens_per_hour"] / 1e6)
        assert cost_per_m_tokens(free_chip) == pytest.approx(energy_only, rel=REL)

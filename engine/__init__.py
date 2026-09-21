"""Shared GPU unit-economics engine (layer 1). See unit_economics.py.

The engine answers, for one installed GPU, what an hour costs, what it earns and how fast the
GPU pays back. Company models (layer 2) import from ``engine.unit_economics`` directly; the
names below are re-exported for convenience only.
"""

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

__all__ = [
    "HOURS_PER_MONTH",
    "HOURS_PER_YEAR",
    "INPUT_DESCRIPTIONS",
    "INPUT_FIELDS",
    "INPUT_UNITS",
    "MODES",
    "GPUEconomicsInputs",
    "breakdown",
    "breakeven_price_per_gpu_hour",
    "capital_cost_per_gpu_hour",
    "capital_recovery_factor",
    "cash_cost_per_gpu_hour",
    "cash_margin_per_gpu_hour",
    "cost_per_gpu_hour",
    "cost_per_m_tokens",
    "energy_cost_per_gpu_hour",
    "facility_cost_per_gpu_hour",
    "margin_per_gpu_hour",
    "payback_months",
    "revenue_per_gpu_hour",
    "tokens_per_gpu_hour",
]

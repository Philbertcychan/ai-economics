"""Shared GPU unit-economics engine (layer 1). See unit_economics.py.

The engine answers, for one installed GPU, what an hour costs, what it earns and how fast the
chip pays back. Company models (layer 2) and the site (layer 3) import from
``engine.unit_economics`` directly; the names below are re-exported for convenience only.
"""

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

__all__ = [
    "HOURS_PER_MONTH",
    "HOURS_PER_YEAR",
    "INPUT_DESCRIPTIONS",
    "INPUT_FIELDS",
    "INPUT_UNITS",
    "GPUEconomicsInputs",
    "cost_per_m_tokens",
    "margin_per_gpu_hour",
    "payback_months",
]

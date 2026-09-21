"""GPU unit economics: what one installed GPU costs to run and what it earns (layer 1).

The idea
--------
Every company in this repository is, underneath, a collection of GPUs. So before modelling a
company we model ONE GPU: what an hour of it costs, what that hour sells for, and how long the
GPU takes to pay for itself. A company model is then "how many GPUs, bought when, financed
how" on top of this.

The pivot unit is the GPU-hour, because it is where the value chain meets:

* the OWNER of the GPU (a neocloud such as CoreWeave) has a cost per GPU-hour;
* the owner RENTS the hour to a customer at a price per GPU-hour - the owner's revenue and
  the customer's cost;
* the customer (an AI lab) turns the hour into TOKENS and sells those.

So this module has one cost stack and two ways to earn revenue from it: ``mode="rental"``
(sell the hour) and ``mode="tokens"`` (sell what the hour produces).

The cost of a GPU-hour
----------------------
Four layers, each its own function so it can be read, tested and shown on the workbook::

    capital   the purchase price, spread over the GPU's life including the cost of money
    energy    electricity for the GPU and the cooling around it
    facility  the data-centre space the GPU occupies, rented by the kW
    other     staff, maintenance, software, network

``cash cost`` is energy + facility + other: what is paid out every hour the GPU exists.
``fully loaded cost`` adds capital. The cash view answers "does it cover its running costs";
the fully loaded view answers "is it worth buying".

Conventions
-----------
* "Per GPU-hour" always means per WALL-CLOCK hour of an installed GPU: 8,760 hours a year,
  busy or not. Idle time is handled by ``utilization``, never by shrinking the hour count,
  because a GPU that is rented 60% of the time still costs money 100% of the time.
* Money is USD, energy is kWh, power is kW, rates are decimals (0.08 means 8%).
* Capital is charged as an ANNUITY (see ``capital_recovery_factor``), not as depreciation
  plus interest on the full price. The two are compared in ``docs/modeling-approach.md``.
* Nothing here is a forecast or an assumption. Values live in ``assumptions/*.csv`` where
  each one has a source and a status; this module is only arithmetic.
"""

import math
from dataclasses import asdict, dataclass, fields

HOURS_PER_YEAR = 8760
HOURS_PER_MONTH = 730  # 8760 / 12; a payback convention, not a calendar month
MODES = ("tokens", "rental")

# Unit label per input field, in field order. The xlsx exporter keys number formats off these
# exact strings ("USD" -> thousands separator, "share"/"decimal" -> percent), so treat them as
# an interface, not as free text.
INPUT_UNITS: dict[str, str] = {
    "chip_cost": "USD",
    "power_draw_kw": "kW",
    "pue": "ratio",
    "electricity_price_kwh": "USD/kWh",
    "utilization": "share",
    "tokens_per_sec": "tokens/s",
    "price_per_m_tokens": "USD/M tokens",
    "depreciation_years": "years",
    "financing_rate": "decimal",
    "price_per_gpu_hour": "USD/GPU-hour",
    "facility_cost_per_kw_month": "USD/kW-month",
    "other_opex_per_gpu_hour": "USD/GPU-hour",
    "idle_power_share": "share",
    "residual_value_share": "share",
}

# One plain-English line per input, shown next to the value on the workbook's Inputs sheet.
INPUT_DESCRIPTIONS: dict[str, str] = {
    "chip_cost": (
        "All-in installed cost of one GPU: the accelerator plus its share of the server, "
        "networking and rack, in USD."
    ),
    "power_draw_kw": (
        "Electrical power one GPU draws at load, including its share of the host server, in kW."
    ),
    "pue": (
        "Power usage effectiveness: total facility power divided by IT power. "
        "1.0 would be a data centre with no cooling or distribution overhead."
    ),
    "electricity_price_kwh": "Delivered price of electricity, in USD per kWh.",
    "utilization": (
        "Share of wall-clock hours in which the GPU is earning revenue (rented out, or "
        "producing billable tokens), from 0 to 1."
    ),
    "tokens_per_sec": "Sustained output tokens per second from one GPU while it is producing.",
    "price_per_m_tokens": "Blended selling price, in USD per million output tokens.",
    "depreciation_years": "Economic life of the GPU in years: how long it earns before retirement.",
    "financing_rate": (
        "Annual cost of the money tied up in the GPU (blend of debt interest and the return "
        "equity requires), as a decimal (0.08 means 8% a year)."
    ),
    "price_per_gpu_hour": "Rental price of one GPU for one hour, in USD (rental mode).",
    "facility_cost_per_kw_month": (
        "Data-centre space, rented by the kW of IT load per month, excluding electricity. "
        "0 means the cost is ignored or sits elsewhere."
    ),
    "other_opex_per_gpu_hour": (
        "Staff, maintenance, software and network per GPU-hour, in USD. 0 means ignored."
    ),
    "idle_power_share": (
        "Power drawn while idle as a share of power at load. 1.0 is the cautious default: "
        "the GPU is billed for full power every hour."
    ),
    "residual_value_share": (
        "Share of chip_cost recovered by selling or redeploying the GPU at end of life. "
        "0 is the cautious default."
    ),
}


@dataclass(frozen=True)
class GPUEconomicsInputs:
    """The assumptions behind one GPU's economics.

    The first nine fields are required. The last five are optional refinements whose defaults
    switch them off, so the simplest possible case needs only nine numbers. Frozen so a set of
    inputs can be passed around and compared safely; make a variant with
    ``dataclasses.replace(inputs, utilization=0.8)``. Validation runs on construction, so an
    instance that exists is one the engine can accept.
    """

    chip_cost: float
    power_draw_kw: float
    pue: float
    electricity_price_kwh: float
    utilization: float
    tokens_per_sec: float
    price_per_m_tokens: float
    depreciation_years: float
    financing_rate: float
    price_per_gpu_hour: float = 0.0
    facility_cost_per_kw_month: float = 0.0
    other_opex_per_gpu_hour: float = 0.0
    idle_power_share: float = 1.0
    residual_value_share: float = 0.0

    def __post_init__(self) -> None:
        # Every check is written as `not (bound holds)` rather than `value < bound` so that a
        # NaN, which fails every comparison, is rejected instead of quietly flowing into the
        # model and turning every output into NaN.
        for name in ("utilization", "idle_power_share", "residual_value_share"):
            value = getattr(self, name)
            if not (0.0 <= value <= 1.0):
                raise ValueError(f"{name} must be between 0 and 1 (a share); got {value!r}")
        if not (self.pue >= 1.0):
            raise ValueError(
                f"pue must be >= 1 (facility power cannot be less than IT power); got {self.pue!r}"
            )
        # Sits in a denominator, so zero is not allowed.
        if not (self.depreciation_years > 0):
            raise ValueError(f"depreciation_years must be > 0; got {self.depreciation_years!r}")
        for name in (
            "chip_cost",
            "tokens_per_sec",  # zero is valid: a rental business sells hours, not tokens
            "power_draw_kw",
            "electricity_price_kwh",
            "price_per_m_tokens",
            "financing_rate",
            "price_per_gpu_hour",
            "facility_cost_per_kw_month",
            "other_opex_per_gpu_hour",
        ):
            value = getattr(self, name)
            if not (value >= 0):
                raise ValueError(f"{name} must be >= 0; got {value!r}")

    def to_dict(self) -> dict[str, float]:
        """Return the inputs as a plain ``{field: value}`` dict, in field order."""
        return asdict(self)


INPUT_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(GPUEconomicsInputs))
"""Field names in declaration order; the canonical ordering for the Inputs sheet."""


# --- Cost stack ---------------------------------------------------------------------------


def capital_recovery_factor(rate: float, years: float) -> float:
    """Share of a purchase price that must be earned back EACH YEAR to repay it with interest.

    This is the mortgage formula. Borrow 1 dollar at ``rate`` for ``years``; the level annual
    payment that clears the loan is the capital recovery factor::

        crf = rate / (1 - (1 + rate) ** -years)

    At 8% over 5 years it is 0.2505: each year the GPU must earn 25 cents per dollar of its
    price just to return the money with its cost. With ``rate == 0`` it collapses to plain
    straight-line depreciation, ``1 / years``.

    Why an annuity rather than "depreciation + interest on the full price": interest is owed
    on what is still outstanding, which falls as the GPU earns its cost back. Charging
    interest on the full price every year overstates the cost of capital (0.28 instead of
    0.25 in the example).
    """
    if not (years > 0):
        raise ValueError(f"years must be > 0; got {years!r}")
    if not (rate >= 0):
        raise ValueError(f"rate must be >= 0; got {rate!r}")
    if rate == 0:
        return 1.0 / years
    return rate / (1.0 - (1.0 + rate) ** -years)


def capital_cost_per_gpu_hour(inputs: GPUEconomicsInputs) -> float:
    """The purchase price of the GPU expressed as a cost per wall-clock hour, in USD.

    Price, less what the GPU will fetch at the end of its life (discounted back to today,
    because that money arrives years from now), turned into a level annual charge and spread
    over 8,760 hours::

        recoverable = chip_cost * residual_value_share / (1 + financing_rate) ** years
        per_year    = (chip_cost - recoverable) * capital_recovery_factor(rate, years)
        per_hour    = per_year / 8760
    """
    years, rate = inputs.depreciation_years, inputs.financing_rate
    recoverable = inputs.chip_cost * inputs.residual_value_share / (1.0 + rate) ** years
    per_year = (inputs.chip_cost - recoverable) * capital_recovery_factor(rate, years)
    return per_year / HOURS_PER_YEAR


def energy_cost_per_gpu_hour(inputs: GPUEconomicsInputs) -> float:
    """Electricity for one GPU for one wall-clock hour, cooling included, in USD.

    ``power_draw_kw * pue`` is what the meter sees when the GPU is busy. When it is idle it
    draws ``idle_power_share`` of that. Averaged over busy and idle hours::

        load_factor = utilization + (1 - utilization) * idle_power_share
        per_hour    = power_draw_kw * pue * electricity_price_kwh * load_factor
    """
    load_factor = inputs.utilization + (1.0 - inputs.utilization) * inputs.idle_power_share
    return inputs.power_draw_kw * inputs.pue * inputs.electricity_price_kwh * load_factor


def facility_cost_per_gpu_hour(inputs: GPUEconomicsInputs) -> float:
    """Data-centre space for one GPU for one hour, in USD.

    Space is rented by the kW of IT load per month, so a GPU's share is its power draw::

        per_hour = facility_cost_per_kw_month * power_draw_kw * 12 / 8760
    """
    return inputs.facility_cost_per_kw_month * inputs.power_draw_kw * 12.0 / HOURS_PER_YEAR


def cash_cost_per_gpu_hour(inputs: GPUEconomicsInputs) -> float:
    """What is paid out for every hour the GPU exists: energy + facility + other opex."""
    return (
        energy_cost_per_gpu_hour(inputs)
        + facility_cost_per_gpu_hour(inputs)
        + inputs.other_opex_per_gpu_hour
    )


def cost_per_gpu_hour(inputs: GPUEconomicsInputs) -> float:
    """Fully loaded cost of a wall-clock GPU-hour: cash cost plus the capital charge."""
    return cash_cost_per_gpu_hour(inputs) + capital_cost_per_gpu_hour(inputs)


# --- Output and revenue -------------------------------------------------------------------


def tokens_per_gpu_hour(inputs: GPUEconomicsInputs) -> float:
    """Tokens produced in a wall-clock hour: ``tokens_per_sec * 3600 * utilization``."""
    return inputs.tokens_per_sec * 3600.0 * inputs.utilization


def cost_per_m_tokens(inputs: GPUEconomicsInputs) -> float:
    """Fully loaded cost to produce one million output tokens, in USD.

    Everything it costs to keep the GPU installed for an hour, divided by the tokens that hour
    actually produces once idle time is netted out. The gap between this and
    ``price_per_m_tokens`` is the token seller's margin. With ``utilization == 0`` nothing is
    produced, so the unit cost is infinite.
    """
    tokens = tokens_per_gpu_hour(inputs)
    if tokens == 0:
        return math.inf
    return cost_per_gpu_hour(inputs) / tokens * 1e6


def revenue_per_gpu_hour(inputs: GPUEconomicsInputs, mode: str = "tokens") -> float:
    """Revenue one GPU earns per wall-clock hour, in USD.

    ``mode="rental"``: the hour itself is sold, so revenue is the hourly price times the share
    of hours that are rented. ``mode="tokens"``: the hour's output is sold.
    """
    if mode == "rental":
        return inputs.price_per_gpu_hour * inputs.utilization
    if mode == "tokens":
        return tokens_per_gpu_hour(inputs) / 1e6 * inputs.price_per_m_tokens
    raise ValueError(f"mode must be one of {MODES}; got {mode!r}")


def cash_margin_per_gpu_hour(inputs: GPUEconomicsInputs, mode: str = "tokens") -> float:
    """Revenue less cash cost per wall-clock hour: the GPU's contribution before capital.

    This is the per-GPU analogue of EBITDA - earnings before the cost of the asset itself -
    and is what repays the purchase price.
    """
    return revenue_per_gpu_hour(inputs, mode) - cash_cost_per_gpu_hour(inputs)


def margin_per_gpu_hour(inputs: GPUEconomicsInputs, mode: str = "tokens") -> float:
    """Fully loaded profit per wall-clock hour: revenue less cash cost less the capital charge.

    Positive means the GPU earns more than its cost of capital over its life, which is the
    test of whether it was worth buying.
    """
    return revenue_per_gpu_hour(inputs, mode) - cost_per_gpu_hour(inputs)


def breakeven_price_per_gpu_hour(inputs: GPUEconomicsInputs) -> float:
    """Rental price at which the fully loaded margin is zero, in USD per rented hour.

    Costs accrue every hour but only rented hours earn, so the price must cover the full cost
    divided by utilization. Infinite when nothing is rented.
    """
    if inputs.utilization == 0:
        return math.inf
    return cost_per_gpu_hour(inputs) / inputs.utilization


def payback_months(inputs: GPUEconomicsInputs, mode: str = "tokens") -> float:
    """Months until the GPU's cash margin has repaid its purchase price.

    Defined the way operators quote it (CoreWeave's S-1, p.97, measures payback through
    adjusted EBITDA per GPU): price divided by the monthly cash margin, before financing and
    before depreciation. Infinite when the cash margin is zero or negative - a GPU that does
    not cover its running costs never pays back. Customer prepayments, which shorten
    CoreWeave's figure, are a company-level matter and are handled in the company model.
    """
    cash_margin = cash_margin_per_gpu_hour(inputs, mode)
    if cash_margin <= 0:
        return math.inf
    return inputs.chip_cost / (cash_margin * HOURS_PER_MONTH)


def breakdown(inputs: GPUEconomicsInputs, mode: str = "tokens") -> dict[str, float]:
    """Every intermediate quantity, in reading order, for the workbook and the site.

    One dict so that a reader can follow the arithmetic from inputs to answer without opening
    this file, and so the exporter has a single thing to lay out.
    """
    return {
        "capital_cost_per_gpu_hour": capital_cost_per_gpu_hour(inputs),
        "energy_cost_per_gpu_hour": energy_cost_per_gpu_hour(inputs),
        "facility_cost_per_gpu_hour": facility_cost_per_gpu_hour(inputs),
        "other_opex_per_gpu_hour": inputs.other_opex_per_gpu_hour,
        "cash_cost_per_gpu_hour": cash_cost_per_gpu_hour(inputs),
        "cost_per_gpu_hour": cost_per_gpu_hour(inputs),
        "tokens_per_gpu_hour": tokens_per_gpu_hour(inputs),
        "cost_per_m_tokens": cost_per_m_tokens(inputs),
        "revenue_per_gpu_hour": revenue_per_gpu_hour(inputs, mode),
        "cash_margin_per_gpu_hour": cash_margin_per_gpu_hour(inputs, mode),
        "margin_per_gpu_hour": margin_per_gpu_hour(inputs, mode),
        "breakeven_price_per_gpu_hour": breakeven_price_per_gpu_hour(inputs),
        "payback_months": payback_months(inputs, mode),
    }

"""GPU unit economics: what one installed GPU costs to run and what it earns (layer 1).

Purpose
-------
Every company model in this repository - CoreWeave, Nebius, the hyperscalers' capex lines -
rests on the same underlying question: what does a GPU-hour cost, what does it sell for, and
how long until the chip pays for itself? This module is the single place where those per-GPU
quantities are defined, so that every company is judged with the same yardstick and a change
to the definition propagates everywhere at once.

What is here today
------------------
* ``GPUEconomicsInputs`` - the nine assumptions the engine needs, with unit labels
  (``INPUT_UNITS``), plain-English descriptions (``INPUT_DESCRIPTIONS``) and input validation.
  This is plumbing and is complete; the xlsx exporter and the company models read these
  tables to build their "Inputs" sheet.
* Three engine functions - ``cost_per_m_tokens``, ``margin_per_gpu_hour`` and
  ``payback_months``. Their bodies are deliberately NOT written: the financial logic in this
  repository is authored by Philbert, not generated. Each raises ``NotImplementedError`` and
  carries a reference definition in its docstring that ``tests/test_unit_economics.py`` pins.
  Run ``uv run pytest tests/test_unit_economics.py --runxfail`` to see the tests that are
  waiting on the implementation. Each docstring also works the reference case through in
  numbers; those figures duplicate ``EXPECTED`` in the test module, so a revised definition
  means updating both in the same commit.

Conventions
-----------
* "Per GPU-hour" always means per WALL-CLOCK hour of an installed GPU: 8,760 hours a year,
  whether or not the GPU is busy. Idle time is captured by ``utilization``, never by shrinking
  the hour count. This matters because a GPU that is 60% utilised still depreciates and
  accrues financing cost for 100% of the hours.
* Money is USD, energy is kWh, power is kW, rates are decimals (0.08 means 8%).
* Costs are fully loaded (energy plus depreciation plus financing) unless a function's
  docstring says otherwise.
"""

from dataclasses import asdict, dataclass, fields

HOURS_PER_YEAR = 8760
HOURS_PER_MONTH = 730  # 8760 / 12; the payback convention, not a calendar month

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
}

# One plain-English line per input, shown next to the value on the workbook's Inputs sheet.
# Written for a finance reader who has not opened this file.
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
        "Share of wall-clock hours in which the GPU is producing billable output, from 0 to 1."
    ),
    "tokens_per_sec": "Sustained output tokens per second from one GPU while it is producing.",
    "price_per_m_tokens": "Blended selling price, in USD per million output tokens.",
    "depreciation_years": "Straight-line useful life of the GPU, in years.",
    "financing_rate": (
        "Annual cost of capital charged on chip_cost, as a decimal (0.08 means 8% a year)."
    ),
}


@dataclass(frozen=True)
class GPUEconomicsInputs:
    """The assumptions behind one GPU's economics.

    Frozen so that a set of inputs can be passed around, hashed and compared safely; make a
    variant with ``dataclasses.replace(inputs, utilization=0.8)``. Validation runs on
    construction, so an instance that exists is one the engine can accept.
    """

    chip_cost: float
    """USD per GPU, all-in installed (accelerator + share of server, network, rack)."""

    power_draw_kw: float
    """kW per GPU at load, including its share of host/server power."""

    pue: float
    """Facility power / IT power; 1.0 is the physical floor."""

    electricity_price_kwh: float
    """USD per kWh, delivered."""

    utilization: float
    """0..1, share of wall-clock hours producing billable output."""

    tokens_per_sec: float
    """Sustained output tokens per second per GPU while producing."""

    price_per_m_tokens: float
    """USD per million tokens, blended sell price."""

    depreciation_years: float
    """Straight-line useful life in years."""

    financing_rate: float
    """Annual cost of capital applied to chip_cost, as a decimal (0.08 = 8%)."""

    def __post_init__(self) -> None:
        # Every check is written as `not (bound holds)` rather than `value < bound` so that a
        # NaN, which fails every comparison, is rejected instead of quietly flowing into the
        # model and turning every output into NaN.
        if not (0.0 <= self.utilization <= 1.0):
            raise ValueError(
                f"utilization must be between 0 and 1 (a share of hours); got {self.utilization!r}"
            )
        if not (self.pue >= 1.0):
            raise ValueError(
                f"pue must be >= 1 (facility power cannot be less than IT power); got {self.pue!r}"
            )
        # These two sit in a denominator of the reference definitions, so zero is not allowed.
        for name in ("depreciation_years", "tokens_per_sec"):
            value = getattr(self, name)
            if not (value > 0):
                raise ValueError(f"{name} must be > 0; got {value!r}")
        for name in (
            "chip_cost",
            "power_draw_kw",
            "electricity_price_kwh",
            "price_per_m_tokens",
            "financing_rate",
        ):
            value = getattr(self, name)
            if not (value >= 0):
                raise ValueError(f"{name} must be >= 0; got {value!r}")

    def to_dict(self) -> dict[str, float]:
        """Return the inputs as a plain ``{field: value}`` dict, in field order.

        Used by the company models to build their Inputs frame and by the fingerprinting in
        the refresh pipeline; the key order matches ``INPUT_UNITS`` and ``INPUT_DESCRIPTIONS``.
        """
        return asdict(self)


INPUT_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(GPUEconomicsInputs))
"""Field names of ``GPUEconomicsInputs`` in declaration order; the canonical ordering for
the Inputs sheet and for any table that walks the assumptions."""


def cost_per_m_tokens(inputs: GPUEconomicsInputs) -> float:
    """Fully loaded cost to produce one million output tokens, in USD.

    In words: take everything it costs to keep one GPU installed and powered for a wall-clock
    hour - electricity, plus that hour's slice of depreciation and financing on the chip -
    then divide by the tokens the hour actually produces once idle time is netted out, and
    scale to a million tokens. The gap between this number and ``price_per_m_tokens`` is the
    operator's room to manoeuvre.

    Reference definition - Philbert may revise; tests pin these::

        energy_cost_per_hour  = power_draw_kw * pue * electricity_price_kwh
        capital_cost_per_hour = chip_cost * (1 / depreciation_years + financing_rate) / 8760
        tokens_per_hour       = tokens_per_sec * 3600 * utilization
        cost_per_m_tokens     = (energy_cost_per_hour + capital_cost_per_hour)
                                / tokens_per_hour * 1e6

    Reference case (``tests/test_unit_economics.py::REFERENCE``): 0.2079317 USD per million
    tokens, made up of 0.10 USD/h of energy and 1.0228311 USD/h of capital spread over 5.4
    million tokens an hour.

    Left to Philbert: ``utilization == 0`` passes validation but leaves ``tokens_per_hour`` at
    zero, so the reference definition divides by zero there; decide whether that should be
    ``math.inf`` or an error.
    """
    # TODO(philbert): implement cost_per_m_tokens per the reference definition in the docstring.
    raise NotImplementedError("TODO(philbert): implement engine.unit_economics.cost_per_m_tokens")


def margin_per_gpu_hour(inputs: GPUEconomicsInputs) -> float:
    """Fully loaded profit one installed GPU earns per wall-clock hour, in USD.

    In words: the revenue the GPU's tokens fetch in an hour, less the electricity to make them,
    less that hour's slice of depreciation and financing on the chip. "Fully loaded" means
    depreciation is charged even though it is not cash - this is the accounting margin, the
    one that tells you whether the asset earns its keep over its life. For the cash view see
    ``payback_months``.

    Reference definition - Philbert may revise; tests pin these::

        energy_cost_per_hour  = power_draw_kw * pue * electricity_price_kwh
        capital_cost_per_hour = chip_cost * (1 / depreciation_years + financing_rate) / 8760
        tokens_per_hour       = tokens_per_sec * 3600 * utilization
        revenue_per_hour      = tokens_per_hour / 1e6 * price_per_m_tokens
        margin_per_gpu_hour   = revenue_per_hour - energy_cost_per_hour - capital_cost_per_hour

    A useful identity the tests check: the margin equals the per-token spread
    ``(price_per_m_tokens - cost_per_m_tokens)`` times the millions of tokens produced in
    the hour.

    Reference case: 2.70 USD/h of revenue less 0.10 energy less 1.0228311 capital =
    1.5771690 USD per GPU-hour.
    """
    # TODO(philbert): implement margin_per_gpu_hour per the reference definition in the docstring.
    raise NotImplementedError("TODO(philbert): implement engine.unit_economics.margin_per_gpu_hour")


def payback_months(inputs: GPUEconomicsInputs) -> float:
    """Months of operation until the GPU's cash contribution has repaid its purchase price.

    In words: how many months does it take for the cash the GPU throws off - revenue less
    electricity less the interest on the money tied up in the chip - to add up to what the
    chip cost? Depreciation is excluded because it is not a cash outflow; the chip was paid
    for up front, and this number measures how fast that outlay comes back. Financing is
    included because interest is paid in cash every period. A payback shorter than the
    depreciable life means the asset returns its cost before it wears out.

    Reference definition - Philbert may revise; tests pin these::

        energy_cost_per_hour       = power_draw_kw * pue * electricity_price_kwh
        tokens_per_hour            = tokens_per_sec * 3600 * utilization
        revenue_per_hour           = tokens_per_hour / 1e6 * price_per_m_tokens
        cash_contribution_per_hour = revenue_per_hour - energy_cost_per_hour
                                     - chip_cost * financing_rate / 8760
        payback_months             = chip_cost / (cash_contribution_per_hour * 730)

    Return ``math.inf`` when ``cash_contribution_per_hour <= 0``: a GPU that does not cover
    its own power and interest never pays back, and the tests check for infinity rather than
    a negative or divide-by-zero result.

    Reference case: 32,000 USD over 2.3077626 USD/h of cash contribution = 18.99485 months.
    """
    # TODO(philbert): implement payback_months per the reference definition in the docstring.
    raise NotImplementedError("TODO(philbert): implement engine.unit_economics.payback_months")

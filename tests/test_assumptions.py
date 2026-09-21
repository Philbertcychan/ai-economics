"""Tests for ``companies/assumptions.py`` and for the registers committed in ``assumptions/``."""

from pathlib import Path

import pytest

from companies.assumptions import (
    ASSUMPTION_COLUMNS,
    assumptions_path,
    engine_inputs,
    load_assumptions,
    to_inputs_frame,
    unconfirmed,
)
from data import ASSUMPTIONS_DIR
from engine.unit_economics import GPUEconomicsInputs, margin_per_gpu_hour

HEADER = ",".join(ASSUMPTION_COLUMNS)
GOOD_ROWS = [
    "chip_cost,30000,USD,25000,40000,external,https://example.com/spec,confirmed,list price",
    "power_draw_kw,1.2,kW,,,derived,Filing p.2,proposed,facility power per GPU",
    "pue,1.0,ratio,,,judgment,,proposed,already at the meter",
    "electricity_price_kwh,0.06,USD/kWh,,,external,https://example.com/power,proposed,",
    "utilization,0.9,share,,,judgment,,proposed,",
    "tokens_per_sec,0,tokens/s,,,judgment,,proposed,rental only",
    "price_per_m_tokens,0,USD/M tokens,,,judgment,,proposed,rental only",
    "depreciation_years,6,years,4,6,disclosed,Filing p.F-16,overridden,",
    "financing_rate,0.11,decimal,,,disclosed,Filing p.F-34,proposed,",
    "price_per_gpu_hour,2.5,USD/GPU-hour,,,judgment,,proposed,",
    "contract_years,4,years,2,5,disclosed,Filing p.96,proposed,not an engine input",
]


def write(tmp_path: Path, rows: list[str], ticker: str = "AAA") -> Path:
    path = assumptions_path(ticker, tmp_path)
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8", newline="\n")
    return path


def test_missing_register_is_none(tmp_path: Path) -> None:
    assert load_assumptions("AAA", tmp_path) is None


def test_loads_types_and_blanks(tmp_path: Path) -> None:
    write(tmp_path, GOOD_ROWS)
    frame = load_assumptions("aaa", tmp_path)  # ticker is case-insensitive
    assert frame is not None and tuple(frame.columns) == ASSUMPTION_COLUMNS
    chip = frame.set_index("name").loc["chip_cost"]
    assert (chip["value"], chip["low"], chip["high"]) == (30000.0, 25000.0, 40000.0)
    assert frame.set_index("name")["low"].isna()["pue"]
    assert frame["source"].tolist()[2] == ""  # blank text stays blank, not NaN


def test_engine_inputs_takes_engine_fields_only(tmp_path: Path) -> None:
    write(tmp_path, GOOD_ROWS)
    inputs = engine_inputs(load_assumptions("AAA", tmp_path))
    assert isinstance(inputs, GPUEconomicsInputs)
    assert inputs.price_per_gpu_hour == 2.5 and inputs.depreciation_years == 6
    assert inputs.facility_cost_per_kw_month == 0.0  # omitted optional field keeps its default
    assert not hasattr(inputs, "contract_years")


def test_engine_inputs_names_a_missing_required_field(tmp_path: Path) -> None:
    write(tmp_path, [r for r in GOOD_ROWS if not r.startswith("financing_rate")])
    with pytest.raises(ValueError, match="financing_rate"):
        engine_inputs(load_assumptions("AAA", tmp_path))


def test_inputs_frame_shows_basis_status_and_range(tmp_path: Path) -> None:
    write(tmp_path, GOOD_ROWS)
    inputs = to_inputs_frame(load_assumptions("AAA", tmp_path)).set_index("name")
    assert list(inputs.columns) == ["value", "unit", "source", "note"]
    assert inputs.loc["chip_cost", "note"].startswith("[external; confirmed; range 25000 to 40000]")
    assert inputs.loc["pue", "note"].startswith("[judgment; proposed]")
    assert inputs.loc["depreciation_years", "source"] == "Filing p.F-16"


def test_unconfirmed_lists_open_decisions(tmp_path: Path) -> None:
    write(tmp_path, GOOD_ROWS)
    open_items = unconfirmed(load_assumptions("AAA", tmp_path))
    assert "chip_cost" not in open_items and "depreciation_years" not in open_items
    assert "financing_rate" in open_items


@pytest.mark.parametrize(
    ("bad_row", "message"),
    [
        ("chip_cost,1,USD,,,external,https://x,proposed,", "duplicate names"),
        ("x,abc,USD,,,judgment,,proposed,", "non-numeric value"),
        ("x,,USD,,,judgment,,proposed,", "no value"),
        ("x,1,USD,,,guess,,proposed,", "basis must be one of"),
        ("x,1,USD,,,judgment,,approved,", "status must be one of"),
        ("x,1,USD,,,disclosed,,proposed,", "basis needs a source"),
        ("x,9,USD,1,5,judgment,,proposed,", "outside its low/high range"),
    ],
)
def test_malformed_rows_fail_loudly(tmp_path: Path, bad_row: str, message: str) -> None:
    write(tmp_path, [*GOOD_ROWS, bad_row])
    with pytest.raises(ValueError, match=message):
        load_assumptions("AAA", tmp_path)


def test_missing_column_is_an_error(tmp_path: Path) -> None:
    assumptions_path("AAA", tmp_path).write_text("name,value\nx,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        load_assumptions("AAA", tmp_path)


@pytest.mark.parametrize("path", sorted(ASSUMPTIONS_DIR.glob("*.csv")), ids=lambda p: p.stem)
def test_committed_registers_load_and_run_the_engine(path: Path) -> None:
    frame = load_assumptions(path.stem, ASSUMPTIONS_DIR)
    assert frame is not None
    inputs = engine_inputs(frame)
    mode = "rental" if inputs.price_per_gpu_hour > 0 else "tokens"
    assert margin_per_gpu_hour(inputs, mode) == margin_per_gpu_hour(inputs, mode)  # not NaN

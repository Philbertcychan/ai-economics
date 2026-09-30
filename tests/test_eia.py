"""Offline tests for ``data/eia.py`` and ``scripts/pull_eia860m.py``.

No network and no real data: ``write_workbook`` builds a small EIA-860M look-alike with openpyxl
(title block with the "as of" month, header row at a non-zero offset, a blank-then-NOTES
footnote, numeric blanks written as single spaces and one text blank as an empty string the way
EIA writes them, a blank nameplate, planned status strings, a planned retirement date beside the
operating date, a balancing authority literally called ``NA`` and a generator ID with a leading
zero) and every expected number below is worked out by hand from those rows. The index page is a
trimmed copy of the real page's markup with made-up months. Companies and plants are invented.
"""
# ruff: noqa: E501  (fixture rows are one generator per line, wider than 100 columns on purpose)

from __future__ import annotations

import datetime as dt
import email.message
import hashlib
import json
import logging
import math
import random
import re
import urllib.error
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from openpyxl import Workbook

from data import eia
from data.eia import (
    CAPACITY_SUMMARY_COLUMNS,
    CORE_SHEETS,
    DEFAULT_USER_AGENT,
    INDEX_URL,
    MIN_REQUEST_INTERVAL_S,
    PLANNED_STATUS_CODES,
    PLANNED_SUMMARY_COLUMNS,
    SUMMARIES,
    TECHNOLOGY_GROUPS,
    TIDY_COLUMNS,
    UNDER_CONSTRUCTION_CODES,
    UNREPORTED_TECHNOLOGY,
    EIA860MClient,
    EIAError,
    capacity_by_fuel,
    check_workbook_bytes,
    find_header_row,
    group_technology,
    months_behind,
    newest_file_url,
    parse_index,
    period_from_name,
    planned_by_year_and_fuel,
    planned_in_window,
    previous_outputs,
    process_workbook,
    processed_is_current,
    raw_provenance,
    read_processed,
    read_sheet,
    read_source,
    read_summaries,
    retired_by_year_and_fuel,
    sheet_key,
    sheet_period,
    state_summary,
    status_code,
    summarise,
    tidy_all,
    tidy_workbook,
    user_agent,
    workbook_name,
    workbook_sheet_names,
    write_processed,
)
from scripts import pull_eia860m

TODAY = dt.date(2026, 9, 27)
FILE_NAME = "march_generator2026.xlsx"
AS_OF = "March 2026"

# --- Fixture workbook ------------------------------------------------------------------------

# Column layout per sheet, as EIA lays it out: a map column the tidy frame ignores; the operating
# sheet dates first operation and carries a planned retirement date that must be ignored; the
# retired sheet carries the operating date and the retirement date and no Status; the canceled
# sheet has no dates and no status.
_BASE_HEADER = [
    "Entity ID",
    "Entity Name",
    "Plant ID",
    "Plant Name",
    "Google Map",
    "Plant State",
    "Balancing Authority Code",
    "Generator ID",
    "Nameplate Capacity (MW)",
    "Net Summer Capacity (MW)",
    "Technology",
    "Energy Source Code",
    "Prime Mover Code",
]
OPERATING_HEADER = [
    *_BASE_HEADER,
    "Operating Month",
    "Operating Year",
    "Planned Retirement Month",
    "Planned Retirement Year",
    "Status",
]
PLANNED_HEADER = [*_BASE_HEADER, "Planned Operation Month", "Planned Operation Year", "Status"]
RETIRED_HEADER = [
    *_BASE_HEADER,
    "Operating Month",
    "Operating Year",
    "Retirement Month",
    "Retirement Year",
]
CANCELED_HEADER = list(_BASE_HEADER)

CC = "Natural Gas Fired Combined Cycle"
CT = "Natural Gas Fired Combustion Turbine"
SOLAR = "Solar Photovoltaic"
WIND = "Onshore Wind Turbine"
COAL = "Conventional Steam Coal"
OP = "(OP) Operating"
SB = "(SB) Standby/Backup: available for service but not normally used"
V = "(V) Under construction, more than 50 percent complete"
U = "(U) Under construction, less than or equal to 50 percent complete"
TS = "(TS) Construction complete, but not yet in commercial operation"
P = "(P) Planned for installation, but regulatory approvals not initiated"
L = "(L) Regulatory approvals pending. Not under construction"

# Rows are deliberately out of plant order, and within a plant out of generator order, so the
# sort is exercised. " " is EIA's numeric blank and "" its text blank. Nu Gas is a second plant
# of an existing technology whose capacities (0.6 + 0.3) sum inexactly in binary, so the MW
# rounding is load-bearing; its balancing authority is the literal code "NA" and its generator
# IDs keep a leading zero, which a default CSV read would destroy. Alpha Gas unit 1 has a planned
# retirement date that must not become its year.
OPERATING_ROWS: list[list[Any]] = [
    [3, "Gamma Wind", 30, "Gamma Breeze", "Map", "TX", "", "W1", 200, 200, WIND, "WND", "WT", 12, 2020, " ", " ", SB],
    [1, "Alpha Power", 10, "Alpha Gas", "Map", "TX", "ERCO", "2", 250.5, 240, CT, "NG", "CT", 6, 2015, " ", " ", OP],
    [1, "Alpha Power", 10, "Alpha Gas", "Map", "TX", "ERCO", "1", 500, 480, CC, "NG", "CA", 6, 2015, 12, 2030, OP],
    [2, "Beta Solar", 20, "Beta Sun", "Map", "CA", "CISO", "S1", 100, " ", SOLAR, "SUN", "PV", 3, 2022, " ", " ", OP],
    [4, "Delta Nuclear", 40, "Delta Atom", "Map", "IL", "PJM", "1", " ", 1000, "Nuclear", "NUC", "ST", 1, 1985, " ", " ", OP],
    [12, "Nu Power", 120, "Nu Gas", "Map", "OK", "NA", "02", 0.6, 0.6, CC, "NG", "CA", 6, 2018, " ", " ", OP],
    [12, "Nu Power", 120, "Nu Gas", "Map", "OK", "NA", "01", 0.3, 0.3, CC, "NG", "CA", 6, 2018, " ", " ", OP],
]  # fmt: skip
# Four technologies in the 2026-2028 window (the report shows three) and one unit outside it.
PLANNED_ROWS: list[list[Any]] = [
    [5, "Epsilon Dev", 50, "Epsilon CC", "Map", "TX", "ERCO", "1", 600, 570, CC, "NG", "CA", 6, 2027, V],
    [5, "Epsilon Dev", 50, "Epsilon CC", "Map", "TX", "ERCO", "2", 400, 380, CC, "NG", "CA", 12, 2027, P],
    [6, "Zeta Storage", 60, "Zeta Batt", "Map", "CA", "CISO", "B1", 150, 150, "Batteries", "MWH", "BA", 3, 2026, TS],
    [7, "Eta Solar", 70, "Eta Sun", "Map", "TX", "ERCO", "S1", 300, 300, SOLAR, "SUN", "PV", 9, 2026, U],
    [8, "Theta Solar", 80, "Theta Sun", "Map", "AZ", "AZPS", "S1", 120, 120, SOLAR, "SUN", "PV", 1, 2028, L],
    [8, "Theta Solar", 80, "Theta Sun", "Map", "AZ", "AZPS", "S2", 60, 60, SOLAR, "SUN", "PV", 4, 2028, U],
    [13, "Xi Wind", 130, "Xi Breeze", "Map", "OK", "SWPP", "W1", 90, 90, WIND, "WND", "WT", 5, 2026, P],
    [14, "Omicron Gas", 140, "Omicron CC", "Map", "TX", "ERCO", "1", 700, 680, CC, "NG", "CA", 1, 2029, P],
]  # fmt: skip
# Two rows have a blank entity and plant ID, the way EIA lists 1960s reactors: data rows, not
# footnotes, and they must survive. Both are generator "1", so only the tie-break columns order
# them; they are listed in reverse name order to prove it.
RETIRED_ROWS: list[list[Any]] = [
    [9, "Iota Coal", 90, "Iota Steam", "Map", "IL", "PJM", "2", 800, 750, COAL, "BIT", "ST", 1, 1972, 5, 2024],
    [9, "Iota Coal", 90, "Iota Steam", "Map", "IL", "PJM", "1", 800, 760, COAL, "BIT", "ST", 1, 1970, 5, 2024],
    [" ", "Nu Historic", " ", "Nu Pile", "Map", "CO", " ", "1", 100, 90, "Nuclear", "NUC", "ST", 2, 1965, 6, 1980],
    [" ", "Kappa Historic", " ", "Kappa Atom", "Map", "CO", " ", "1", 300, 280, "Nuclear", "NUC", "ST", 7, 1979, 8, 1989],
    [10, "Lambda Gas", 100, "Lambda Peaker", "Map", "TX", "ERCO", "GT1", 50, 45, CT, "NG", "GT", 6, 1990, 11, 2025],
]  # fmt: skip
CANCELED_ROWS: list[list[Any]] = [
    [11, "Mu Dev", 110, "Mu Wind", "Map", "OK", "SWPP", "1", 250, 250, WIND, "WND", "WT"],
]
NOTES = (
    "NOTES:\nCapacity from facilities with a total generator nameplate capacity less than 1 MW "
    "are excluded from this report.\nSources: made up for the test suite."
)
SHEETS: dict[str, tuple[list[str], list[list[Any]]]] = {
    "Operating": (OPERATING_HEADER, OPERATING_ROWS),
    "Planned": (PLANNED_HEADER, PLANNED_ROWS),
    "Retired": (RETIRED_HEADER, RETIRED_ROWS),
    "Canceled or Postponed": (CANCELED_HEADER, CANCELED_ROWS),
}
ROW_COUNTS = {"operating": 7, "planned": 8, "retired": 5, "canceled_or_postponed": 1}


def write_workbook(
    path: Path,
    *,
    header_row: int = 3,
    sheets: dict[str, tuple[list[str], list[list[Any]]]] | None = None,
    footnotes: bool = True,
    as_of: str = AS_OF,
    as_of_by_sheet: dict[str, str] | None = None,
) -> Path:
    """Write the fixture workbook: title naming the month, blank rows, header at ``header_row``
    (1-based), data, then a blank line and the NOTES block, like the real file."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, (header, rows) in (sheets or SHEETS).items():
        sheet = workbook.create_sheet(name)
        month = (as_of_by_sheet or {}).get(name, as_of)
        sheet.append([f"Inventory of {name} Generators as of {month}"])
        for _ in range(header_row - 2):
            sheet.append([""] * len(header))
        sheet.append(header)
        for row in rows:
            sheet.append(row)
        if footnotes:
            sheet.append([""])
            sheet.append([NOTES])
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    return write_workbook(tmp_path / FILE_NAME)


def without_column(header: list[str], rows: list[list[Any]], column: str) -> tuple[list, list]:
    index = header.index(column)
    return [c for c in header if c != column], [r[:index] + r[index + 1 :] for r in rows]


def renamed(header: list[str], old: str, new: str) -> list[str]:
    assert old in header
    return [new if c == old else c for c in header]


def with_cell(rows: list[list[Any]], row: int, header: list[str], column: str, value: Any) -> list:
    edited = [list(r) for r in rows]
    edited[row][header.index(column)] = value
    return edited


def csv_text(frame: pd.DataFrame) -> str:
    return frame.to_csv(index=False, lineterminator="\n")


# --- Header detection and tidy columns -------------------------------------------------------


def test_find_header_row_by_entity_id_cell() -> None:
    rows = [["Inventory of ..."], ["", ""], ["entity id ", "Entity Name"], [1, "A"]]
    assert find_header_row(rows) == 2  # case and trailing space do not matter
    assert find_header_row([["Title"], [None, "Entity ID"]]) == -1  # must be the first cell
    assert find_header_row([]) == -1


def test_sheet_period_from_the_title_block() -> None:
    assert (
        sheet_period([["Inventory of Operating Generators as of August 2026"], ["", ""]])
        == "2026-08"
    )
    assert sheet_period([[None, "Inventory ... as of March 2026 - Puerto Rico"]]) == "2026-03"
    assert sheet_period([["Inventory of Generators"], [1, 2]]) is None
    assert sheet_period([["as of Smarch 2026"]]) is None and sheet_period([]) is None


@pytest.mark.parametrize("header_row", [3, 7])
def test_read_sheet_finds_the_header_at_any_offset(tmp_path: Path, header_row: int) -> None:
    path = write_workbook(tmp_path / FILE_NAME, header_row=header_row)
    frame = read_sheet(path, "Operating")
    assert len(frame) == len(OPERATING_ROWS)  # title block and footnotes are gone
    assert frame["plant_name"].tolist() == [
        "Alpha Gas",
        "Alpha Gas",
        "Beta Sun",
        "Gamma Breeze",
        "Delta Atom",
        "Nu Gas",
        "Nu Gas",
    ]


def test_read_sheet_columns_dtypes_and_blanks(workbook: Path) -> None:
    frame = read_sheet(workbook, "Operating")
    assert list(frame.columns) == list(TIDY_COLUMNS)
    for column in ("entity_id", "plant_id", "year", "month"):
        assert str(frame[column].dtype) == "Int64", column
    assert frame["nameplate_mw"].dtype == "float64" and frame["net_summer_mw"].dtype == "float64"
    # text, never numbers: "01" keeps its zero; sorted by plant then unit, not in EIA's order
    assert frame["generator_id"].tolist() == ["1", "2", "S1", "W1", "1", "01", "02"]
    assert frame["plant_id"].tolist() == [10, 10, 20, 30, 40, 120, 120]
    alpha = frame[(frame["plant_id"] == 10) & (frame["generator_id"] == "2")].iloc[0]
    assert alpha["entity_name"] == "Alpha Power" and alpha["state"] == "TX"
    assert alpha["balancing_authority"] == "ERCO" and alpha["technology"] == CT
    assert alpha["energy_source"] == "NG" and alpha["prime_mover"] == "CT"
    assert alpha["nameplate_mw"] == 250.5 and alpha["net_summer_mw"] == 240.0
    assert alpha["status"] == OP and alpha["status_code"] == "OP"
    assert alpha["year"] == 2015 and alpha["month"] == 6
    # EIA's single-space blanks become NaN for numbers; "" and " " both become "" for text
    beta = frame[frame["plant_name"] == "Beta Sun"].iloc[0]
    assert math.isnan(beta["net_summer_mw"]) and beta["nameplate_mw"] == 100.0
    delta = frame[frame["plant_name"] == "Delta Atom"].iloc[0]
    assert math.isnan(delta["nameplate_mw"]) and delta["net_summer_mw"] == 1000.0
    gamma = frame[frame["plant_name"] == "Gamma Breeze"].iloc[0]
    assert gamma["status_code"] == "SB" and gamma["balancing_authority"] == ""
    # the literal code "NA" is a balancing authority, not a missing value
    assert frame.loc[frame["plant_name"] == "Nu Gas", "balancing_authority"].tolist() == [
        "NA",
        "NA",
    ]
    assert not frame["status"].isna().any() and not frame["technology"].isna().any()


def test_operating_sheet_ignores_the_planned_retirement_date(workbook: Path) -> None:
    frame = read_sheet(workbook, "Operating")
    alpha_1 = frame[(frame["plant_id"] == 10) & (frame["generator_id"] == "1")].iloc[0]
    assert (alpha_1["year"], alpha_1["month"]) == (2015, 6)  # not 2030 / 12


def test_planned_sheet_dates_and_status_codes(workbook: Path) -> None:
    frame = read_sheet(workbook, "Planned")
    assert frame["year"].tolist() == [2027, 2027, 2026, 2026, 2028, 2028, 2026, 2029]
    assert frame["month"].tolist() == [6, 12, 3, 9, 1, 4, 5, 1]
    assert frame["status_code"].tolist() == ["V", "P", "TS", "U", "L", "U", "P", "P"]
    assert frame["status"].iloc[0] == V


def test_retired_sheet_uses_the_retirement_date_and_keeps_blank_ids(workbook: Path) -> None:
    frame = read_sheet(workbook, "Retired")
    assert len(frame) == len(RETIRED_ROWS)
    kappa = frame[frame["plant_name"] == "Kappa Atom"].iloc[0]
    assert pd.isna(kappa["entity_id"]) and pd.isna(kappa["plant_id"])
    assert kappa["balancing_authority"] == "" and kappa["nameplate_mw"] == 300.0
    assert kappa["year"] == 1989 and kappa["month"] == 8  # retirement, not first operation
    # blank plant IDs sort last; the two blank-ID reactors share generator "1" and are ordered
    # by the tie-break columns (entity name), not by EIA's row order
    assert frame["year"].tolist() == [2024, 2024, 2025, 1989, 1980]
    assert frame["plant_name"].tolist() == [
        "Iota Steam",
        "Iota Steam",
        "Lambda Peaker",
        "Kappa Atom",
        "Nu Pile",
    ]
    assert frame["generator_id"].tolist() == ["1", "2", "GT1", "1", "1"]
    assert (frame["status"] == "").all() and (frame["status_code"] == "").all()  # no column


@pytest.mark.parametrize("seed", [None, 1, 2, 3])
def test_tidy_output_does_not_depend_on_row_order(tmp_path: Path, seed: int | None) -> None:
    """Reversing or shuffling every sheet's rows gives byte-identical CSV text, tied keys
    included (the two blank-ID reactors swap places when the rows are reversed)."""
    shuffled = {}
    for name, (header, rows) in SHEETS.items():
        rows = [list(r) for r in rows]
        if seed is None:
            rows.reverse()
        else:
            random.Random(seed).shuffle(rows)
        shuffled[name] = (header, rows)
    original = tidy_all(write_workbook(tmp_path / "a" / FILE_NAME))
    reordered = tidy_all(write_workbook(tmp_path / "b" / FILE_NAME, sheets=shuffled))
    for key in original:
        assert csv_text(original[key]) == csv_text(reordered[key]), key


def test_sheet_without_dates_or_status(workbook: Path) -> None:
    frame = read_sheet(workbook, "Canceled or Postponed")
    assert len(frame) == 1
    assert frame["year"].isna().all() and frame["month"].isna().all()
    assert frame["status"].tolist() == [""] and frame["generator_id"].tolist() == ["1"]


def test_tidy_workbook_keys_every_sheet_and_reads_the_period(workbook: Path) -> None:
    tidied = tidy_workbook(workbook)
    frames = tidied.frames
    assert set(frames) == set(ROW_COUNTS) and set(CORE_SHEETS) <= set(frames)
    assert {k: len(v) for k, v in frames.items()} == ROW_COUNTS
    assert tidied.period == "2026-03" and tidied.skipped_sheets == []
    pd.testing.assert_frame_equal(frames["planned"], read_sheet(workbook, "Planned"))
    assert tidy_all(workbook).keys() == frames.keys()
    assert sheet_key("Operating_PR") == "operating_pr"
    assert sheet_key("Canceled or Postponed") == "canceled_or_postponed"


def test_status_code() -> None:
    assert status_code(V) == "V" and status_code(TS) == "TS" and status_code(OP) == "OP"
    assert status_code("(ot) Other") == "OT"
    assert status_code("Operating") == "" and status_code(None) == "" and status_code(" ") == ""
    assert UNDER_CONSTRUCTION_CODES == {"V", "U", "TS"}
    assert UNDER_CONSTRUCTION_CODES < PLANNED_STATUS_CODES


# --- Validation ------------------------------------------------------------------------------


def test_missing_required_column_names_sheet_and_column(tmp_path: Path) -> None:
    header, rows = without_column(OPERATING_HEADER, OPERATING_ROWS, "Technology")
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Operating": (header, rows)})
    with pytest.raises(ValueError, match=r"sheet 'Operating'.*Technology"):
        read_sheet(path, "Operating")


@pytest.mark.parametrize(
    ("sheet", "column"),
    [
        ("Retired", "Retirement Year"),
        ("Retired", "Retirement Month"),
        ("Planned", "Planned Operation Year"),
        ("Planned", "Planned Operation Month"),
        ("Operating", "Operating Year"),
        ("Operating", "Operating Month"),
        ("Operating", "Status"),
        ("Planned", "Status"),
        ("Retired", "Generator ID"),
        ("Canceled or Postponed", "Generator ID"),
    ],
)
def test_each_sheet_kind_requires_its_own_date_status_and_id_columns(
    tmp_path: Path, sheet: str, column: str
) -> None:
    """A renamed header is an error, never a fall-through to the other date pair on the sheet."""
    header, rows = SHEETS[sheet]
    path = write_workbook(
        tmp_path / FILE_NAME, sheets={sheet: (renamed(header, column, f"{column} (old)"), rows)}
    )
    with pytest.raises(
        ValueError, match=rf"sheet '{re.escape(sheet)}': missing columns \['{re.escape(column)}'\]"
    ):
        read_sheet(path, sheet)


def test_retired_and_canceled_sheets_need_no_status(workbook: Path) -> None:
    assert "Status" not in RETIRED_HEADER and "Status" not in CANCELED_HEADER
    assert len(read_sheet(workbook, "Retired")) == 5  # loads without one


@pytest.mark.parametrize(
    ("sheet", "status", "match"),
    [
        ("Planned", "V - Under construction, more than 50 percent complete", "without a code"),
        ("Planned", "( V ) Under construction", "without a code"),
        (
            "Planned",
            "(XX) A code EIA does not use",
            r"codes outside \['L', 'OT', 'P', 'T', 'TS', 'U', 'V'\]",
        ),
        ("Planned", "(OP) Operating", "codes outside"),
        ("Operating", "Operating", "without a code"),
        ("Operating", " ", "without a code"),
    ],
)
def test_status_must_carry_a_known_code(
    tmp_path: Path, sheet: str, status: str, match: str
) -> None:
    header, rows = SHEETS[sheet]
    rows = with_cell(rows, 0, header, "Status", status)
    path = write_workbook(tmp_path / FILE_NAME, sheets={sheet: (header, rows)})
    with pytest.raises(ValueError, match=rf"sheet '{sheet}': 'Status'.*{match}"):
        read_sheet(path, sheet)
    assert (
        status_code("(OS) Out of service") == "OS"
    )  # an operating code outside the planned set is fine there


def test_non_numeric_nameplate_is_an_error(tmp_path: Path) -> None:
    rows = with_cell(OPERATING_ROWS, 1, OPERATING_HEADER, "Nameplate Capacity (MW)", "n/a")
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Operating": (OPERATING_HEADER, rows)})
    with pytest.raises(ValueError, match=r"sheet 'Operating'.*Nameplate Capacity.*n/a"):
        read_sheet(path, "Operating")


@pytest.mark.parametrize("bad_year", [2026.5, "soon"])
def test_non_integer_year_is_an_error(tmp_path: Path, bad_year: Any) -> None:
    rows = with_cell(PLANNED_ROWS, 0, PLANNED_HEADER, "Planned Operation Year", bad_year)
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Planned": (PLANNED_HEADER, rows)})
    with pytest.raises(ValueError, match=r"sheet 'Planned'.*Planned Operation Year"):
        read_sheet(path, "Planned")


@pytest.mark.parametrize(
    ("sheet", "column", "value", "match"),
    [
        (
            "Planned",
            "Planned Operation Month",
            13,
            r"'Planned Operation Month' has values outside 1 to 12: \[13",
        ),
        ("Planned", "Planned Operation Month", 0, r"outside 1 to 12: \[0"),
        (
            "Planned",
            "Planned Operation Year",
            20277,
            r"'Planned Operation Year' has values outside 1880 to 2100: \[20277",
        ),
        ("Planned", "Planned Operation Year", 27, r"outside 1880 to 2100: \[27"),
        (
            "Planned",
            "Planned Operation Year",
            1e30,
            r"'Planned Operation Year' has values outside 1880 to 2100",
        ),
        (
            "Retired",
            "Retirement Year",
            0,
            r"'Retirement Year' has values outside 1880 to 2100: \[0",
        ),
        (
            "Operating",
            "Nameplate Capacity (MW)",
            -5,
            r"'Nameplate Capacity \(MW\)' has values outside 0 or more: \[-5",
        ),
        (
            "Operating",
            "Net Summer Capacity (MW)",
            "inf",
            r"'Net Summer Capacity \(MW\)' has infinite values",
        ),
        ("Operating", "Nameplate Capacity (MW)", "Infinity", r"infinite values"),
        (
            "Operating",
            "Nameplate Capacity (MW)",
            True,
            r"'Nameplate Capacity \(MW\)' has TRUE/FALSE cells",
        ),
        (
            "Planned",
            "Planned Operation Year",
            True,
            r"'Planned Operation Year' has TRUE/FALSE cells",
        ),
        ("Operating", "Plant ID", 1e30, r"'Plant ID' has values outside"),
    ],
)
def test_values_a_generator_table_cannot_hold_are_errors(
    tmp_path: Path, sheet: str, column: str, value: Any, match: str
) -> None:
    header, rows = SHEETS[sheet]
    path = write_workbook(
        tmp_path / FILE_NAME, sheets={sheet: (header, with_cell(rows, 0, header, column, value))}
    )
    with pytest.raises(ValueError, match=match) as info:
        read_sheet(path, sheet)
    assert str(info.value).startswith(f"sheet '{sheet}': '{column}'")


def test_a_generator_listed_twice_for_its_plant_is_an_error(tmp_path: Path) -> None:
    rows = with_cell(OPERATING_ROWS, 1, OPERATING_HEADER, "Generator ID", "1")  # Alpha Gas 2 -> 1
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Operating": (OPERATING_HEADER, rows)})
    with pytest.raises(
        ValueError,
        match=r"sheet 'Operating': generator listed twice for its plant: \[\(10, '1'\)\]",
    ):
        read_sheet(path, "Operating")
    # the same generator ID at two plants, or on two rows without a plant ID, is not a duplicate
    assert (
        read_sheet(write_workbook(tmp_path / "ok" / FILE_NAME), "Retired")["generator_id"]
        .tolist()
        .count("1")
        == 3
    )


@pytest.mark.parametrize(
    ("trailer", "match"),
    [
        ([" ", "Source: Form EIA-860M"], r"'Plant Name' is blank on rows \[11\]"),
        (
            [None, "Total", None, None, None, None, None, None, 1050.5, 1920],
            r"'Plant Name' is blank on rows \[11\]",
        ),
        (
            [
                15,
                "Rho Power",
                150,
                "Rho Plant",
                "Map",
                "TX",
                "ERCO",
                " ",
                10,
                10,
                CT,
                "NG",
                "GT",
                1,
                2020,
                " ",
                " ",
                OP,
            ],
            r"'Generator ID' is blank on rows \[11\]",
        ),
    ],
)
def test_a_row_without_plant_name_or_generator_id_is_an_error(
    tmp_path: Path, trailer: list[Any], match: str
) -> None:
    """A note or total row among the generators must not be counted as a unit."""
    rows = [*OPERATING_ROWS, trailer]  # header on sheet row 3, so the trailer is row 11
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Operating": (OPERATING_HEADER, rows)})
    with pytest.raises(ValueError, match=rf"sheet 'Operating': {match}"):
        read_sheet(path, "Operating")


def test_blank_year_is_allowed(tmp_path: Path) -> None:
    rows = with_cell(PLANNED_ROWS, 0, PLANNED_HEADER, "Planned Operation Year", " ")
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Planned": (PLANNED_HEADER, rows)})
    frame = read_sheet(path, "Planned")
    assert pd.isna(
        frame.loc[(frame["plant_id"] == 50) & (frame["generator_id"] == "1"), "year"].iloc[0]
    )


def test_duplicate_header_column_is_an_error(tmp_path: Path) -> None:
    header = [
        *OPERATING_HEADER[:4],
        "Technology",
        *OPERATING_HEADER[5:],
    ]  # Google Map -> Technology
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Operating": (header, OPERATING_ROWS)})
    with pytest.raises(ValueError, match=r"sheet 'Operating': duplicate columns \['Technology'\]"):
        read_sheet(path, "Operating")


def test_missing_header_row_unknown_sheet_and_unknown_kind(tmp_path: Path) -> None:
    header = ["Entity", *OPERATING_HEADER[1:]]  # first cell is not "Entity ID"
    sheets = {"Operating": (header, OPERATING_ROWS), "Notes": (OPERATING_HEADER, OPERATING_ROWS)}
    path = write_workbook(tmp_path / FILE_NAME, sheets=sheets)
    with pytest.raises(ValueError, match=r"sheet 'Operating'.*Entity ID"):
        read_sheet(path, "Operating")
    with pytest.raises(ValueError, match=r"sheet 'Planned'.*not in"):
        read_sheet(path, "Planned")
    with pytest.raises(ValueError, match=r"sheet 'Notes': not a generator sheet"):
        read_sheet(path, "Notes")  # a valid table under a name that says nothing about its dates


def test_tidy_workbook_requires_the_core_sheets(tmp_path: Path) -> None:
    sheets = {k: v for k, v in SHEETS.items() if k != "Retired"}
    path = write_workbook(tmp_path / FILE_NAME, sheets=sheets)
    with pytest.raises(ValueError, match=r"missing sheets \['retired'\]"):
        tidy_all(path)
    # a core sheet that is present but not a generator table is an error, not a skip
    broken = {**SHEETS, "Retired": (["Definitions"], [["a"], ["b"]])}
    with pytest.raises(ValueError, match=r"sheet 'Retired': no header row"):
        tidy_workbook(write_workbook(tmp_path / "b" / FILE_NAME, sheets=broken))


def test_tidy_workbook_skips_extra_sheets_that_are_not_generator_tables(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sheets = {
        **SHEETS,
        "Notes": (["Definitions"], [["V means under construction"], ["Source: EIA"]]),
    }
    sheets["Layout"] = (OPERATING_HEADER, OPERATING_ROWS)  # a table, but of no known kind
    path = write_workbook(tmp_path / FILE_NAME, sheets=sheets)
    with caplog.at_level(logging.WARNING, logger="data.eia"):
        tidied = tidy_workbook(path)
    assert set(tidied.frames) == set(ROW_COUNTS)
    assert tidied.skipped_sheets == ["Notes", "Layout"] and tidied.period == "2026-03"
    assert [r.getMessage() for r in caplog.records] == [
        f"{FILE_NAME}: sheet 'Notes' is not a generator table; skipped",
        f"{FILE_NAME}: sheet 'Layout' is not a generator table; skipped",
    ]


def test_two_sheets_with_the_same_key_are_an_error(tmp_path: Path) -> None:
    sheets = {**SHEETS, "Operating PR": SHEETS["Operating"], "Operating_PR": SHEETS["Operating"]}
    path = write_workbook(tmp_path / FILE_NAME, sheets=sheets)
    with pytest.raises(
        ValueError, match=r"sheets \['Operating PR', 'Operating_PR'\] share the key 'operating_pr'"
    ):
        tidy_workbook(path)


def test_period_comes_from_the_titles_and_must_agree_with_the_file_name(tmp_path: Path) -> None:
    # a browser-renamed download has no period in its name: the titles supply it
    renamed_copy = write_workbook(tmp_path / "august_generator2026 (1).xlsx", as_of="August 2026")
    assert period_from_name(renamed_copy.name) is None
    assert tidy_workbook(renamed_copy).period == "2026-08"
    # a name that disagrees with the titles is an error naming both
    mislabelled = write_workbook(tmp_path / "december_generator2031.xlsx", as_of=AS_OF)
    with pytest.raises(
        ValueError, match=r"file name says 2031-12 but the sheet titles say 2026-03"
    ):
        tidy_workbook(mislabelled)
    # sheets that disagree with each other are an error too
    mixed = write_workbook(tmp_path / "mixed" / FILE_NAME, as_of_by_sheet={"Planned": "April 2026"})
    with pytest.raises(ValueError, match=r"sheet titles disagree on the period"):
        tidy_workbook(mixed)
    # no title anywhere: the name alone
    untitled = write_workbook(tmp_path / "u" / FILE_NAME, as_of="some time")
    assert tidy_workbook(untitled).period == "2026-03"


def test_workbook_without_footnotes_parses_the_same(tmp_path: Path) -> None:
    with_notes = tidy_all(write_workbook(tmp_path / "a" / FILE_NAME))
    without = tidy_all(write_workbook(tmp_path / "b" / FILE_NAME, footnotes=False))
    for key in with_notes:
        pd.testing.assert_frame_equal(with_notes[key], without[key])


def understate_dimensions(path: Path) -> int:
    """Rewrite every sheet's ``<dimension>`` record to claim three rows; returns how many."""
    changed = 0
    parts: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            content = archive.read(info.filename)
            if info.filename.startswith("xl/worksheets/sheet"):
                content, count = re.subn(
                    rb'<dimension ref="[^"]*"', b'<dimension ref="A1:B3"', content
                )
                changed += count
            parts[info.filename] = content
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return changed


def test_an_understated_dimension_record_does_not_truncate_a_sheet(tmp_path: Path) -> None:
    expected = tidy_all(write_workbook(tmp_path / "a" / FILE_NAME))
    patched = write_workbook(tmp_path / "b" / FILE_NAME)
    assert understate_dimensions(patched) == len(SHEETS)
    frames = tidy_all(patched)
    for key in expected:
        pd.testing.assert_frame_equal(frames[key], expected[key])


# --- Summaries (expected numbers worked out by hand from the fixture rows) --------------------


def test_capacity_by_fuel(workbook: Path) -> None:
    table = capacity_by_fuel(read_sheet(workbook, "Operating"))
    assert list(table.columns) == ["technology", "units", "nameplate_mw", "net_summer_mw"]
    # largest nameplate first; the nuclear unit's blank nameplate counts as nothing
    assert table["technology"].tolist() == [CC, CT, WIND, SOLAR, "Nuclear"]
    assert table["units"].tolist() == [3, 1, 1, 1, 1]
    # combined cycle is three units at two plants: 500 + 0.6 + 0.3, which floats sum to
    # 500.90000000000003 before the rounding to 0.1 MW
    assert table["nameplate_mw"].tolist() == [500.9, 250.5, 200.0, 100.0, 0.0]
    assert table["net_summer_mw"].tolist() == [480.9, 240.0, 200.0, 0.0, 1000.0]
    assert "500.9," in csv_text(table) and "500.90000000000003" not in csv_text(table)


def test_summaries_label_a_blank_technology_and_break_mw_ties_by_name() -> None:
    operating = pd.DataFrame(
        {
            "technology": ["Zeta", "", "Alpha", "Alpha"],
            "nameplate_mw": [10.0, 60.5, 5.0, 5.0],
            "net_summer_mw": [10.0, 75.7, 5.0, 5.0],
        }
    )
    table = capacity_by_fuel(operating)
    assert table["technology"].tolist() == [UNREPORTED_TECHNOLOGY, "Alpha", "Zeta"]  # 60.5, 10, 10
    assert table["units"].tolist() == [1, 2, 1]
    assert UNREPORTED_TECHNOLOGY and UNREPORTED_TECHNOLOGY.strip() == UNREPORTED_TECHNOLOGY
    planned = pd.DataFrame(
        {
            "year": pd.array([2027, 2027, 2027], dtype="Int64"),
            "technology": ["Beta", "Alpha", ""],
            "nameplate_mw": [1.0, 1.0, 2.0],
            "net_summer_mw": [1.0, 1.0, 2.0],
            "status_code": ["V", "P", "P"],
        }
    )
    assert planned_by_year_and_fuel(planned)["technology"].tolist() == [
        UNREPORTED_TECHNOLOGY,
        "Alpha",
        "Beta",
    ]
    retired = pd.DataFrame(
        {
            "year": pd.array([2020, 2020], dtype="Int64"),
            "technology": ["Beta", "Alpha"],
            "nameplate_mw": [1.0, 1.0],
            "net_summer_mw": [1.0, 1.0],
        }
    )
    assert retired_by_year_and_fuel(retired)["technology"].tolist() == ["Alpha", "Beta"]


def test_planned_by_year_and_fuel(workbook: Path) -> None:
    table = planned_by_year_and_fuel(read_sheet(workbook, "Planned"))
    assert list(table.columns) == [
        "year",
        "technology",
        "units",
        "nameplate_mw",
        "net_summer_mw",
        "under_construction_mw",
        "under_construction_share",
    ]
    rows = [tuple(r) for r in table.itertuples(index=False)]
    assert rows == [
        # 2026: solar 300 (U) and the battery 150 (TS) are under construction, wind 90 (P) is not
        (2026, SOLAR, 1, 300.0, 300.0, 300.0, 1.0),
        (2026, "Batteries", 1, 150.0, 150.0, 150.0, 1.0),
        (2026, WIND, 1, 90.0, 90.0, 0.0, 0.0),
        # 2027: two combined-cycle units, 600 (V) + 400 (P); only the V unit is being built
        (2027, CC, 2, 1000.0, 950.0, 600.0, 0.6),
        # 2028: 120 approvals pending (L) + 60 under construction (U): 60 / 180 rounds to 0.333
        (2028, SOLAR, 2, 180.0, 180.0, 60.0, 0.333),
        # 2029: outside the report's window, present in the summary
        (2029, CC, 1, 700.0, 680.0, 0.0, 0.0),
    ]
    assert str(table["year"].dtype) == "Int64"
    text = csv_text(table)
    assert ",0.333\n" in text and "0.33333" not in text


def test_planned_share_is_computed_from_unrounded_sums_and_guards_a_zero_total() -> None:
    def frame(nameplates: list[float], codes: list[str]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "year": pd.array([2027] * len(codes), dtype="Int64"),
                "technology": ["X"] * len(codes),
                "nameplate_mw": nameplates,
                "net_summer_mw": [1.0] * len(codes),
                "status_code": codes,
            }
        )

    # two units of 0.14 MW, one under construction: the share is 0.5, not 0.1 / 0.3
    small = planned_by_year_and_fuel(frame([0.14, 0.14], ["V", "P"]))
    assert small["nameplate_mw"].tolist() == [0.3] and small["under_construction_mw"].tolist() == [
        0.1
    ]
    assert small["under_construction_share"].tolist() == [0.5]
    # nothing planned in MW: NaN, and never inf when the numerator is not zero
    blank = planned_by_year_and_fuel(frame([float("nan"), float("nan")], ["V", "P"]))
    assert blank["nameplate_mw"].tolist() == [0.0] and math.isnan(
        blank["under_construction_share"].iloc[0]
    )
    netted = planned_by_year_and_fuel(frame([600.0, -600.0], ["V", "P"]))
    assert netted["under_construction_mw"].tolist() == [600.0]
    assert math.isnan(netted["under_construction_share"].iloc[0])


def test_blank_year_sorts_last_in_the_summaries_and_still_counts(tmp_path: Path) -> None:
    planned_rows = with_cell(PLANNED_ROWS, 0, PLANNED_HEADER, "Planned Operation Year", " ")
    retired_rows = with_cell(RETIRED_ROWS, 0, RETIRED_HEADER, "Retirement Year", " ")
    path = write_workbook(
        tmp_path / FILE_NAME,
        sheets={
            "Planned": (PLANNED_HEADER, planned_rows),
            "Retired": (RETIRED_HEADER, retired_rows),
        },
    )
    planned = planned_by_year_and_fuel(read_sheet(path, "Planned"))
    last = planned.iloc[-1]
    assert pd.isna(last["year"]) and last["technology"] == CC
    assert (last["units"], last["nameplate_mw"], last["under_construction_mw"]) == (1, 600.0, 600.0)
    assert planned["year"].tolist()[:-1] == [2026, 2026, 2026, 2027, 2028, 2029]
    assert planned["nameplate_mw"].sum() == 2420.0 == sum(r[8] for r in PLANNED_ROWS)
    retired = retired_by_year_and_fuel(read_sheet(path, "Retired"))
    rows = [tuple(r) for r in retired.itertuples(index=False)]
    assert rows[:-1] == [
        (1980, "Nuclear", 1, 100.0, 90.0),
        (1989, "Nuclear", 1, 300.0, 280.0),
        (2024, COAL, 1, 800.0, 760.0),
        (2025, CT, 1, 50.0, 45.0),
    ]
    assert pd.isna(rows[-1][0]) and rows[-1][1:] == (COAL, 1, 800.0, 750.0)


def test_retired_by_year_and_fuel(workbook: Path) -> None:
    table = retired_by_year_and_fuel(read_sheet(workbook, "Retired"))
    assert list(table.columns) == ["year", "technology", "units", "nameplate_mw", "net_summer_mw"]
    rows = [tuple(r) for r in table.itertuples(index=False)]
    assert rows == [
        (1980, "Nuclear", 1, 100.0, 90.0),
        (1989, "Nuclear", 1, 300.0, 280.0),
        (2024, COAL, 2, 1600.0, 1510.0),  # 800 + 800; 760 + 750
        (2025, CT, 1, 50.0, 45.0),
    ]


def test_state_summary(workbook: Path) -> None:
    table = state_summary(read_sheet(workbook, "Operating"), read_sheet(workbook, "Planned"))
    assert list(table.columns) == [
        "state",
        "operating_units",
        "operating_mw",
        "planned_units",
        "planned_mw",
        "under_construction_mw",
    ]
    rows = [tuple(r) for r in table.itertuples(index=False)]
    assert rows == [
        # TX operating 500 + 250.5 + 200; planned 600 + 400 + 300 + 700, of which V 600 and U 300
        ("TX", 3, 950.5, 4, 2000.0, 900.0),
        ("AZ", 0, 0.0, 2, 180.0, 60.0),  # planned only; 60 of it under construction
        ("CA", 1, 100.0, 1, 150.0, 150.0),  # the TS battery counts as under construction
        ("OK", 2, 0.9, 1, 90.0, 0.0),  # 0.6 + 0.3 is 0.8999999999999999 before rounding
        ("IL", 1, 0.0, 0, 0.0, 0.0),  # operating only, and its nameplate is blank
    ]
    text = csv_text(table)
    assert "OK,2,0.9,1,90.0,0.0\n" in text and "0.8999" not in text


def test_planned_in_window(workbook: Path) -> None:
    summary = planned_by_year_and_fuel(read_sheet(workbook, "Planned"))
    window = planned_in_window(summary, 2026, 3)
    assert list(window.columns) == ["technology", "nameplate_mw"]
    # 2026-2028: CC 1000 (2027), solar 300 (2026) + 180 (2028), batteries 150, wind 90; the
    # 2029 combined-cycle unit (700) is outside the window
    assert [tuple(r) for r in window.itertuples(index=False)] == [
        (CC, 1000.0),
        (SOLAR, 480.0),
        ("Batteries", 150.0),
        (WIND, 90.0),
    ]
    assert [tuple(r) for r in planned_in_window(summary, 2029, 1).itertuples(index=False)] == [
        (CC, 700.0)
    ]
    assert planned_in_window(summary, 2030, 3).empty
    # a blank year is outside every window; ties sort by technology
    extra = pd.DataFrame(
        {
            "year": pd.array([2026, None], dtype="Int64"),
            "technology": ["Aaa", "Zzz"],
            "nameplate_mw": [90.0, 5.0],
        }
    )
    both = planned_in_window(pd.concat([summary, extra], ignore_index=True), 2026, 1)
    assert both["technology"].tolist() == [
        SOLAR,
        "Batteries",
        "Aaa",
        WIND,
    ]  # Aaa before Onshore Wind at 90
    with pytest.raises(ValueError, match="years must be at least 1"):
        planned_in_window(summary, 2026, 0)
    with pytest.raises(ValueError, match="planned_in_window: missing columns"):
        planned_in_window(pd.DataFrame({"year": [2026]}), 2026, 1)


def test_summaries_name_missing_columns() -> None:
    with pytest.raises(ValueError, match="capacity_by_fuel.*nameplate_mw"):
        capacity_by_fuel(pd.DataFrame({"technology": ["X"]}))
    with pytest.raises(ValueError, match="planned.*status_code"):
        planned_by_year_and_fuel(pd.DataFrame({"year": [1], "technology": ["X"]}))


def test_read_processed_round_trips_the_tidy_frames(tmp_path: Path, workbook: Path) -> None:
    frames = tidy_all(workbook)
    write_processed(frames, summarise(frames), tmp_path, {"source": "EIA860M"})
    for key, frame in frames.items():
        pd.testing.assert_frame_equal(read_processed(tmp_path / f"{key}.csv"), frame)
    operating = read_processed(tmp_path / "operating.csv")
    assert (
        operating["balancing_authority"].tolist().count("NA") == 2
    )  # the code, not a missing value
    assert (
        "01" in operating["generator_id"].tolist()
        and (operating["balancing_authority"] == "").sum() == 1
    )
    assert (
        str(read_processed(tmp_path / "retired.csv")["plant_id"].dtype) == "Int64"
    )  # blanks stay nullable ints
    # what a default read would do, and why the reader exists
    naive = pd.read_csv(tmp_path / "operating.csv")
    assert naive["balancing_authority"].isna().sum() == 3 and "1" in naive["generator_id"].tolist()
    (tmp_path / "other.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"other\.csv: columns \['a', 'b'\] are not"):
        read_processed(tmp_path / "other.csv")


# --- Index page --------------------------------------------------------------------------------

# Trimmed from the real page's markup: three live months out of order, the newer months EIA
# pre-writes inside an HTML comment, and an unrelated workbook link that must be ignored.
INDEX_HTML = """
<div class="accordion first-open">
  <h3>2026</h3>
  <table class="basic-table full-width"><tbody>
    <!--<tr>
      <td>December 2026</td>
      <td><a href="/electricity/data/eia860m/xls/december_generator2026.xlsx" title="EIA 860M December 2026"><span class="ico xls"><span>XLS</span></span></a></td>
    </tr>
    <tr>
      <td>November 2026</td>
      <td><a href="/electricity/data/eia860m/archive/xls/november_generator2026.xlsx" title="EIA 860M November 2026"><span class="ico xls"><span>XLS</span></span></a></td>
    </tr>-->
    <tr>
      <td>July 2026</td>
      <td><a href="/electricity/data/eia860m/archive/xls/july_generator2026.xlsx" title="EIA 860M July 2026"><span class="ico xls"><span>XLS</span></span></a></td>
    </tr>
    <tr>
      <td>August 2026</td>
      <td><a href="/electricity/data/eia860m/xls/august_generator2026.xlsx" title="EIA 860M August 2026"><span class="ico xls"><span>XLS</span></span></a></td>
    </tr>
  </tbody></table>
  <h3>2025</h3>
  <table class="basic-table full-width"><tbody>
    <tr>
      <td>December 2025</td>
      <td><a href="/electricity/data/eia860m/archive/xls/december_generator2025.xlsx" title="EIA 860M December 2025"><span class="ico xls"><span>XLS</span></span></a></td>
    </tr>
  </tbody></table>
  <p><a href="/electricity/data/eia860m/xls/layout_generator.xlsx">Layout</a></p>
</div>
"""  # noqa: E501
AUGUST_URL = "https://www.eia.gov/electricity/data/eia860m/xls/august_generator2026.xlsx"
JULY_ARCHIVE_URL = (
    "https://www.eia.gov/electricity/data/eia860m/archive/xls/july_generator2026.xlsx"
)


def test_parse_index_newest_first_ignoring_commented_out_months(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="data.eia"):
        files = parse_index(INDEX_HTML)
    assert files == [
        (2026, 8, AUGUST_URL),
        (2026, 7, JULY_ARCHIVE_URL),
        (2025, 12, "https://www.eia.gov/electricity/data/eia860m/archive/xls/december_generator2025.xlsx"),
    ]  # fmt: skip
    assert caplog.records == []  # layout_generator.xlsx names no year, so it is not a workbook link
    assert newest_file_url(INDEX_HTML) == AUGUST_URL
    # absolute links and a different base are respected
    absolute = '<a href="https://example.invalid/x/may_generator2030.xlsx">'
    assert newest_file_url(absolute) == "https://example.invalid/x/may_generator2030.xlsx"
    assert newest_file_url('<a href="xls/june_generator2020.xlsx">', "https://h.invalid/a/") == (
        "https://h.invalid/a/xls/june_generator2020.xlsx"
    )


def test_parse_index_warns_about_workbook_links_it_could_not_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A newer month linked with a suffix would otherwise be skipped and an older month returned as newest."""
    odd = [
        '<a href="/x/september_generator2026.xlsx?v=2">',
        '<a href="/x/september_generator2026_revised.xlsx">',
        '<a href="/x/september_generator2026.xlsx ">',
        "<a href=/x/september_generator2026.xlsx>",
        '<a href="/x/smog_generator2026.xlsx">',
    ]
    for link in odd:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="data.eia"):
            assert newest_file_url(INDEX_HTML + link) == AUGUST_URL
        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1 and "was not recognised" in messages[0], link
        assert "september_generator2026" in messages[0] or "smog_generator2026" in messages[0]


def test_parse_index_duplicate_month_prefers_the_current_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    archive = '<a href="/electricity/data/eia860m/archive/xls/august_generator2026.xlsx">'
    current = '<a href="/electricity/data/eia860m/xls/august_generator2026.xlsx">'
    other = '<a href="/electricity/data/eia860m/xls/revised/august_generator2026.xlsx">'
    with caplog.at_level(logging.WARNING, logger="data.eia"):
        assert (
            newest_file_url(archive + current) == AUGUST_URL
        )  # archive listed first: still the xls/ link
        assert newest_file_url(current + archive) == AUGUST_URL
        assert newest_file_url(current + other) == AUGUST_URL  # two current paths: the first wins
        assert newest_file_url(current + current) == AUGUST_URL
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 3 and all("index lists 2026-08 twice" in m for m in messages)
    assert "revised" in messages[2]


def test_newest_file_url_without_links_raises() -> None:
    with pytest.raises(EIAError, match="no <month>_generator<year>.xlsx links"):
        newest_file_url("<html><body>maintenance</body></html>")


def test_period_from_name() -> None:
    assert period_from_name("august_generator2026.xlsx") == "2026-08"
    assert period_from_name(AUGUST_URL) == "2026-08"
    assert period_from_name("C:/x/March_Generator2026.xlsx") == "2026-03"
    assert period_from_name("layout_generator.xlsx") is None
    assert period_from_name("smog_generator2026.xlsx") is None
    assert period_from_name("august_generator2026 (1).xlsx") is None


def test_months_behind() -> None:
    assert months_behind("2026-08", dt.date(2026, 9, 27)) == 1
    assert months_behind("2026-12", dt.date(2027, 1, 1)) == 1  # across a year end
    assert months_behind("2026-08", dt.date(2026, 8, 31)) == 0
    assert months_behind("2026-08", dt.date(2027, 2, 1)) == 6


# --- Client: User-Agent, pacing, cache, manifest ----------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeFetch:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> bytes:
        self.calls.append((url, headers))
        if url not in self.routes:
            raise EIAError(f"unexpected URL {url}", status=404, url=url)
        return self.routes[url]


def make_client(
    tmp_path: Path, routes: dict[str, bytes], **kwargs: Any
) -> tuple[EIA860MClient, FakeFetch, FakeClock]:
    fetch = FakeFetch(routes)
    clock = FakeClock()
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("user_agent", "tests/1.0 tests@example.invalid")
    client = EIA860MClient(tmp_path / "raw", fetch=fetch, sleep=clock.sleep, clock=clock, **kwargs)
    return client, fetch, clock


def test_user_agent_prefers_its_own_variable_then_the_edgar_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIA_USER_AGENT", raising=False)
    monkeypatch.setenv("EDGAR_USER_AGENT", "Ada Lovelace ada@example.com")
    assert user_agent() == "Ada Lovelace ada@example.com"
    monkeypatch.setenv("EIA_USER_AGENT", "eia-only/1.0")
    assert user_agent() == "eia-only/1.0"
    monkeypatch.delenv("EDGAR_USER_AGENT")
    monkeypatch.setenv("EIA_USER_AGENT", "  ")
    assert user_agent() == DEFAULT_USER_AGENT and "ai-economics" in DEFAULT_USER_AGENT
    assert MIN_REQUEST_INTERVAL_S >= 1.0  # two requests a run; there is no reason to hurry


def test_latest_file_url_download_cache_and_manifest(
    tmp_path: Path, workbook: Path, caplog: pytest.LogCaptureFixture
) -> None:
    body = workbook.read_bytes()
    client, fetch, clock = make_client(
        tmp_path, {INDEX_URL: INDEX_HTML.encode("utf-8"), AUGUST_URL: body}
    )
    assert client.latest_file_url() == AUGUST_URL
    path = client.download(AUGUST_URL)
    assert path == tmp_path / "raw" / "2026-09-27" / "august_generator2026.xlsx"
    assert path.read_bytes() == body
    assert [url for url, _ in fetch.calls] == [INDEX_URL, AUGUST_URL]
    assert fetch.calls[0][1] == {"User-Agent": "tests/1.0 tests@example.invalid"}
    assert clock.sleeps and clock.sleeps[0] >= MIN_REQUEST_INTERVAL_S - 1e-9  # paced
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # no .part left behind

    # the same day: cached, no network; a second instance too
    assert client.download(AUGUST_URL) == path and len(fetch.calls) == 2
    again, again_fetch, _ = make_client(tmp_path, {})
    assert again.download(AUGUST_URL) == path and again_fetch.calls == []
    assert again.cached_path(AUGUST_URL) == path

    manifest_path = client.write_manifest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_path == path.parent / "manifest.json"
    assert manifest["source"] == "EIA860M" and manifest["pulled_at"].endswith("Z")
    assert [e["path"] for e in manifest["files"]] == ["august_generator2026.xlsx"]
    entry = manifest["files"][0]
    assert entry["sha256"] == hashlib.sha256(body).hexdigest() and entry["bytes"] == len(body)
    assert entry["source_url"] == AUGUST_URL and entry["fetched_at"].endswith("Z")
    # a later instance that fetched nothing carries the provenance forward
    later = json.loads(again.write_manifest().read_text(encoding="utf-8"))
    assert later["files"][0]["source_url"] == AUGUST_URL
    assert later["files"][0]["fetched_at"] == entry["fetched_at"]
    assert manifest_path.read_bytes().count(b"\r") == 0
    assert raw_provenance(path, entry["sha256"]) == {
        "url": AUGUST_URL,
        "fetched_at": entry["fetched_at"],
    }
    assert raw_provenance(path, "0" * 64) == {"url": None, "fetched_at": None}

    # a new day pulls afresh, into its own directory, and notices the bytes did not change
    tomorrow, tomorrow_fetch, _ = make_client(
        tmp_path, {AUGUST_URL: body}, today=TODAY + dt.timedelta(days=1)
    )
    with caplog.at_level(logging.INFO, logger="data.eia"):
        fresh = tomorrow.download(AUGUST_URL)
    assert fresh == tmp_path / "raw" / "2026-09-28" / "august_generator2026.xlsx"
    assert [url for url, _ in tomorrow_fetch.calls] == [AUGUST_URL]
    assert tomorrow.cached_path(AUGUST_URL) == fresh  # newest dated directory wins
    assert [r.getMessage() for r in caplog.records] == [
        f"august_generator2026.xlsx is byte-identical to {path}"
    ]


def test_latest_file_url_warns_when_the_newest_month_is_stale(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    routes = {INDEX_URL: INDEX_HTML.encode("utf-8")}
    fresh, _, _ = make_client(tmp_path, routes, today=dt.date(2026, 11, 30))  # 3 months: fine
    late, _, _ = make_client(tmp_path, routes, today=dt.date(2026, 12, 1))  # 4 months: warn
    with caplog.at_level(logging.WARNING, logger="data.eia"):
        assert fresh.latest_file_url() == AUGUST_URL
        assert caplog.records == []
        assert late.latest_file_url() == AUGUST_URL
    assert len(caplog.records) == 1
    assert "2026-08, 4 months behind today (2026-12-01)" in caplog.records[0].getMessage()


def test_download_rejects_a_non_workbook_link(tmp_path: Path) -> None:
    client, fetch, _ = make_client(tmp_path, {})
    with pytest.raises(EIAError, match="xlsx"):
        client.download("https://www.eia.gov/electricity/data/eia860m/")
    assert fetch.calls == []


def test_download_refuses_a_body_that_is_not_a_workbook(tmp_path: Path, workbook: Path) -> None:
    """An HTML page served with status 200 is never stored, so the failure is not sticky."""
    html = b"<html><body>Site maintenance</body></html>"
    client, fetch, _ = make_client(tmp_path, {AUGUST_URL: html})
    with pytest.raises(EIAError, match=r"not an \.xlsx workbook \(BadZipFile\); starts b'<html>"):
        client.download(AUGUST_URL)
    dated = tmp_path / "raw" / "2026-09-27"
    assert dated.is_dir() and list(dated.iterdir()) == []
    assert json.loads(client.write_manifest().read_text(encoding="utf-8"))["files"] == []
    # a truncated workbook and a workbook without the core sheets are refused too
    body = workbook.read_bytes()
    short, _, _ = make_client(tmp_path, {AUGUST_URL: body[: len(body) // 2]})
    with pytest.raises(EIAError, match=r"not an \.xlsx workbook"):
        short.download(AUGUST_URL)
    partial = write_workbook(tmp_path / "partial.xlsx", sheets={"Operating": SHEETS["Operating"]})
    incomplete, _, _ = make_client(tmp_path, {AUGUST_URL: partial.read_bytes()})
    with pytest.raises(EIAError, match=r"lacks sheets \['planned', 'retired'\]"):
        incomplete.download(AUGUST_URL)
    assert list(dated.iterdir()) == [dated / "manifest.json"]
    # the same day, a healthy server: fetched and stored, not the cached failure
    healthy, healthy_fetch, _ = make_client(tmp_path, {AUGUST_URL: body})
    assert healthy.download(AUGUST_URL).read_bytes() == body
    assert [url for url, _ in healthy_fetch.calls] == [AUGUST_URL]
    assert workbook_sheet_names(body) == list(SHEETS)
    check_workbook_bytes(body, "test")


def test_cached_copy_is_checked_against_the_manifest(tmp_path: Path, workbook: Path) -> None:
    body = workbook.read_bytes()
    client, _, _ = make_client(tmp_path, {AUGUST_URL: body})
    path = client.download(AUGUST_URL)
    client.write_manifest()
    with open(path, "ab") as fh:
        fh.write(b"tampered")
    with pytest.raises(EIAError, match="does not match today's manifest") as info:
        client.download(AUGUST_URL)
    assert info.value.url == AUGUST_URL
    # a cached file that is not a workbook is refused even before a manifest exists
    other = tmp_path / "other"
    fresh, fresh_fetch, _ = make_client(other, {AUGUST_URL: body})
    fresh.cache_dir().joinpath("august_generator2026.xlsx").write_bytes(b"<html>")
    with pytest.raises(EIAError, match=r"not an \.xlsx workbook"):
        fresh.download(AUGUST_URL)
    assert fresh_fetch.calls == []


def test_default_fetch_retries_then_gives_up() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def urlopen(request, timeout):  # noqa: ANN001 - mimics urllib.request.urlopen
        attempts.append(1)
        raise urllib.error.HTTPError(request.full_url, 503, "down", None, None)

    fetch = eia.build_default_fetch(sleep=sleeps.append, urlopen=urlopen)
    with pytest.raises(EIAError) as info:
        fetch("https://www.eia.gov/x.xlsx", {"User-Agent": "t"})
    assert info.value.status == 503 and len(attempts) == eia.MAX_RETRIES + 1
    assert len(sleeps) == eia.MAX_RETRIES and all(s >= MIN_REQUEST_INTERVAL_S for s in sleeps)

    def not_found(request, timeout):  # noqa: ANN001
        raise urllib.error.HTTPError(request.full_url, 404, "missing", None, None)

    with pytest.raises(EIAError) as info:
        eia.build_default_fetch(sleep=sleeps.append, urlopen=not_found)("https://x.invalid", {})
    assert info.value.status == 404 and len(sleeps) == eia.MAX_RETRIES  # no retry on 404


def test_default_fetch_honours_retry_after_and_retries_network_errors() -> None:
    headers = email.message.Message()
    headers["Retry-After"] = "7"
    sleeps: list[float] = []

    def throttled(request, timeout):  # noqa: ANN001
        raise urllib.error.HTTPError(request.full_url, 429, "slow down", headers, None)

    with pytest.raises(EIAError) as info:
        eia.build_default_fetch(sleep=sleeps.append, urlopen=throttled)("https://x.invalid", {})
    assert info.value.status == 429 and sleeps == [7.0] * eia.MAX_RETRIES

    attempts: list[int] = []
    sleeps.clear()

    def unreachable(request, timeout):  # noqa: ANN001
        attempts.append(1)
        raise urllib.error.URLError("connection reset")

    with pytest.raises(
        EIAError, match="network error for https://x.invalid: connection reset"
    ) as info:
        eia.build_default_fetch(sleep=sleeps.append, urlopen=unreachable)("https://x.invalid", {})
    assert info.value.status is None and len(attempts) == eia.MAX_RETRIES + 1
    assert sleeps == [1.0, 2.0, 4.0]  # backoff doubles from RETRY_BACKOFF_S


# --- CLI ----------------------------------------------------------------------------------------

EXPECTED_FILES = [
    "canceled_or_postponed.csv",
    "operating.csv",
    "planned.csv",
    "retired.csv",
    *(f"summaries/{name}.csv" for name in SUMMARIES),
]


def test_cli_file_end_to_end(tmp_path: Path, workbook: Path, capsys: pytest.CaptureFixture) -> None:
    out = tmp_path / "processed" / "eia860m"
    (out / "summaries").mkdir(parents=True)
    # An earlier run's leftovers: two files it listed (from a sheet and a summary that no longer
    # exist) and one CSV nobody listed, which is not this script's to delete.
    (out / "stale.csv").write_text("old\n", encoding="utf-8")
    (out / "summaries" / "old_summary.csv").write_text("old\n", encoding="utf-8")
    (out / "unrelated.csv").write_text("keep\n", encoding="utf-8")
    (out / "source.json").write_text(
        json.dumps(
            {
                "source": "EIA860M",
                "files": ["stale.csv", "summaries/old_summary.csv", "operating.csv"],
            }
        ),
        encoding="utf-8",
    )
    assert pull_eia860m.main(["--file", str(workbook), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "EIA-860M 2026-03" in printed and "operating 7 rows" in printed
    assert f"{CC} 501" in printed  # top operating technology by nameplate (500.9)
    planned_line = next(line for line in printed.splitlines() if "planned 2026-2028" in line)
    assert planned_line.endswith(
        f"{CC} 1,000; {SOLAR} 480; Batteries 150"
    )  # three of four; 2029 excluded

    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == sorted([*EXPECTED_FILES, "source.json", "unrelated.csv"])
    assert not (out / "stale.csv").exists() and not (out / "summaries" / "old_summary.csv").exists()

    operating = (out / "operating.csv").read_bytes()
    assert b"\r" not in operating and operating.endswith(b"\n")
    assert operating.split(b"\n")[0].decode() == ",".join(TIDY_COLUMNS)
    text = operating.decode("utf-8")
    assert "Alpha Gas,2,TX,ERCO," in text and ",250.5,240.0," in text
    assert ",Beta Sun,S1,CA,CISO,Solar Photovoltaic,SUN,PV,100.0,," in text  # blank stays blank
    assert ",Nu Gas,01,OK,NA," in text
    pd.testing.assert_frame_equal(
        read_processed(out / "operating.csv"), read_sheet(workbook, "Operating")
    )
    planned = pd.read_csv(out / "summaries" / "planned_by_year_and_fuel.csv")
    assert planned.loc[planned["year"] == 2027, "under_construction_share"].tolist() == [0.6]
    states = pd.read_csv(out / "summaries" / "state_summary.csv")
    assert states["state"].tolist() == ["TX", "AZ", "CA", "OK", "IL"]

    source_bytes = (out / "source.json").read_bytes()
    assert b"\r" not in source_bytes and source_bytes.endswith(b"\n")
    source = json.loads(source_bytes)
    assert list(source) == [
        "source",
        "url",
        "fetched_at",
        "file",
        "period",
        "sha256",
        "bytes",
        "skipped_sheets",
        "rows",
        "files",
    ]
    assert source["source"] == "EIA860M" and source["url"] is None and source["fetched_at"] is None
    assert source["file"] == FILE_NAME and source["period"] == "2026-03"
    assert source["sha256"] == hashlib.sha256(workbook.read_bytes()).hexdigest()
    assert source["bytes"] == workbook.stat().st_size and source["skipped_sheets"] == []
    assert source["rows"] == ROW_COUNTS
    assert source["files"] == EXPECTED_FILES

    # a second run over the same workbook rewrites identical bytes, source.json included
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert pull_eia860m.main(["--file", str(workbook), "--out", str(out)]) == 0
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_cli_never_deletes_files_it_did_not_list(tmp_path: Path, workbook: Path) -> None:
    """A shared --out folder without an EIA source.json loses nothing."""
    out = tmp_path / "shared"
    (out / "summaries").mkdir(parents=True)
    (out / "reported.csv").write_text("theirs\n", encoding="utf-8")
    (out / "summaries" / "my_notes.csv").write_text("mine\n", encoding="utf-8")
    (out / "readme.txt").write_text("x\n", encoding="utf-8")
    (out / "source.json").write_text(
        '{"source": "SEC", "files": ["reported.csv"]}\n', encoding="utf-8"
    )
    assert pull_eia860m.main(["--file", str(workbook), "--out", str(out)]) == 0
    for kept in ("reported.csv", "summaries/my_notes.csv", "readme.txt"):
        assert (out / kept).is_file(), kept
    assert json.loads((out / "source.json").read_text(encoding="utf-8"))["source"] == "EIA860M"
    assert (
        previous_outputs(tmp_path / "nowhere") == [] and read_source(tmp_path / "nowhere") is None
    )
    (out / "source.json").write_text("not json", encoding="utf-8")
    assert previous_outputs(out) == [] and read_source(out) is None


def test_cli_file_from_a_dated_raw_folder_carries_its_provenance(
    tmp_path: Path, workbook: Path, capsys: pytest.CaptureFixture
) -> None:
    body = write_workbook(
        tmp_path / "src" / "august_generator2026.xlsx", as_of="August 2026"
    ).read_bytes()
    client, _, _ = make_client(tmp_path, {AUGUST_URL: body})
    cached = client.download(AUGUST_URL)
    client.write_manifest()
    fetched_at = json.loads((cached.parent / "manifest.json").read_text(encoding="utf-8"))["files"][
        0
    ]["fetched_at"]
    out = tmp_path / "out"
    assert pull_eia860m.main(["--file", str(cached), "--out", str(out)]) == 0
    source = json.loads((out / "source.json").read_text(encoding="utf-8"))
    assert source["url"] == AUGUST_URL and source["fetched_at"] == fetched_at
    assert source["period"] == "2026-08" and "pulled_at" not in source
    assert "EIA-860M 2026-08" in capsys.readouterr().out


def test_cli_download_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    body = write_workbook(
        tmp_path / "src" / "august_generator2026.xlsx", as_of="August 2026"
    ).read_bytes()
    client, fetch, _ = make_client(
        tmp_path, {INDEX_URL: INDEX_HTML.encode("utf-8"), AUGUST_URL: body}
    )
    out = tmp_path / "out"
    code = pull_eia860m.run(
        file=None, out_dir=out, raw_dir=tmp_path / "raw", dry_run=False, client=client
    )
    assert code == 0
    assert [url for url, _ in fetch.calls] == [INDEX_URL, AUGUST_URL]
    raw = tmp_path / "raw" / "2026-09-27"
    assert (raw / "august_generator2026.xlsx").read_bytes() == body
    manifest = json.loads((raw / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["source_url"] == AUGUST_URL
    source = json.loads((out / "source.json").read_text(encoding="utf-8"))
    assert (
        source["url"] == AUGUST_URL and source["fetched_at"] == manifest["files"][0]["fetched_at"]
    )
    assert source["period"] == "2026-08" and source["file"] == "august_generator2026.xlsx"
    assert source["sha256"] == manifest["files"][0]["sha256"] and source["rows"] == ROW_COUNTS
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == sorted([*EXPECTED_FILES, "source.json"])
    printed = capsys.readouterr().out
    assert "EIA-860M 2026-08" in printed and "planned 2026-2028" in printed


def test_cli_dry_run_without_a_file_reads_only_the_index(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    client, fetch, _ = make_client(tmp_path, {INDEX_URL: INDEX_HTML.encode("utf-8")})
    out = tmp_path / "out"
    code = pull_eia860m.run(
        file=None, out_dir=out, raw_dir=tmp_path / "raw", dry_run=True, client=client
    )
    assert code == 0
    assert [url for url, _ in fetch.calls] == [INDEX_URL]
    printed = capsys.readouterr().out
    assert f"Dry run: would tidy {AUGUST_URL}" in printed and str(out / "operating.csv") in printed
    assert str(tmp_path / "raw") in printed and "one CSV per extra sheet" in printed
    assert not out.exists() and not (tmp_path / "raw").exists()


def test_cli_dry_run_with_a_file_writes_nothing(
    tmp_path: Path, workbook: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "out"
    assert pull_eia860m.main(["--file", str(workbook), "--out", str(out), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "Dry run" in printed and str(workbook) in printed
    assert str(out / "operating.csv") in printed and str(out / "source.json") in printed
    assert str(out / "summaries" / "state_summary.csv") in printed
    assert not out.exists()


def test_cli_reports_a_bad_workbook_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    header, rows = without_column(OPERATING_HEADER, OPERATING_ROWS, "Plant State")
    path = write_workbook(tmp_path / FILE_NAME, sheets={**SHEETS, "Operating": (header, rows)})
    out = tmp_path / "out"
    assert pull_eia860m.main(["--file", str(path), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "ValueError" in err and "Plant State" in err
    assert pull_eia860m.main(["--file", str(tmp_path / "missing.xlsx"), "--out", str(out)]) == 1
    assert "not a file" in capsys.readouterr().err
    mislabelled = write_workbook(tmp_path / "december_generator2031.xlsx")  # titles say March 2026
    assert pull_eia860m.main(["--file", str(mislabelled), "--out", str(out)]) == 1
    assert "file name says 2031-12 but the sheet titles say 2026-03" in capsys.readouterr().err
    assert not out.exists()  # nothing was written by any of the three


def test_write_processed_lists_every_file(tmp_path: Path, workbook: Path) -> None:
    frames = tidy_all(workbook)
    written = write_processed(frames, summarise(frames), tmp_path, {"source": "EIA860M"})
    assert [p.relative_to(tmp_path).as_posix() for p in written] == [*EXPECTED_FILES, "source.json"]
    assert all(p.is_file() for p in written)
    assert previous_outputs(tmp_path) == EXPECTED_FILES


# --- The processed folder as the refresh and the site see it -------------------------------------


def test_process_workbook_is_what_the_cli_writes(tmp_path: Path, workbook: Path) -> None:
    """One entry point for both callers: the folder it writes is the CLI's, byte for byte."""
    through_cli = tmp_path / "cli"
    assert pull_eia860m.main(["--file", str(workbook), "--out", str(through_cli)]) == 0
    out = tmp_path / "direct"
    processed = process_workbook(workbook, out)
    assert processed.period == "2026-03" and processed.source["file"] == FILE_NAME
    assert [p.relative_to(out).as_posix() for p in processed.written] == [
        *EXPECTED_FILES,
        "source.json",
    ]
    assert sorted(processed.summaries) == sorted(SUMMARIES)
    assert {
        p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()
    } == {
        p.relative_to(through_cli).as_posix(): p.read_bytes()
        for p in through_cli.rglob("*")
        if p.is_file()
    }
    # A workbook that fails validation writes nothing: the folder is left as it was.
    header, rows = without_column(OPERATING_HEADER, OPERATING_ROWS, "Plant State")
    bad = write_workbook(
        tmp_path / "bad" / FILE_NAME, sheets={**SHEETS, "Operating": (header, rows)}
    )
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="Plant State"):
        process_workbook(bad, out)
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_processed_is_current_compares_the_file_name_and_checks_the_files(
    tmp_path: Path, workbook: Path
) -> None:
    url = f"https://www.eia.gov/electricity/data/eia860m/xls/{FILE_NAME}"
    assert workbook_name(url) == FILE_NAME
    assert workbook_name(f"{url}?v=2") == FILE_NAME  # a query string is not part of the name
    out = tmp_path / "out"
    assert not processed_is_current(out, url)  # nothing there yet
    process_workbook(workbook, out)
    assert processed_is_current(out, url)
    assert not processed_is_current(out, AUGUST_URL)  # a newer month is not this one
    assert not processed_is_current(out, url.replace("march_", "March_"))  # names are exact
    # A folder someone half-emptied is not current, whatever source.json says.
    (out / "summaries" / "state_summary.csv").unlink()
    assert not processed_is_current(out, url)
    # Nor is a source.json without a file list, or another source's record.
    (out / "source.json").write_text(
        json.dumps({"source": "EIA860M", "file": FILE_NAME}), encoding="utf-8"
    )
    assert not processed_is_current(out, url)
    (out / "source.json").write_text(
        json.dumps({"source": "SEC", "file": FILE_NAME, "files": ["operating.csv"]}),
        encoding="utf-8",
    )
    assert not processed_is_current(out, url) and read_source(out) is None


def test_read_summaries_round_trips_what_write_processed_wrote(
    tmp_path: Path, workbook: Path
) -> None:
    out = tmp_path / "out"
    processed = process_workbook(workbook, out)
    summaries = read_summaries(out)
    assert summaries.period == "2026-03" and summaries.file == FILE_NAME
    assert tuple(summaries.planned.columns) == PLANNED_SUMMARY_COLUMNS
    assert tuple(summaries.capacity.columns) == CAPACITY_SUMMARY_COLUMNS
    pd.testing.assert_frame_equal(summaries.capacity, processed.summaries["capacity_by_fuel"])
    # The planned summary is written with an Int64 year; the reader gives it back the same way.
    pd.testing.assert_frame_equal(
        summaries.planned, processed.summaries["planned_by_year_and_fuel"]
    )
    assert summaries.planned["year"].dtype == "Int64"
    # A blank year and a blank share (a year with nothing planned in MW) survive the round trip.
    planned = tidy_all(workbook)["planned"]
    planned.loc[planned["plant_name"] == "Xi Breeze", "year"] = pd.NA
    planned.loc[planned["plant_name"] == "Eta Sun", "nameplate_mw"] = 0.0
    frames = {**tidy_all(workbook), "planned": planned}
    write_processed(frames, summarise(frames), out, processed.source)
    again = read_summaries(out).planned
    assert again["year"].isna().sum() == 1 and again["under_construction_share"].isna().sum() == 1
    assert again.loc[again["technology"] == WIND, "year"].isna().all()


@pytest.mark.parametrize(
    ("relative", "text", "message"),
    [
        ("source.json", "not json", "source.json: not a readable EIA860M record"),
        ("source.json", '{"source": "SEC", "period": "2026-03"}', "not a readable EIA860M record"),
        (
            "source.json",
            '{"source": "EIA860M", "period": "March 2026"}',
            "period 'March 2026' is not YYYY-MM",
        ),
        (
            "source.json",
            '{"source": "EIA860M", "period": "2026-13"}',
            "period '2026-13' is not YYYY-MM",
        ),
        ("source.json", '{"source": "EIA860M"}', "period None is not YYYY-MM"),
        (
            "summaries/capacity_by_fuel.csv",
            "technology,units,nameplate_mw\nSolar Photovoltaic,1,100.0\n",
            "summaries/capacity_by_fuel.csv: columns ['technology', 'units', 'nameplate_mw'] are not",
        ),
        (
            "summaries/capacity_by_fuel.csv",
            "technology,units,nameplate_mw,net_summer_mw\nSolar Photovoltaic,1,lots,100.0\n",
            "summaries/capacity_by_fuel.csv: 'nameplate_mw' is not numeric",
        ),
        (
            "summaries/capacity_by_fuel.csv",
            "technology,units,nameplate_mw,net_summer_mw\nSolar Photovoltaic,1,,100.0\n",
            "summaries/capacity_by_fuel.csv: 'nameplate_mw' is blank on rows [0]",
        ),
        (
            "summaries/capacity_by_fuel.csv",
            "technology,units,nameplate_mw,net_summer_mw\nSolar Photovoltaic,1,-5,100.0\n",
            "summaries/capacity_by_fuel.csv: 'nameplate_mw' is negative on rows [0]",
        ),
        (
            "summaries/planned_by_year_and_fuel.csv",
            "year,technology,units,nameplate_mw,net_summer_mw,under_construction_mw,under_construction_share\n"
            "2027.5,Batteries,1,100.0,100.0,0.0,0.0\n",
            "summaries/planned_by_year_and_fuel.csv: 'year' is not a whole number on rows [0]",
        ),
        ("summaries/planned_by_year_and_fuel.csv", "", "summaries/planned_by_year_and_fuel.csv: "),
    ],
)
def test_read_summaries_names_a_malformed_file(
    tmp_path: Path, workbook: Path, relative: str, text: str, message: str
) -> None:
    out = tmp_path / "out"
    process_workbook(workbook, out)
    (out / relative).write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(message)):
        read_summaries(out)


def test_read_summaries_needs_the_folder_and_all_three_files(
    tmp_path: Path, workbook: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        read_summaries(tmp_path / "nowhere")
    out = tmp_path / "out"
    process_workbook(workbook, out)
    for relative in (
        "source.json",
        "summaries/planned_by_year_and_fuel.csv",
        "summaries/capacity_by_fuel.csv",
    ):
        path = out / relative
        kept = path.read_bytes()
        path.unlink()
        with pytest.raises(FileNotFoundError, match=re.escape(str(path))):
            read_summaries(out)
        path.write_bytes(kept)
    assert read_summaries(out).period == "2026-03"


# --- Technology groups ---------------------------------------------------------------------------

# Every technology name in the August 2026 file, with the group the power stage reads it as.
REAL_TECHNOLOGY_GROUPS = {
    "Natural Gas Fired Combined Cycle": "Gas",
    "Natural Gas Fired Combustion Turbine": "Gas",
    "Natural Gas Steam Turbine": "Gas",
    "Natural Gas Internal Combustion Engine": "Gas",
    "Natural Gas with Compressed Air Storage": "Gas",
    "Other Natural Gas": "Gas",
    "Conventional Steam Coal": "Coal",
    "Coal Integrated Gasification Combined Cycle": "Coal",
    "Nuclear": "Nuclear",
    "Solar Photovoltaic": "Solar",
    "Solar Thermal with Energy Storage": "Solar",
    "Solar Thermal without Energy Storage": "Solar",
    "Batteries": "Batteries",
    "Onshore Wind Turbine": "Wind",
    "Offshore Wind Turbine": "Wind",
    "Conventional Hydroelectric": "Hydro",
    "Hydroelectric Pumped Storage": "Hydro",
    "Petroleum Liquids": "Other",
    "Petroleum Coke": "Other",
    "Wood/Wood Waste Biomass": "Other",
    "Other Waste Biomass": "Other",
    "Landfill Gas": "Other",  # biogenic, not natural gas
    "Other Gases": "Other",  # blast-furnace and other process gases
    "Municipal Solid Waste": "Other",
    "Geothermal": "Other",
    "All Other": "Other",
    "Flywheels": "Other",
    UNREPORTED_TECHNOLOGY: "Other",
}


def test_group_technology_covers_every_real_name() -> None:
    assert TECHNOLOGY_GROUPS == (
        "Gas",
        "Coal",
        "Nuclear",
        "Solar",
        "Batteries",
        "Wind",
        "Hydro",
        "Other",
    )
    for name, group in REAL_TECHNOLOGY_GROUPS.items():
        assert group_technology(name) == group, name
    assert set(REAL_TECHNOLOGY_GROUPS.values()) == set(TECHNOLOGY_GROUPS)
    # Case and whitespace are forgiven; a blank, a missing or an unknown name is Other.
    assert group_technology("  onshore wind turbine ") == "Wind"
    assert group_technology("NATURAL GAS FIRED COMBINED CYCLE") == "Gas"
    for unknown in ("", " ", None, float("nan"), "Fusion", "Tidal"):
        assert group_technology(unknown) == "Other", unknown


def test_committed_summaries_use_only_known_technology_names() -> None:
    # A committed-artefact guard, like the register and stack checks: the summaries in the
    # repository carry no technology name outside the list above, so the site never regroups a
    # name blind. It is the one test that reads data/processed/eia860m, on purpose.
    processed = Path(__file__).resolve().parents[1] / "data" / "processed" / "eia860m" / "summaries"
    if not processed.is_dir():
        pytest.skip("no EIA-860M summaries committed")
    names: set[str] = set()
    for path in processed.glob("*_fuel.csv"):
        names |= set(pd.read_csv(path, keep_default_na=False)["technology"])
    assert names <= set(REAL_TECHNOLOGY_GROUPS), names - set(REAL_TECHNOLOGY_GROUPS)

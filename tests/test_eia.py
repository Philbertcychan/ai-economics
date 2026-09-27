"""Offline tests for ``data/eia.py`` and ``scripts/pull_eia860m.py``.

No network and no real data: ``write_workbook`` builds a small EIA-860M look-alike with openpyxl
(title block, header row at a non-zero offset, a blank-then-NOTES footnote, blank cells written
as single spaces the way EIA writes them, a blank nameplate, planned status strings) and every
expected number below is worked out by hand from those rows. The index page is a trimmed copy
of the real page's markup with made-up months. Companies and plants are invented.
"""
# ruff: noqa: E501  (fixture rows are one generator per line, wider than 100 columns on purpose)

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from openpyxl import Workbook

from data import eia
from data.eia import (
    CORE_SHEETS,
    DEFAULT_USER_AGENT,
    INDEX_URL,
    MIN_REQUEST_INTERVAL_S,
    TIDY_COLUMNS,
    UNDER_CONSTRUCTION_CODES,
    EIA860MClient,
    EIAError,
    capacity_by_fuel,
    find_header_row,
    newest_file_url,
    parse_index,
    period_from_name,
    planned_by_year_and_fuel,
    read_sheet,
    retired_by_year_and_fuel,
    sheet_key,
    state_summary,
    status_code,
    tidy_all,
    user_agent,
)
from scripts import pull_eia860m
from scripts.pull_eia860m import SUMMARIES, write_processed

TODAY = dt.date(2026, 9, 27)
FILE_NAME = "march_generator2026.xlsx"

# --- Fixture workbook ------------------------------------------------------------------------

# Column layout per sheet, as EIA lays it out (a map column the tidy frame ignores, no Status on
# the retired sheet, no dates or status on the canceled sheet).
OPERATING_HEADER = [
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
    "Operating Month",
    "Operating Year",
    "Status",
]
PLANNED_HEADER = [c for c in OPERATING_HEADER if c not in ("Operating Month", "Operating Year")]
PLANNED_HEADER[13:13] = ["Planned Operation Month", "Planned Operation Year"]
RETIRED_HEADER = [c for c in OPERATING_HEADER if c != "Status"] + [
    "Retirement Month",
    "Retirement Year",
]
CANCELED_HEADER = OPERATING_HEADER[:13]

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

# Rows are deliberately out of plant order so the sort is exercised. " " is EIA's blank.
OPERATING_ROWS: list[list[Any]] = [
    [3, "Gamma Wind", 30, "Gamma Breeze", "Map", "TX", "ERCO", "W1", 200, 200, WIND, "WND", "WT", 12, 2020, SB],
    [1, "Alpha Power", 10, "Alpha Gas", "Map", "TX", "ERCO", "1", 500, 480, CC, "NG", "CA", 6, 2015, OP],
    [1, "Alpha Power", 10, "Alpha Gas", "Map", "TX", "ERCO", "2", 250.5, 240, CT, "NG", "CT", 6, 2015, OP],
    [2, "Beta Solar", 20, "Beta Sun", "Map", "CA", "CISO", "S1", 100, " ", SOLAR, "SUN", "PV", 3, 2022, OP],
    [4, "Delta Nuclear", 40, "Delta Atom", "Map", "IL", "PJM", "1", " ", 1000, "Nuclear", "NUC", "ST", 1, 1985, OP],
]  # fmt: skip
PLANNED_ROWS: list[list[Any]] = [
    [5, "Epsilon Dev", 50, "Epsilon CC", "Map", "TX", "ERCO", "1", 600, 570, CC, "NG", "CA", 6, 2027, V],
    [5, "Epsilon Dev", 50, "Epsilon CC", "Map", "TX", "ERCO", "2", 400, 380, CC, "NG", "CA", 12, 2027, P],
    [6, "Zeta Storage", 60, "Zeta Batt", "Map", "CA", "CISO", "B1", 150, 150, "Batteries", "MWH", "BA", 3, 2026, TS],
    [7, "Eta Solar", 70, "Eta Sun", "Map", "TX", "ERCO", "S1", 300, 300, SOLAR, "SUN", "PV", 9, 2026, U],
    [8, "Theta Solar", 80, "Theta Sun", "Map", "AZ", "AZPS", "S1", 120, 120, SOLAR, "SUN", "PV", 1, 2028, L],
]  # fmt: skip
# The third row has a blank entity and plant ID, the way EIA lists 1960s reactors: a data row,
# not a footnote, and it must survive.
RETIRED_ROWS: list[list[Any]] = [
    [9, "Iota Coal", 90, "Iota Steam", "Map", "IL", "PJM", "1", 800, 760, COAL, "BIT", "ST", 1, 1970, 5, 2024],
    [9, "Iota Coal", 90, "Iota Steam", "Map", "IL", "PJM", "2", 800, 750, COAL, "BIT", "ST", 1, 1972, 5, 2024],
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


def write_workbook(
    path: Path,
    *,
    header_row: int = 3,
    sheets: dict[str, tuple[list[str], list[list[Any]]]] | None = None,
    footnotes: bool = True,
) -> Path:
    """Write the fixture workbook: title, blank rows, header at ``header_row`` (1-based), data,
    then a blank line and the NOTES block, like the real file."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, (header, rows) in (sheets or SHEETS).items():
        sheet = workbook.create_sheet(name)
        sheet.append([f"Inventory of {name} Generators as of March 2026"])
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


def with_cell(rows: list[list[Any]], row: int, header: list[str], column: str, value: Any) -> list:
    edited = [list(r) for r in rows]
    edited[row][header.index(column)] = value
    return edited


# --- Header detection and tidy columns -------------------------------------------------------


def test_find_header_row_by_entity_id_cell() -> None:
    rows = [["Inventory of ..."], ["", ""], ["entity id ", "Entity Name"], [1, "A"]]
    assert find_header_row(rows) == 2  # case and trailing space do not matter
    assert find_header_row([["Title"], [None, "Entity ID"]]) == -1  # must be the first cell
    assert find_header_row([]) == -1


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
    ]


def test_read_sheet_columns_dtypes_and_blanks(workbook: Path) -> None:
    frame = read_sheet(workbook, "Operating")
    assert list(frame.columns) == list(TIDY_COLUMNS)
    for column in ("entity_id", "plant_id", "year", "month"):
        assert str(frame[column].dtype) == "Int64", column
    assert frame["nameplate_mw"].dtype == "float64" and frame["net_summer_mw"].dtype == "float64"
    assert frame["generator_id"].tolist() == ["1", "2", "S1", "W1", "1"]  # text, never numbers
    # sorted by plant then unit, not in EIA's order
    assert frame["plant_id"].tolist() == [10, 10, 20, 30, 40]
    alpha = frame[frame["generator_id"] == "2"].iloc[0]
    assert alpha["entity_name"] == "Alpha Power" and alpha["state"] == "TX"
    assert alpha["balancing_authority"] == "ERCO" and alpha["technology"] == CT
    assert alpha["energy_source"] == "NG" and alpha["prime_mover"] == "CT"
    assert alpha["nameplate_mw"] == 250.5 and alpha["net_summer_mw"] == 240.0
    assert alpha["status"] == OP and alpha["status_code"] == "OP"
    assert alpha["year"] == 2015 and alpha["month"] == 6
    # EIA's single-space blanks become NaN for numbers and "" for text
    beta = frame[frame["plant_name"] == "Beta Sun"].iloc[0]
    assert math.isnan(beta["net_summer_mw"]) and beta["nameplate_mw"] == 100.0
    delta = frame[frame["plant_name"] == "Delta Atom"].iloc[0]
    assert math.isnan(delta["nameplate_mw"]) and delta["net_summer_mw"] == 1000.0
    assert frame[frame["plant_name"] == "Gamma Breeze"]["status_code"].iloc[0] == "SB"
    assert not frame["status"].isna().any() and not frame["technology"].isna().any()


def test_planned_sheet_dates_and_status_codes(workbook: Path) -> None:
    frame = read_sheet(workbook, "Planned")
    assert frame["year"].tolist() == [2027, 2027, 2026, 2026, 2028]  # Planned Operation Year
    assert frame["month"].tolist() == [6, 12, 3, 9, 1]
    assert frame["status_code"].tolist() == ["V", "P", "TS", "U", "L"]
    assert frame["status"].iloc[0] == V


def test_retired_sheet_uses_the_retirement_date_and_keeps_blank_ids(workbook: Path) -> None:
    frame = read_sheet(workbook, "Retired")
    assert len(frame) == len(RETIRED_ROWS)
    kappa = frame[frame["plant_name"] == "Kappa Atom"].iloc[0]
    assert pd.isna(kappa["entity_id"]) and pd.isna(kappa["plant_id"])
    assert kappa["balancing_authority"] == "" and kappa["nameplate_mw"] == 300.0
    assert kappa["year"] == 1989 and kappa["month"] == 8  # retirement, not first operation
    assert frame["year"].tolist() == [2024, 2024, 2025, 1989]  # blank plant id sorts last
    assert (frame["status"] == "").all() and (frame["status_code"] == "").all()  # no column


def test_sheet_without_dates_or_status(workbook: Path) -> None:
    frame = read_sheet(workbook, "Canceled or Postponed")
    assert len(frame) == 1
    assert frame["year"].isna().all() and frame["month"].isna().all()
    assert frame["status"].tolist() == [""] and frame["generator_id"].tolist() == ["1"]


def test_tidy_all_keys_every_sheet(workbook: Path) -> None:
    frames = tidy_all(workbook)
    assert set(frames) == {"operating", "planned", "retired", "canceled_or_postponed"}
    assert set(CORE_SHEETS) <= set(frames)
    assert {k: len(v) for k, v in frames.items()} == {
        "operating": 5,
        "planned": 5,
        "retired": 4,
        "canceled_or_postponed": 1,
    }
    pd.testing.assert_frame_equal(frames["planned"], read_sheet(workbook, "Planned"))
    assert sheet_key("Operating_PR") == "operating_pr"
    assert sheet_key("Canceled or Postponed") == "canceled_or_postponed"


def test_status_code() -> None:
    assert status_code(V) == "V" and status_code(TS) == "TS" and status_code(OP) == "OP"
    assert status_code("(ot) Other") == "OT"
    assert status_code("Operating") == "" and status_code(None) == "" and status_code(" ") == ""
    assert UNDER_CONSTRUCTION_CODES == {"V", "U", "TS"}


# --- Validation ------------------------------------------------------------------------------


def test_missing_required_column_names_sheet_and_column(tmp_path: Path) -> None:
    header, rows = without_column(OPERATING_HEADER, OPERATING_ROWS, "Technology")
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Operating": (header, rows)})
    with pytest.raises(ValueError, match=r"sheet 'Operating'.*Technology"):
        read_sheet(path, "Operating")


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


def test_blank_year_is_allowed(tmp_path: Path) -> None:
    rows = with_cell(PLANNED_ROWS, 0, PLANNED_HEADER, "Planned Operation Year", " ")
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Planned": (PLANNED_HEADER, rows)})
    frame = read_sheet(path, "Planned")
    assert pd.isna(frame.loc[frame["generator_id"] == "1", "year"].iloc[0])


def test_missing_header_row_and_unknown_sheet(tmp_path: Path) -> None:
    header = ["Entity", *OPERATING_HEADER[1:]]  # first cell is not "Entity ID"
    path = write_workbook(tmp_path / FILE_NAME, sheets={"Operating": (header, OPERATING_ROWS)})
    with pytest.raises(ValueError, match=r"sheet 'Operating'.*Entity ID"):
        read_sheet(path, "Operating")
    with pytest.raises(ValueError, match=r"sheet 'Planned'.*not in"):
        read_sheet(path, "Planned")


def test_tidy_all_requires_the_core_sheets(tmp_path: Path) -> None:
    sheets = {k: v for k, v in SHEETS.items() if k != "Retired"}
    path = write_workbook(tmp_path / FILE_NAME, sheets=sheets)
    with pytest.raises(ValueError, match=r"missing sheets \['retired'\]"):
        tidy_all(path)


def test_workbook_without_footnotes_parses_the_same(tmp_path: Path) -> None:
    with_notes = tidy_all(write_workbook(tmp_path / "a" / FILE_NAME))
    without = tidy_all(write_workbook(tmp_path / "b" / FILE_NAME, footnotes=False))
    for key in with_notes:
        pd.testing.assert_frame_equal(with_notes[key], without[key])


# --- Summaries (expected numbers worked out by hand from the fixture rows) --------------------


def test_capacity_by_fuel(workbook: Path) -> None:
    table = capacity_by_fuel(read_sheet(workbook, "Operating"))
    assert list(table.columns) == ["technology", "units", "nameplate_mw", "net_summer_mw"]
    # largest nameplate first; the nuclear unit's blank nameplate counts as nothing
    assert table["technology"].tolist() == [CC, CT, WIND, SOLAR, "Nuclear"]
    assert table["units"].tolist() == [1, 1, 1, 1, 1]
    assert table["nameplate_mw"].tolist() == [500.0, 250.5, 200.0, 100.0, 0.0]
    assert table["net_summer_mw"].tolist() == [480.0, 240.0, 200.0, 0.0, 1000.0]


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
        # 2026: solar 300 (U) and the battery 150 (TS) are both under construction
        (2026, SOLAR, 1, 300.0, 300.0, 300.0, 1.0),
        (2026, "Batteries", 1, 150.0, 150.0, 150.0, 1.0),
        # 2027: two combined-cycle units, 600 (V) + 400 (P); only the V unit is being built
        (2027, CC, 2, 1000.0, 950.0, 600.0, 0.6),
        # 2028: approvals pending (L) is not construction
        (2028, SOLAR, 1, 120.0, 120.0, 0.0, 0.0),
    ]
    assert str(table["year"].dtype) == "Int64"


def test_planned_share_is_nan_when_nothing_is_planned_in_mw() -> None:
    planned = pd.DataFrame(
        {
            "year": pd.array([2027, 2027], dtype="Int64"),
            "technology": ["X", "X"],
            "nameplate_mw": [float("nan"), float("nan")],
            "net_summer_mw": [1.0, 2.0],
            "status_code": ["V", "P"],
        }
    )
    table = planned_by_year_and_fuel(planned)
    assert table["nameplate_mw"].tolist() == [0.0]
    assert math.isnan(table["under_construction_share"].iloc[0])


def test_retired_by_year_and_fuel(workbook: Path) -> None:
    table = retired_by_year_and_fuel(read_sheet(workbook, "Retired"))
    assert list(table.columns) == ["year", "technology", "units", "nameplate_mw", "net_summer_mw"]
    rows = [tuple(r) for r in table.itertuples(index=False)]
    assert rows == [
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
        # TX operating 500 + 250.5 + 200; planned 600 + 400 + 300, of which V 600 and U 300
        ("TX", 3, 950.5, 3, 1300.0, 900.0),
        ("CA", 1, 100.0, 1, 150.0, 150.0),  # the TS battery counts as under construction
        ("AZ", 0, 0.0, 1, 120.0, 0.0),  # planned only
        ("IL", 1, 0.0, 0, 0.0, 0.0),  # operating only, and its nameplate is blank
    ]


def test_summaries_name_missing_columns() -> None:
    with pytest.raises(ValueError, match="capacity_by_fuel.*nameplate_mw"):
        capacity_by_fuel(pd.DataFrame({"technology": ["X"]}))
    with pytest.raises(ValueError, match="planned.*status_code"):
        planned_by_year_and_fuel(pd.DataFrame({"year": [1], "technology": ["X"]}))


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
  <p><a href="/electricity/data/eia860m/xls/layout_generator.xlsx">Layout</a>
     <a href="/electricity/data/eia860m/xls/smog_generator2026.xlsx">not a month</a></p>
</div>
"""  # noqa: E501
AUGUST_URL = "https://www.eia.gov/electricity/data/eia860m/xls/august_generator2026.xlsx"


def test_parse_index_newest_first_ignoring_commented_out_months() -> None:
    files = parse_index(INDEX_HTML)
    assert files == [
        (2026, 8, AUGUST_URL),
        (2026, 7, "https://www.eia.gov/electricity/data/eia860m/archive/xls/july_generator2026.xlsx"),
        (2025, 12, "https://www.eia.gov/electricity/data/eia860m/archive/xls/december_generator2025.xlsx"),
    ]  # fmt: skip
    assert newest_file_url(INDEX_HTML) == AUGUST_URL
    # absolute links and a different base are respected
    absolute = '<a href="https://example.invalid/x/may_generator2030.xlsx">'
    assert newest_file_url(absolute) == "https://example.invalid/x/may_generator2030.xlsx"
    assert newest_file_url('<a href="xls/june_generator2020.xlsx">', "https://h.invalid/a/") == (
        "https://h.invalid/a/xls/june_generator2020.xlsx"
    )


def test_newest_file_url_without_links_raises() -> None:
    with pytest.raises(EIAError, match="no <month>_generator<year>.xlsx links"):
        newest_file_url("<html><body>maintenance</body></html>")


def test_period_from_name() -> None:
    assert period_from_name("august_generator2026.xlsx") == "2026-08"
    assert period_from_name(AUGUST_URL) == "2026-08"
    assert period_from_name("C:/x/March_Generator2026.xlsx") == "2026-03"
    assert period_from_name("layout_generator.xlsx") is None
    assert period_from_name("smog_generator2026.xlsx") is None


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


def test_user_agent_reuses_the_edgar_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGAR_USER_AGENT", "Ada Lovelace ada@example.com")
    assert user_agent() == "Ada Lovelace ada@example.com"
    monkeypatch.delenv("EDGAR_USER_AGENT")
    assert user_agent() == DEFAULT_USER_AGENT and "ai-economics" in DEFAULT_USER_AGENT
    assert MIN_REQUEST_INTERVAL_S >= 1.0  # two requests a run; there is no reason to hurry


def test_latest_file_url_download_cache_and_manifest(tmp_path: Path, workbook: Path) -> None:
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

    # a new day pulls afresh, into its own directory, and notices the bytes did not change
    tomorrow, tomorrow_fetch, _ = make_client(
        tmp_path, {AUGUST_URL: body}, today=TODAY + dt.timedelta(days=1)
    )
    fresh = tomorrow.download(AUGUST_URL)
    assert fresh == tmp_path / "raw" / "2026-09-28" / "august_generator2026.xlsx"
    assert [url for url, _ in tomorrow_fetch.calls] == [AUGUST_URL]
    assert tomorrow.cached_path(AUGUST_URL) == fresh  # newest dated directory wins


def test_download_rejects_a_non_workbook_link(tmp_path: Path) -> None:
    client, fetch, _ = make_client(tmp_path, {})
    with pytest.raises(EIAError, match="xlsx"):
        client.download("https://www.eia.gov/electricity/data/eia860m/")
    assert fetch.calls == []


def test_default_fetch_retries_then_gives_up() -> None:
    import urllib.error

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


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_file_end_to_end(tmp_path: Path, workbook: Path, capsys: pytest.CaptureFixture) -> None:
    out = tmp_path / "processed" / "eia860m"
    (out / "summaries").mkdir(parents=True)
    (out / "stale.csv").write_text("old\n", encoding="utf-8")  # from a sheet that no longer exists
    assert pull_eia860m.main(["--file", str(workbook), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "EIA-860M 2026-03" in printed and "operating 5 rows" in printed
    assert f"{CC} 500" in printed  # top operating technology by nameplate
    assert "planned 2026-2028" in printed and f"{CC} 1,000" in printed

    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == [
        "canceled_or_postponed.csv",
        "operating.csv",
        "planned.csv",
        "retired.csv",
        "source.json",
        *(f"summaries/{name}.csv" for name in sorted(SUMMARIES)),
    ]
    assert not (out / "stale.csv").exists()

    operating = (out / "operating.csv").read_bytes()
    assert b"\r" not in operating and operating.endswith(b"\n")
    assert operating.split(b"\n")[0].decode() == ",".join(TIDY_COLUMNS)
    text = operating.decode("utf-8")
    assert "Alpha Gas,2,TX,ERCO," in text and ",250.5,240.0," in text
    assert ",Beta Sun,S1,CA,CISO,Solar Photovoltaic,SUN,PV,100.0,," in text  # blank stays blank
    planned = pd.read_csv(out / "summaries" / "planned_by_year_and_fuel.csv")
    assert planned.loc[planned["year"] == 2027, "under_construction_share"].tolist() == [0.6]
    states = pd.read_csv(out / "summaries" / "state_summary.csv")
    assert states["state"].tolist() == ["TX", "CA", "AZ", "IL"]

    source = json.loads((out / "source.json").read_text(encoding="utf-8"))
    assert source["source"] == "EIA860M" and source["url"] is None
    assert source["file"] == FILE_NAME and source["period"] == "2026-03"
    assert source["sha256"] == hashlib.sha256(workbook.read_bytes()).hexdigest()
    assert source["bytes"] == workbook.stat().st_size and source["pulled_at"].endswith("Z")
    assert source["rows"] == {
        "canceled_or_postponed": 1,
        "operating": 5,
        "planned": 5,
        "retired": 4,
    }

    # a second run over the same workbook rewrites identical CSV bytes
    before = {p: p.read_bytes() for p in out.rglob("*.csv")}
    assert pull_eia860m.main(["--file", str(workbook), "--out", str(out)]) == 0
    assert {p: p.read_bytes() for p in out.rglob("*.csv")} == before


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
    assert pull_eia860m.main(["--file", str(path), "--out", str(tmp_path / "out")]) == 1
    err = capsys.readouterr().err
    assert "ValueError" in err and "Plant State" in err
    assert pull_eia860m.main(["--file", str(tmp_path / "missing.xlsx")]) == 1
    assert "not a file" in capsys.readouterr().err


def test_write_processed_lists_every_file(tmp_path: Path, workbook: Path) -> None:
    frames = tidy_all(workbook)
    written = write_processed(frames, tmp_path, {"source": "EIA860M"})
    assert [p.name for p in written] == [
        "canceled_or_postponed.csv",
        "operating.csv",
        "planned.csv",
        "retired.csv",
        *(f"{name}.csv" for name in SUMMARIES),
        "source.json",
    ]
    assert all(p.is_file() for p in written)

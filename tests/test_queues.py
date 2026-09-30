"""Tests for ``data/queues.py`` and ``scripts/pull_queues.py``.

A twelve-row fixture workbook stands in for LBNL's file: the same title row, header names and
column order, with a hybrid, blanks, every status, a request outside the completion window and
two projects that came online in the same year. Every summary is checked against numbers worked
out by hand below, never against the module's own arithmetic. No test touches the network; the
one that reads ``data/processed/queues`` is the committed-artefact guard and says so.
"""

# ruff: noqa: E501  (fixture rows are one request per line, wider than 100 columns on purpose)

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest
from openpyxl import Workbook

from data import QUEUES_PROCESSED_DIR
from data.queues import (
    DATA_SHEET,
    SOURCE_COLUMNS,
    active_by_proposed_year_and_type,
    active_by_region_and_type,
    active_by_state_and_type,
    completion_rates,
    group_type,
    ia_executed_not_operational,
    median_months_to_operation,
    process_workbook,
    proposed_year_bin,
    read_requests,
    read_summaries,
    through_from_name,
)
from scripts.pull_queues import main as pull_main

HEADER = list(SOURCE_COLUMNS)


def d(text: str | None) -> dt.datetime | None:
    return None if text is None else dt.datetime.fromisoformat(text)


# q_id, q_status, q_date, prop_date, on_date, wd_date, ia_date, IA_phase_raw, IA_phase_clean,
# county, state, fips_code, poi_name, region, project_name, utility, entity, developer, cluster,
# service, project_type, type_1, type_2, type_3, type_clean, mw_1, mw_2, mw_3, q_year, prop_year
ROWS: list[list[object]] = [  # noqa: E501
    [
        "A1",
        "active",
        d("2022-03-01"),
        d("2027-06-01"),
        None,
        None,
        None,
        "SIS",
        "System Impact Study",
        "Kern",
        "CA",
        "6029",
        "Sub A",
        "CAISO",
        None,
        "SCE",
        "CAISO",
        None,
        None,
        "NRIS",
        "Generation",
        "Solar",
        "Battery",
        None,
        "Solar+Battery",
        100,
        50,
        None,
        2022,
        2027,
    ],
    [
        "A2",
        "active",
        d("2023-01-15"),
        d("2026-01-01"),
        None,
        None,
        d("2025-05-01"),
        "Executed",
        "IA Executed",
        "Kern",
        "CA",
        "6029",
        "Sub A",
        "CAISO",
        None,
        "SCE",
        "CAISO",
        None,
        None,
        "NRIS",
        "Generation",
        "Gas",
        None,
        None,
        "Gas",
        400,
        None,
        None,
        2023,
        2026,
    ],
    [
        "E1",
        "active",
        d("2024-07-01"),
        d("2028-12-01"),
        None,
        None,
        None,
        "Screening",
        "Feasibility Study",
        "Ector",
        "TX",
        "48135",
        "Sub E",
        "ERCOT",
        None,
        "Oncor",
        "ERCOT",
        None,
        None,
        "ERIS",
        "Generation",
        "Gas",
        None,
        None,
        "Gas",
        1200,
        None,
        None,
        2024,
        2028,
    ],
    [
        "E2",
        "active",
        d("2024-08-01"),
        None,
        None,
        None,
        d("2025-09-01"),
        "Executed",
        "IA Executed",
        "Ector",
        "TX",
        "48135",
        "Sub E",
        "ERCOT",
        None,
        "Oncor",
        "ERCOT",
        None,
        None,
        "ERIS",
        "Generation",
        "Battery",
        None,
        None,
        "Battery",
        300,
        None,
        None,
        2024,
        None,
    ],
    [
        "E3",
        "suspended",
        d("2021-02-01"),
        d("2025-01-01"),
        None,
        None,
        d("2023-01-01"),
        "Executed",
        "IA Executed",
        "Ector",
        "TX",
        "48135",
        "Sub E",
        "ERCOT",
        None,
        "Oncor",
        "ERCOT",
        None,
        None,
        "ERIS",
        "Generation",
        "Wind",
        None,
        None,
        "Wind",
        250,
        None,
        None,
        2021,
        2025,
    ],
    [
        "P1",
        "operational",
        d("2018-01-01"),
        d("2021-01-01"),
        d("2023-01-01"),
        None,
        d("2020-06-01"),
        "In Service",
        "IA Executed",
        "Loudoun",
        "VA",
        "51107",
        "Sub P",
        "PJM",
        None,
        "Dominion",
        "PJM",
        None,
        None,
        "NRIS",
        "Generation",
        "Solar",
        None,
        None,
        "Solar",
        80,
        None,
        None,
        2018,
        2021,
    ],
    [
        "P2",
        "operational",
        d("2019-01-01"),
        d("2022-01-01"),
        d("2025-01-01"),
        None,
        d("2021-06-01"),
        "In Service",
        "IA Executed",
        "Loudoun",
        "VA",
        "51107",
        "Sub P",
        "PJM",
        None,
        "Dominion",
        "PJM",
        None,
        None,
        "NRIS",
        "Generation",
        "Gas",
        None,
        None,
        "Gas",
        600,
        None,
        None,
        2019,
        2022,
    ],
    [
        "P3",
        "operational",
        d("2020-01-01"),
        d("2023-01-01"),
        d("2025-07-02"),
        None,
        d("2022-06-01"),
        "In Service",
        "IA Executed",
        "Loudoun",
        "VA",
        "51107",
        "Sub P",
        "PJM",
        None,
        "Dominion",
        "PJM",
        None,
        None,
        "NRIS",
        "Generation",
        "Battery",
        None,
        None,
        "Battery",
        20,
        None,
        None,
        2020,
        2023,
    ],
    [
        "P4",
        "withdrawn",
        d("2015-01-01"),
        d("2018-01-01"),
        None,
        d("2017-01-01"),
        None,
        "Withdrawn",
        "Withdrawn",
        "Loudoun",
        "VA",
        "51107",
        "Sub P",
        "PJM",
        None,
        "Dominion",
        "PJM",
        None,
        None,
        "NRIS",
        "Generation",
        "Wind",
        None,
        None,
        "Wind",
        320,
        None,
        None,
        2015,
        2018,
    ],
    [
        "P5",
        "active",
        d("2025-03-01"),
        d("2031-01-01"),
        None,
        None,
        None,
        "Cluster",
        "Cluster Study",
        "Loudoun",
        "VA",
        "51107",
        "Sub P",
        "PJM",
        None,
        "Dominion",
        "PJM",
        None,
        None,
        "NRIS",
        "Generation",
        "Nuclear",
        None,
        None,
        "Nuclear",
        900,
        None,
        None,
        2025,
        2031,
    ],
    [
        "P6",
        "active",
        d("2010-01-01"),
        d("2024-01-01"),
        None,
        None,
        None,
        "SIS",
        "System Impact Study",
        "Loudoun",
        "VA",
        "51107",
        "Sub P",
        "PJM",
        None,
        "Dominion",
        "PJM",
        None,
        None,
        "NRIS",
        "Generation",
        "Hydro",
        None,
        None,
        "Hydro",
        40,
        None,
        None,
        2010,
        2024,
    ],
    [
        "S1",
        "withdrawn",
        d("2021-01-01"),
        d("2024-01-01"),
        None,
        d("2024-06-01"),
        None,
        "Withdrawn",
        "Withdrawn",
        "Lee",
        "GA",
        "13177",
        "Sub S",
        "Southeast",
        None,
        "Georgia Power",
        "Georgia Power",
        None,
        None,
        "Other",
        "Generation",
        "Solar",
        None,
        None,
        "Solar",
        150,
        None,
        None,
        2021,
        2024,
    ],
]


def write_workbook(
    path: Path, rows: list[list[object]] | None = None, name: str = DATA_SHEET
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = name
    ws.append(["RETURN TO CONTENTS"])
    ws.append(HEADER)
    for row in rows if rows is not None else ROWS:
        ws.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    return write_workbook(tmp_path / "LBNL_Ix_Queue_Data_File_thru2025.xlsx")


def test_read_requests_tidies_types_and_derives_mw(workbook: Path) -> None:
    requests = read_requests(workbook)
    assert len(requests) == 12
    assert set(requests["q_status"]) == {"active", "withdrawn", "suspended", "operational"}
    by_id = requests.set_index("q_id")
    # A hybrid's capacity is the sum of its parts; a single-type request keeps its mw_1.
    assert by_id.loc["A1", "mw"] == 150.0 and by_id.loc["E1", "mw"] == 1200.0
    assert by_id.loc["E2", "prop_year"] is not None and str(by_id.loc["E2", "prop_date"]) == "NaT"
    assert str(by_id.loc["P1", "on_date"].date()) == "2023-01-01"
    assert by_id.loc["A1", "fips_code"] == "6029"  # text, not a number
    assert requests["q_year"].dtype.name == "Int64"


def test_group_type_and_year_bins() -> None:
    assert group_type("Gas") == "Gas" and group_type("Solar+Battery") == "Solar"
    assert (
        group_type("Offshore Wind") == "Wind" and group_type("Battery+Other Storage") == "Battery"
    )
    assert (
        group_type("Hydro") == "Other" and group_type("") == "Other" and group_type(None) == "Other"
    )
    assert proposed_year_bin(2024, 2025, 5) == "earlier"
    assert proposed_year_bin(2025, 2025, 5) == "2025" and proposed_year_bin(2030, 2025, 5) == "2030"
    assert (
        proposed_year_bin(2031, 2025, 5) == "later"
        and proposed_year_bin(None, 2025, 5) == "Not stated"
    )
    assert through_from_name("LBNL_Ix_Queue_Data_File_thru2025.xlsx") == 2025
    assert through_from_name("queues.xlsx") is None


def test_active_by_region_and_type_by_hand(workbook: Path) -> None:
    table = active_by_region_and_type(read_requests(workbook)).set_index("region")
    # Active: A1 150 (Solar+Battery), A2 400 Gas, E1 1200 Gas, E2 300 Battery, P5 900 Nuclear,
    # P6 40 Hydro. E3 is suspended and does not count.
    assert table.loc["CAISO", "total"] == pytest.approx(0.55)
    assert table.loc["CAISO", "Solar+Battery"] == pytest.approx(0.15)
    assert table.loc["ERCOT", "Gas"] == pytest.approx(1.2) and table.loc[
        "ERCOT", "total"
    ] == pytest.approx(1.5)
    assert table.loc["PJM", "total"] == pytest.approx(0.94)
    assert table.loc["Total", "Gas"] == pytest.approx(1.6) and table.loc[
        "Total", "total"
    ] == pytest.approx(2.99)
    assert list(table.index)[-1] == "Total"
    by_state = active_by_state_and_type(read_requests(workbook)).set_index("state")
    assert by_state.loc["TX", "total"] == pytest.approx(1.5) and by_state.loc[
        "Total", "total"
    ] == pytest.approx(2.99)


def test_ia_executed_not_operational_counts_active_agreements_only(workbook: Path) -> None:
    table = ia_executed_not_operational(read_requests(workbook)).set_index("region")
    # A2 (400 gas, CAISO) and E2 (300 battery, ERCOT); E3 has an agreement but is suspended;
    # the P rows are operational.
    assert table.loc["CAISO", "Gas"] == pytest.approx(0.4)
    assert table.loc["ERCOT", "Battery"] == pytest.approx(0.3)
    assert table.loc["Total", "total"] == pytest.approx(0.7)
    assert "PJM" not in table.index


def test_completion_rates_by_hand(workbook: Path) -> None:
    table = completion_rates(read_requests(workbook)).set_index("region")
    # Requested 2000-2020: P1 80, P2 600, P3 20 (operational), P4 320 (withdrawn), P6 40 (active)
    # in PJM = 1,060 MW; nothing else falls in the window.
    assert table.loc["PJM", "requested_gw"] == pytest.approx(1.06)
    assert table.loc["PJM", "operational_share"] == pytest.approx(700 / 1060, abs=1e-3)
    assert table.loc["PJM", "withdrawn_share"] == pytest.approx(320 / 1060, abs=1e-3)
    assert table.loc["PJM", "active_share"] == pytest.approx(40 / 1060, abs=1e-3)
    assert table.loc["Total", "requested_gw"] == pytest.approx(1.06)


def test_median_months_by_hand(workbook: Path) -> None:
    table = median_months_to_operation(read_requests(workbook)).set_index("year")
    # 2025: P2 (2019-01-01 to 2025-01-01, 2192 days) and P3 (2020-01-01 to 2025-07-02, 2009 days)
    # -> median of 72.0 and 66.0 months = 69.0; PJM the same; 2023: P1 1826 days = 60.0.
    assert table.loc[2025, "n"] == 2 and table.loc[2025, "overall"] == pytest.approx(69.0, abs=0.1)
    assert table.loc[2025, "PJM"] == pytest.approx(69.0, abs=0.1)
    assert table.loc[2023, "overall"] == pytest.approx(60.0, abs=0.1)
    assert table.loc[2024, "n"] == 0 and str(table.loc[2024, "overall"]) == "nan"


def test_active_by_proposed_year_bins(workbook: Path) -> None:
    table = active_by_proposed_year_and_type(read_requests(workbook), 2025).set_index("prop_year")
    assert table.loc["earlier", "total"] == pytest.approx(0.04)  # P6, proposed 2024
    assert table.loc["2026", "Gas"] == pytest.approx(0.4)  # A2
    assert table.loc["2028", "Gas"] == pytest.approx(1.2)  # E1
    assert table.loc["later", "Nuclear"] == pytest.approx(0.9)  # P5, 2031
    assert table.loc["Not stated", "Battery"] == pytest.approx(0.3)  # E2
    assert table.loc["Total", "total"] == pytest.approx(2.99)


def test_read_requests_fails_loudly(tmp_path: Path) -> None:
    bad_status = [list(r) for r in ROWS]
    bad_status[0][1] = "pending"
    with pytest.raises(ValueError, match="q_status"):
        read_requests(write_workbook(tmp_path / "status.xlsx", bad_status))
    text_mw = [list(r) for r in ROWS]
    text_mw[2][25] = "lots"
    with pytest.raises(ValueError, match="mw_1"):
        read_requests(write_workbook(tmp_path / "mw.xlsx", text_mw))
    wb = Workbook()
    ws = wb.active
    ws.title = DATA_SHEET
    ws.append(["RETURN TO CONTENTS"])
    ws.append([c if c != "q_status" else "status" for c in HEADER])
    wb.save(tmp_path / "renamed.xlsx")
    with pytest.raises(ValueError, match="q_status"):
        read_requests(tmp_path / "renamed.xlsx")
    with pytest.raises(ValueError, match=DATA_SHEET):
        read_requests(write_workbook(tmp_path / "sheet.xlsx", name="Data"))


def test_process_workbook_is_deterministic_and_round_trips(workbook: Path, tmp_path: Path) -> None:
    first = process_workbook(workbook, tmp_path / "out1")
    second = process_workbook(workbook, tmp_path / "out2")
    assert first.through == 2025 and second.through == 2025
    digests = []
    for out in (tmp_path / "out1", tmp_path / "out2"):
        files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        digests.append([(f, hashlib.sha256((out / f).read_bytes()).hexdigest()) for f in files])
        assert not any(b"\r" in (out / f).read_bytes() for f in files)
    assert digests[0] == digests[1]
    assert "requests.csv" in dict(digests[0]) and "summaries/completion_rates.csv" in dict(
        digests[0]
    )
    summaries = read_summaries(tmp_path / "out1")
    assert summaries.through == 2025 and summaries.file == workbook.name
    assert summaries.active.set_index("region").loc["Total", "total"] == pytest.approx(2.99)
    # The free-text columns stay out of the committed table.
    header = (tmp_path / "out1" / "requests.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "poi_name" not in header and "developer" not in header and "q_id" in header


def test_year_in_file_name_must_match_the_data(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="thru2024"):
        process_workbook(
            write_workbook(tmp_path / "LBNL_Ix_Queue_Data_File_thru2024.xlsx"), tmp_path / "out"
        )


def test_cli_offline_and_dry_run(
    workbook: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "processed"
    assert pull_main(["--file", str(workbook), "--out", str(out), "--dry-run"]) == 0
    assert not out.exists()
    assert pull_main(["--file", str(workbook), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "12 requests" in text and "active capacity 3 GW" in text
    assert (out / "source.json").is_file() and (
        out / "summaries" / "median_months_to_operation.csv"
    ).is_file()
    assert pull_main(["--file", str(tmp_path / "missing.xlsx"), "--out", str(out)]) == 1


def test_committed_processed_queues_load() -> None:
    # The committed-artefact guard: the folder the grid page reads must load, and its total must
    # be the LBNL file's stated capacity (the report's 2,061 GW adds imputed hybrid storage).
    if not (QUEUES_PROCESSED_DIR / "source.json").is_file():
        pytest.skip("no processed queues committed")
    summaries = read_summaries(QUEUES_PROCESSED_DIR)
    total = summaries.active.set_index("region").loc["Total", "total"]
    assert 1500 < total < 2500
    assert summaries.through >= 2025

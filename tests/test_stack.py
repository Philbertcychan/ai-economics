"""Tests for ``data/stack.py``: the loaders of the stack CSVs and the primer reader.

``write_stack`` writes a small fixture stack into ``tmp_path`` (nine stages with the real chain's
keys but toy text and round toy values, a few metrics, players and conversions, two consumption
tiers, four campuses, one primer). ``tests/test_build_site.py`` imports it to build the stack
pages, so the fixture is defined once. No real figure about a real company appears here.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pandas as pd
import pytest

from data import STACK_DIR
from data.stack import (
    CAMPUS_COLUMNS,
    CONSUMPTION_TIER_COLUMNS,
    CONVERSION_COLUMNS,
    METRIC_COLUMNS,
    PLAYER_COLUMNS,
    STAGE_COLUMNS,
    load_campuses,
    load_consumption_tiers,
    load_conversions,
    load_metrics,
    load_players,
    load_primer,
    load_stages,
    split_stages,
)

# The chain's keys in order. The fixture file lists grid before power so the loader's sort by
# ``order`` is exercised; applications has no lead time and a bottleneck note without a score;
# silicon's name carries the two characters the site must escape.
STAGE_KEYS = [
    "power",
    "grid",
    "datacenter",
    "silicon",
    "memory",
    "systems",
    "compute",
    "models",
    "applications",
]

STAGES_CSV = """\
order,stage,name,unit,sells,buys_from,lead_time_years,bottleneck_score,bottleneck_note,status,summary
2,grid,Grid,MW connected,"a connection, with transformers",power,3,,,skeleton,Example grid summary.
1,power,Power,MW,firm electricity,,3,4,Example bottleneck note.,deep,Example power summary.
3,datacenter,Data centre,MW of IT load,rack-ready megawatts,grid,1.5,,,skeleton,
4,silicon,Wafers & <packaging>,wafers per month,leading-edge wafers,,2,,,skeleton,Example silicon summary.
5,memory,Memory,GB,HBM stacks,,1.5,,,skeleton,
6,systems,Systems,GPUs,servers and racks,silicon; memory,0.75,2,,skeleton,
7,compute,GPU-hours,GPU-hour,rented accelerator time,datacenter; systems,1,,,skeleton,
8,models,Tokens,tokens,model output,compute,0.5,,,skeleton,
9,applications,Applications,tokens per user per day,products,models,,,Note without a score.,skeleton,
"""  # noqa: E501  (CSV rows are one line each by definition)

# Three power metrics: the chain table shows the first two only. The second has a source with no
# URL, the third a URL with no source text.
METRICS_CSV = """\
stage,metric,value,unit,as_of,scope,source_url,source,confidence,note
power,Example capacity,1500000,MW,2026-06,US,https://example.com/power,Example source,high,Toy value & <note>
power,Example price,0.06,USD/kWh,2026Q2,US industrial,,Example filing p.3,medium,
power,Example third metric,42,GW,2026,world,https://example.com/third,,low,Only two show on the index
systems,Example GPU price,32000.5,USD per GPU,2026-09,list price,https://example.com/gpu,Example source,medium,
"""  # noqa: E501

# Five power players: the chain table shows the first four. AAA and BBB are the fixture site's
# companies, so their tickers get company links; ``TRUE`` checks that case does not matter.
PLAYERS_CSV = """\
stage,company,ticker,role,listed,note,source_url
power,Alpha Cloud,AAA,buyer,true,Example note,https://example.com/aaa
power,Gamma <Chips> & Co,,turbines,false,,
power,Player Three,PT3,fuel,TRUE,,
power,Player Four,PF4,nuclear,false,,
power,Player Five,PF5,solar,false,,
systems,Beta Compute,BBB,integrator,true,,
"""

CONVERSIONS_CSV = """\
from_stage,to_stage,factor,unit,as_of,source_url,source,confidence,note
power,grid,0.9,MW connected per MW,2026,https://example.com/conv,Example source,medium,Toy factor
systems,compute,8760,GPU-hours per GPU per year,2026,,,high,
"""

TIERS_CSV = """\
tier,name,examples,tokens_per_user_day,revenue_model,note
heavy,Example heavy tier,"agents, long runs",>1000000,usage,Toy
light,Example light tier,chat,5000-50000,subscription,
"""

# Four toy campuses out of size order: B has both figures (planned high, operating medium, a
# source text with a tag to escape); A has a planned figure only (low) and a sponsor to escape
# nowhere but here; C has an operating figure only, so its planned cell is blank and sorts last;
# D has a fractional operating figure. Sponsors, builders and places are invented.
CAMPUSES_CSV = """\
campus,sponsor,developer,state,planned_mw,planned_basis,planned_as_of,planned_source_url,planned_source,planned_confidence,operating_mw,operating_as_of,operating_source_url,operating_source,operating_confidence,power_source,status,note
Example Campus B,Beta Compute; Zed Labs,Example Builder,TX,1200,total power capacity,2026-03-18,https://example.com/campus-b,Example release (2026-03),high,400,2026-09,https://example.com/campus-b-ops,"Example directory, estimate <b>",medium,gas turbines on site,operating; expanding,Toy note & <caveat>
Example Campus A,Alpha Cloud,,LA,5000,IT load at full build,2026-07,https://example.com/campus-a,Example agency (2026-07),low,,,,,,new gas plants,under construction,No phase operating
Example Campus C,Gamma <Chips> & Co,,GA,,,,,,,600,2026-09-24,https://example.com/campus-c,Example directory,medium,,operating,No planned total published
Example Campus D,Delta,,WI,2263,IT load projected (estimate),2026-09-24,https://example.com/campus-d,Example directory,medium,0.5,2026-09-24,https://example.com/campus-d,Example directory,high,,operating; expanding,
"""  # noqa: E501

PRIMER_MD = """\
## Why power comes first

Example primer text with **emphasis**.

| item | value |
|---|---|
| toy | 1 |
"""

STACK_FILES = {
    "stages": STAGES_CSV,
    "metrics": METRICS_CSV,
    "players": PLAYERS_CSV,
    "conversions": CONVERSIONS_CSV,
    "consumption_tiers": TIERS_CSV,
    "campuses": CAMPUSES_CSV,
}


def write_stack(root: Path, *, primer: str | None = PRIMER_MD, **tables: str) -> Path:
    """Write the fixture stack to ``root/stack`` and return it.

    A keyword named after a table (``stages=...``) replaces that file's text; ``primer=None``
    leaves out ``primers/power.md``.
    """
    unknown = set(tables) - set(STACK_FILES)
    assert not unknown, f"not a stack table: {unknown}"
    stack = root / "stack"
    (stack / "primers").mkdir(parents=True, exist_ok=True)
    for name, text in (STACK_FILES | tables).items():
        (stack / f"{name}.csv").write_text(text, encoding="utf-8", newline="\n")
    if primer is not None:
        (stack / "primers" / "power.md").write_text(primer, encoding="utf-8", newline="\n")
    return stack


def variant(text: str, old: str, new: str) -> str:
    """``text`` with ``old`` swapped for ``new``; fails loudly if the fixture no longer has it."""
    assert old in text, f"fixture no longer contains {old!r}"
    return text.replace(old, new, 1)


# --------------------------------------------------------------------------------------------
# Good files
# --------------------------------------------------------------------------------------------


def test_stages_load_sorted_and_typed(tmp_path: Path) -> None:
    stages = load_stages(write_stack(tmp_path))
    assert tuple(stages.columns) == STAGE_COLUMNS
    assert stages["order"].tolist() == list(range(1, 10))  # by order, not file order
    assert stages["stage"].tolist() == STAGE_KEYS
    assert stages.index.tolist() == list(range(9))
    by_key = stages.set_index("stage")
    power = by_key.loc["power"]
    assert (power["lead_time_years"], power["bottleneck_score"]) == (3.0, 4)
    assert (power["status"], power["bottleneck_note"]) == ("deep", "Example bottleneck note.")
    assert str(stages["bottleneck_score"].dtype) == "Int64"
    assert str(stages["lead_time_years"].dtype) == "float64"
    assert by_key["bottleneck_score"].isna()["grid"]
    assert math.isnan(by_key.loc["applications", "lead_time_years"])
    # Blank text stays blank, never NaN, so the site can test for it.
    assert by_key.loc["power", "buys_from"] == "" and by_key.loc["memory", "summary"] == ""
    assert split_stages(by_key.loc["systems", "buys_from"]) == ["silicon", "memory"]
    assert by_key.loc["grid", "sells"] == "a connection, with transformers"  # quoted comma


def test_metrics_players_conversions_and_tiers_load(tmp_path: Path) -> None:
    stack = write_stack(tmp_path)
    metrics = load_metrics(stack)
    assert tuple(metrics.columns) == METRIC_COLUMNS
    assert metrics["value"].tolist() == [1500000.0, 0.06, 42.0, 32000.5]  # file order kept
    assert metrics["source_url"].tolist()[1] == "" and metrics["source"].tolist()[2] == ""
    players = load_players(stack)
    assert tuple(players.columns) == PLAYER_COLUMNS
    assert players["listed"].tolist() == [True, False, True, False, False, True]
    assert str(players["listed"].dtype) == "bool"
    assert players["ticker"].tolist()[1] == ""
    conversions = load_conversions(stack)
    assert tuple(conversions.columns) == CONVERSION_COLUMNS
    assert conversions["factor"].tolist() == [0.9, 8760.0]
    tiers = load_consumption_tiers(stack)
    assert tuple(tiers.columns) == CONSUMPTION_TIER_COLUMNS
    assert tiers["tier"].tolist() == ["heavy", "light"]
    assert tiers["examples"].tolist()[0] == "agents, long runs"
    assert tiers["tokens_per_user_day"].tolist() == [">1000000", "5000-50000"]  # text, as written


def test_campuses_load_in_file_order_with_typed_figures(tmp_path: Path) -> None:
    campuses = load_campuses(write_stack(tmp_path))
    assert tuple(campuses.columns) == CAMPUS_COLUMNS
    assert campuses["campus"].tolist() == [f"Example Campus {c}" for c in "BACD"]  # file order
    assert campuses.index.tolist() == [0, 1, 2, 3]
    assert str(campuses["planned_mw"].dtype) == "float64"
    assert str(campuses["operating_mw"].dtype) == "float64"
    assert campuses["planned_mw"].tolist()[:2] == [1200.0, 5000.0]
    assert math.isnan(campuses["planned_mw"].tolist()[2])  # C has no planned figure
    assert math.isnan(campuses["operating_mw"].tolist()[1])  # A has no operating figure
    assert campuses["operating_mw"].tolist()[3] == 0.5
    by_name = campuses.set_index("campus")
    a = by_name.loc["Example Campus A"]
    # blanks beside a blank figure stay "" (never NaN), and text is kept as written
    assert (a["operating_source_url"], a["operating_confidence"], a["operating_as_of"]) == (
        "",
        "",
        "",
    )
    assert (
        a["developer"] == ""
        and a["planned_as_of"] == "2026-07"
        and a["planned_confidence"] == "low"
    )
    b = by_name.loc["Example Campus B"]
    assert b["sponsor"] == "Beta Compute; Zed Labs" and b["planned_as_of"] == "2026-03-18"
    assert b["operating_source"] == "Example directory, estimate <b>"  # quoted comma, raw tag
    assert campuses["state"].tolist() == ["TX", "LA", "GA", "WI"]


def test_campuses_file_is_optional(tmp_path: Path) -> None:
    """No campuses.csv is "none yet": an empty frame with the columns and dtypes of a full one."""
    stack = write_stack(tmp_path)
    (stack / "campuses.csv").unlink()
    missing = load_campuses(stack)
    assert missing.shape == (0, len(CAMPUS_COLUMNS)) and tuple(missing.columns) == CAMPUS_COLUMNS
    assert str(missing["planned_mw"].dtype) == "float64" and str(missing["sponsor"].dtype) == "str"
    header_only = load_campuses(write_stack(tmp_path, campuses=CAMPUSES_CSV.splitlines()[0] + "\n"))
    pd.testing.assert_frame_equal(missing, header_only)
    assert load_campuses(tmp_path / "nowhere").empty  # no stack dir at all


def test_header_only_tables_are_empty_frames_with_the_right_columns(tmp_path: Path) -> None:
    """Researchers fill metrics, players and conversions later; a header alone is not an error."""
    stack = write_stack(
        tmp_path,
        metrics=METRICS_CSV.splitlines()[0] + "\n",
        players=PLAYERS_CSV.splitlines()[0] + "\n",
        conversions=CONVERSIONS_CSV.splitlines()[0] + "\n",
        consumption_tiers=TIERS_CSV.splitlines()[0] + "\n",
        campuses=CAMPUSES_CSV.splitlines()[0] + "\n",
    )
    metrics = load_metrics(stack)
    assert metrics.shape == (0, len(METRIC_COLUMNS)) and tuple(metrics.columns) == METRIC_COLUMNS
    assert str(metrics["value"].dtype) == "float64"
    assert load_players(stack).shape == (0, len(PLAYER_COLUMNS))
    assert load_conversions(stack).shape == (0, len(CONVERSION_COLUMNS))
    assert load_consumption_tiers(stack).shape == (0, len(CONSUMPTION_TIER_COLUMNS))
    assert load_campuses(stack).shape == (0, len(CAMPUS_COLUMNS))
    assert len(load_stages(stack)) == 9  # the chain is unaffected


def test_bom_crlf_extra_columns_and_blank_lines_are_tolerated(tmp_path: Path) -> None:
    """Excel on Windows adds a BOM and CRLF; a stray column or trailing blank line is harmless."""
    lines = STAGES_CSV.splitlines()
    text = "﻿" + "\r\n".join([lines[0] + ",extra", *(line + "," for line in lines[1:])])
    text += "\r\n\r\n   \r\n"
    stages = load_stages(write_stack(tmp_path, stages=text))
    assert tuple(stages.columns) == STAGE_COLUMNS and len(stages) == 9
    assert stages["name"].tolist()[0] == "Power"


def test_missing_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="stages.csv"):
        load_stages(tmp_path)
    stack = write_stack(tmp_path)
    (stack / "metrics.csv").unlink()
    with pytest.raises(FileNotFoundError, match="metrics.csv"):
        load_metrics(stack)
    (stack / "stages.csv").unlink()
    with pytest.raises(FileNotFoundError, match="stages.csv"):
        load_players(stack)  # stage references cannot be checked without the chain


def test_empty_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"metrics\.csv: empty file"):
        load_metrics(write_stack(tmp_path, metrics=""))


# --------------------------------------------------------------------------------------------
# Each validation failure names the file and the row (the line number a spreadsheet shows)
# --------------------------------------------------------------------------------------------

GRID_TAIL = ",power,3,,,skeleton,Example grid summary."
POWER_MID = ",,3,4,Example bottleneck note.,deep,"

STAGE_FAILURES = [
    (STAGES_CSV.replace("stage,name,", "stage,"), r"stages\.csv: missing columns \['name'\]"),
    (variant(STAGES_CSV, "3,datacenter,", "3,power,"), r"duplicate stage keys \['power'\]"),
    (variant(STAGES_CSV, "1,power,", "1,Power,"), r"row 3 \(Power\): stage 'Power' must be"),
    (variant(STAGES_CSV, "1,power,", "1,../power,"), r"row 3 \(\.\./power\): stage"),
    (variant(STAGES_CSV, "1,power,", ",power,"), r"row 3 \(power\): order is blank"),
    (
        variant(STAGES_CSV, "1,power,", "one,power,"),
        r"row 3 \(power\): order 'one' is not a number",
    ),
    (
        variant(STAGES_CSV, "1,power,", "1.5,power,"),
        r"row 3 \(power\): order '1.5' is not an integer",
    ),
    (variant(STAGES_CSV, "3,datacenter,", "2,datacenter,"), r"duplicate order values \[2\]"),
    (variant(STAGES_CSV, "1,power,Power,MW,", "1,power,,MW,"), r"row 3 \(power\): name is blank"),
    (
        variant(STAGES_CSV, POWER_MID, ",,three,4,Example bottleneck note.,deep,"),
        r"row 3 \(power\): lead_time_years 'three' is not a number",
    ),
    (
        variant(STAGES_CSV, POWER_MID, ",,-1,4,Example bottleneck note.,deep,"),
        r"row 3 \(power\): lead_time_years -1 is negative",
    ),
    (
        variant(STAGES_CSV, POWER_MID, ",,3,6,Example bottleneck note.,deep,"),
        r"row 3 \(power\): bottleneck_score '6' must be blank or an integer 1 to 5",
    ),
    (
        variant(STAGES_CSV, POWER_MID, ",,3,0,Example bottleneck note.,deep,"),
        r"bottleneck_score '0' must be blank or an integer 1 to 5",
    ),
    (
        variant(STAGES_CSV, POWER_MID, ",,3,2.5,Example bottleneck note.,deep,"),
        r"bottleneck_score '2.5' must be blank or an integer 1 to 5",
    ),
    (
        variant(STAGES_CSV, POWER_MID, ",,3,high,Example bottleneck note.,deep,"),
        r"bottleneck_score 'high' is not a number",
    ),
    (
        variant(STAGES_CSV, POWER_MID, ",,3,4,Example bottleneck note.,final,"),
        r"row 3 \(power\): status must be one of \('skeleton', 'deep'\), not 'final'",
    ),
    (
        variant(STAGES_CSV, POWER_MID, ",,3,4,Example bottleneck note.,,"),
        r"status must be one of \('skeleton', 'deep'\), not ''",
    ),
    (
        variant(STAGES_CSV, GRID_TAIL, ",coal,3,,,skeleton,Example grid summary."),
        r"row 2 \(grid\): buys_from names unknown stage\(s\) \['coal'\]",
    ),
    (
        variant(STAGES_CSV, GRID_TAIL, ",grid,3,,,skeleton,Example grid summary."),
        r"row 2 \(grid\): buys_from names the stage itself",
    ),
    (
        variant(STAGES_CSV, '"a connection, with transformers"', "a connection, with transformers"),
        r"stages\.csv: row 2 has 12 fields, expected 11 \(an unquoted comma",
    ),
]


@pytest.mark.parametrize(("text", "match"), STAGE_FAILURES)
def test_malformed_stages_fail_naming_the_row(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_stages(write_stack(tmp_path, stages=text))


METRIC_ROW_1 = (
    "power,Example capacity,1500000,MW,2026-06,US,https://example.com/power,Example source,high,"
)

METRIC_FAILURES = [
    (METRICS_CSV.replace(",confidence,", ","), r"metrics\.csv: missing columns \['confidence'\]"),
    (
        variant(METRICS_CSV, "Example capacity,1500000,", "Example capacity,lots,"),
        r"metrics\.csv: row 2 \(power / Example capacity\): value 'lots' is not a number",
    ),
    (
        variant(METRICS_CSV, "Example capacity,1500000,", "Example capacity,,"),
        r"row 2 \(power / Example capacity\): value is blank",
    ),
    (
        variant(METRICS_CSV, "power,Example capacity,", "power,,"),
        r"row 2 \(power / \?\): metric is blank",
    ),
    (
        variant(METRICS_CSV, "systems,Example GPU price", "chips,Example GPU price"),
        r"row 5 \(chips / Example GPU price\): stage names unknown stage\(s\) \['chips'\]",
    ),
    (
        variant(METRICS_CSV, "Example source,high,", "Example source,sure,"),
        r"confidence must be one of \('high', 'medium', 'low'\), not 'sure'",
    ),
    (
        variant(METRICS_CSV, "Example source,high,", "Example source,,"),
        r"confidence must be one of \('high', 'medium', 'low'\), not ''",
    ),
    (
        variant(METRICS_CSV, "https://example.com/power", "example.com/power"),
        r"row 2 \(power / Example capacity\): source_url 'example.com/power' does not start "
        r"with http",
    ),
    (
        variant(METRICS_CSV, METRIC_ROW_1 + "Toy value & <note>", METRIC_ROW_1 + "a, b"),
        r"metrics\.csv: row 2 has 11 fields, expected 10",
    ),
]


@pytest.mark.parametrize(("text", "match"), METRIC_FAILURES)
def test_malformed_metrics_fail_naming_the_row(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_metrics(write_stack(tmp_path, metrics=text))


PLAYER_FAILURES = [
    (PLAYERS_CSV.replace(",listed,", ","), r"players\.csv: missing columns \['listed'\]"),
    (
        variant(PLAYERS_CSV, "buyer,true,", "buyer,yes,"),
        r"players\.csv: row 2 \(power / Alpha Cloud\): listed must be true or false, not 'yes'",
    ),
    (
        variant(PLAYERS_CSV, "integrator,true,", "integrator,,"),
        r"row 7 \(systems / Beta Compute\): listed must be true or false, not ''",
    ),
    (
        variant(PLAYERS_CSV, "systems,Beta Compute", "chips,Beta Compute"),
        r"row 7 \(chips / Beta Compute\): stage names unknown stage\(s\) \['chips'\]",
    ),
    (
        variant(PLAYERS_CSV, "power,Alpha Cloud,AAA", "power,,AAA"),
        r"row 2 \(power / \?\): company is blank",
    ),
    (
        variant(PLAYERS_CSV, "https://example.com/aaa", "ftp://example.com/aaa"),
        r"source_url 'ftp://example.com/aaa' does not start with http",
    ),
]


@pytest.mark.parametrize(("text", "match"), PLAYER_FAILURES)
def test_malformed_players_fail_naming_the_row(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_players(write_stack(tmp_path, players=text))


CONVERSION_FAILURES = [
    (CONVERSIONS_CSV.replace(",factor,", ","), r"conversions\.csv: missing columns \['factor'\]"),
    (
        variant(CONVERSIONS_CSV, "power,grid,0.9,", "power,grid,x,"),
        r"conversions\.csv: row 2 \(power -> grid\): factor 'x' is not a number",
    ),
    (
        variant(CONVERSIONS_CSV, "power,grid,0.9,", "coal,grid,0.9,"),
        r"row 2 \(coal -> grid\): from_stage names unknown stage\(s\) \['coal'\]",
    ),
    (
        variant(CONVERSIONS_CSV, "systems,compute,8760", "systems,cloud,8760"),
        r"row 3 \(systems -> cloud\): to_stage names unknown stage\(s\) \['cloud'\]",
    ),
    (
        variant(CONVERSIONS_CSV, ",medium,Toy factor", ",maybe,Toy factor"),
        r"confidence must be one of \('high', 'medium', 'low'\), not 'maybe'",
    ),
    (
        variant(CONVERSIONS_CSV, "https://example.com/conv", "www.example.com/conv"),
        r"source_url 'www.example.com/conv' does not start with http",
    ),
]


@pytest.mark.parametrize(("text", "match"), CONVERSION_FAILURES)
def test_malformed_conversions_fail_naming_the_row(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_conversions(write_stack(tmp_path, conversions=text))


TIER_FAILURES = [
    (TIERS_CSV.replace(",note", ""), r"consumption_tiers\.csv: missing columns \['note'\]"),
    (
        variant(TIERS_CSV, "light,Example light tier", "heavy,Example light tier"),
        r"consumption_tiers\.csv: duplicate tier keys \['heavy'\]",
    ),
    (
        variant(TIERS_CSV, "heavy,Example heavy tier", ",Example heavy tier"),
        r"row 2 \(\?\): tier is blank",
    ),
    (variant(TIERS_CSV, "light,Example light tier", "light,"), r"row 3 \(light\): name is blank"),
    (
        variant(TIERS_CSV, '"agents, long runs"', "agents, long runs"),
        r"consumption_tiers\.csv: row 2 has 7 fields, expected 6",
    ),
]


@pytest.mark.parametrize(("text", "match"), TIER_FAILURES)
def test_malformed_tiers_fail_naming_the_row(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_consumption_tiers(write_stack(tmp_path, consumption_tiers=text))


def campus_row(name: str, **changes: str) -> tuple[str, str]:
    """The ``CAMPUSES_CSV`` line of campus ``name`` (its letter) and a copy with columns changed."""
    header, *lines = CAMPUSES_CSV.splitlines()
    header = header.split(",")
    line = next(r for r in lines if r.startswith(f"Example Campus {name},"))
    cells = next(csv.reader([line]))
    for column, value in changes.items():
        cells[header.index(column)] = value
    return line, ",".join(f'"{cell}"' if "," in cell else cell for cell in cells)


def edited(text: str, name: str, **changes: str) -> str:
    """``text`` (a campuses CSV) with one campus's columns replaced."""
    return variant(text, *campus_row(name, **changes))


# (campus letter, columns to change, expected message). Rows: B is row 2, A row 3, C row 4.
CAMPUS_FAILURES = [
    ("A", {"campus": ""}, r"row 3 \(\?\): campus is blank"),
    ("C", {"campus": "Example Campus A"}, r"duplicate campus names \['Example Campus A'\]"),
    ("B", {"state": "Tx"}, r"row 2 \(Example Campus B\): state 'Tx' must be two capital letters"),
    ("B", {"state": "Texas"}, r"state 'Texas' must be two capital letters"),
    ("B", {"state": ""}, r"state '' must be two capital letters"),
    ("B", {"planned_mw": "lots"}, r"row 2 \(Example Campus B\): planned_mw 'lots' is not a number"),
    ("B", {"planned_mw": "-5"}, r"row 2 \(Example Campus B\): planned_mw -5 is negative"),
    ("C", {"operating_mw": "six hundred"}, r"operating_mw 'six hundred' is not a number"),
    (
        "A",
        {"planned_source_url": ""},
        r"row 3 \(Example Campus A\): planned_mw '5000' has no planned_source_url",
    ),
    (
        "A",
        {"planned_confidence": ""},
        r"row 3 \(Example Campus A\): planned_mw '5000' has no planned_confidence",
    ),
    (
        "C",
        {"operating_source_url": ""},
        r"row 4 \(Example Campus C\): operating_mw '600' has no operating_source_url",
    ),
    (
        "C",
        {"operating_confidence": ""},
        r"row 4 \(Example Campus C\): operating_mw '600' has no operating_confidence",
    ),
    (
        "A",
        {"planned_confidence": "sure"},
        r"planned_confidence must be one of \('high', 'medium', 'low'\), not 'sure'",
    ),
    (
        "C",
        {"operating_confidence": "Medium"},
        r"operating_confidence must be one of .*, not 'Medium'",
    ),
    (
        "A",
        {"planned_source_url": "example.com/a"},
        r"row 3 \(Example Campus A\): planned_source_url 'example.com/a' does not start with http",
    ),
    (
        "C",
        {"operating_source_url": "ftp://example.com/c"},
        r"operating_source_url 'ftp://example.com/c' does not start with http",
    ),
    # a blank figure needs no source or confidence, but what is there must still be well formed
    (
        "A",
        {"operating_source_url": "www.example.com"},
        r"row 3 \(Example Campus A\): operating_source_url 'www.example.com' does not start",
    ),
    (
        "A",
        {"operating_confidence": "maybe"},
        r"row 3 \(Example Campus A\): operating_confidence must be one of .*, not 'maybe'",
    ),
    (
        "A",
        {"planned_as_of": "2026"},
        r"row 3 \(Example Campus A\): planned_as_of '2026' is not YYYY-MM or YYYY-MM-DD",
    ),
    ("A", {"planned_as_of": "2026-13"}, r"planned_as_of '2026-13' is not YYYY-MM or YYYY-MM-DD"),
    ("A", {"planned_as_of": "2026-7"}, r"planned_as_of '2026-7' is not"),
    ("A", {"planned_as_of": "July 2026"}, r"planned_as_of 'July 2026' is not"),
    ("C", {"operating_as_of": "2026-09-32"}, r"operating_as_of '2026-09-32' is not YYYY-MM"),
    ("C", {"operating_as_of": "20260924"}, r"operating_as_of '20260924' is not"),
]


@pytest.mark.parametrize(("name", "changes", "match"), CAMPUS_FAILURES)
def test_malformed_campuses_fail_naming_the_row(
    tmp_path: Path, name: str, changes: dict[str, str], match: str
) -> None:
    text = edited(CAMPUSES_CSV, name, **changes)
    assert text != CAMPUSES_CSV
    with pytest.raises(ValueError, match=match):
        load_campuses(write_stack(tmp_path, campuses=text))


def test_malformed_campuses_file_shape_fails_naming_the_file(tmp_path: Path) -> None:
    missing = CAMPUSES_CSV.replace(",planned_confidence,", ",planned_conf,")
    with pytest.raises(
        ValueError, match=r"campuses\.csv: missing columns \['planned_confidence'\]"
    ):
        load_campuses(write_stack(tmp_path, campuses=missing))
    quoted = '"Example directory, estimate <b>"'
    unquoted = variant(CAMPUSES_CSV, quoted, quoted.strip('"'))
    with pytest.raises(ValueError, match=r"campuses\.csv: row 2 has 19 fields, expected 18"):
        load_campuses(write_stack(tmp_path, campuses=unquoted))


def test_campus_problems_are_reported_together(tmp_path: Path) -> None:
    text = edited(CAMPUSES_CSV, "B", state="Tx")
    text = edited(text, "A", planned_confidence="")
    text = edited(text, "C", operating_mw="-1")
    with pytest.raises(ValueError) as excinfo:
        load_campuses(write_stack(tmp_path, campuses=text))
    message = str(excinfo.value)
    assert message.startswith("campuses.csv: ")
    assert "row 2 (Example Campus B): state 'Tx' must be two capital letters" in message
    assert "row 3 (Example Campus A): planned_mw '5000' has no planned_confidence" in message
    assert "row 4 (Example Campus C): operating_mw -1 is negative" in message


def test_every_problem_in_a_file_is_reported_at_once(tmp_path: Path) -> None:
    text = variant(STAGES_CSV, "1,power,", "one,power,")
    text = variant(text, GRID_TAIL, ",coal,3,,,skeleton,Example grid summary.")
    with pytest.raises(ValueError) as excinfo:
        load_stages(write_stack(tmp_path, stages=text))
    message = str(excinfo.value)
    assert "row 3 (power): order 'one' is not a number" in message
    assert "row 2 (grid): buys_from names unknown stage(s) ['coal']" in message


def test_row_numbers_count_physical_lines(tmp_path: Path) -> None:
    """A blank line between rows shifts the line a spreadsheet shows, and the error follows it."""
    lines = METRICS_CSV.splitlines()
    text = "\n".join([lines[0], "", lines[1], lines[2].replace("0.06", "cheap"), *lines[3:]]) + "\n"
    with pytest.raises(ValueError, match=r"row 4 \(power / Example price\): value 'cheap'"):
        load_metrics(write_stack(tmp_path, metrics=text))


# --------------------------------------------------------------------------------------------
# Primers and helpers
# --------------------------------------------------------------------------------------------


def test_load_primer(tmp_path: Path) -> None:
    primers = write_stack(tmp_path) / "primers"
    assert load_primer("power", primers) == PRIMER_MD
    assert load_primer("grid", primers) is None  # no file, no primer
    assert load_primer("power", tmp_path / "elsewhere") is None
    (primers / "grid.md").write_text("﻿# Grid\n", encoding="utf-8")
    assert load_primer("grid", primers) == "# Grid\n"  # BOM dropped
    with pytest.raises(ValueError, match="invalid stage key"):
        load_primer("../secrets", primers)


def test_split_stages() -> None:
    assert split_stages("silicon; memory") == ["silicon", "memory"]
    assert split_stages("power") == ["power"]
    assert split_stages("") == [] and split_stages(" ; ") == []


# --------------------------------------------------------------------------------------------
# The committed stack
# --------------------------------------------------------------------------------------------


def test_committed_stack_loads() -> None:
    """The real ``stack/`` must load cleanly, or the site silently drops the stack pages."""
    stages = load_stages(STACK_DIR)
    assert stages["order"].tolist() == list(range(1, 10))
    assert stages["stage"].tolist() == STAGE_KEYS
    for loader in (load_metrics, load_players, load_conversions, load_consumption_tiers):
        loader(STACK_DIR)
    campuses = load_campuses(STACK_DIR)
    assert len(campuses) >= 1 and campuses["campus"].is_unique
    # every figure that is present carries a source: the loader enforces it, this restates it
    for figure in ("planned", "operating"):
        present = campuses[f"{figure}_mw"].notna()
        assert (campuses.loc[present, f"{figure}_source_url"].str.startswith("http")).all()

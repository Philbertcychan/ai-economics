"""Tests for ``data/stack.py``: the loaders of the stack CSVs and the primer reader.

``write_stack`` writes a small fixture stack into ``tmp_path`` (nine stages with the real chain's
keys but toy text and round toy values, a few metrics, players and conversions, two consumption
tiers, one primer). ``tests/test_build_site.py`` imports it to build the stack pages, so the
fixture is defined once. No real figure about a real company appears here.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from data import STACK_DIR
from data.stack import (
    CONSUMPTION_TIER_COLUMNS,
    CONVERSION_COLUMNS,
    METRIC_COLUMNS,
    PLAYER_COLUMNS,
    STAGE_COLUMNS,
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


def test_header_only_tables_are_empty_frames_with_the_right_columns(tmp_path: Path) -> None:
    """Researchers fill metrics, players and conversions later; a header alone is not an error."""
    stack = write_stack(
        tmp_path,
        metrics=METRICS_CSV.splitlines()[0] + "\n",
        players=PLAYERS_CSV.splitlines()[0] + "\n",
        conversions=CONVERSIONS_CSV.splitlines()[0] + "\n",
        consumption_tiers=TIERS_CSV.splitlines()[0] + "\n",
    )
    metrics = load_metrics(stack)
    assert metrics.shape == (0, len(METRIC_COLUMNS)) and tuple(metrics.columns) == METRIC_COLUMNS
    assert str(metrics["value"].dtype) == "float64"
    assert load_players(stack).shape == (0, len(PLAYER_COLUMNS))
    assert load_conversions(stack).shape == (0, len(CONVERSION_COLUMNS))
    assert load_consumption_tiers(stack).shape == (0, len(CONSUMPTION_TIER_COLUMNS))
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

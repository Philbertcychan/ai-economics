"""Tests for ``data/disclosed.py`` and for the datasets committed in ``data/disclosed/``."""

from pathlib import Path

import pytest

from data import DISCLOSED_DIR
from data.disclosed import DISCLOSED_COLUMNS, disclosed_series, load_disclosed

HEADER = ",".join(DISCLOSED_COLUMNS)
ROWS = [
    "2025Q2,active_power_mw,470,MW,approximately,8-K EX-99.1,2025-08-12,0001-25-000039,,https://x/b",
    "2025Q1,active_power_mw,420,MW,approximately,8-K EX-99.1,2025-05-14,0001-25-000010,,https://x/a",
    "2025Q1,revenue_backlog_usd_bn,25.9,USD bn,,8-K EX-99.1,2025-05-14,0001-25-000010,,https://x/a",
]


def write(tmp_path: Path, rows: list[str]) -> None:
    text = "\n".join([HEADER, *rows]) + "\n"
    (tmp_path / "AAA.csv").write_text(text, encoding="utf-8", newline="\n")


def test_missing_file_is_none(tmp_path: Path) -> None:
    assert load_disclosed("AAA", tmp_path) is None
    assert disclosed_series(None, "active_power_mw").empty


def test_series_is_sorted_by_period(tmp_path: Path) -> None:
    write(tmp_path, ROWS)
    series = disclosed_series(load_disclosed("aaa", tmp_path), "active_power_mw")
    assert series.to_dict() == {"2025Q1": 420.0, "2025Q2": 470.0}
    assert disclosed_series(load_disclosed("AAA", tmp_path), "nope").empty


@pytest.mark.parametrize(
    ("bad_row", "message"),
    [
        ("2025Q1,active_power_mw,430,MW,,8-K,2025-05-14,0001,,https://x/a", "duplicate period/kpi"),
        ("2025Q3,active_power_mw,lots,MW,,8-K,2025-11-10,0001,,https://x/c", "non-numeric value"),
        ("Q3 2025,active_power_mw,590,MW,,8-K,2025-11-10,0001,,https://x/c", "period must look"),
        ("2025Q3,active_power_mw,590,MW,,8-K,2025-11-10,0001,,", "without a source url"),
    ],
)
def test_malformed_rows_fail_loudly(tmp_path: Path, bad_row: str, message: str) -> None:
    write(tmp_path, [*ROWS, bad_row])
    with pytest.raises(ValueError, match=message):
        load_disclosed("AAA", tmp_path)


@pytest.mark.parametrize("path", sorted(DISCLOSED_DIR.glob("*.csv")), ids=lambda p: p.stem)
def test_committed_datasets_load(path: Path) -> None:
    frame = load_disclosed(path.stem)
    assert frame is not None and not frame.empty
    assert set(frame["qualifier"]) <= {"", "approximately", "more than", "nearly"}
    assert frame["url"].str.startswith("https://www.sec.gov/Archives/edgar/data/").all()

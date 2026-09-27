"""Tests for ``data/signals.py`` and for the committed ledger in ``signals/``."""

from pathlib import Path

import pytest

from data.signals import LEDGER_COLUMNS, SIGNALS_DIR, load_signals, signals_for_stage

HEADER = ",".join(LEDGER_COLUMNS)
# Long CSV lines are the point of these fixtures; keeping them on one line keeps them readable.
ROWS = [  # noqa: E501
    '2026-08-11,contract,compute,Alpha Cloud,Beta Labs,"Beta commits $4 billion over five years",4,USD bn,https://www.sec.gov/x,8-K,confirmed,AAA: revenue_backlog_usd_bn,',  # noqa: E501
    '2026-09-01,statement,power,Utility Co,,"Says 2 GW of data-centre load requested in its territory",2,GW,https://example.com/y,press release,reported,stack: power/demand_pipeline_gw,',  # noqa: E501
    '2026-07-15,price,memory,Tracker,,"Contract NAND prices up 10% quarter on quarter",10,%,https://example.com/z,tracker release,confirmed,stack: memory/nand_contract_price,',  # noqa: E501
]


def write(tmp_path: Path, rows: list[str]) -> Path:
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "stages.csv").write_text(
        "order,stage,name,unit\n1,power,Power,MW\n5,memory,Memory,GB\n7,compute,GPU-hours,GPU-hour\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "signals"
    ledger.mkdir()
    (ledger / "ledger.csv").write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return ledger


def test_missing_ledger_is_none(tmp_path: Path) -> None:
    assert load_signals(tmp_path) is None
    assert signals_for_stage(None, "power").empty


def test_loads_sorted_newest_first_and_filters_by_stage(tmp_path: Path) -> None:
    ledger = write(tmp_path, ROWS)
    frame = load_signals(ledger, stack_dir=tmp_path / "stack")
    assert frame["date"].tolist() == ["2026-09-01", "2026-08-11", "2026-07-15"]
    assert frame["value"].tolist() == [2.0, 4.0, 10.0]
    memory = signals_for_stage(frame, "memory")
    assert len(memory) == 1 and memory.loc[0, "kind"] == "price"


@pytest.mark.parametrize(
    ("bad_row", "message"),
    [
        ("Sept 1 2026,statement,power,X,,claim,,,https://a,src,confirmed,watch,", "date must be"),
        ("2026-09-01,rumour,power,X,,claim,,,https://a,src,confirmed,watch,", "kind must be"),
        ("2026-09-01,statement,power,X,,claim,,,https://a,src,likely,watch,", "confidence must be"),
        ("2026-09-01,statement,orbit,X,,claim,,,https://a,src,confirmed,watch,", "unknown stage"),
        ("2026-09-01,statement,power,X,,claim,,,sec.gov/a,src,confirmed,watch,", "source_url must"),
        (
            "2026-09-01,statement,power,X,,claim,two,GW,https://a,src,confirmed,watch,",
            "value must be",
        ),
    ],
)
def test_malformed_rows_fail_loudly(tmp_path: Path, bad_row: str, message: str) -> None:
    ledger = write(tmp_path, [*ROWS, bad_row])
    with pytest.raises(ValueError, match=message):
        load_signals(ledger, stack_dir=tmp_path / "stack")


def test_committed_ledger_loads() -> None:
    frame = load_signals(SIGNALS_DIR)
    if frame is None:
        pytest.skip("no ledger committed yet")
    assert not frame.empty
    assert frame["source_url"].str.startswith("http").all()

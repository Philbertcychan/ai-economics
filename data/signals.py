"""The signals ledger: dated, sourced events from outside the filings that bear on the model.

Why a ledger
------------
Filings are complete and slow. What moves these companies between filings is contracts,
construction, power deals, policy and prices, announced in press releases, earnings calls and
the news. Those facts belong in the model, but not by editing a number in the dark. Each one is
recorded here first, with its source and a confidence rating, and mapped to the stage of the
value chain or the assumption it bears on. Changing an assumption because of a signal is then a
logged decision, and a rumour cannot leak into a forecast unexamined.

File: ``signals/ledger.csv``, one row per event.

Columns
-------
``date``          when the event happened or was announced (YYYY-MM-DD)
``kind``          one of ``contract``, ``buildout``, ``energy``, ``financing``, ``policy``,
                  ``statement``, ``price``
``stage``         the value-chain stage it bears on (a ``stage`` in ``stack/stages.csv``)
``actor``         who did or said it
``counterparty``  the other side, if any
``claim``         the event in one sentence, with the number
``value``         the headline number, if there is one (plain number)
``unit``          its unit (``USD bn``, ``MW``, ``GW``, ``USD/GPU-hour``, ...); blank if no value
``source_url``    where it can be checked
``source``        publisher and document
``confidence``    ``confirmed`` (a filing, an official release or the actor's own statement),
                  ``reported`` (credible press, named sources) or ``speculated`` (rumour,
                  unnamed sources, analyst estimate)
``maps_to``       what it should change: ``<TICKER>: <assumption name>``,
                  ``stack: <stage>/<metric>``
                  or ``watch`` when it is context only
``note``          caveats, and what was done with it (which assumption changed, when)
"""

import csv
from pathlib import Path

import pandas as pd

from data import REPO_ROOT, SIGNALS_DIR

LEDGER_COLUMNS = (
    "date",
    "kind",
    "stage",
    "actor",
    "counterparty",
    "claim",
    "value",
    "unit",
    "source_url",
    "source",
    "confidence",
    "maps_to",
    "note",
)
KINDS = ("contract", "buildout", "energy", "financing", "policy", "statement", "price")
CONFIDENCE = ("confirmed", "reported", "speculated")


def _known_stages(stack_dir: Path) -> set[str]:
    """The stage keys in ``stack/stages.csv``; empty when there is no stack yet.

    Read the way ``data/stack.py`` reads it (a BOM and padded header cells are tolerated), so a
    stack the stack loader accepts is never rejected here. A stages file without a ``stage``
    column is a malformed stack, reported as a ValueError like every other bad input.
    """
    path = stack_dir / "stages.csv"
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return set()
    header = [cell.strip() for cell in rows[0]]
    if "stage" not in header:
        raise ValueError(
            f"{path.name}: no 'stage' column, so the ledger's stages cannot be checked"
        )
    column = header.index("stage")
    return {row[column].strip() for row in rows[1:] if len(row) > column and row[column].strip()}


def load_signals(
    directory: Path = SIGNALS_DIR, stack_dir: Path | None = None
) -> pd.DataFrame | None:
    """Read and check the ledger; ``None`` when there is no ledger yet.

    Malformed rows are errors, not warnings: a signal with no source or an unknown confidence
    is exactly the kind of thing the ledger exists to keep out.
    """
    path = Path(directory) / "ledger.csv"
    if not path.exists():
        return None
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    missing = [c for c in LEDGER_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")
    frame = frame[list(LEDGER_COLUMNS)].copy()
    problems: list[str] = []
    bad_date = frame.loc[~frame["date"].str.fullmatch(r"\d{4}-\d{2}-\d{2}"), "claim"].tolist()
    if bad_date:
        problems.append(f"date must be YYYY-MM-DD for {bad_date}")
    for column, allowed in (("kind", KINDS), ("confidence", CONFIDENCE)):
        bad = frame.loc[~frame[column].isin(allowed), "claim"].tolist()
        if bad:
            problems.append(f"{column} must be one of {allowed} for {bad}")
    stages = _known_stages(REPO_ROOT / "stack" if stack_dir is None else Path(stack_dir))
    if stages:
        bad = frame.loc[~frame["stage"].isin(stages), "claim"].tolist()
        if bad:
            problems.append(f"unknown stage for {bad}")
    unsourced = frame.loc[~frame["source_url"].str.startswith("http"), "claim"].tolist()
    if unsourced:
        problems.append(f"source_url must start with http for {unsourced}")
    values = pd.to_numeric(frame["value"].replace("", None), errors="coerce")
    bad_value = frame.loc[values.isna() & (frame["value"] != ""), "claim"].tolist()
    if bad_value:
        problems.append(f"value must be a plain number or blank for {bad_value}")
    frame["value"] = values.astype("float64")
    if frame["claim"].eq("").any():
        problems.append("every row needs a claim")
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    return frame.sort_values("date", ascending=False, kind="stable").reset_index(drop=True)


def signals_for_stage(signals: pd.DataFrame | None, stage: str) -> pd.DataFrame:
    """The ledger rows for one stage, newest first; empty frame when there are none."""
    if signals is None:
        return pd.DataFrame(columns=list(LEDGER_COLUMNS))
    return signals[signals["stage"] == stage].reset_index(drop=True)

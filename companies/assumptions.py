"""The assumptions register: every number the models rely on that is not a reported fact.

Why a register
--------------
A model is arithmetic applied to assumptions. The arithmetic can be checked by anyone; the
assumptions are where judgement lives and where a model is won or lost. So they are kept out
of the code, in one CSV per company (``assumptions/<TICKER>.csv``) that opens in Excel, and
each row says where the number came from and who has signed it off.

Columns
-------
``name``    the variable, in snake_case; engine inputs use the engine's field names
``value``   the number used
``unit``    unit label (drives number formats on the workbook)
``low`` / ``high``  the range the value could plausibly sit in, for sensitivities; may be blank
``basis``   how the value was obtained, one of:
            ``disclosed``  stated in a filing (source gives the page)
            ``derived``    computed from disclosed figures (note gives the arithmetic)
            ``external``   from a third-party source (source gives the link)
            ``judgment``   no good source; a reasoned choice
``source``  filing page or link
``status``  ``proposed`` (Claude's suggestion), ``confirmed`` or ``overridden`` (Philbert's
            decision). The model runs on proposed values; the workbook shows the status so a
            reader can see which numbers have been signed off.
``note``    the reasoning, the arithmetic, and the caveats, in a sentence or two
"""

import math
from pathlib import Path

import pandas as pd

from data import ASSUMPTIONS_DIR
from engine.unit_economics import INPUT_FIELDS, GPUEconomicsInputs

ASSUMPTION_COLUMNS = ("name", "value", "unit", "low", "high", "basis", "source", "status", "note")
BASES = ("disclosed", "derived", "external", "judgment")
STATUSES = ("proposed", "confirmed", "overridden")
# Columns of the workbook's Inputs sheet (scripts/export_xlsx.py INPUT_COLUMNS).
_INPUT_COLUMNS = ("name", "value", "unit", "source", "note")


def assumptions_path(ticker: str, directory: Path = ASSUMPTIONS_DIR) -> Path:
    """Where a company's register lives: ``<directory>/<TICKER>.csv``."""
    return Path(directory) / f"{ticker.upper()}.csv"


def load_assumptions(ticker: str, directory: Path = ASSUMPTIONS_DIR) -> pd.DataFrame | None:
    """Read and check a company's register; ``None`` when the company has no register yet.

    Raises ``ValueError`` naming the row when the file is malformed, because a silently
    skipped assumption is worse than a loud failure.
    """
    path = assumptions_path(ticker, directory)
    if not path.exists():
        return None
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    missing = [c for c in ASSUMPTION_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")
    frame = frame[list(ASSUMPTION_COLUMNS)].copy()
    problems: list[str] = []
    if frame["name"].duplicated().any():
        problems.append(f"duplicate names {sorted(frame.loc[frame['name'].duplicated(), 'name'])}")
    for column in ("value", "low", "high"):
        numbers = pd.to_numeric(frame[column].replace("", None), errors="coerce")
        bad = frame.loc[numbers.isna() & (frame[column] != ""), "name"].tolist()
        if bad:
            problems.append(f"non-numeric {column} for {bad}")
        frame[column] = numbers.astype("float64")
    no_value = frame.loc[frame["value"].isna(), "name"].tolist()
    if no_value:
        problems.append(f"no value for {no_value}")
    for column, allowed in (("basis", BASES), ("status", STATUSES)):
        bad = frame.loc[~frame[column].isin(allowed), "name"].tolist()
        if bad:
            problems.append(f"{column} must be one of {allowed} for {bad}")
    unsourced = frame.loc[
        frame["basis"].isin(("disclosed", "derived", "external")) & (frame["source"] == ""),
        "name",
    ].tolist()
    if unsourced:
        problems.append(f"basis needs a source for {unsourced}")
    out_of_range = [
        row.name_
        for row in frame.rename(columns={"name": "name_"}).itertuples()
        if (not math.isnan(row.low) and row.value < row.low)
        or (not math.isnan(row.high) and row.value > row.high)
    ]
    if out_of_range:
        problems.append(f"value outside its low/high range for {out_of_range}")
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    return frame


def to_inputs_frame(assumptions: pd.DataFrame) -> pd.DataFrame:
    """The register as the workbook's Inputs sheet: name, value, unit, source, note.

    Basis, status and range are folded into the note so a finance reader sees, next to each
    blue number, how solid it is and whether it has been signed off.
    """
    rows = []
    for row in assumptions.itertuples(index=False):
        tag = f"[{row.basis}; {row.status}"
        if not math.isnan(row.low) or not math.isnan(row.high):
            low = "" if math.isnan(row.low) else f"{row.low:g}"
            high = "" if math.isnan(row.high) else f"{row.high:g}"
            tag += f"; range {low} to {high}"
        rows.append(
            {
                "name": row.name,
                "value": float(row.value),
                "unit": row.unit,
                "source": row.source,
                "note": f"{tag}] {row.note}".strip(),
            }
        )
    return pd.DataFrame(rows, columns=list(_INPUT_COLUMNS))


def engine_inputs(assumptions: pd.DataFrame) -> GPUEconomicsInputs:
    """Build the engine's inputs from the rows whose names are engine fields.

    Optional engine fields that the register omits keep the engine's defaults; a missing
    REQUIRED field is an error that names it.
    """
    values = {
        row.name: float(row.value)
        for row in assumptions.itertuples(index=False)
        if row.name in INPUT_FIELDS
    }
    try:
        return GPUEconomicsInputs(**values)
    except TypeError as exc:  # a required field is absent
        raise ValueError(f"assumptions register is missing engine inputs: {exc}") from exc


def unconfirmed(assumptions: pd.DataFrame) -> list[str]:
    """Names still at ``proposed``: the decisions waiting for Philbert."""
    return assumptions.loc[assumptions["status"] == "proposed", "name"].tolist()

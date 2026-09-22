"""Disclosed KPIs: numbers a company states in words, which SEC's structured data does not carry.

Revenue and capex arrive as tagged facts (``data/edgar.py``). Operating KPIs such as active
power, contracted power and revenue backlog do not: they appear in the annual report's prose
and in the earnings press release attached to each quarter's 8-K. They are read from those
documents into ``data/disclosed/<TICKER>.csv``, one row per figure, each pointing at the filing
it came from. Every row was checked against the sentence that states it before it was admitted;
the sentences themselves are kept outside the repository.

Columns: ``period`` (``2025Q3``), ``kpi``, ``value``, ``unit``, ``qualifier`` (the company's own
hedge: "approximately", "more than", or blank), ``form``, ``filed``, ``accession``, ``page``
and ``url``.
The qualifier matters: "more than 850 MW" is a floor, not a measurement.
"""

from pathlib import Path

import pandas as pd

from data import DISCLOSED_DIR

DISCLOSED_COLUMNS = (
    "period",
    "kpi",
    "value",
    "unit",
    "qualifier",
    "form",
    "filed",
    "accession",
    "page",
    "url",
)


def load_disclosed(ticker: str, directory: Path = DISCLOSED_DIR) -> pd.DataFrame | None:
    """Read a company's disclosed KPIs; ``None`` when it has none. Malformed files are errors."""
    path = Path(directory) / f"{ticker.upper()}.csv"
    if not path.exists():
        return None
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    missing = [c for c in DISCLOSED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{path.name}: missing columns {missing}")
    frame = frame[list(DISCLOSED_COLUMNS)].copy()
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    problems = []
    if frame["value"].isna().any():
        problems.append(f"non-numeric value in rows {frame.index[frame['value'].isna()].tolist()}")
    bad_period = frame.loc[~frame["period"].str.fullmatch(r"\d{4}(Q[1-4])?"), "period"].tolist()
    if bad_period:
        problems.append(f"period must look like 2025Q3 or 2025: {bad_period}")
    if frame.duplicated(["period", "kpi"]).any():
        dupes = frame.loc[frame.duplicated(["period", "kpi"]), ["period", "kpi"]].values.tolist()
        problems.append(f"duplicate period/kpi {dupes}")
    unsourced = frame.loc[frame["url"] == "", ["period", "kpi"]].values.tolist()
    if unsourced:
        problems.append(f"rows without a source url {unsourced}")
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    return frame


def disclosed_series(disclosed: pd.DataFrame | None, kpi: str) -> pd.Series:
    """One KPI as ``period -> value``, sorted by period; empty when the KPI is absent."""
    if disclosed is None:
        return pd.Series(dtype="float64")
    rows = disclosed[disclosed["kpi"] == kpi].sort_values("period")
    return pd.Series(rows["value"].to_numpy(), index=rows["period"].to_numpy(), dtype="float64")

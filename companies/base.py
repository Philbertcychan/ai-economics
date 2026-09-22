"""Shared plumbing for company models: the interface, data loading, frames and export.

Purpose
-------
Every company in this repository (CoreWeave, Nebius, later the chip and hyperscaler
lines) is modelled by a small class that turns *reported* SEC figures plus a handful of
*assumptions* into per-period *drivers* and *outputs*. This module holds everything those
classes share, so a company file contains only what is specific to that company: its
ticker and its driver logic. Its assumptions live in ``assumptions/<TICKER>.csv``, where each
value has a source and a sign-off status that Philbert owns.

There is no financial-model logic here. ``BaseCompanyModel.build`` raises
``NotImplementedError`` until a subclass implements it; the refresh pipeline reports such a
company as "pending" and still publishes its reported data.

Writing a company model
-----------------------
1. Subclass ``BaseCompanyModel`` and set ``ticker`` (``name``, ``cik`` and ``layer`` are
   filled in from ``data.edgar.COMPANIES``); optionally set ``engine_defaults``.
2. ``load_data()`` fills ``self.filings``, ``self.reported`` (tidy SEC facts, see
   ``data.edgar.facts_to_frame``) and ``self.as_of``; it needs no logic from you.
3. ``build()`` is yours: read ``self.reported`` / ``self.reported_series(concept)`` and
   the assumptions, then set ``self.drivers`` and ``self.outputs`` (and ``self.inputs``
   if the defaults are not enough).
4. ``to_frames()`` packages inputs, drivers, outputs (+ ``reported``) in the layout the
   exporter, the refresh pipeline and the site all read; ``to_xlsx(path)`` writes the
   workbook. That layout ("Frames contract") and the placeholder syntax for live Excel
   formulas (``{item}``, ``{item@prev}``, ``{drivers.item}``, ``{in.name}``) are documented
   at the top of ``scripts/export_xlsx.py``.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from companies.assumptions import load_assumptions, to_inputs_frame
from data import ASSUMPTIONS_DIR, DISCLOSED_DIR, PROCESSED_DIR
from data.disclosed import load_disclosed
from data.edgar import (
    COMPANIES,
    EdgarClient,
    Filing,
    calendar_series,
    facts_to_frame,
    load_processed_facts,
)
from engine.unit_economics import (
    INPUT_DESCRIPTIONS,
    INPUT_FIELDS,
    INPUT_UNITS,
    GPUEconomicsInputs,
)
from scripts.export_xlsx import INPUT_COLUMNS, export_workbook

# Order of the required keys in ``to_frames()``; extra keys (``reported``) follow.
FRAME_ORDER: tuple[str, ...] = ("inputs", "drivers", "outputs")

# ``filings.csv`` columns as written by the refresh pipeline (``Filing.to_row``). The offline
# loader reads them back; the last three are optional so a hand-made CSV still loads.
FILINGS_CSV_COLUMNS: tuple[str, ...] = (
    "accession",
    "form",
    "filing_date",
    "report_date",
    "primary_document",
    "url",
    "size",
    "is_xbrl",
)
_TRUE_STRINGS = frozenset({"1", "true", "yes", "y", "t"})


class ModelNotBuilt(RuntimeError):
    """``to_frames()`` or ``to_xlsx()`` was called before ``build()`` set drivers and outputs."""


@runtime_checkable
class CompanyModel(Protocol):
    """What the exporter, the refresh pipeline and the site need from any company model.

    ``BaseCompanyModel`` satisfies this; so does any duck-typed stand-in used in tests.
    """

    ticker: str
    name: str
    cik: str
    layer: str

    def load_data(self) -> None: ...

    def build(self) -> None: ...

    def to_frames(self) -> dict[str, pd.DataFrame]: ...

    def to_xlsx(self, path: Path | str) -> Path: ...


class BaseCompanyModel:
    """Shared state and plumbing for one company's model; concrete companies subclass it.

    Class attributes ``ticker``, ``name``, ``cik`` and ``layer`` identify the company. A
    subclass only has to set ``ticker``: when it is one of ``data.edgar.COMPANIES``, the other
    three are copied from there at class-creation time so the registry stays the single
    source of truth. A subclass for a company outside that registry sets all four itself.

    Assumptions come from the register ``assumptions/<TICKER>.csv`` (see
    ``companies/assumptions.py``). ``engine_defaults`` is an older, in-code alternative kept
    for tests and quick experiments; the register wins whenever it exists.
    """

    ticker: str
    name: str
    cik: str
    layer: str
    engine_defaults: GPUEconomicsInputs | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        info = COMPANIES.get(getattr(cls, "ticker", ""))
        if info is None:
            return
        # Only fill what the subclass did not set itself, so an override still wins.
        for attribute in ("name", "cik", "layer"):
            if not hasattr(cls, attribute):
                setattr(cls, attribute, getattr(info, attribute))

    def __init__(
        self,
        edgar: EdgarClient | None = None,
        *,
        processed_dir: Path = PROCESSED_DIR,
        assumptions_dir: Path = ASSUMPTIONS_DIR,
        disclosed_dir: Path = DISCLOSED_DIR,
    ) -> None:
        """Create an empty model.

        Args:
            edgar: an ``EdgarClient`` - or any object with ``filings(ticker)`` and
                ``company_facts(ticker)`` - used by ``load_data()``. ``None`` means offline:
                ``load_data()`` reads the processed CSVs instead, or leaves the model empty.
            processed_dir: root of the tidy CSVs (``<processed_dir>/<TICKER>/reported.csv``
                and ``filings.csv``); tests point this at a temporary directory.
            assumptions_dir: folder of the assumptions registers (``<TICKER>.csv``); when a
                register exists it becomes the Inputs sheet. See ``companies/assumptions.py``.
        """
        self.edgar = edgar
        self.processed_dir = Path(processed_dir)
        self.assumptions_dir = Path(assumptions_dir)
        self.disclosed_dir = Path(disclosed_dir)
        self.filings: list[Filing] = []
        self.reported: pd.DataFrame | None = None
        self.disclosed: pd.DataFrame | None = None  # KPIs stated in prose; data/disclosed.py
        self.inputs: pd.DataFrame | None = None
        self.drivers: pd.DataFrame | None = None
        self.outputs: pd.DataFrame | None = None
        self.as_of: str | None = None

    # -- data ---------------------------------------------------------------------------

    def load_data(self) -> None:
        """Fill ``filings``, ``reported``, ``disclosed`` and ``as_of``; missing data is allowed.

        With an EDGAR client the filings and XBRL facts come from it (live or from its own
        cache). Without one, the processed CSVs written by ``scripts/refresh.py`` are read if
        they exist; if they do not, the model simply stays empty so ``build()`` and
        ``summary()`` can still run (as ``model pending`` / no data).
        """
        if self.edgar is not None:
            self.filings = _coerce_filings(self.cik, self.edgar.filings(self.ticker))
            self.reported = facts_to_frame(self.edgar.company_facts(self.ticker))
        else:
            self.filings = self._read_filings_csv()
            self.reported = load_processed_facts(self.ticker, self.processed_dir)
        self.disclosed = load_disclosed(self.ticker, self.disclosed_dir)
        self.filings.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
        # ISO dates sort lexicographically, so the newest filing date is the max string.
        self.as_of = max((f.filing_date for f in self.filings if f.filing_date), default=None)

    def _read_filings_csv(self) -> list[Filing]:
        path = self.processed_dir / self.ticker.upper() / "filings.csv"
        if not path.is_file():
            return []
        with open(path, encoding="utf-8", newline="") as fh:
            return [_filing_from_row(self.cik, row) for row in csv.DictReader(fh)]

    def reported_series(self, concept: str, freq: str = "Q") -> pd.DataFrame:
        """Calendar series (``period, end, val, derived``) for one reported concept.

        A thin wrapper over ``data.edgar.calendar_series``; returns an empty frame with the
        same columns when nothing has been loaded, so callers need no ``None`` checks.
        """
        facts = self.reported if self.reported is not None else facts_to_frame({})
        return calendar_series(facts, concept, freq)

    def latest_filing(self) -> Filing | None:
        """The most recent loaded filing, or ``None`` before ``load_data()`` / with no filings."""
        if not self.filings:
            return None
        return max(self.filings, key=lambda f: (f.filing_date, f.accession))

    # -- frames -------------------------------------------------------------------------

    def default_inputs(self) -> pd.DataFrame:
        """Inputs frame built from ``engine_defaults``; empty (correct columns) when it is None.

        One row per ``GPUEconomicsInputs`` field, in field order, with the unit and description
        the engine publishes, so the workbook's Inputs sheet and the defined names ``in_<name>``
        line up with the engine's vocabulary. Subclasses that need more assumptions than the
        engine's nine, or want to cite a source per row, override this or set ``self.inputs``
        in ``build()``.
        """
        register = load_assumptions(self.ticker, self.assumptions_dir)
        if register is not None:
            # The register carries a source and a sign-off status per row, which the bare
            # engine defaults cannot, so it wins whenever it exists.
            return to_inputs_frame(register)
        rows: list[dict[str, Any]] = []
        if self.engine_defaults is not None:
            values = self.engine_defaults.to_dict()
            source = f"{type(self).__name__}.engine_defaults"
            for name in INPUT_FIELDS:
                rows.append(
                    {
                        "name": name,
                        "value": float(values[name]),
                        "unit": INPUT_UNITS[name],
                        "source": source,
                        "note": INPUT_DESCRIPTIONS[name],
                    }
                )
        return pd.DataFrame(rows, columns=list(INPUT_COLUMNS))

    def build(self) -> None:
        """Populate ``self.drivers`` and ``self.outputs`` (and optionally ``self.inputs``).

        Subclasses override this; the base raises so an unfinished model is reported as
        pending rather than exporting an empty workbook.
        """
        raise NotImplementedError(f"{type(self).__name__}.build() is not implemented")

    @property
    def is_built(self) -> bool:
        """True once ``build()`` has set both ``drivers`` and ``outputs``."""
        return self.drivers is not None and self.outputs is not None

    def to_frames(self) -> dict[str, pd.DataFrame]:
        """Frames for the exporter: ``inputs``, ``drivers``, ``outputs`` [+ ``reported``].

        The layout is the "Frames contract" section at the top of ``scripts/export_xlsx.py``.

        Raises:
            ModelNotBuilt: ``build()`` has not populated ``drivers`` and ``outputs`` yet.
        """
        if not self.is_built:
            raise ModelNotBuilt(
                f"{self.ticker}: call build() before to_frames(); drivers/outputs are not set"
            )
        assert self.drivers is not None and self.outputs is not None  # narrowed by is_built
        frames: dict[str, pd.DataFrame] = {
            "inputs": self.inputs if self.inputs is not None else self.default_inputs(),
            "drivers": self.drivers,
            "outputs": self.outputs,
        }
        if self.reported is not None:
            frames["reported"] = self.reported
        return frames

    def to_xlsx(self, path: Path | str) -> Path:
        """Export the model to an Excel workbook at ``path`` and return the path.

        Raises:
            ModelNotBuilt: see ``to_frames()``.
        """
        return export_workbook(
            self.to_frames(),
            path,
            title=f"{self.name} ({self.ticker}) operating model",
            subtitle=(
                "Reported figures come from SEC EDGAR XBRL company facts; every assumption is "
                "on the Inputs sheet."
            ),
            meta={
                "as_of": self.as_of or "no filings loaded",
                "cik": self.cik,
                "layer": self.layer,
            },
        )

    def summary(self) -> dict[str, Any]:
        """One-line status for the dashboard and the refresh diff.

        Shape: ``{ticker, name, layer, cik, as_of, model_status, latest_filing}`` where
        ``model_status`` is ``"built"`` or ``"pending"`` and ``latest_filing`` is
        ``{form, date, url}`` or ``None``.
        """
        latest = self.latest_filing()
        return {
            "ticker": self.ticker,
            "name": self.name,
            "layer": self.layer,
            "cik": self.cik,
            "as_of": self.as_of,
            "model_status": "built" if self.is_built else "pending",
            "latest_filing": (
                {"form": latest.form, "date": latest.filing_date, "url": latest.url}
                if latest is not None
                else None
            ),
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(ticker={self.ticker!r}, as_of={self.as_of!r})"


# -- filings helpers -------------------------------------------------------------------------


def _coerce_filings(cik: str, filings: Iterable[Any]) -> list[Filing]:
    """Accept ``Filing`` objects or ``filings.csv``-shaped mappings from a duck-typed client."""
    return [f if isinstance(f, Filing) else _filing_from_row(cik, f) for f in filings]


def _filing_from_row(cik: str, row: Mapping[str, Any]) -> Filing:
    """Rebuild a ``Filing`` from one ``filings.csv`` row (the inverse of ``Filing.to_row``).

    ``url`` is not read back: it is derived from the CIK, accession and document, and the
    company CIK is the one thing the CSV does not carry.
    """
    size = _clean(row.get("size"))
    report_date = _clean(row.get("report_date"))
    return Filing(
        cik=cik,
        accession=str(row["accession"]).strip(),
        form=str(row.get("form", "")).strip(),
        filing_date=str(row.get("filing_date", "")).strip(),
        report_date=report_date,
        primary_document=str(row.get("primary_document", "") or "").strip(),
        description=_clean(row.get("description")),
        size=int(float(size)) if size is not None else None,
        is_xbrl=str(row.get("is_xbrl", "")).strip().lower() in _TRUE_STRINGS,
    )


def _clean(value: Any) -> str | None:
    """Empty strings, ``None`` and NaN (pandas' CSV blanks) all mean "not reported"."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ("", "nan", "None", "<NA>") else text

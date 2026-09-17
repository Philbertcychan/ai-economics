"""Daily refresh: pull SEC data, re-export changed models, rebuild the site, write a diff.

Purpose
-------
This is the one command that keeps the repository current::

    uv run scripts/refresh.py                                   # fetch, export, site, diff
    uv run scripts/refresh.py --tickers CRWV,NBIS --since 2025-06-01 --no-download
    uv run scripts/refresh.py --dry-run                         # print the plan, touch nothing

For every tracked company (``data.edgar.COMPANIES``, optionally narrowed with ``--tickers``) it

1. pulls the filing index and writes ``data/processed/<TICKER>/filings.csv``, noting which
   accessions were not there last time;
2. pulls the XBRL company facts and writes the tidy ``reported.csv``;
3. downloads the primary document of every filing filed on or after ``--since`` that is not
   in the raw cache yet - not only the *new* ones, so a download that failed yesterday is
   retried today (``--no-download`` in CI: raw documents are gitignored, so fetching them
   there is waste);
4. writes the raw-cache manifest (sha256 per file) so every number is traceable to bytes SEC
   served on a given day. Manifests are the one part of ``data/raw`` that is versioned; the CI
   workflow commits them together with the data change they document;
5. if a model class is registered in ``companies.REGISTRY``: loads, builds and exports it to
   ``models/<TICKER>.xlsx`` - but only when the frames' fingerprint changed, so an unchanged
   model never churns the committed workbook. A ``build()`` that is still a ``TODO(philbert)``
   is reported as ``pending``; companies tracked for their reported data only are ``no-model``;
6. writes ``site/data/<TICKER>.json`` (reported series plus model outputs) for the dashboard.

After the loop it writes ``site/data/companies.json`` (whose per-company ``model_status`` is
``built``, ``pending`` or ``no-model``; see ``SITE_MODEL_STATUS``), rebuilds ``site/build/``
and records the whole run in ``data/last_refresh_diff.md``. The CI workflow commits all of it
on the days when ``data/processed`` or ``models`` changed.

Design notes
------------
* Robustness over purity. Every step of every company runs in its own ``try``: a failure is
  logged, recorded in ``CompanyResult.error`` and the run carries on with the next step and
  the next company. Where yesterday's processed CSVs exist they stand in for a failed fetch,
  so a transient SEC outage never blanks the dashboard. The diff file is always written and
  the exit code is 1 when anything failed, so CI notices without losing the partial results.
* Idempotent. With unchanged upstream data a second run rewrites identical bytes everywhere
  except the diff file (which carries the run timestamp) and the ``as_of`` field of
  ``companies.json`` (which the site footer shows as "Last refresh"). The CI workflow
  therefore looks only at ``data/processed`` and ``models`` to decide whether to commit.
* Byte-stable output: UTF-8, LF line endings and ``index=False`` for every CSV and JSON, so a
  Windows laptop and the Linux runner commit the same files.
* No model logic. What a model computes lives in ``companies/``; this module only moves data.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from data import (
    CALLS_MD,
    LAST_REFRESH_DIFF,
    MODELS_DIR,
    PROCESSED_DIR,
    SITE_BUILD_DIR,
    SITE_DIR,
)
from data.edgar import (
    COMPANIES,
    INSTANT_CONCEPTS,
    STANDARD_CONCEPTS,
    CompanyInfo,
    EdgarClient,
    Filing,
    calendar_series,
    facts_to_frame,
    load_processed_facts,
)
from scripts.build_site import BuildReport
from scripts.build_site import build as build_site_pages
from scripts.export_xlsx import fingerprint

log = logging.getLogger(__name__)

DEFAULT_SINCE = "2025-01-01"

# Column order of processed/<TICKER>/filings.csv; identical to data.edgar.Filing.to_row().
FILINGS_COLUMNS: tuple[str, ...] = (
    "accession",
    "form",
    "filing_date",
    "report_date",
    "primary_document",
    "url",
    "size",
    "is_xbrl",
)
MODEL_STATUSES: tuple[str, ...] = ("no-model", "pending", "unchanged", "changed", "error")
# Pipeline status -> ``model_status`` in site/data/companies.json. The site knows three values:
#   "built"    - a model class is registered and build() produced outputs for the dashboard;
#   "pending"  - a model class is registered but build() is still a TODO. An errored model is
#                reported the same way because it has no trustworthy output to display;
#   "no-model" - the company is tracked for its reported data only (the site's "data only").
SITE_MODEL_STATUS: dict[str, str] = {
    "changed": "built",
    "unchanged": "built",
    "pending": "pending",
    "no-model": "no-model",
    "error": "pending",
}
# models/manifest.json: ticker -> {fingerprint, exported_at, as_of} of the last export.
MODELS_MANIFEST = "manifest.json"
OUTPUTS_ITEM_COLUMN = "item"  # first column of processed/<TICKER>/outputs.csv
MAX_LISTED_FILINGS = 20  # a first run would otherwise list decades of 10-Qs in the diff


# --------------------------------------------------------------------------------------------
# Configuration and results
# --------------------------------------------------------------------------------------------


@dataclass
class RefreshConfig:
    """What to refresh. ``tickers=None`` means every tracked company."""

    tickers: tuple[str, ...] | None = None
    since: str = DEFAULT_SINCE  # uncached documents of filings from this date on are downloaded
    download: bool = True
    build_site: bool = True
    dry_run: bool = False


@dataclass
class RefreshPaths:
    """Where the pipeline reads and writes. Tests point every field at a temporary directory."""

    processed_dir: Path = PROCESSED_DIR
    models_dir: Path = MODELS_DIR
    site_dir: Path = SITE_DIR
    site_build_dir: Path = SITE_BUILD_DIR
    diff_path: Path = LAST_REFRESH_DIFF
    calls_md: Path = CALLS_MD

    @property
    def site_data_dir(self) -> Path:
        # Derived, not configurable: the site builder reads its JSON from <site_dir>/data, so
        # a second, independent field could silently build a site with no companies.
        return self.site_dir / "data"


@dataclass
class CompanyResult:
    """Outcome of one company's refresh. ``error`` lists every step that failed, or is None."""

    ticker: str
    new_filings: list[Filing] = field(default_factory=list)
    total_filings: int = 0
    facts_rows: int = 0
    downloaded: list[Path] = field(default_factory=list)
    model_status: str = "no-model"  # one of MODEL_STATUSES
    changed_items: list[str] = field(default_factory=list)
    error: str | None = None
    # What companies.json needs per company. Defaults keep ``CompanyResult(ticker=...)`` valid
    # for callers that only care about the pipeline outcome above.
    as_of: str | None = None  # latest filing date
    latest_filing: dict[str, str] | None = None  # {form, date, url}
    has_data: bool = False  # at least one reported series reached the site JSON


@dataclass
class RefreshResult:
    """Outcome of a whole run; ``errors`` is empty exactly when the exit code is 0."""

    started_at: str
    finished_at: str
    companies: list[CompanyResult]
    site: BuildReport | None
    errors: list[str]


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def utc_iso(moment: datetime) -> str:
    """Format a datetime as ``2026-09-12T11:00:00Z``; a naive value is taken to be UTC already."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_scalar(value: Any) -> Any:
    """A pandas/numpy scalar as plain JSON: missing values become ``None``, dates ISO strings."""
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> Any:
    """Parsed JSON, or ``None`` when the file is missing or unreadable (logged, never fatal)."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return None


def _write_csv(table: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")


def _record_error(result: CompanyResult, step: str, exc: BaseException) -> None:
    """Log a failed step and append it to ``result.error``; the run itself carries on."""
    message = f"{step}: {type(exc).__name__}: {exc}"
    log.error("%s: %s", result.ticker, message, exc_info=log.isEnabledFor(logging.DEBUG))
    result.error = message if result.error is None else f"{result.error}; {message}"


# --------------------------------------------------------------------------------------------
# Filings (step 1 and 3)
# --------------------------------------------------------------------------------------------


def _newest_first(filings: list[Filing]) -> list[Filing]:
    # ISO dates sort lexicographically; the accession breaks ties so the order is stable.
    return sorted(filings, key=lambda f: (f.filing_date, f.accession), reverse=True)


def read_filings_csv(path: Path, cik: str) -> list[Filing]:
    """Rebuild ``Filing`` objects from ``filings.csv``, newest first.

    Returns ``[]`` when the file is absent or unreadable. The CSV does not carry the CIK (it is
    the same on every row), so the caller supplies it.
    """
    if not path.is_file():
        return []
    try:
        table = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return []
    filings = []
    for row in table.to_dict("records"):
        size = str(row.get("size", "")).strip()
        filings.append(
            Filing(
                cik=cik,
                accession=str(row.get("accession", "")),
                form=str(row.get("form", "")),
                filing_date=str(row.get("filing_date", "")),
                report_date=str(row.get("report_date", "")) or None,
                primary_document=str(row.get("primary_document", "")),
                description=None,
                size=int(float(size)) if size else None,
                is_xbrl=str(row.get("is_xbrl", "")).lower() == "true",
            )
        )
    return _newest_first(filings)


def _refresh_filings(
    client: EdgarClient, info: CompanyInfo, company_dir: Path
) -> tuple[list[Filing], list[Filing]]:
    """Fetch the filing index, rewrite ``filings.csv`` and return ``(all, new)`` newest first."""
    filings = _newest_first(list(client.filings(info.ticker)))
    known = {f.accession for f in read_filings_csv(company_dir / "filings.csv", info.cik)}
    new = [f for f in filings if f.accession not in known]
    table = pd.DataFrame([f.to_row() for f in filings], columns=list(FILINGS_COLUMNS))
    # Nullable integer so a missing size is written blank rather than turning the column float.
    table["size"] = pd.to_numeric(table["size"], errors="coerce").astype("Int64")
    _write_csv(table, company_dir / "filings.csv")
    return filings, new


def _latest_filing_entry(filings: list[Filing]) -> dict[str, str] | None:
    """``{form, date, url}`` of the newest filing, the shape ``companies.json`` shows."""
    if not filings:
        return None
    latest = filings[0]  # callers pass newest-first lists
    return {"form": latest.form, "date": latest.filing_date, "url": latest.url}


def _download_missing(
    client: EdgarClient, ticker: str, filings: list[Filing], since: str, result: CompanyResult
) -> None:
    """Download the primary document of every filing since ``since`` that is not cached yet.

    The candidates are all tracked filings, not just the new ones: ``filings.csv`` already
    lists a filing whose download failed, so "new" alone would never retry it. One bad
    document does not stop the rest.
    """
    for filing in filings:
        if filing.filing_date < since:
            continue
        try:
            if client.cached_filing_path(ticker, filing) is None:
                result.downloaded.append(client.download_filing(ticker, filing))
        except Exception as exc:  # any failure is recorded, never raised
            _record_error(result, f"download {filing.accession}", exc)


# --------------------------------------------------------------------------------------------
# Model export (step 5)
# --------------------------------------------------------------------------------------------


def changed_line_items(previous: pd.DataFrame | None, current: pd.DataFrame) -> list[str]:
    """Line items whose values differ between two finance-layout frames (index = item).

    Items new in ``current`` count as changed, so a first export lists every item; items that
    disappeared are appended. Values are compared per period label and NaN-aware; dtype and
    column order do not matter, so a frame read back from ``outputs.csv`` equals its source.
    """

    def rows(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
        periods = [str(period) for period in frame.columns]
        return {
            str(item): dict(zip(periods, map(_json_scalar, values), strict=True))
            for item, values in zip(
                frame.index, frame.itertuples(index=False, name=None), strict=True
            )
        }

    before = rows(previous) if previous is not None else {}
    after = rows(current)
    changed = [item for item, values in after.items() if before.get(item) != values]
    changed.extend(item for item in before if item not in after)
    return changed


def _read_outputs_csv(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    try:
        return pd.read_csv(path, index_col=OUTPUTS_ITEM_COLUMN, encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return None


def _write_outputs_csv(outputs: pd.DataFrame, path: Path) -> None:
    """Values only (no labels/units): the file exists so the next run can name what changed."""
    table = outputs.copy()
    table.columns = [str(period) for period in table.columns]
    table.index = pd.Index([str(item) for item in table.index], name=OUTPUTS_ITEM_COLUMN)
    _write_csv(table.reset_index(), path)


def _refresh_model(
    model_cls: type,
    client: EdgarClient,
    ticker: str,
    paths: RefreshPaths,
    result: CompanyResult,
    exported_at: str,
) -> dict[str, pd.DataFrame] | None:
    """Build the model and export it when its frames changed. Returns the frames, or None.

    Sets ``result.model_status`` to ``pending`` (build() not written), ``unchanged`` (same
    fingerprint as the last export and the workbook still exists) or ``changed`` (exported).
    """
    model = model_cls(edgar=client)
    model.load_data()
    try:
        model.build()
    except NotImplementedError as exc:
        log.info("%s: model pending - %s", ticker, exc)
        result.model_status = "pending"
        return None
    frames = model.to_frames()
    digest = fingerprint(frames)

    manifest_path = paths.models_dir / MODELS_MANIFEST
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        manifest = {}
    previous = manifest.get(ticker)
    if not isinstance(previous, dict):
        previous = {}
    workbook = paths.models_dir / f"{ticker}.xlsx"
    if previous.get("fingerprint") == digest and workbook.is_file():
        result.model_status = "unchanged"
        return frames

    model.to_xlsx(workbook)
    outputs_path = paths.processed_dir / ticker / "outputs.csv"
    result.changed_items = changed_line_items(_read_outputs_csv(outputs_path), frames["outputs"])
    _write_outputs_csv(frames["outputs"], outputs_path)
    # The manifest goes last: if anything above failed, the next run simply retries the export.
    manifest[ticker] = {
        "fingerprint": digest,
        "exported_at": exported_at,
        "as_of": getattr(model, "as_of", None),
    }
    _write_json(manifest_path, dict(sorted(manifest.items())))
    result.model_status = "changed"
    log.info("%s: model changed, exported %s", ticker, workbook)
    return frames


# --------------------------------------------------------------------------------------------
# Site JSON (step 6)
# --------------------------------------------------------------------------------------------


def site_series(facts: pd.DataFrame, concept: str) -> tuple[str, pd.DataFrame]:
    """``(freq, calendar series)`` the dashboard shows for one concept: "Q" where it can.

    A chart labelled quarterly must not be annual or partial data in disguise, so:

    * an instant concept whose only points are Q4 balances belongs to an annual (20-F) filer
      and is published as the year-end series, like that filer's flow concepts;
    * a flow concept falls back to annual when the quarterly series stops short of the latest
      annual figure or covers fewer calendar years than the annual one. Years before the
      first quarterly point do not count against it: a 10-K's comparatives reach further back
      than a 10-Q's, and without this every filer would flip to annual for the weeks between
      its 10-K and its next 10-Q.
    """
    quarterly = calendar_series(facts, concept, "Q")
    annual = calendar_series(facts, concept, "A")
    if quarterly.empty or annual.empty:
        return ("A", annual) if quarterly.empty else ("Q", quarterly)
    if concept in INSTANT_CONCEPTS:
        only_year_ends = all(str(period).endswith("Q4") for period in quarterly["period"])
        return ("A", annual) if only_year_ends else ("Q", quarterly)
    quarter_years = {str(period)[:4] for period in quarterly["period"]}
    first_year = min(quarter_years)
    annual_years = {str(period) for period in annual["period"] if str(period) >= first_year}
    as_recent = max(quarterly["end"]) >= max(annual["end"])  # ISO dates compare as strings
    if as_recent and len(quarter_years) >= len(annual_years):
        return "Q", quarterly
    return "A", annual


def reported_payload(facts: pd.DataFrame | None) -> dict[str, dict[str, Any]]:
    """``{concept: {unit, freq, tag, points: [[period, value], ...]}}`` for the dashboard.

    Every ``STANDARD_CONCEPTS`` key with data is included, at the frequency ``site_series``
    picks (a 20-F filer such as Nebius has no quarterly frames, so all of its series are
    annual). Points are sorted by period; missing values are ``null``.
    """
    if facts is None or facts.empty:
        return {}
    reported: dict[str, dict[str, Any]] = {}
    for concept in STANDARD_CONCEPTS:
        rows = facts[facts["concept"] == concept]
        if rows.empty:
            continue
        freq, series = site_series(facts, concept)
        if series.empty:
            continue
        points = sorted(
            (
                [str(period), _json_scalar(value)]
                for period, value in zip(series["period"], series["val"], strict=True)
            ),
            key=lambda point: point[0],
        )
        first = rows.iloc[0]
        reported[concept] = {
            "unit": str(first["unit"]),
            "freq": freq,
            # Which XBRL element the series is, so a reader can check it against the filing.
            "tag": f"{first['taxonomy']}:{first['tag']}",
            "points": points,
        }
    return reported


def outputs_payload(outputs: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """``{item: {label, unit, points: [[period, value], ...]}}`` from a finance-layout frame."""
    labels: Mapping[str, str] = outputs.attrs.get("labels", {})
    units: Mapping[str, str] = outputs.attrs.get("units", {})
    periods = [str(period) for period in outputs.columns]
    payload: dict[str, dict[str, Any]] = {}
    rows = outputs.itertuples(index=False, name=None)
    for item, values in zip(outputs.index, rows, strict=True):
        name = str(item)
        payload[name] = {
            "label": str(labels.get(name, name)),
            "unit": str(units.get(name, "")),
            "points": [
                [period, _json_scalar(value)] for period, value in zip(periods, values, strict=True)
            ],
        }
    return payload


def inputs_payload(inputs: pd.DataFrame) -> list[dict[str, Any]]:
    """One ``{name, value, unit, source, note}`` per assumption row."""
    return [
        {str(key): _json_scalar(value) for key, value in row.items()}
        for row in inputs.to_dict("records")
    ]


def _company_payload(
    info: CompanyInfo,
    result: CompanyResult,
    reported: dict[str, dict[str, Any]],
    frames: dict[str, pd.DataFrame] | None,
) -> dict[str, Any]:
    return {
        "ticker": info.ticker,
        "name": info.name,
        "layer": info.layer,
        "cik": info.cik,
        "as_of": result.as_of,
        "reported": reported,
        "outputs": outputs_payload(frames["outputs"]) if frames is not None else None,
        "inputs": inputs_payload(frames["inputs"]) if frames is not None else None,
    }


def _companies_payload(
    as_of: str, selected: Mapping[str, CompanyInfo], results: list[CompanyResult]
) -> dict[str, Any]:
    by_ticker = {result.ticker: result for result in results}
    entries = []
    for ticker, info in selected.items():
        result = by_ticker.get(ticker, CompanyResult(ticker=ticker))
        entries.append(
            {
                "ticker": ticker,
                "name": info.name,
                "layer": info.layer,
                "cik": info.cik,
                "model_status": SITE_MODEL_STATUS.get(result.model_status, "pending"),
                "latest_filing": result.latest_filing,
                "has_data": result.has_data,
            }
        )
    return {"as_of": as_of, "companies": entries}


# --------------------------------------------------------------------------------------------
# One company
# --------------------------------------------------------------------------------------------


def refresh_company(
    info: CompanyInfo,
    *,
    client: EdgarClient,
    model_cls: type | None,
    config: RefreshConfig,
    paths: RefreshPaths,
    started_at: str,
) -> CompanyResult:
    """Run steps 1-6 for one company. Never raises: failures land in ``CompanyResult.error``.

    ``client`` may be any object with the ``EdgarClient`` methods used here (``filings``,
    ``company_facts``, ``cached_filing_path``, ``download_filing``, ``write_manifest``); tests
    inject a fake.
    """
    ticker = info.ticker
    result = CompanyResult(ticker=ticker)
    company_dir = paths.processed_dir / ticker
    company_dir.mkdir(parents=True, exist_ok=True)

    # 1. Filing index. On failure yesterday's CSV keeps as_of / latest filing on the site honest.
    # Broad excepts throughout: a bad response, a full disk or a model bug must all be recorded
    # for this company and must not stop the others. The exception type lands in the message.
    new: list[Filing] = []
    try:
        filings, new = _refresh_filings(client, info, company_dir)
    except Exception as exc:
        _record_error(result, "filings", exc)
        filings = read_filings_csv(company_dir / "filings.csv", info.cik)
    result.new_filings = new
    result.total_filings = len(filings)
    result.latest_filing = _latest_filing_entry(filings)
    result.as_of = result.latest_filing["date"] if result.latest_filing else None
    log.info("%s: %d filings tracked, %d new", ticker, len(filings), len(new))

    # 2. XBRL facts. On failure the previous reported.csv feeds the site JSON instead.
    facts: pd.DataFrame | None
    try:
        facts = facts_to_frame(client.company_facts(ticker))
        _write_csv(facts, company_dir / "reported.csv")
        result.facts_rows = len(facts)
    except Exception as exc:
        _record_error(result, "facts", exc)
        facts = load_processed_facts(ticker, paths.processed_dir)

    # 3. Documents not in the raw cache yet (skipped with --no-download). `new` only feeds the
    # diff; it is not the download list, or a failed download would never be retried.
    if config.download:
        _download_missing(client, ticker, filings, config.since, result)

    # 4. Raw-cache manifest.
    try:
        client.write_manifest(ticker)
    except Exception as exc:
        _record_error(result, "manifest", exc)

    # 5. Model export, only for companies with a registered model class.
    frames: dict[str, pd.DataFrame] | None = None
    if model_cls is None:
        result.model_status = "no-model"
    else:
        try:
            frames = _refresh_model(model_cls, client, ticker, paths, result, started_at)
        except Exception as exc:
            _record_error(result, "model", exc)
            result.model_status = "error"

    # 6. Dashboard JSON.
    try:
        reported = reported_payload(facts)
        result.has_data = bool(reported)
        _write_json(
            paths.site_data_dir / f"{ticker}.json",
            _company_payload(info, result, reported, frames),
        )
    except Exception as exc:
        _record_error(result, "site json", exc)
    return result


# --------------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------------


def _select_companies(
    companies: Mapping[str, CompanyInfo], tickers: tuple[str, ...] | None
) -> dict[str, CompanyInfo]:
    """The companies to refresh, in request order; unknown tickers raise before anything runs."""
    if tickers is None:
        return dict(companies)
    selected = {}
    for ticker in tickers:
        key = ticker.upper()
        if key not in companies:
            raise KeyError(f"unknown ticker {ticker!r}; known: {', '.join(companies)}")
        selected[key] = companies[key]
    return selected


def _load_registry() -> Mapping[str, type]:
    # Imported lazily: ``companies`` pulls in the engine and the exporter, and this module must
    # stay importable (and --dry-run cheap) even while those are being worked on.
    from companies import REGISTRY

    return REGISTRY


def _plan(
    config: RefreshConfig,
    selected: Mapping[str, CompanyInfo],
    registry: Mapping[str, type],
    paths: RefreshPaths,
) -> str:
    """Human-readable dry-run plan."""
    tickers = ", ".join(
        f"{ticker} ({'model' if ticker in registry else 'data only'})" for ticker in selected
    )
    lines = [
        "Dry run: nothing is fetched and nothing is written.",
        f"  tickers:            {tickers}",
        f"  since:              {config.since} (uncached documents of filings from this date on)",
        f"  download documents: {'yes' if config.download else 'no'}",
        f"  build site:         {'yes' if config.build_site else 'no'}",
        f"  processed dir:      {paths.processed_dir}",
        f"  models dir:         {paths.models_dir}",
        f"  site data dir:      {paths.site_data_dir}",
        f"  site build dir:     {paths.site_build_dir}",
        f"  diff:               {paths.diff_path}",
    ]
    return "\n".join(lines)


def run(
    config: RefreshConfig,
    *,
    client: EdgarClient | None = None,
    registry: Mapping[str, type] | None = None,
    companies: Mapping[str, CompanyInfo] | None = None,
    paths: RefreshPaths | None = None,
    now: Callable[[], datetime] | None = None,
) -> RefreshResult:
    """Refresh every selected company, then the site and the diff file.

    Args:
        config: what to refresh; see ``RefreshConfig``.
        client: EDGAR client (a real one is created when omitted); tests inject a fake.
        registry: ticker -> model class; defaults to ``companies.REGISTRY``.
        companies: ticker -> ``CompanyInfo``; defaults to ``data.edgar.COMPANIES``.
        paths: where to read and write; defaults to the repository layout.
        now: clock returning an aware UTC datetime; injected by tests for stable timestamps.

    Raises:
        KeyError: ``config.tickers`` names a company that is not tracked. Everything else is
        caught per step and reported in the result.
    """
    clock = now or (lambda: datetime.now(UTC))
    paths = paths or RefreshPaths()
    selected = _select_companies(COMPANIES if companies is None else companies, config.tickers)
    registry = _load_registry() if registry is None else registry
    started_at = utc_iso(clock())

    if config.dry_run:
        print(_plan(config, selected, registry, paths))
        return RefreshResult(started_at, started_at, companies=[], site=None, errors=[])

    client = client or EdgarClient()
    results: list[CompanyResult] = []
    for ticker, info in selected.items():
        log.info("refreshing %s (%s)", ticker, info.name)
        results.append(
            refresh_company(
                info,
                client=client,
                model_cls=registry.get(ticker),
                config=config,
                paths=paths,
                started_at=started_at,
            )
        )
    errors = [f"{result.ticker}: {result.error}" for result in results if result.error]

    try:
        _write_json(
            paths.site_data_dir / "companies.json",
            _companies_payload(started_at, selected, results),
        )
    except Exception as exc:  # recorded like a company failure; the diff must still be written
        log.error("companies.json: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
        errors.append(f"companies.json: {type(exc).__name__}: {exc}")

    site: BuildReport | None = None
    if config.build_site:
        try:
            site = build_site_pages(
                site_dir=paths.site_dir, out_dir=paths.site_build_dir, calls_md=paths.calls_md
            )
            log.info("site: %d pages built into %s", len(site.pages), paths.site_build_dir)
        except Exception as exc:
            log.error("site build failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            errors.append(f"site: {type(exc).__name__}: {exc}")

    result = RefreshResult(started_at, utc_iso(clock()), results, site, errors)
    write_diff(result, paths.diff_path)
    return result


# --------------------------------------------------------------------------------------------
# Diff file (step 8)
# --------------------------------------------------------------------------------------------


def _plural(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun if count == 1 else plural or noun + 's'}"


def render_diff(result: RefreshResult) -> str:
    """Markdown summary of a run for GitHub: what is new, what changed, what failed."""
    companies = result.companies
    new_total = sum(len(c.new_filings) for c in companies)
    downloaded = sum(len(c.downloaded) for c in companies)
    statuses = Counter(c.model_status for c in companies)
    models = ", ".join(f"{statuses[s]} {s}" for s in MODEL_STATUSES if statuses[s]) or "none"
    new_text = "no new filings" if new_total == 0 else _plural(new_total, "new filing")
    lines = [
        f"# Refresh {result.started_at}",
        "",
        f"{_plural(len(companies), 'company', 'companies')}: {new_text}, "
        f"{_plural(downloaded, 'document')} downloaded, models: {models}, "
        f"{_plural(len(result.errors), 'error')}. Finished {result.finished_at}.",
    ]
    for company in companies:
        lines += ["", f"## {company.ticker}", ""]
        if company.new_filings:
            lines.append(
                f"- New filings ({len(company.new_filings)} of {company.total_filings} tracked):"
            )
            for filing in company.new_filings[:MAX_LISTED_FILINGS]:
                lines.append(
                    f"  - {filing.form} {filing.filing_date} [{filing.accession}]({filing.url})"
                )
            hidden = len(company.new_filings) - MAX_LISTED_FILINGS
            if hidden > 0:
                lines.append(
                    f"  - ... and {hidden} more (see data/processed/{company.ticker}/filings.csv)"
                )
        else:
            lines.append(f"- New filings: none ({company.total_filings} tracked)")
        model_line = f"- Model: {company.model_status}"
        if company.changed_items:
            model_line += " - changed line items: " + ", ".join(company.changed_items)
        lines.append(model_line)
        if company.downloaded:
            lines.append(f"- Downloaded: {_plural(len(company.downloaded), 'document')}")
        if company.error:
            lines.append(f"- Error: {company.error}")
    if result.site is not None:
        site = result.site
        lines += [
            "",
            "## Site",
            "",
            f"- {_plural(len(site.pages), 'page')} built: {site.companies} companies, "
            f"{site.writeups} writeups, {site.calls} calls",
        ]
        lines += [f"- Warning: {warning}" for warning in site.warnings]
    lines += ["", "## Errors", ""]
    lines += [f"- {error}" for error in result.errors] or ["None."]
    return "\n".join(lines) + "\n"


def write_diff(result: RefreshResult, path: Path) -> None:
    """Write ``render_diff(result)`` to ``path`` (UTF-8, LF), creating parent directories."""
    _write_text(path, render_diff(result))


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def _parse_tickers(text: str) -> tuple[str, ...]:
    tickers = tuple(part.strip().upper() for part in text.split(",") if part.strip())
    if not tickers:
        raise argparse.ArgumentTypeError("expected a comma-separated list such as CRWV,NBIS")
    return tickers


def _parse_iso_date(text: str) -> str:
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from None
    return text


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit code 0 when every step succeeded, 1 otherwise, 2 for bad arguments."""
    parser = argparse.ArgumentParser(
        prog="refresh",
        description="Pull SEC filings and facts, re-export changed models, rebuild the site.",
    )
    parser.add_argument(
        "--tickers",
        type=_parse_tickers,
        default=None,
        metavar="CRWV,NBIS",
        help="comma-separated subset of the tracked companies (default: all)",
    )
    parser.add_argument(
        "--since",
        type=_parse_iso_date,
        default=DEFAULT_SINCE,
        metavar="YYYY-MM-DD",
        help="download the documents, where not cached yet, of filings from this date on "
        f"(default {DEFAULT_SINCE})",
    )
    parser.add_argument(
        "--no-download",
        dest="download",
        action="store_false",
        help="skip downloading filing documents (CI does this; raw documents are gitignored)",
    )
    parser.add_argument(
        "--skip-site", dest="build_site", action="store_false", help="do not rebuild site/build"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan; fetch nothing, write nothing"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    unknown = [t for t in args.tickers or () if t not in COMPANIES]
    if unknown:
        parser.error(f"unknown ticker(s) {', '.join(unknown)}; known: {', '.join(COMPANIES)}")

    config = RefreshConfig(
        tickers=args.tickers,
        since=args.since,
        download=args.download,
        build_site=args.build_site,
        dry_run=args.dry_run,
    )
    result = run(config)
    if config.dry_run:
        return 0

    new_total = sum(len(c.new_filings) for c in result.companies)
    print(
        f"refreshed {_plural(len(result.companies), 'company', 'companies')}: "
        f"{_plural(new_total, 'new filing')}, {_plural(len(result.errors), 'error')}; "
        f"see {LAST_REFRESH_DIFF}"
    )
    for error in result.errors:
        print(f"  error: {error}")
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

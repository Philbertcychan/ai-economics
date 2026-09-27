"""Build the static site in ``site/build/`` from JSON summaries, markdown writeups and calls.md.

Purpose
-------
``scripts/refresh.py`` writes a machine-readable summary of every company into ``site/data/`` and
the human writes essays into ``site/content/``. This module turns both into a small static site
that GitHub Pages can serve: an index with one comparison table of the companies (plus the calls
table and the writeups list once either has entries); one page per company with a key-figures
strip, server-rendered tables and Chart.js charts layered on top; one page per published
writeup; the stack (``stack/*.csv``, loaded by ``data/stack.py``) as one table of the chain
plus a page per stage with its primer, figures, conversions and players; and the signals ledger
(``signals/ledger.csv``, loaded by ``data/signals.py``) as one table of every signal, with the
rows for a stage or a company repeated on that page. There is no framework and no bundler:
templates are ``string.Template`` files in ``site/templates/`` and the browser assets are copied
verbatim from ``site/static/``.

Design notes
------------
* The pages carry labels, numbers, source lines and statuses, and no prose about the project or
  the method; that lives in the README. A section with nothing in it is left out, not apologised
  for, and its nav link goes with it.
* Every link is relative because GitHub Pages serves project sites under ``/<repo>/``.
* Every value that arrives from JSON or frontmatter is HTML-escaped. Writeup bodies are the
  author's own markdown and are rendered as trusted HTML.
* The builder never fails on missing inputs. A fresh clone with no data builds an empty-state
  site and lists what was missing in ``BuildReport.warnings``.
* Output is deterministic (sorted iteration, no timestamps beyond ``as_of``) so a rebuild with
  unchanged inputs produces byte-identical files.
* The generated subtrees of the output dir are deleted before each build, so an output dir that
  overlaps the sources (``--out site``, ``--out .``) is refused rather than built.
* This module is presentation only. Its one piece of arithmetic is display ratios of figures
  refresh.py already published (change on a year earlier, capex / revenue); nothing here models
  or forecasts.

CLI: ``uv run scripts/build_site.py [--out DIR] [--site-dir DIR] [--calls FILE] [--stack DIR]
[--signals DIR]``
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import re
import shutil
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import urlsplit

import markdown

from data import (
    CALLS_MD,
    SIGNALS_DIR,
    SITE_BUILD_DIR,
    SITE_DIR,
    SITE_STATIC_DIR,
    SITE_TEMPLATES_DIR,
    STACK_DIR,
)
from data.signals import CONFIDENCE, load_signals
from data.stack import (
    load_consumption_tiers,
    load_conversions,
    load_metrics,
    load_players,
    load_primer,
    load_stages,
    split_stages,
)

log = logging.getLogger(__name__)

REPO_OWNER = "Philbertcychan"
REPO_URL = f"https://github.com/{REPO_OWNER}/ai-economics"
CHART_JS_URL = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
SITE_NAME = "AI economics"

# The calls ledger is a GitHub-flavoured markdown table with exactly this header; calls.md at the
# repo root documents the row format and the outcome vocabulary.
CALLS_HEADER = ("date", "claim", "falsifying number", "deadline", "outcome")
CALLS_KEYS = ("date", "claim", "falsifying_number", "deadline", "outcome")

# writeups/index.html is the listing page, so a writeup may not take that file name.
RESERVED_SLUGS = frozenset({"index"})

# What a build reads from a site dir. The output dir must stay clear of these (see _check_out_dir).
SOURCE_SUBDIRS = ("content", "data", "static", "templates")

# Display order and labels for the tidy XBRL concepts refresh.py writes. Anything not listed is
# appended after these, in alphabetical order, with a de-snaked label.
CONCEPT_LABELS: dict[str, str] = {
    "revenue": "Revenue",
    "cost_of_revenue": "Cost of revenue",
    "operating_income": "Operating income",
    "net_income": "Net income",
    "d_and_a": "Depreciation & amortisation",
    "capex": "Capex",
    "cfo": "Cash from operations",
    "cash": "Cash",
    "ppe_net": "PP&E, net",
    "long_term_debt": "Long-term debt",
    "interest_expense": "Interest expense",
    "debt_principal": "Debt, principal",
    "interest_expense_debt": "Interest on debt",
    "debt_proceeds": "Debt raised",
    "debt_repayments": "Debt repaid",
    "deferred_revenue": "Deferred revenue (customer prepayments)",
    "deferred_revenue_change": "Change in deferred revenue",
    "deferred_revenue_recognised": "Prepaid revenue recognised",
    "receivables": "Receivables",
    "shares_outstanding": "Shares outstanding",
}

# The tiles at the top of a company page, in reading order: the income line, what is being spent,
# what funds it, and the balance sheet it lands on.
KEY_FIGURES = ("revenue", "capex", "cfo", "cash", "long_term_debt", "ppe_net")

# Row order of the index table: the layer with company models comes first. A layer not listed
# here sorts after these, alphabetically.
LAYER_ORDER = ("neocloud", "chip", "hyperscaler")

# The index lists this many writeups; writeups/index.html lists all of them.
INDEX_WRITEUPS = 5

# Shown where a figure or a comparison does not exist.
MISSING = "–"

# The chain table shows this many metrics (the first in metrics.csv) and players per stage.
INDEX_KEY_FIGURES = 2
INDEX_PLAYERS = 4

TEMPLATE_NAMES = (
    "base",
    "index",
    "company",
    "writeup",
    "writeups_index",
    "stack_index",
    "stack_stage",
    "signals_index",
)

# Tolerates a UTF-8 BOM (Windows editors add one) and CRLF line endings.
_BOM = chr(0xFEFF)
_FRONTMATTER_RE = re.compile(
    r"\A" + _BOM + r"?---[ \t]*\r?\n(?P<meta>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(?P<body>.*)\Z",
    re.DOTALL,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")
_TABLE_SEPARATOR_CELL_RE = re.compile(r":?-+:?")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class BuildReport:
    """What one build produced. ``pages`` are paths relative to the output dir, POSIX style."""

    pages: list[str] = field(default_factory=list)
    writeups: int = 0
    companies: int = 0
    calls: int = 0
    stages: int = 0
    signals: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Writeup:
    """One published essay from ``site/content/``. ``body_html`` is already rendered markdown."""

    slug: str
    title: str
    date: str
    company: str
    summary: str
    tags: tuple[str, ...]
    body_html: str

    @property
    def href(self) -> str:
        """Location of the page relative to the site root."""
        return f"writeups/{self.slug}.html"


# --------------------------------------------------------------------------------------------
# Parsing helpers (pure functions)
# --------------------------------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``key: value`` frontmatter between ``---`` fences from the markdown body.

    Keys are lower-cased. A value wrapped in one matching pair of quotes is unwrapped so
    ``title: "A: b"`` works; a quote at one end only belongs to the text (a title that ends in a
    quoted term keeps its closing quote). Text without a leading fence is returned untouched with
    an empty mapping.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    meta: dict[str, str] = {}
    for raw_line in match.group("meta").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue  # a stray line without a colon is not a key; ignore rather than fail
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        meta[key.strip().lower()] = value
    return meta, match.group("body")


def _split_table_row(line: str) -> list[str]:
    """Split one GFM table row on unescaped pipes and unescape ``\\|`` inside cells."""
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _UNESCAPED_PIPE_RE.split(row)]


def parse_calls(md: str) -> list[dict[str, str]]:
    """Read the calls ledger table out of ``calls.md``.

    Only the table whose header matches ``CALLS_HEADER`` is read, HTML comments are dropped first
    (calls.md keeps an example row inside one), and the table ends at the first non-table line.
    A row whose cells are all empty is a placeholder, not a call, and is skipped.
    """
    rows: list[dict[str, str]] = []
    in_table = False
    for raw_line in _HTML_COMMENT_RE.sub("", md).splitlines():
        line = raw_line.strip()
        if not in_table:
            if line.startswith("|"):
                cells = [cell.lower() for cell in _split_table_row(line)]
                in_table = cells == list(CALLS_HEADER)
            continue
        if not line.startswith("|"):
            break
        cells = _split_table_row(line)
        if all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells):
            continue  # the |---|---| delimiter row
        if not any(cells):
            continue  # "| | | |" left over from the writeup template would render a blank row
        padded = (cells + [""] * len(CALLS_KEYS))[: len(CALLS_KEYS)]
        rows.append(dict(zip(CALLS_KEYS, padded, strict=True)))
    return rows


def slugify(title: str) -> str:
    """ASCII, lower-case, hyphen-separated file stem for a writeup title."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug or "untitled"


def render_markdown(md: str) -> str:
    """Render trusted markdown (the author's own writeups) to HTML.

    Tables are wrapped in ``.table-scroll`` so a wide sensitivity table scrolls inside its box
    instead of forcing the whole page sideways on a phone.
    """
    rendered = markdown.markdown(
        md, extensions=["tables", "fenced_code", "toc"], output_format="html"
    )
    return rendered.replace("<table>", '<div class="table-scroll"><table>').replace(
        "</table>", "</table></div>"
    )


# --------------------------------------------------------------------------------------------
# Number formatting (presentation only)
# --------------------------------------------------------------------------------------------
#
# site/static/dashboard.js formats chart ticks and tooltips with a port of fmt_value (compact
# form), so a chart and the table under it show the same text for the same value. The vectors
# below (value | unit | display) are repeated verbatim in dashboard.js, and
# tests/test_build_site.py pins fmt_value to them and fails when the two copies drift apart.
# When a rule changes here, change formatValue there and both tables.
#
#   fmt: 1.15e9 | USD | $1.1bn
#   fmt: 1.25e9 | USD | $1.2bn
#   fmt: 982000000 | USD | $982m
#   fmt: 9.95e6 | USD | $9.9m
#   fmt: 32000 | USD | $32k
#   fmt: -100000000 | USD | -$100m
#   fmt: 1500 | USD m | $1.5bn
#   fmt: 123.456 | USD | $123
#   fmt: 100.5 | USD | $100
#   fmt: 12.5 | USD | $12.50
#   fmt: 2.75 | USD | $2.75
#   fmt: 1.5772 | USD | $1.58
#   fmt: 0.2079 | USD | $0.21
#   fmt: 0.08 | USD/kWh | $0.08
#   fmt: 0.5 | USD/M tokens | $0.50
#   fmt: 0.55 | % | 55.0%
#   fmt: 0.6 | share | 60.0%
#   fmt: 3.5 | x | 3.50x
#   fmt: 0.125 | x | 0.12x
#   fmt: 240000000 | shares | 240m
#   fmt: 2500000 | tokens/s | 2.5m
#   fmt: 2500 | tokens/s | 2,500
#   fmt: 1250 | GPUs | 1,250
#   fmt: -1250 | GPUs | -1,250
#   fmt: 18.99485 | months | 18.99


def _as_float(value: Any) -> float | None:
    """Coerce a JSON scalar to float; ``None`` for missing, NaN or non-numeric input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _abbreviate(magnitude: float) -> str:
    """``1234567890 -> '1.2bn'``, ``982000000 -> '982m'``; one decimal below ten units."""
    for divisor, suffix in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if magnitude >= divisor:
            scaled = magnitude / divisor
            # Billions always keep a decimal ("$1.0bn" not "$1bn") so columns line up.
            decimals = 1 if suffix == "bn" or scaled < 10 else 0
            return f"{scaled:.{decimals}f}{suffix}"
    return (
        f"{magnitude:,.0f}" if magnitude >= 100 or magnitude.is_integer() else f"{magnitude:,.2f}"
    )


def fmt_money(value: Any, *, unit: str = "USD", compact: bool = True) -> str:
    """Format a USD amount for tables: ``$1.2bn`` / ``$982m`` / ``$12k``, or exact when not compact.

    ``unit="USD m"`` means the value is already in millions and is scaled back to dollars first.
    Missing or non-numeric input renders as an em dash so tables never show ``None``.
    """
    number = _as_float(value)
    if number is None:
        return "—"
    if unit.strip().lower() in {"usd m", "usdm", "usd mn", "usd million", "usd millions"}:
        number *= 1e6
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if compact:
        return f"{sign}${_abbreviate(magnitude)}"
    exact = f"{magnitude:,.2f}"
    return f"{sign}${exact.removesuffix('.00')}"


def fmt_value(value: Any, unit: str = "", *, compact: bool = True) -> str:
    """Format any model/reported value according to its unit label.

    ``USD...`` is money, ``%``/``share``/``decimal``/``percent`` are fractions shown as percent,
    ``x`` is a multiple, ``shares`` is always abbreviated; anything else is a plain number,
    abbreviated from a million up when ``compact``. The vector table above this section shows
    each rule, and ``formatValue`` in site/static/dashboard.js mirrors the compact form.
    """
    if isinstance(value, str) and _as_float(value) is None:
        return value  # free-text inputs such as "see note" pass through untouched
    number = _as_float(value)
    if number is None:
        return "—"
    kind = unit.strip().lower()
    if kind.startswith("usd"):
        return fmt_money(number, unit=unit, compact=compact)
    if kind in {"%", "share", "decimal", "percent"}:
        return f"{number * 100:.1f}%"
    if kind == "x":
        return f"{number:.2f}x"
    if kind == "shares" or (compact and abs(number) >= 1e6):
        return ("-" if number < 0 else "") + _abbreviate(abs(number))
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"


_ISO_UTC_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?Z")


def fmt_timestamp(as_of: str) -> str:
    """``2026-09-17T13:43:24Z -> '2026-09-17 13:43 UTC'``; any other text is returned unchanged."""
    match = _ISO_UTC_RE.fullmatch(as_of.strip())
    return f"{match.group(1)} {match.group(2)} UTC" if match else as_of


# --------------------------------------------------------------------------------------------
# Display arithmetic on reported series (pure functions)
# --------------------------------------------------------------------------------------------
#
# The index table and the key-figures tiles show the latest reported point of a series, its
# change on the same period a year earlier, and capex as a share of revenue. All three are read
# straight off points refresh.py already published; none of it is a model.

_PERIOD_RE = re.compile(r"(?P<year>\d{4})(?P<quarter>Q[1-4])?")


def _points(raw: Any) -> list[tuple[str, Any]]:
    """Normalise ``[["2025Q2", 1.2e9], ...]`` and drop malformed entries instead of crashing."""
    points: list[tuple[str, Any]] = []
    for item in raw or []:
        if isinstance(item, list | tuple) and len(item) >= 2:
            points.append((str(item[0]), item[1]))
    return points


def latest_point(points: list[tuple[str, Any]]) -> tuple[str, float] | None:
    """The numeric point with the latest period label, or ``None`` when there is none.

    Labels within one series sort as text (``2025Q4 < 2026Q1``, ``2024 < 2025``). A null value is
    skipped so a placeholder for a quarter not yet filed never counts as the latest figure.
    """
    numeric = [
        (period, number) for period, value in points if (number := _as_float(value)) is not None
    ]
    return max(numeric, key=lambda point: point[0]) if numeric else None


def value_at(points: list[tuple[str, Any]], period: str) -> float | None:
    """The numeric value published for ``period``, or ``None``."""
    for label, value in points:
        if label == period:
            return _as_float(value)
    return None


def prior_year_period(period: str) -> str | None:
    """The label one year earlier: ``2026Q2 -> 2025Q2`` (same quarter) and ``2025 -> 2024``.

    ``None`` for anything that is not a calendar quarter or year, such as a model label
    (``2026E``): a comparison across label kinds would not be like for like.
    """
    match = _PERIOD_RE.fullmatch(period.strip())
    if match is None:
        return None
    return f"{int(match.group('year')) - 1:04d}{match.group('quarter') or ''}"


def yoy_change(current: float | None, prior: float | None) -> float | None:
    """Fractional change on the year-earlier figure (``0.25`` is +25%), or ``None``.

    There is no percentage on a zero or negative base: a cash outflow of 50 that becomes an
    inflow of 100 is not "-300%", and the reader is better served by a blank than by that number.
    """
    if current is None or prior is None or prior <= 0:
        return None
    return current / prior - 1.0


def capex_to_revenue(capex: float | None, revenue: float | None) -> float | None:
    """Capex as a fraction of revenue for one period; ``None`` without both or on revenue <= 0."""
    if capex is None or revenue is None or revenue <= 0:
        return None
    return capex / revenue


def _change_percent(change: float | None) -> float | None:
    """The change as displayed, in percent to one decimal, so text and colour cannot disagree."""
    return None if change is None or not math.isfinite(change) else round(change * 100, 1)


def fmt_change(change: float | None) -> str:
    """``0.177 -> '+17.7%'``, ``-0.2 -> '-20.0%'``, ``0 -> '0.0%'``; an en dash for ``None``."""
    percent = _change_percent(change)
    if percent is None:
        return MISSING
    return "0.0%" if percent == 0 else f"{percent:+,.1f}%"


def change_direction(change: float | None) -> str:
    """CSS class for a change: ``pos``, ``neg``, ``flat``, or ``""`` when there is no figure."""
    percent = _change_percent(change)
    if percent is None:
        return ""
    return "flat" if percent == 0 else ("pos" if percent > 0 else "neg")


@dataclass(frozen=True)
class Figure:
    """One reported point with its change on the same period a year earlier."""

    period: str
    value: float
    unit: str
    change: float | None


def series_figure(series: Any, period: str | None = None) -> Figure | None:
    """The latest point of a reported series, or the point at ``period`` when one is given.

    ``None`` when the series is missing or has no numeric value there.
    """
    if not isinstance(series, dict):
        return None
    points = _points(series.get("points"))
    if period is None:
        latest = latest_point(points)
        if latest is None:
            return None
        period, value = latest
    else:
        found = value_at(points, period)
        if found is None:
            return None
        value = found
    prior_period = prior_year_period(period)
    prior = value_at(points, prior_period) if prior_period else None
    return Figure(period, value, str(series.get("unit") or ""), yoy_change(value, prior))


def headline_figures(reported: Any) -> tuple[Figure | None, Figure | None]:
    """``(revenue, capex)`` for one index row, both for the same period.

    The row carries a single period label and a capex / revenue ratio, so capex is read at
    revenue's latest period and left out when it has no point there (a later capex filing, or an
    annual capex series beside quarterly revenue). Without revenue the row falls back to the
    latest capex point.
    """
    if not isinstance(reported, dict):
        return None, None
    revenue = series_figure(reported.get("revenue"))
    capex = series_figure(reported.get("capex"), revenue.period if revenue else None)
    return revenue, capex


# --------------------------------------------------------------------------------------------
# HTML fragments. Every dynamic value passes through ``_e``.
# --------------------------------------------------------------------------------------------


def _e(value: Any) -> str:
    """HTML-escape a scalar for text or attribute context; ``None`` becomes an empty string."""
    return "" if value is None else html.escape(str(value), quote=True)


# The model_status vocabulary of site/data/companies.json (SITE_MODEL_STATUS in scripts/refresh.py
# is where it is written): `built`, `pending`, and `no-model` for companies tracked for their
# reported figures only (no class in companies.REGISTRY).
_BADGE_LABELS: dict[str, str] = {
    "built": "model",
    "pending": "model in progress",
    "no-model": "reported data",
}


def _model_status(model_status: Any) -> str:
    """The JSON status, lower-cased; a missing one reads as ``pending`` (refresh.py's default)."""
    return str(model_status or "pending").lower()


def _badge(model_status: Any) -> str:
    status = _model_status(model_status)
    label = _BADGE_LABELS.get(status, _BADGE_LABELS["pending"])
    return f'<span class="badge badge-{_e(status)}">{label}</span>'


def _workbook_link(ticker: str, model_status: Any) -> str:
    """Link to ``models/<TICKER>.xlsx`` on GitHub, only once the model is built.

    The workbook is exported by a built model, so for any other status the link would be a 404
    on GitHub.
    """
    if _model_status(model_status) != "built":
        return ""
    url = f"{REPO_URL}/blob/main/models/{ticker}.xlsx"
    return f'<a href="{_e(url)}" rel="noopener">Workbook</a>'


def _filing_link(latest_filing: Any) -> str:
    """``10-Q · 2025-08-14`` linked to EDGAR, or a status when nothing has been pulled."""
    if not isinstance(latest_filing, dict) or not latest_filing.get("url"):
        return '<span class="muted">no filing</span>'
    text = " · ".join(str(latest_filing[k]) for k in ("form", "date") if latest_filing.get(k))
    return f'<a href="{_e(latest_filing["url"])}" rel="noopener">{_e(text or "filing")}</a>'


def _layer_sort_key(company: dict[str, Any]) -> tuple[int, str, str]:
    layer = str(company.get("layer") or "").lower()
    rank = LAYER_ORDER.index(layer) if layer in LAYER_ORDER else len(LAYER_ORDER)
    return rank, layer, str(company["ticker"]).upper()


def _figure_cell(figure: Figure | None) -> str:
    text = fmt_value(figure.value, figure.unit) if figure else MISSING
    return f'<td class="num">{_e(text)}</td>'


def _change_cell(figure: Figure | None) -> str:
    change = figure.change if figure else None
    classes = " ".join(part for part in ("num", "chg", change_direction(change)) if part)
    return f'<td class="{classes}">{_e(fmt_change(change))}</td>'


_COMPANIES_COLUMNS: tuple[tuple[str, bool], ...] = (  # (header, numeric)
    ("Company", False),
    ("Layer", False),
    ("Period", False),
    ("Revenue", True),
    ("Revenue YoY", True),
    ("Capex", True),
    ("Capex YoY", True),
    ("Capex / revenue", True),
    ("Model", False),
)


def _companies_table(
    companies: list[dict[str, Any]], data_by_ticker: dict[str, Any], root: str
) -> str:
    """The index table: one row per company, by layer then ticker. No companies, no rows."""
    head = "".join(
        f'<th scope="col" class="num">{_e(label)}</th>'
        if numeric
        else f'<th scope="col">{_e(label)}</th>'
        for label, numeric in _COMPANIES_COLUMNS
    )
    rows = []
    for company in sorted(companies, key=_layer_sort_key):
        ticker = str(company["ticker"]).upper()
        data = data_by_ticker.get(ticker)
        revenue, capex = headline_figures(data.get("reported") if isinstance(data, dict) else None)
        shown = revenue or capex
        ratio = None
        # A ratio of two figures in different units (USD against USD m) would be off by 1e6.
        if revenue and capex and revenue.unit == capex.unit:
            ratio = capex_to_revenue(capex.value, revenue.value)
        href = f"{root}companies/{ticker}.html"
        rows.append(
            "<tr>"
            f'<th scope="row"><a class="co-ticker" href="{_e(href)}">{_e(ticker)}</a> '
            f'<span class="co-name">{_e(company.get("name") or "")}</span></th>'
            f'<td class="layer">{_e(company.get("layer") or "")}</td>'
            f'<td class="nowrap">{_e(shown.period if shown else MISSING)}</td>'
            f"{_figure_cell(revenue)}{_change_cell(revenue)}"
            f"{_figure_cell(capex)}{_change_cell(capex)}"
            f'<td class="num">{_e(fmt_value(ratio, "%") if ratio is not None else MISSING)}</td>'
            f"<td>{_badge(company.get('model_status'))}</td>"
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table class="companies">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _key_figures_html(data: dict[str, Any] | None) -> str:
    """The tiles under a company header: label, latest value, change on a year earlier, period.

    Each tile names its own period because series do not always end together (a balance-sheet
    line can lag a quarter). A series with no numeric point gets no tile.
    """
    reported = (data or {}).get("reported") or {}
    tiles = []
    for concept in KEY_FIGURES:
        figure = series_figure(reported.get(concept))
        if figure is None:
            continue
        change = ""
        if figure.change is not None:
            change = (
                f'<span class="chg {change_direction(figure.change)}">'
                f"{_e(fmt_change(figure.change))} YoY</span> · "
            )
        tiles.append(
            '<div class="kpi">'
            f"<dt>{_e(CONCEPT_LABELS[concept])}</dt>"
            f'<dd class="kpi-value">{_e(fmt_value(figure.value, figure.unit))}</dd>'
            f'<dd class="kpi-note">{change}{_e(figure.period)}</dd>'
            "</div>"
        )
    if not tiles:
        return ""
    return (
        '<section id="key-figures" class="section"><h2>Key figures</h2>'
        f'<dl class="kpis">{"".join(tiles)}</dl></section>'
    )


def _section(section_id: str, heading: str, body: str) -> str:
    """A headed section, or nothing at all when there is no body to put in it."""
    if not body:
        return ""
    return f'<section id="{_e(section_id)}" class="section"><h2>{_e(heading)}</h2>{body}</section>'


def _writeups_list(writeups: list[Writeup], root: str, known: set[str]) -> str:
    if not writeups:
        return ""
    items = []
    for w in writeups:
        company = w.company.upper()
        if company in known:
            who = f'<a href="{_e(root + "companies/" + company + ".html")}">{_e(company)}</a>'
        else:
            who = _e(w.company)
        meta = " · ".join(part for part in (_e(w.date), who) if part)
        items.append(
            "<li>"
            f'<a class="writeup-title" href="{_e(root + w.href)}">{_e(w.title)}</a>'
            f'<span class="meta">{meta}</span>'
            + (f'<p class="summary">{_e(w.summary)}</p>' if w.summary else "")
            + "</li>"
        )
    return f'<ul class="writeup-list">{"".join(items)}</ul>'


def _calls_table(calls: list[dict[str, str]]) -> str:
    if not calls:
        return ""
    head = "".join(f'<th scope="col">{_e(h.capitalize())}</th>' for h in CALLS_HEADER)
    rows = []
    for call in calls:
        outcome = call.get("outcome", "")
        rows.append(
            "<tr>"
            f'<td class="nowrap">{_e(call.get("date", ""))}</td>'
            f"<td>{_e(call.get('claim', ''))}</td>"
            f"<td>{_e(call.get('falsifying_number', ''))}</td>"
            f'<td class="nowrap">{_e(call.get("deadline", ""))}</td>'
            f'<td class="outcome outcome-{_e(slugify(outcome))}">{_e(outcome)}</td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table class="calls">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _series_table(
    rows: list[tuple[str, str, list[tuple[str, Any]]]], *, max_periods: int, caption: str
) -> str:
    """Wide table: one row per series, one column per period (the latest ``max_periods``)."""
    periods = sorted({period for _, _, pts in rows for period, _ in pts})[-max_periods:]
    if not periods:
        return ""
    head = "".join(f'<th scope="col" class="num">{_e(p)}</th>' for p in periods)
    body = []
    for label, unit, pts in rows:
        lookup = {period: value for period, value in pts}
        cells = "".join(
            f'<td class="num">{_e(fmt_value(lookup.get(p), unit))}</td>' for p in periods
        )
        body.append(
            f'<tr><th scope="row">{_e(label)}</th><td class="unit">{_e(unit)}</td>{cells}</tr>'
        )
    return (
        '<div class="table-scroll"><table class="series">'
        f"<caption>{_e(caption)}</caption>"
        f'<thead><tr><th scope="col">Item</th><th scope="col">Unit</th>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _ordered_concepts(reported: dict[str, Any]) -> list[str]:
    known = [c for c in CONCEPT_LABELS if c in reported]
    extra = sorted(c for c in reported if c not in CONCEPT_LABELS)
    return known + extra


def _reported_html(data: dict[str, Any] | None) -> str:
    reported = (data or {}).get("reported") or {}
    by_freq: dict[str, list[tuple[str, str, list[tuple[str, Any]]]]] = {}
    elements: list[tuple[str, str]] = []  # (label, XBRL element) for the source list
    for concept in _ordered_concepts(reported):
        series = reported.get(concept) or {}
        points = _points(series.get("points"))
        if not points:
            continue
        label = CONCEPT_LABELS.get(concept, concept.replace("_", " ").capitalize())
        freq = str(series.get("freq") or "Q").upper()
        by_freq.setdefault(freq, []).append((label, str(series.get("unit") or ""), points))
        if series.get("tag"):
            elements.append((label, str(series["tag"])))
    if not by_freq:
        return '<p class="note">No data</p>'
    parts = []
    for freq in sorted(by_freq, key=lambda f: (f != "Q", f)):  # quarters first
        # Annual points come from SEC's CYyyyy frames, so for a filer whose year does not end in
        # December the column is a calendar year, not the fiscal year a finance reader expects.
        caption = "Quarters" if freq == "Q" else "Calendar years"
        parts.append(_series_table(by_freq[freq], max_periods=6, caption=caption))
    if elements:
        # Filers tag the same line differently (capex is three different elements across the
        # seven companies), so the page names the element instead of implying they are identical.
        items = "".join(
            f"<li>{html.escape(label)}: <code>{html.escape(tag)}</code></li>"
            for label, tag in elements
        )
        parts.append(
            f'<details class="sources"><summary>XBRL elements</summary><ul>{items}</ul></details>'
        )
    return "".join(parts)


def _has_model_content(data: dict[str, Any] | None) -> bool:
    return bool((data or {}).get("outputs") or (data or {}).get("inputs"))


_SeriesRow = tuple[str, str, list[tuple[str, Any]]]  # (label, unit, points)


def _output_rows(data: dict[str, Any] | None) -> list[_SeriesRow]:
    outputs = (data or {}).get("outputs") or {}
    rows = []
    for item, series in outputs.items():
        series = series or {}
        rows.append(
            (
                str(series.get("label") or item),
                str(series.get("unit") or ""),
                _points(series.get("points")),
            )
        )
    return rows


def _is_single_period(rows: list[_SeriesRow]) -> bool:
    """True when no output has more than one point: a snapshot, with nothing to chart."""
    return bool(rows) and all(len(points) <= 1 for _, _, points in rows)


def _snapshot_table(rows: list[_SeriesRow]) -> str:
    """Single-period outputs as Output | Value | Unit, with the period in the caption.

    Outputs that do not share one period get a Period column instead, so no value is shown
    under a label that is not its own.
    """
    periods = {points[0][0] for _, _, points in rows if points}
    shared = next(iter(periods)) if len(periods) == 1 else None
    body = []
    for label, unit, points in rows:
        period, value = points[0] if points else (MISSING, None)
        period_cell = "" if shared else f'<td class="nowrap">{_e(period)}</td>'
        body.append(
            f'<tr><th scope="row">{_e(label)}</th>{period_cell}'
            f'<td class="num">{_e(fmt_value(value, unit))}</td>'
            f'<td class="unit">{_e(unit)}</td></tr>'
        )
    caption = f"Outputs · {shared}" if shared else "Outputs"
    period_head = "" if shared else '<th scope="col">Period</th>'
    return (
        f'<div class="table-scroll"><table class="outputs"><caption>{_e(caption)}</caption>'
        f'<thead><tr><th scope="col">Output</th>{period_head}'
        '<th scope="col" class="num">Value</th><th scope="col">Unit</th></tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


_NOTE_TAG_RE = re.compile(r"\s*\[(?P<basis>[^\[\]]*)\]\s*(?P<note>.*)", re.DOTALL)


def split_basis(note: Any) -> tuple[str, str]:
    """Split the leading bracketed tag of an assumption's note from the rest: ``(basis, note)``.

    ``"[derived; proposed; range 4 to 6] Fleet average."`` gives
    ``("derived; proposed; range 4 to 6", "Fleet average.")``. A note without a leading tag
    comes back whole with an empty basis; brackets further into the note are left alone.
    """
    text = "" if note is None else str(note)
    match = _NOTE_TAG_RE.fullmatch(text)
    if match is None:
        return "", text.strip()
    return " ".join(match.group("basis").split()), match.group("note").strip()


def _inputs_table(data: dict[str, Any] | None) -> str:
    """The assumptions register: one row per model input, exact values (not abbreviated)."""
    inputs = (data or {}).get("inputs") or []
    rows = []
    for row in inputs:
        if not isinstance(row, dict):
            continue
        value = fmt_value(row.get("value"), str(row.get("unit") or ""), compact=False)
        basis, note = split_basis(row.get("note"))
        rows.append(
            f'<tr><th scope="row"><code>{_e(row.get("name"))}</code></th>'
            f'<td class="num">{_e(value)}</td><td class="unit">{_e(row.get("unit"))}</td>'
            f'<td class="basis">{_e(basis)}</td>'
            f"<td>{_e(row.get('source'))}</td><td>{_e(note)}</td></tr>"
        )
    if not rows:
        return ""
    return (
        '<div class="table-scroll"><table class="inputs"><caption>Assumptions</caption><thead><tr>'
        '<th scope="col">Assumption</th><th scope="col" class="num">Value</th>'
        '<th scope="col">Unit</th><th scope="col">Basis</th><th scope="col">Source</th>'
        '<th scope="col">Note</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _model_section(data: dict[str, Any] | None, model_status: Any) -> str:
    """Outputs and the assumptions behind them, for a built model only.

    Outputs over several periods get charts and a table by period. Single-period outputs get one
    Output | Value | Unit table and no chart host: a chart of one bar per output says less than
    the number does. Any status other than ``built`` renders nothing, since the badge in the page
    header already carries it.
    """
    if _model_status(model_status) != "built" or not _has_model_content(data):
        return ""
    rows = _output_rows(data)
    if _is_single_period(rows):
        outputs = _snapshot_table(rows)
    elif rows:
        outputs = '<div class="charts" data-charts="outputs"></div>' + _series_table(
            rows, max_periods=12, caption="Outputs"
        )
    else:
        outputs = ""
    return _section("model", "Model", outputs + _inputs_table(data))


def _inline_json(data: Any) -> str:
    """JSON safe to embed in a ``<script type="application/json">`` (no ``</script>`` escape)."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")


# --------------------------------------------------------------------------------------------
# The stack: one table of the chain, one page per stage. Rows arrive as plain dicts from
# data/stack.py, already validated; this section only formats them.
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stack:
    """The value chain as loaded from ``stack/``: stages in order, and the tables about them.

    Rows are plain dicts (``DataFrame.to_dict("records")``) so the renderers stay free of
    pandas. A secondary table that failed to load is empty here, with the reason in the build
    warnings, and the empty-section rule hides it on the pages.
    """

    stages: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    players: list[dict[str, Any]]
    conversions: list[dict[str, Any]]
    tiers: list[dict[str, Any]]
    primers: dict[str, str]  # stage key -> rendered HTML, for the stages that have one

    def name_of(self, key: str) -> str:
        """The display name of a stage key; the key itself when it is not in the chain."""
        return next((str(s["name"]) for s in self.stages if s["stage"] == key), key)


_STACK_TABLES: tuple[tuple[str, Callable[[Path], Any]], ...] = (
    ("metrics", load_metrics),
    ("players", load_players),
    ("conversions", load_conversions),
    ("tiers", load_consumption_tiers),
)


def load_stack(stack_dir: Path, warnings: list[str]) -> Stack | None:
    """Read the stack tables; ``None``, with a warning, when there is no usable ``stages.csv``.

    Without stages there is nothing to hang the other tables on, so the stack is left out and
    its nav link with it. A secondary table that is missing or malformed is left empty and
    named in ``warnings``: the chain still renders. Primers are rendered with the writeup
    pipeline, since they are the author's own markdown.
    """
    try:
        stages = load_stages(stack_dir)
    except FileNotFoundError:
        warnings.append(f"stages.csv missing from {stack_dir}; stack pages not built")
        return None
    except ValueError as exc:
        warnings.append(f"{exc}; stack pages not built")
        return None
    tables: dict[str, list[dict[str, Any]]] = {}
    for name, loader in _STACK_TABLES:
        try:
            tables[name] = loader(stack_dir).to_dict("records")
        except (FileNotFoundError, ValueError) as exc:
            warnings.append(f"{exc}; stack {name} left empty")
            tables[name] = []
    primers: dict[str, str] = {}
    for key in stages["stage"]:
        text = load_primer(str(key), stack_dir / "primers")
        if text is not None:
            primers[str(key)] = render_markdown(text)
    return Stack(stages=stages.to_dict("records"), primers=primers, **tables)


def fmt_number(value: Any) -> str:
    """A plain number for the stack tables: thousands separators, up to two decimals, no unit.

    ``1500000 -> '1,500,000'``, ``0.75 -> '0.75'``, ``3.0 -> '3'``. The unit column says what
    the number is, so there is no currency symbol and no abbreviation guessed from a unit label.
    An en dash for a missing value.
    """
    number = _as_float(value)
    if number is None:
        return MISSING
    text = f"{number:,.2f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _stage_href(root: str, key: Any) -> str:
    return f"{root}stack/{key}.html"


def _company_link(ticker: Any, root: str, known: set[str]) -> str:
    """A ticker linked to its company page when the site has one, else the ticker as text."""
    text = str(ticker or "")
    if text.upper() in known:
        return f'<a href="{_e(root + "companies/" + text.upper() + ".html")}">{_e(text)}</a>'
    return _e(text)


def _source_link(url: Any, text: Any) -> str:
    """``source`` linked to ``source_url``; the host stands in when the text is blank."""
    if not url:
        return _e(text)
    label = str(text or "").strip() or urlsplit(str(url)).netloc or "source"
    return f'<a href="{_e(url)}" rel="noopener">{_e(label)}</a>'


def _confidence_cell(confidence: Any) -> str:
    level = str(confidence or "").lower()
    return f'<td class="confidence confidence-{_e(level)}">{_e(level)}</td>'


def _table(table_class: str, head: list[tuple[str, bool]], rows: list[str]) -> str:
    """A ``.table-scroll`` table, or nothing without rows; ``head`` is (label, numeric)."""
    if not rows:
        return ""
    header = "".join(
        f'<th scope="col" class="num">{_e(label)}</th>'
        if numeric
        else f'<th scope="col">{_e(label)}</th>'
        for label, numeric in head
    )
    return (
        f'<div class="table-scroll"><table class="{_e(table_class)}">'
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


_STAGES_HEAD = [
    ("#", True),
    ("Stage", False),
    ("Unit", False),
    ("Sells", False),
    ("Lead time (years)", True),
    ("Bottleneck", True),
    ("Key figures", False),
    ("Players", False),
]


def _stages_table(stack: Stack, root: str, known: set[str]) -> str:
    """The chain as one table in stage order: the two first metrics and four first players of
    each stage ride along so the index says what a stage is measured in and who is in it."""
    rows = []
    for stage in stack.stages:
        key = str(stage["stage"])
        figures = [m for m in stack.metrics if m["stage"] == key][:INDEX_KEY_FIGURES]
        # The spaces around the label span are collapsed by the block layout but keep the cell
        # readable as plain text ("1,500,000 MW Example capacity").
        figures_html = " ".join(
            f'<span class="figure">{_e(fmt_number(m["value"]))} {_e(m["unit"])} '
            f'<span class="figure-label">{_e(m["metric"])}</span></span>'
            for m in figures
        )
        players = [p for p in stack.players if p["stage"] == key][:INDEX_PLAYERS]
        players_html = ", ".join(
            _company_link(p["ticker"], root, known) if p["ticker"] else _e(p["company"])
            for p in players
        )
        rows.append(
            "<tr>"
            f'<td class="num order">{_e(stage["order"])}</td>'
            f'<th scope="row"><a href="{_e(_stage_href(root, key))}">{_e(stage["name"])}</a></th>'
            f'<td class="unit">{_e(stage["unit"])}</td>'
            f'<td class="sells">{_e(stage["sells"])}</td>'
            f'<td class="num">{_e(fmt_number(stage["lead_time_years"]))}</td>'
            f'<td class="num">{_e(fmt_number(stage["bottleneck_score"]))}</td>'
            f"<td>{figures_html or MISSING}</td>"
            f"<td>{players_html or MISSING}</td>"
            "</tr>"
        )
    return _table("stages", _STAGES_HEAD, rows)


_TIERS_HEAD = [
    ("Tier", False),
    ("Name", False),
    ("Examples", False),
    ("Tokens per user per day", False),
    ("Revenue model", False),
]


def _tiers_table(tiers: list[dict[str, Any]]) -> str:
    rows = [
        "<tr>"
        f'<th scope="row">{_e(t["tier"])}</th>'
        f"<td>{_e(t['name'])}</td>"
        f'<td class="examples">{_e(t["examples"])}</td>'
        f'<td class="nowrap">{_e(t["tokens_per_user_day"])}</td>'
        f"<td>{_e(t['revenue_model'])}</td>"
        "</tr>"
        for t in tiers
    ]
    return _table("tiers", _TIERS_HEAD, rows)


def _stage_facts(stack: Stack, stage: dict[str, Any], root: str) -> str:
    """The ``dt``/``dd`` pairs under a stage heading; a fact with no value is left out."""
    buys_from = " · ".join(
        f'<a href="{_e(_stage_href(root, key))}">{_e(stack.name_of(key))}</a>'
        for key in split_stages(stage["buys_from"])
    )
    lead = _as_float(stage["lead_time_years"])
    lead_text = "" if lead is None else f"{fmt_number(lead)} {'year' if lead == 1 else 'years'}"
    score = fmt_number(stage["bottleneck_score"])
    bottleneck = " · ".join(
        part
        for part in (
            f"{score} / 5" if score != MISSING else "",
            _e(stage["bottleneck_note"]),
        )
        if part
    )
    status = str(stage["status"] or "")
    facts = (
        ("Unit", _e(stage["unit"])),
        ("Sells", _e(stage["sells"])),
        ("Buys from", buys_from),
        ("Lead time", _e(lead_text)),
        ("Bottleneck", bottleneck),
        ("Status", f'<span class="badge badge-{_e(status)}">{_e(status)}</span>' if status else ""),
    )
    return "\n".join(f"      <dt>{label}</dt><dd>{value}</dd>" for label, value in facts if value)


_FIGURES_HEAD = [
    ("Metric", False),
    ("Value", True),
    ("Unit", False),
    ("As of", False),
    ("Scope", False),
    ("Confidence", False),
    ("Source", False),
    ("Note", False),
]


def _figures_table(metrics: list[dict[str, Any]]) -> str:
    rows = [
        "<tr>"
        f'<th scope="row">{_e(m["metric"])}</th>'
        f'<td class="num">{_e(fmt_number(m["value"]))}</td>'
        f'<td class="unit">{_e(m["unit"])}</td>'
        f'<td class="nowrap">{_e(m["as_of"])}</td>'
        f"<td>{_e(m['scope'])}</td>"
        f"{_confidence_cell(m['confidence'])}"
        f"<td>{_source_link(m['source_url'], m['source'])}</td>"
        f"<td>{_e(m['note'])}</td>"
        "</tr>"
        for m in metrics
    ]
    return _table("figures", _FIGURES_HEAD, rows)


_CONVERSIONS_HEAD = [
    ("From → To", False),
    ("Factor", True),
    ("Unit", False),
    ("As of", False),
    ("Confidence", False),
    ("Source", False),
    ("Note", False),
]


def _conversions_table(stack: Stack, conversions: list[dict[str, Any]], root: str) -> str:
    rows = []
    for c in conversions:
        ends = " → ".join(
            f'<a href="{_e(_stage_href(root, key))}">{_e(stack.name_of(str(key)))}</a>'
            for key in (c["from_stage"], c["to_stage"])
        )
        rows.append(
            "<tr>"
            f'<th scope="row">{ends}</th>'
            f'<td class="num">{_e(fmt_number(c["factor"]))}</td>'
            f'<td class="unit">{_e(c["unit"])}</td>'
            f'<td class="nowrap">{_e(c["as_of"])}</td>'
            f"{_confidence_cell(c['confidence'])}"
            f"<td>{_source_link(c['source_url'], c['source'])}</td>"
            f"<td>{_e(c['note'])}</td>"
            "</tr>"
        )
    return _table("conversions", _CONVERSIONS_HEAD, rows)


_PLAYERS_HEAD = [
    ("Company", False),
    ("Ticker", False),
    ("Role", False),
    ("Listed", False),
    ("Note", False),
]


def _players_table(players: list[dict[str, Any]], root: str, known: set[str]) -> str:
    rows = [
        "<tr>"
        f'<th scope="row">{_source_link(p["source_url"], p["company"])}</th>'
        f"<td>{_company_link(p['ticker'], root, known)}</td>"
        f"<td>{_e(p['role'])}</td>"
        f"<td>{'yes' if p['listed'] else 'no'}</td>"
        f"<td>{_e(p['note'])}</td>"
        "</tr>"
        for p in players
    ]
    return _table("players", _PLAYERS_HEAD, rows)


def _stage_nav(stack: Stack, index: int, root: str) -> str:
    """Previous stage, the chain, next stage: whichever neighbours exist."""
    parts = []
    if index > 0:
        prev = stack.stages[index - 1]
        parts.append(
            f'<a class="prev" href="{_e(_stage_href(root, prev["stage"]))}">'
            f"← {_e(prev['order'])} {_e(prev['name'])}</a>"
        )
    parts.append(f'<a href="{_e(root + "stack/index.html")}">Stack</a>')
    if index + 1 < len(stack.stages):
        nxt = stack.stages[index + 1]
        parts.append(
            f'<a class="next" href="{_e(_stage_href(root, nxt["stage"]))}">'
            f"{_e(nxt['order'])} {_e(nxt['name'])} →</a>"
        )
    return "".join(parts)


# --------------------------------------------------------------------------------------------
# The signals ledger: every row on signals/index.html, a stage's rows on its page, a company's
# rows on its page. Rows arrive validated from data/signals.py; this section only formats them.
# --------------------------------------------------------------------------------------------


def load_ledger(signals_dir: Path, stack_dir: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """The ledger as plain dicts, newest first; an empty list when there is nothing to show.

    No ledger is not a problem to report: ``data/signals.py`` treats it as "none yet", and the
    empty-section rule hides the signals and their nav link. A malformed ledger is named in
    ``warnings`` and the site is built without signals, on the stack's rule: the loader refuses
    the file so a bad row cannot reach a page, and the builder never fails on an input.
    Rows are dicts (``DataFrame.to_dict("records")``) so the renderers stay free of pandas; a
    blank ``value`` arrives as NaN, which ``fmt_number`` shows as the en dash.
    """
    try:
        frame = load_signals(signals_dir, stack_dir=stack_dir)
    except (ValueError, KeyError, OSError) as exc:
        # pandas' parser errors are ValueErrors; an unreadable file is an OSError. The loader
        # prefixes its own messages with the file name; pandas and the OS do not.
        message = str(exc)
        if "ledger.csv" not in message:
            message = f"ledger.csv: {message}"
        warnings.append(f"{message}; signals left empty")
        return []
    return [] if frame is None else frame.to_dict("records")


def _stage_link(stack: Stack | None, key: Any, root: str) -> str:
    """A stage key as its display name linked to the stage page; the key as text without one.

    The ledger can be built without a stack (or name a stage the stack does not have), and a
    link to a page that was not built would go nowhere.
    """
    text = str(key or "")
    if stack is not None and any(s["stage"] == text for s in stack.stages):
        return f'<a href="{_e(_stage_href(root, text))}">{_e(stack.name_of(text))}</a>'
    return _e(text)


def _signal_value(signal: dict[str, Any]) -> str:
    """``25 USD bn``: the plain number with its unit; the en dash when the signal has no number."""
    number = fmt_number(signal["value"])
    if number == MISSING:
        return MISSING
    return " ".join(part for part in (number, str(signal["unit"] or "").strip()) if part)


def _maps_to_parts(maps_to: Any) -> tuple[str, str]:
    """``("CRWV", "cost_of_debt")`` from ``CRWV: cost_of_debt``; ``("", text)`` without a colon.

    The one place the ticker is read out of ``maps_to``, so the index link, the company-page
    filter and the "Bears on" column cannot disagree about a row (case and spacing forgiven).
    """
    text = str(maps_to or "")
    head, sep, tail = text.partition(":")
    if not sep:
        return "", text.strip()
    return head.strip().upper(), tail.strip()


def _maps_to_html(maps_to: Any, root: str, known: set[str]) -> str:
    """``maps_to`` as written, with a leading ticker linked to its company page when one exists.

    ``CRWV: cost_of_debt`` links CRWV; ``stack: compute/price`` and ``watch`` are text.
    """
    text = str(maps_to or "")
    ticker, tail = _maps_to_parts(text)
    if ticker in known:
        # Shown as the ledger wrote it (case included); the link is to the canonical page.
        return f"{_company_link(text.partition(':')[0].strip(), root, known)}: {_e(tail)}"
    return _e(text)


def _bears_on(maps_to: Any) -> str:
    """What follows the ticker in ``maps_to``: the assumption a company-page signal bears on."""
    return _maps_to_parts(maps_to)[1]


def company_signals(signals: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    """The rows mapped to ``ticker``: ``maps_to`` opens with the ticker and a colon, any case."""
    wanted = ticker.upper()
    return [s for s in signals if _maps_to_parts(s["maps_to"])[0] == wanted]


def signals_meta(signals: list[dict[str, Any]]) -> str:
    """``6 signals · 5 confirmed · 1 reported · 0 speculated``, in the ledger's level order."""
    counts = Counter(str(s["confidence"]).lower() for s in signals)
    total = len(signals)
    parts = [f"{total} signal{'' if total == 1 else 's'}"]
    parts += [f"{counts.get(level, 0)} {level}" for level in CONFIDENCE]
    return " · ".join(parts)


# Column key -> (header, numeric). The three tables pick from these: a stage page needs no Stage
# column (the stage is the page) and no Bears-on (that is the ledger owner's business); a
# company page adds what each signal bears on; the index adds the stage and drops the
# counterparty, which the claim already names, to keep nine columns.
_SIGNALS_HEAD: dict[str, tuple[str, bool]] = {
    "date": ("Date", False),
    "kind": ("Kind", False),
    "stage": ("Stage", False),
    "actor": ("Actor", False),
    "counterparty": ("Counterparty", False),
    "claim": ("Claim", False),
    "value": ("Value", True),
    "confidence": ("Confidence", False),
    "bears_on": ("Bears on", False),  # the assumption named after the ticker (company pages)
    "maps_to": ("Bears on", False),  # the whole maps_to, ticker linked (the index)
    "source": ("Source", False),
}
STAGE_SIGNAL_COLUMNS = (
    "date",
    "kind",
    "actor",
    "counterparty",
    "claim",
    "value",
    "confidence",
    "source",
)
COMPANY_SIGNAL_COLUMNS = (
    "date",
    "kind",
    "actor",
    "counterparty",
    "claim",
    "value",
    "confidence",
    "bears_on",
    "source",
)
INDEX_SIGNAL_COLUMNS = (
    "date",
    "kind",
    "stage",
    "actor",
    "claim",
    "value",
    "confidence",
    "maps_to",
    "source",
)


def _signal_cell(
    column: str, signal: dict[str, Any], root: str, known: set[str], stack: Stack | None
) -> str:
    match column:
        case "date":
            return f'<td class="nowrap">{_e(signal["date"])}</td>'
        case "kind":
            return f'<td class="kind">{_e(signal["kind"])}</td>'
        case "stage":
            return f"<td>{_stage_link(stack, signal['stage'], root)}</td>"
        case "claim":
            return f'<td class="claim">{_e(signal["claim"])}</td>'
        case "value":
            return f'<td class="num">{_e(_signal_value(signal))}</td>'
        case "confidence":
            return _confidence_cell(signal["confidence"])
        case "bears_on":
            return f"<td>{_e(_bears_on(signal['maps_to']))}</td>"
        case "maps_to":
            return f"<td>{_maps_to_html(signal['maps_to'], root, known)}</td>"
        case "source":
            return f"<td>{_source_link(signal['source_url'], signal['source'])}</td>"
        case _:  # actor, counterparty: text as written
            return f"<td>{_e(signal[column])}</td>"


def _signals_table(
    signals: list[dict[str, Any]],
    columns: tuple[str, ...],
    root: str,
    known: set[str],
    stack: Stack | None = None,
) -> str:
    """The ledger rows given, in the order given, as one ``.table-scroll`` table; "" for none."""
    rows = [
        "<tr>" + "".join(_signal_cell(c, s, root, known, stack) for c in columns) + "</tr>"
        for s in signals
    ]
    return _table("signals", [_SIGNALS_HEAD[c] for c in columns], rows)


# --------------------------------------------------------------------------------------------
# Output validation
# --------------------------------------------------------------------------------------------


class _LinkAuditor(HTMLParser):
    """Collect ``href``/``src`` values that start with ``/`` (they break under ``/<repo>/``)."""

    def __init__(self) -> None:
        super().__init__()
        self.absolute: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"href", "src"} and value and value.startswith("/"):
                self.absolute.append(f"<{tag} {name}={value!r}>")


def audit_html(text: str) -> list[str]:
    """Return problems with a generated page: unparsable markup or absolute links."""
    auditor = _LinkAuditor()
    try:
        auditor.feed(text)
        auditor.close()
    except Exception as exc:  # html.parser is lenient; anything it rejects is worth a warning
        return [f"does not parse: {exc}"]
    return [f"absolute link {link}" for link in auditor.absolute]


# --------------------------------------------------------------------------------------------
# Loading inputs
# --------------------------------------------------------------------------------------------


def _read_json(path: Path, warnings: list[str], required: bool) -> Any:
    if not path.exists():
        if required:
            warnings.append(f"{path.name} missing; run `uv run scripts/refresh.py` to create it")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.append(f"{path.name} is not valid JSON ({exc}); ignored")
        return None


def load_writeups(content_dir: Path, warnings: list[str]) -> list[Writeup]:
    """Read published writeups from ``content_dir``, newest first.

    Files starting with ``_`` and anything not ``status: published`` are skipped, so a missing
    status can never publish by accident. ``status: draft`` is skipped silently; a file skipped
    for any other reason (no frontmatter, no status, unknown status) is named in ``warnings`` so
    the author can see why the page is missing.
    """
    writeups: list[Writeup] = []
    used_slugs: set[str] = set()
    if not content_dir.is_dir():
        return writeups
    for path in sorted(content_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        if _FRONTMATTER_RE.match(text) is None:
            warnings.append(
                f"{path.name}: no frontmatter found (the file must start with a '---' line); "
                "skipped"
            )
            continue
        meta, body = parse_frontmatter(text)
        if "status" not in meta:
            warnings.append(f"{path.name}: no status in frontmatter; treated as draft")
            continue
        status = meta["status"].lower()
        if status != "published":
            if status != "draft":
                warnings.append(f"{path.name}: unknown status {status!r}; treated as draft")
            continue
        title = meta.get("title") or path.stem
        if "title" not in meta:
            warnings.append(f"{path.name}: no title in frontmatter; using the file name")
        date = meta.get("date", "")
        if not _ISO_DATE_RE.fullmatch(date):
            warnings.append(f"{path.name}: date {date!r} is not YYYY-MM-DD")
        slug = slugify(meta.get("slug") or title)
        if slug in RESERVED_SLUGS:
            warnings.append(
                f"{path.name}: slug {slug!r} is the writeups listing page; using '{slug}-writeup'"
            )
            slug = f"{slug}-writeup"
        if slug in used_slugs:
            warnings.append(
                f"{path.name}: slug {slug!r} already used; suffixing with the file stem"
            )
            slug = f"{slug}-{slugify(path.stem)}"
        used_slugs.add(slug)
        tags = tuple(t.strip() for t in meta.get("tags", "").split(",") if t.strip())
        writeups.append(
            Writeup(
                slug=slug,
                title=title,
                date=date,
                company=meta.get("company", ""),
                summary=meta.get("summary", ""),
                tags=tags,
                body_html=render_markdown(body),
            )
        )
    # Newest first; ties broken by title (ascending) so the order is stable across machines.
    writeups.sort(key=lambda w: w.title)
    writeups.sort(key=lambda w: w.date, reverse=True)
    return writeups


def _load_templates(templates_dir: Path) -> dict[str, Template]:
    templates = {}
    for name in TEMPLATE_NAMES:
        path = templates_dir / f"{name}.html"
        if not path.exists():
            raise FileNotFoundError(f"site template missing: {path}")
        templates[name] = Template(path.read_text(encoding="utf-8"))
    return templates


def _pick_dir(preferred: Path, fallback: Path) -> Path:
    """Let a custom site dir ship its own templates/static, else use the repo's."""
    return preferred if preferred.is_dir() else fallback


# --------------------------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------------------------


class _SiteWriter:
    """Writes finished pages, audits them, and records them in the report."""

    def __init__(
        self,
        out_dir: Path,
        templates: dict[str, Template],
        *,
        as_of: str,
        nav: tuple[tuple[str, str], ...],
    ) -> None:
        self.out_dir = out_dir
        self.templates = templates
        self.as_of = as_of
        self.nav = nav  # (label, href relative to the site root) for the sections that exist
        self.report = BuildReport()

    def page(self, rel: str, *, title: str, content: str, scripts: str = "") -> None:
        root = "../" * (rel.count("/"))  # companies/X.html -> "../"; index.html -> ""
        full_title = title if title == SITE_NAME else f"{title} · {SITE_NAME}"
        nav_links = "\n".join(
            f'    <a href="{_e(root + href)}">{_e(label)}</a>' for label, href in self.nav
        )
        if self.as_of:
            updated = (
                f'Updated <time datetime="{_e(self.as_of)}">{_e(fmt_timestamp(self.as_of))}</time>'
            )
        else:
            updated = "Not refreshed"
        text = self.templates["base"].substitute(
            title=_e(full_title),
            root=root,
            content=content,
            nav_links=nav_links,
            updated=updated,
            repo_url=_e(REPO_URL),
            scripts=scripts,
        )
        for problem in audit_html(text):
            self.report.warnings.append(f"{rel}: {problem}")
        path = self.out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        self.report.pages.append(rel)


def _check_out_dir(out_dir: Path, source_dirs: list[Path]) -> None:
    """Raise ``ValueError`` when resetting ``out_dir`` could delete the build's own inputs.

    ``_reset_out_dir`` removes ``<out>/data``, ``<out>/static``, ``<out>/stack`` and
    ``<out>/signals`` among others, so ``--out site`` would wipe the refresh JSON and the
    stylesheet, and ``--out .`` the ``data`` package, the stack CSVs and the ledger themselves.
    Refused: a source dir, anything inside one, and any ancestor of one (the site dir, the repo
    root). ``site/build`` is none of these.
    """
    out = out_dir.resolve()
    for source in source_dirs:
        source = source.resolve()
        if out == source or source in out.parents or out in source.parents:
            raise ValueError(
                f"refusing to build into {out_dir}: it is, contains, or lies inside the "
                f"source directory {source}, and a build first deletes <out>/companies, "
                "<out>/writeups, <out>/stack, <out>/signals, <out>/data and <out>/static. Use "
                "a separate directory such as site/build."
            )


def _reset_out_dir(out_dir: Path) -> None:
    # Only the generated subtrees are cleared so a stray .gitkeep or CNAME survives a rebuild.
    for sub in ("companies", "writeups", "stack", "signals", "data", "static"):
        shutil.rmtree(out_dir / sub, ignore_errors=True)
        (out_dir / sub).mkdir(parents=True, exist_ok=True)


def build(
    *,
    site_dir: Path = SITE_DIR,
    out_dir: Path = SITE_BUILD_DIR,
    calls_md: Path = CALLS_MD,
    templates_dir: Path | None = None,
    static_dir: Path | None = None,
    stack_dir: Path = STACK_DIR,
    signals_dir: Path = SIGNALS_DIR,
) -> BuildReport:
    """Render the whole site into ``out_dir`` and return what was built.

    ``site_dir`` holds ``data/`` and ``content/``; templates and static assets come from it too
    when present, otherwise from the repo's ``site/templates`` and ``site/static``. ``stack_dir``
    holds the chain's CSVs and primers, ``signals_dir`` the ledger. Missing inputs produce an
    empty-state site plus warnings, never an exception. The one refusal is an ``out_dir`` that
    overlaps the sources (``ValueError`` from ``_check_out_dir``), raised before anything is
    deleted or written.
    """
    templates_src = templates_dir or _pick_dir(site_dir / "templates", SITE_TEMPLATES_DIR)
    static_src = static_dir or _pick_dir(site_dir / "static", SITE_STATIC_DIR)
    # The repo's own site dir is guarded as well as ``site_dir``: templates and static assets
    # fall back to it, and its ``data/`` holds the refresh output. The stack and signals dirs
    # are sources too, and a build deletes <out>/stack and <out>/signals.
    _check_out_dir(
        out_dir,
        [root / sub for root in (site_dir, SITE_DIR) for sub in SOURCE_SUBDIRS]
        + [templates_src, static_src, stack_dir, signals_dir],
    )
    templates = _load_templates(templates_src)
    data_dir = site_dir / "data"
    content_dir = site_dir / "content"
    warnings: list[str] = []

    companies_doc = _read_json(data_dir / "companies.json", warnings, required=True) or {}
    companies = [c for c in companies_doc.get("companies") or [] if isinstance(c, dict)]
    companies = [c for c in companies if c.get("ticker")]
    if companies_doc and not companies:
        warnings.append("companies.json lists no companies")
    as_of = str(companies_doc.get("as_of") or "")
    known = {str(c["ticker"]).upper() for c in companies}

    # Every company file is read up front because the index table needs the reported series too.
    data_by_ticker: dict[str, Any] = {}
    for company in companies:
        ticker = str(company["ticker"]).upper()
        data = _read_json(data_dir / f"{ticker}.json", warnings, required=False)
        if data is not None and not isinstance(data, dict):
            warnings.append(f"{ticker}.json is not a JSON object; ignored")
            data = None
        if data is None and company.get("has_data", False):
            warnings.append(f"{ticker}.json missing although has_data is true")
        if _model_status(company.get("model_status")) != "built" and _has_model_content(data):
            warnings.append(
                f"{ticker}.json carries model outputs or inputs but model_status is not 'built'; "
                "they are not shown"
            )
        data_by_ticker[ticker] = data

    writeups = load_writeups(content_dir, warnings)
    stack = load_stack(stack_dir, warnings)
    # The ledger's stage column is checked against this build's stack, not the repo's.
    signals = load_ledger(signals_dir, stack_dir, warnings)

    if calls_md.exists():
        calls = parse_calls(calls_md.read_text(encoding="utf-8"))
    else:
        calls = []
        warnings.append(f"{calls_md.name} not found; calls table left empty")

    # A link to a section that was left out would go nowhere, so the nav follows the content.
    nav = [("Companies", "index.html#companies")]
    if stack:
        nav.append(("Stack", "stack/index.html"))
    if signals:
        nav.append(("Signals", "signals/index.html"))
    if writeups:
        nav.append(("Writeups", "writeups/index.html"))
    if calls:
        nav.append(("Calls", "index.html#calls"))

    _reset_out_dir(out_dir)
    writer = _SiteWriter(out_dir, templates, as_of=as_of, nav=tuple(nav))
    writer.report.warnings.extend(warnings)

    # Index -----------------------------------------------------------------------------------
    index_writeups = _writeups_list(writeups[:INDEX_WRITEUPS], "", known)
    if index_writeups:
        index_writeups += '<p class="more"><a href="writeups/index.html">All writeups</a></p>'
    index_content = templates["index"].substitute(
        root="",
        companies_table=_companies_table(companies, data_by_ticker, ""),
        calls_section=_section("calls", "Calls", _calls_table(calls)),
        writeups_section=_section("writeups", "Writeups", index_writeups),
    )
    writer.page("index.html", title=SITE_NAME, content=index_content)

    # Company pages ---------------------------------------------------------------------------
    chart_scripts = f'<script src="{CHART_JS_URL}" defer></script>'
    for company in sorted(companies, key=lambda c: c["ticker"]):
        ticker = str(company["ticker"]).upper()
        data = data_by_ticker[ticker]
        cik = str(company.get("cik") or (data or {}).get("cik") or "")
        edgar_url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik}&type=&dateb=&owner=include&count=40"
            if cik
            else "https://www.sec.gov/edgar/search/"
        )
        layer = company.get("layer") or (data or {}).get("layer") or ""
        eyebrow = " · ".join(
            part
            for part in (
                f'<span class="layer">{_e(layer)}</span>' if layer else "",
                f"CIK {_e(cik)}" if cik else "",
            )
            if part
        )
        header_links = " ".join(
            part
            for part in (
                _filing_link(company.get("latest_filing")),
                f'<a href="{_e(edgar_url)}" rel="noopener">EDGAR</a>',
                _workbook_link(ticker, company.get("model_status")),
            )
            if part
        )
        mine = [w for w in writeups if w.company.upper() == ticker]
        content = templates["company"].substitute(
            root="../",
            ticker=_e(ticker),
            name=_e(company.get("name") or (data or {}).get("name") or ticker),
            eyebrow=eyebrow,
            status_badge=_badge(company.get("model_status")),
            header_links=header_links,
            key_figures=_key_figures_html(data),
            model_section=_model_section(data, company.get("model_status")),
            reported_html=_reported_html(data),
            signals_section=_section(
                "signals",
                "Signals",
                _signals_table(
                    company_signals(signals, ticker), COMPANY_SIGNAL_COLUMNS, "../", known
                ),
            ),
            writeups_section=_section(
                "company-writeups", "Writeups", _writeups_list(mine, "../", known)
            ),
            inline_json=_inline_json(data) if data is not None else "",
        )
        writer.page(
            f"companies/{ticker}.html",
            title=f"{company.get('name') or ticker} ({ticker})",
            content=content,
            scripts=chart_scripts,
        )

    # Writeups --------------------------------------------------------------------------------
    for w in writeups:
        company = w.company.upper()
        if company in known:
            company_html = f'<a href="../companies/{_e(company)}.html">{_e(company)}</a>'
        else:
            company_html = _e(w.company)
        tags_html = "".join(f'<li class="tag">{_e(t)}</li>' for t in w.tags)
        content = templates["writeup"].substitute(
            root="../",
            title=_e(w.title),
            date=_e(w.date),
            company=company_html,
            summary=_e(w.summary),
            tags=f'<ul class="tags">{tags_html}</ul>' if tags_html else "",
            body=w.body_html,
            calls_link=' · <a href="../index.html#calls">Calls</a>' if calls else "",
        )
        writer.page(w.href, title=w.title, content=content)

    writeups_index = templates["writeups_index"].substitute(
        root="../", writeups_list=_writeups_list(writeups, "../", known)
    )
    writer.page("writeups/index.html", title="Writeups", content=writeups_index)

    # Stack -----------------------------------------------------------------------------------
    if stack:
        index_content = templates["stack_index"].substitute(
            root="../",
            meta=f"{len(stack.stages)} stages",
            stages_table=_stages_table(stack, "../", known),
            tiers_section=_section(
                "consumption-tiers", "Consumption tiers", _tiers_table(stack.tiers)
            ),
        )
        writer.page("stack/index.html", title="Stack", content=index_content)
        for i, stage in enumerate(stack.stages):
            key = str(stage["stage"])
            metrics = [m for m in stack.metrics if m["stage"] == key]
            players = [p for p in stack.players if p["stage"] == key]
            conversions = [c for c in stack.conversions if key in (c["from_stage"], c["to_stage"])]
            primer = stack.primers.get(key, "")
            summary = str(stage["summary"] or "")
            content = templates["stack_stage"].substitute(
                root="../",
                eyebrow=f"Stage {_e(stage['order'])} of {len(stack.stages)}",
                name=_e(stage["name"]),
                stage=_e(key),
                summary=f'    <p class="lede">{_e(summary)}</p>' if summary else "",
                facts=_stage_facts(stack, stage, "../"),
                primer_section=_section(
                    "primer", "Primer", f'<div class="prose">{primer}</div>' if primer else ""
                ),
                figures_section=_section("figures", "Figures", _figures_table(metrics)),
                conversions_section=_section(
                    "conversions", "Conversions", _conversions_table(stack, conversions, "../")
                ),
                players_section=_section(
                    "players", "Players", _players_table(players, "../", known)
                ),
                signals_section=_section(
                    "signals",
                    "Signals",
                    _signals_table(
                        [s for s in signals if s["stage"] == key],
                        STAGE_SIGNAL_COLUMNS,
                        "../",
                        known,
                    ),
                ),
                stage_nav=_stage_nav(stack, i, "../"),
            )
            writer.page(f"stack/{key}.html", title=str(stage["name"]), content=content)

    # Signals ---------------------------------------------------------------------------------
    if signals:
        content = templates["signals_index"].substitute(
            root="../",
            meta=signals_meta(signals),
            signals_table=_signals_table(signals, INDEX_SIGNAL_COLUMNS, "../", known, stack),
        )
        writer.page("signals/index.html", title="Signals", content=content)

    # Data, static assets, Pages marker -------------------------------------------------------
    if data_dir.is_dir():
        for src in sorted(data_dir.glob("*.json")):
            shutil.copyfile(src, out_dir / "data" / src.name)
    (out_dir / "data" / "calls.json").write_text(
        json.dumps(calls, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    if static_src.is_dir():
        for src in sorted(static_src.iterdir()):
            if src.is_file() and not src.name.startswith("."):
                shutil.copyfile(src, out_dir / "static" / src.name)
    else:
        writer.report.warnings.append(f"static dir missing: {static_src}")
    # GitHub Pages runs Jekyll by default, which would drop files it dislikes; this opts out.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    writer.report.writeups = len(writeups)
    writer.report.companies = len(companies)
    writer.report.calls = len(calls)
    writer.report.stages = len(stack.stages) if stack else 0
    writer.report.signals = len(signals)
    return writer.report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: build the site and print a one-screen report."""
    parser = argparse.ArgumentParser(description="Build the static site into site/build/.")
    parser.add_argument("--out", type=Path, default=SITE_BUILD_DIR, help="output directory")
    parser.add_argument(
        "--site-dir", type=Path, default=SITE_DIR, help="dir with data/ and content/"
    )
    parser.add_argument("--calls", type=Path, default=CALLS_MD, help="path to calls.md")
    parser.add_argument(
        "--stack", type=Path, default=STACK_DIR, help="dir with stages.csv and the stack tables"
    )
    parser.add_argument(
        "--signals", type=Path, default=SIGNALS_DIR, help="dir with the ledger.csv of signals"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        report = build(
            site_dir=args.site_dir,
            out_dir=args.out,
            calls_md=args.calls,
            stack_dir=args.stack,
            signals_dir=args.signals,
        )
    except ValueError as exc:  # an --out that overlaps the sources; nothing was touched
        parser.error(str(exc))
    print(
        f"built {len(report.pages)} pages into {args.out}: {report.companies} companies, "
        f"{report.writeups} writeups, {report.calls} calls, {report.stages} stages, "
        f"{report.signals} signals"
    )
    for warning in report.warnings:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Build the static site in ``site/build/`` from JSON summaries, markdown writeups and calls.md.

Purpose
-------
``scripts/refresh.py`` writes a machine-readable summary of every company into ``site/data/`` and
the human writes essays into ``site/content/``. This module turns both into a small static site
that GitHub Pages can serve: an index with the project question, a companies grid and the calls
ledger; one page per company with server-rendered tables and Chart.js charts layered on top; and
one page per published writeup. There is no framework and no bundler: templates are
``string.Template`` files in ``site/templates/`` and the browser assets are copied verbatim from
``site/static/``.

Design notes
------------
* Every link is relative because GitHub Pages serves project sites under ``/<repo>/``.
* Every value that arrives from JSON or frontmatter is HTML-escaped. Writeup bodies are the
  author's own markdown and are rendered as trusted HTML.
* The builder never fails on missing inputs. A fresh clone with no data builds an empty-state
  site and lists what was missing in ``BuildReport.warnings``.
* Output is deterministic (sorted iteration, no timestamps beyond ``as_of``) so a rebuild with
  unchanged inputs produces byte-identical files.
* The generated subtrees of the output dir are deleted before each build, so an output dir that
  overlaps the sources (``--out site``, ``--out .``) is refused rather than built.
* This module is presentation only. Nothing here computes a financial number; it formats what the
  model already wrote to JSON.

CLI: ``uv run scripts/build_site.py [--out DIR] [--site-dir DIR] [--calls FILE]``
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
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from string import Template
from typing import Any

import markdown

from data import CALLS_MD, SITE_BUILD_DIR, SITE_DIR, SITE_STATIC_DIR, SITE_TEMPLATES_DIR

log = logging.getLogger(__name__)

# TODO(philbert): set REPO_OWNER to your GitHub handle so the repo and workbook links resolve.
REPO_OWNER = "<owner>"
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
    "shares_outstanding": "Shares outstanding",
}

TEMPLATE_NAMES = ("base", "index", "company", "writeup", "writeups_index")

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
    "built": "model built",
    "pending": "model pending",
    "no-model": "data only",
}


def _badge(model_status: str | None) -> str:
    status = (model_status or "pending").lower()
    label = _BADGE_LABELS.get(status, "model pending")
    return f'<span class="badge badge-{_e(status)}">{label}</span>'


def _workbook_link(ticker: str, model_status: str | None) -> str:
    """Link to ``models/<TICKER>.xlsx`` on GitHub, only once the model is built.

    The workbook is exported by a built model, so for a pending or data-only company the link
    would be a 404 on GitHub.
    """
    status = (model_status or "pending").lower()
    if status == "built":
        url = f"{REPO_URL}/blob/main/models/{ticker}.xlsx"
        return f'<a href="{_e(url)}" rel="noopener">Workbook: models/{_e(ticker)}.xlsx</a>'
    if status == "no-model":
        return ""  # no workbook is planned, so there is nothing to announce
    return '<span class="muted">Workbook: not exported yet</span>'


def _filing_link(latest_filing: Any) -> str:
    """``10-Q · 2025-08-14`` linked to EDGAR, or a plain note when nothing has been pulled."""
    if not isinstance(latest_filing, dict) or not latest_filing.get("url"):
        return '<span class="muted">no filing pulled yet</span>'
    text = " · ".join(str(latest_filing[k]) for k in ("form", "date") if latest_filing.get(k))
    return f'<a href="{_e(latest_filing["url"])}">{_e(text or "latest filing")}</a>'


def _company_card(company: dict[str, Any], root: str) -> str:
    ticker = str(company.get("ticker", "")).upper()
    name = company.get("name") or ticker
    href = f"{root}companies/{ticker}.html"
    data_note = "" if company.get("has_data", True) else '<p class="muted">no data yet</p>'
    return (
        '<article class="card">'
        f'<h3><a href="{_e(href)}">{_e(ticker)}</a></h3>'
        f'<p class="card-name">{_e(name)}</p>'
        f'<p class="card-meta"><span class="layer">{_e(company.get("layer", ""))}</span> '
        f"{_badge(company.get('model_status'))}</p>"
        f'<p class="card-filing">Latest filing: {_filing_link(company.get("latest_filing"))}</p>'
        f"{data_note}"
        "</article>"
    )


def _writeups_list(writeups: list[Writeup], root: str, known: set[str], empty_text: str) -> str:
    if not writeups:
        return f'<p class="note">{_e(empty_text)}</p>'
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
        return '<p class="note">No calls logged yet. The first writeup will add one.</p>'
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


def _points(raw: Any) -> list[tuple[str, Any]]:
    """Normalise ``[["2025Q2", 1.2e9], ...]`` and drop malformed entries instead of crashing."""
    points: list[tuple[str, Any]] = []
    for item in raw or []:
        if isinstance(item, list | tuple) and len(item) >= 2:
            points.append((str(item[0]), item[1]))
    return points


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
        return (
            '<p class="note">No reported data yet. <code>uv run scripts/refresh.py</code> '
            "pulls it from SEC EDGAR.</p>"
        )
    parts = []
    for freq in sorted(by_freq, key=lambda f: (f != "Q", f)):  # quarters first
        # Annual points come from SEC's CYyyyy frames, so for a filer whose year does not end in
        # December the column is a calendar year, not the fiscal year a finance reader expects.
        caption = "Latest quarters" if freq == "Q" else "Latest calendar years (SEC frames)"
        parts.append(_series_table(by_freq[freq], max_periods=6, caption=caption))
    if elements:
        # Filers tag the same line differently (capex is three different elements across the
        # seven companies), so the page names the element instead of implying they are identical.
        items = "".join(
            f"<li>{html.escape(label)}: <code>{html.escape(tag)}</code></li>"
            for label, tag in elements
        )
        parts.append(
            '<details class="sources"><summary>XBRL elements behind these series</summary>'
            f"<ul>{items}</ul></details>"
        )
    return "".join(parts)


def _outputs_html(data: dict[str, Any] | None, ticker: str, model_status: str | None) -> str:
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
    if not rows:
        if (model_status or "").lower() == "no-model":
            return (
                f'<p class="note">No model for {_e(ticker)}: it is tracked for its reported '
                "figures only (see TODO.md for what comes next).</p>"
            )
        return (
            f'<p class="note">Model pending: the {_e(ticker)} drivers are not written yet '
            "(see TODO.md). Reported figures above are live.</p>"
        )
    return _series_table(rows, max_periods=12, caption="Model outputs by period")


def _inputs_html(data: dict[str, Any] | None) -> str:
    inputs = (data or {}).get("inputs") or []
    if not inputs:
        return ""
    rows = []
    for row in inputs:
        row = row or {}
        value = fmt_value(row.get("value"), str(row.get("unit") or ""), compact=False)
        rows.append(
            f'<tr><th scope="row"><code>{_e(row.get("name"))}</code></th>'
            f'<td class="num">{_e(value)}</td><td class="unit">{_e(row.get("unit"))}</td>'
            f"<td>{_e(row.get('source'))}</td><td>{_e(row.get('note'))}</td></tr>"
        )
    return (
        '<section id="inputs"><h2>Assumptions</h2>'
        '<p class="muted">These are the blue cells of the workbook. Change them there.</p>'
        '<div class="table-scroll"><table class="inputs"><thead><tr>'
        '<th scope="col">Name</th><th scope="col" class="num">Value</th><th scope="col">Unit</th>'
        '<th scope="col">Source</th><th scope="col">Note</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


def _inline_json(data: Any) -> str:
    """JSON safe to embed in a ``<script type="application/json">`` (no ``</script>`` escape)."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")


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

    def __init__(self, out_dir: Path, templates: dict[str, Template], as_of: str) -> None:
        self.out_dir = out_dir
        self.templates = templates
        self.as_of = as_of
        self.report = BuildReport()

    def page(self, rel: str, *, title: str, content: str, scripts: str = "") -> None:
        root = "../" * (rel.count("/"))  # companies/X.html -> "../"; index.html -> ""
        full_title = title if title == SITE_NAME else f"{title} · {SITE_NAME}"
        text = self.templates["base"].substitute(
            title=_e(full_title),
            root=root,
            content=content,
            as_of=_e(self.as_of),
            owner=_e(REPO_OWNER),
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

    ``_reset_out_dir`` removes ``<out>/data`` and ``<out>/static`` among others, so ``--out site``
    would wipe the refresh JSON and the stylesheet, and ``--out .`` the ``data`` package itself.
    Refused: a source dir, anything inside one, and any ancestor of one (the site dir, the repo
    root). ``site/build`` is none of these.
    """
    out = out_dir.resolve()
    for source in source_dirs:
        source = source.resolve()
        if out == source or source in out.parents or out in source.parents:
            raise ValueError(
                f"refusing to build into {out_dir}: it is, contains, or lies inside the site "
                f"source directory {source}, and a build first deletes <out>/companies, "
                "<out>/writeups, <out>/data and <out>/static. Use a separate directory such as "
                "site/build."
            )


def _reset_out_dir(out_dir: Path) -> None:
    # Only the generated subtrees are cleared so a stray .gitkeep or CNAME survives a rebuild.
    for sub in ("companies", "writeups", "data", "static"):
        shutil.rmtree(out_dir / sub, ignore_errors=True)
        (out_dir / sub).mkdir(parents=True, exist_ok=True)


def build(
    *,
    site_dir: Path = SITE_DIR,
    out_dir: Path = SITE_BUILD_DIR,
    calls_md: Path = CALLS_MD,
    templates_dir: Path | None = None,
    static_dir: Path | None = None,
) -> BuildReport:
    """Render the whole site into ``out_dir`` and return what was built.

    ``site_dir`` holds ``data/`` and ``content/``; templates and static assets come from it too
    when present, otherwise from the repo's ``site/templates`` and ``site/static``. Missing inputs
    produce an empty-state site plus warnings, never an exception. The one refusal is an
    ``out_dir`` that overlaps the sources (``ValueError`` from ``_check_out_dir``), raised before
    anything is deleted or written.
    """
    templates_src = templates_dir or _pick_dir(site_dir / "templates", SITE_TEMPLATES_DIR)
    static_src = static_dir or _pick_dir(site_dir / "static", SITE_STATIC_DIR)
    # The repo's own site dir is guarded as well as ``site_dir``: templates and static assets
    # fall back to it, and its ``data/`` holds the refresh output.
    _check_out_dir(
        out_dir,
        [root / sub for root in (site_dir, SITE_DIR) for sub in SOURCE_SUBDIRS]
        + [templates_src, static_src],
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
    as_of = str(companies_doc.get("as_of") or "not yet refreshed")
    known = {str(c["ticker"]).upper() for c in companies}

    writeups = load_writeups(content_dir, warnings)

    if calls_md.exists():
        calls = parse_calls(calls_md.read_text(encoding="utf-8"))
    else:
        calls = []
        warnings.append(f"{calls_md.name} not found; calls table left empty")

    _reset_out_dir(out_dir)
    writer = _SiteWriter(out_dir, templates, as_of)
    writer.report.warnings.extend(warnings)

    # Index -----------------------------------------------------------------------------------
    cards = "".join(_company_card(c, "") for c in sorted(companies, key=lambda c: c["ticker"]))
    if not cards:
        cards = (
            '<p class="note">No company data yet. Run <code>uv run scripts/refresh.py</code>.</p>'
        )
    index_content = templates["index"].substitute(
        root="",
        companies_cards=cards,
        writeups_list=_writeups_list(writeups[:5], "", known, "No writeups published yet."),
        calls_table=_calls_table(calls),
    )
    writer.page("index.html", title=SITE_NAME, content=index_content)

    # Company pages ---------------------------------------------------------------------------
    chart_scripts = f'<script src="{CHART_JS_URL}" defer></script>'
    for company in sorted(companies, key=lambda c: c["ticker"]):
        ticker = str(company["ticker"]).upper()
        data = _read_json(data_dir / f"{ticker}.json", writer.report.warnings, required=False)
        if data is None and company.get("has_data", False):
            writer.report.warnings.append(f"{ticker}.json missing although has_data is true")
        cik = str(company.get("cik") or (data or {}).get("cik") or "")
        edgar_url = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik}&type=&dateb=&owner=include&count=40"
            if cik
            else "https://www.sec.gov/edgar/search/"
        )
        mine = [w for w in writeups if w.company.upper() == ticker]
        content = templates["company"].substitute(
            root="../",
            ticker=_e(ticker),
            name=_e(company.get("name") or (data or {}).get("name") or ticker),
            layer=_e(company.get("layer") or (data or {}).get("layer") or ""),
            cik=_e(cik or "n/a"),
            company_as_of=_e((data or {}).get("as_of") or as_of),
            status_badge=_badge(company.get("model_status")),
            latest_filing=_filing_link(company.get("latest_filing")),
            workbook_link=_workbook_link(ticker, company.get("model_status")),
            edgar_url=_e(edgar_url),
            reported_html=_reported_html(data),
            outputs_html=_outputs_html(data, ticker, company.get("model_status")),
            inputs_html=_inputs_html(data),
            writeups_list=_writeups_list(mine, "../", known, f"No writeups on {ticker} yet."),
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
        )
        writer.page(w.href, title=w.title, content=content)

    writeups_index = templates["writeups_index"].substitute(
        root="../",
        writeups_list=_writeups_list(writeups, "../", known, "No writeups published yet."),
    )
    writer.page("writeups/index.html", title="Writeups", content=writeups_index)

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
    return writer.report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: build the site and print a one-screen report."""
    parser = argparse.ArgumentParser(description="Build the static site into site/build/.")
    parser.add_argument("--out", type=Path, default=SITE_BUILD_DIR, help="output directory")
    parser.add_argument(
        "--site-dir", type=Path, default=SITE_DIR, help="dir with data/ and content/"
    )
    parser.add_argument("--calls", type=Path, default=CALLS_MD, help="path to calls.md")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        report = build(site_dir=args.site_dir, out_dir=args.out, calls_md=args.calls)
    except ValueError as exc:  # an --out that overlaps the sources; nothing was touched
        parser.error(str(exc))
    print(
        f"built {len(report.pages)} pages into {args.out}: {report.companies} companies, "
        f"{report.writeups} writeups, {report.calls} calls"
    )
    for warning in report.warnings:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

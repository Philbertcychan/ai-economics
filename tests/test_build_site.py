"""Tests for scripts/build_site.py.

Self-contained: ``make_site`` writes a small fixture site (two fictional companies, one quarterly
filer without a model and one annual filer with a built one, two published writeups, a draft, an
underscore-prefixed template, a calls.md with an escaped pipe and a blank placeholder row, the
fixture stack from ``tests/test_stack.py`` and a five-row signals ledger) into ``tmp_path`` and
the tests build it and inspect the output. Templates and static assets come from the real
``site/templates`` and ``site/static``, so the tests also exercise the shipped HTML and hold it to
the site's voice: labels, numbers, source lines and statuses, no explanatory prose. No network.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from test_stack import PRIMER_MD, STAGE_KEYS, STAGES_CSV, variant, write_stack

from data import REPO_ROOT, SITE_CONTENT_DIR, SITE_DIR, SITE_STATIC_DIR
from scripts.build_site import (
    CHART_JS_URL,
    MISSING,
    REPO_URL,
    SOURCE_SUBDIRS,
    BuildReport,
    Figure,
    _check_out_dir,
    audit_html,
    build,
    capex_to_revenue,
    change_direction,
    fmt_change,
    fmt_money,
    fmt_number,
    fmt_timestamp,
    fmt_value,
    headline_figures,
    latest_point,
    load_writeups,
    main,
    parse_calls,
    parse_frontmatter,
    prior_year_period,
    series_figure,
    signals_meta,
    slugify,
    split_basis,
    value_at,
    yoy_change,
)

# --------------------------------------------------------------------------------------------
# Fixture site
# --------------------------------------------------------------------------------------------

COMPANIES_JSON = {
    "as_of": "2026-09-12T11:00:00Z",
    "companies": [
        {
            "ticker": "AAA",
            "name": "Alpha Cloud, Inc.",
            "layer": "neocloud",
            "cik": "0000000001",
            "model_status": "pending",
            "latest_filing": {
                "form": "10-Q",
                "date": "2026-08-12",
                "url": "https://www.sec.gov/Archives/edgar/data/1/example/aaa-20260630.htm",
            },
            "has_data": True,
        },
        {
            "ticker": "BBB",
            "name": "Beta Compute N.V.",
            "layer": "neocloud",
            "cik": "0000000002",
            "model_status": "built",
            "latest_filing": None,
            "has_data": True,
        },
    ],
}

# Fictional companies and round toy values throughout: the builder only formats what it is given,
# and the repo must not carry invented figures, forecasts or graded calls about real companies.
# AAA is a quarterly filer: revenue and capex end in 2025Q3 with a 2024Q3 point to compare with,
# cash from operations and cash end a quarter earlier, and the year-earlier cash from operations
# is negative, so that tile has no percentage. It has no debt or PP&E series (no tile for those).
AAA_JSON = {
    "ticker": "AAA",
    "name": "Alpha Cloud, Inc.",
    "layer": "neocloud",
    "as_of": "2026-08-12",
    "reported": {
        "revenue": {
            "unit": "USD",
            "freq": "Q",
            "tag": "us-gaap:Revenues",
            "points": [
                ["2024Q3", 1.0e9],
                ["2025Q1", 982000000],
                ["2025Q2", 1200000000],
                ["2025Q3", 1400000000],
            ],
        },
        "capex": {
            "unit": "USD",
            "freq": "Q",
            "points": [["2024Q3", 3.5e9], ["2025Q1", 1.9e9], ["2025Q2", 2.9e9], ["2025Q3", 2.8e9]],
        },
        "cfo": {
            "unit": "USD",
            "freq": "Q",
            "points": [["2024Q2", -5.0e7], ["2025Q1", -100000000], ["2025Q2", 2.5e8]],
        },
        "cash": {
            "unit": "USD",
            "freq": "Q",
            "points": [["2024Q2", 1.0e9], ["2025Q1", 1.3e9], ["2025Q2", 1.15e9]],
        },
    },
    "outputs": None,
    "inputs": None,
}

# BBB is an annual filer: what refresh.py publishes for a company without quarterly frames.
BBB_JSON = {
    "ticker": "BBB",
    "name": "Beta Compute N.V.",
    "layer": "neocloud",
    "as_of": "2026-08-07",
    "reported": {
        "revenue": {"unit": "USD", "freq": "A", "points": [["2024", 8.0e7], ["2025", 100000000]]},
        "shares_outstanding": {"unit": "shares", "freq": "A", "points": [["2025", 240000000]]},
        "capex": {"unit": "USD", "freq": "A", "points": [["2024", 300000000], ["2025", 4.0e8]]},
    },
    "outputs": {
        "toy_output": {
            "label": "Toy output",
            "unit": "USD m",
            "points": [["2025A", 500.0], ["2026E", 1500.0]],
        },
        "toy_ratio": {
            "label": "Toy ratio",
            "unit": "%",
            "points": [["2025A", 0.55], ["2026E", 0.6]],
        },
    },
    "inputs": [
        {
            "name": "toy_price",
            "value": 32000,
            "unit": "USD",
            "source": "example source",
            # The register's note opens with a bracketed tag; the page shows it as its own column.
            "note": "[derived; proposed; range 4 to 6] example note & <caveat>",
        },
        {"name": "toy_share", "value": 0.6, "unit": "share", "source": "assumption", "note": ""},
        {
            "name": "toy_life",
            "value": 6,
            "unit": "years",
            "source": "",
            "note": "plain note [not a tag]",
        },
    ],
}

CALLS_MD = """# Calls

Every writeup's position lands here with the number that would falsify it.

| date | claim | falsifying number | deadline | outcome |
|---|---|---|---|---|
| 2026-09-21 | example claim: toy > $5bn | toy < $4bn \\| example cut | 2027-03-31 | open |
| 2026-09-22 | example claim: toy ratio rises | toy ratio below 50% | 2027-02-28 | wrong |
| | | | | |

<!--
Example row (not a call):
| 2026-01-01 | example claim | example number | 2026-12-31 | open |
Outcome vocabulary: open / right / wrong / partial
-->
"""

WRITEUP_AAA = """---
title: What an Alpha Cloud GPU-hour earns
date: 2026-09-21
company: AAA
summary: Example summary with <angle> brackets, to check escaping.
status: published
tags: unit-economics, alpha-cloud
---

## The question

Example question.

| driver | low | base | high |
|---|---|---|---|
| toy share | 50% | 60% | 70% |

## Position

Example position.

## What would prove this wrong

Example falsifier.
"""

WRITEUP_INDUSTRY = """---
title: "Depreciation & the GPU-hour"
date: 2026-09-25
company: industry
summary: Why useful life is the number that decides the argument.
status: published
---

Body text with **emphasis**.
"""

WRITEUP_DRAFT = """---
title: Draft on hyperscaler capex
date: 2026-09-30
company: CCC
summary: Not ready.
status: draft
---

Unfinished.
"""

# Published status on purpose: the leading underscore alone must keep it out of the build.
UNDERSCORE_TEMPLATE = """---
title: Template must never publish
date: 2026-01-01
company: industry
summary: If you can read this on the site, the underscore rule is broken.
status: published
---

## Position
"""

# Five toy signals against the fixture stack and the two fixture companies, out of date order so
# the "newest first" rule is exercised: two on power (one with no number, one whose claim has the
# two characters the site must escape), two mapped to AAA on compute, one mapped to BBB with its
# ticker in lower case and no source text. Fictional throughout, like the rest of the fixture.
LEDGER_CSV = """\
date,kind,stage,actor,counterparty,claim,value,unit,source_url,source,confidence,maps_to,note
2026-08-11,contract,compute,Alpha Cloud,Beta Labs,Beta commits $4 billion over five years,4,USD bn,https://example.com/aaa-contract,Example 8-K,confirmed,AAA: toy_price,Example note
2026-09-01,statement,power,Utility Co,,Says 2 GW of data-centre load <requested> & queued in its territory,2,GW,https://example.com/power-statement,Example press release,reported,stack: power/demand_pipeline_gw,
2026-07-15,price,power,Tracker,,Contract power prices flat quarter on quarter,,,https://example.com/power-price,Example tracker,speculated,watch,No number
2026-06-30,financing,systems,Beta Compute,lenders,Borrows $1.5 billion against servers,1.5,USD bn,https://example.com/bbb-loan,,confirmed,bbb: toy_life,Lower-case ticker on purpose
2026-05-01,buildout,compute,Alpha Cloud,,Second site of 300 MW under construction,300,MW,https://example.com/aaa-site,Example release,reported,AAA: toy_share,
"""  # noqa: E501  (CSV rows are one line each by definition)


def write_ledger(root: Path, text: str = LEDGER_CSV) -> Path:
    """Write ``text`` as ``root/signals/ledger.csv`` and return the signals dir."""
    signals = root / "signals"
    signals.mkdir(exist_ok=True)
    (signals / "ledger.csv").write_text(text, encoding="utf-8", newline="\n")
    return signals


def make_site(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write the fixture site and, beside it, the fixture stack and ledger.

    Returns (site_dir, out_dir, calls_md); builds pass ``stack_dir=tmp_path / "stack"`` and
    ``signals_dir=tmp_path / "signals"`` explicitly, so no test reads the repo's own tables.
    """
    write_stack(tmp_path)
    write_ledger(tmp_path)
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    (site / "content").mkdir()
    (site / "data" / "companies.json").write_text(json.dumps(COMPANIES_JSON), encoding="utf-8")
    (site / "data" / "AAA.json").write_text(json.dumps(AAA_JSON), encoding="utf-8")
    (site / "data" / "BBB.json").write_text(json.dumps(BBB_JSON), encoding="utf-8")
    content = site / "content"
    (content / "2026-09-21-alpha-cloud-gpu-hour.md").write_text(WRITEUP_AAA, encoding="utf-8")
    (content / "2026-09-25-depreciation.md").write_text(WRITEUP_INDUSTRY, encoding="utf-8")
    (content / "2026-09-30-draft.md").write_text(WRITEUP_DRAFT, encoding="utf-8")
    (content / "_template.md").write_text(UNDERSCORE_TEMPLATE, encoding="utf-8")
    calls = tmp_path / "calls.md"
    calls.write_text(CALLS_MD, encoding="utf-8")
    return site, tmp_path / "build", calls


@pytest.fixture
def built(tmp_path: Path) -> tuple[Path, BuildReport]:
    site, out, calls = make_site(tmp_path)
    return out, build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class LinkCollector(HTMLParser):
    """Collects every href/src so tests can check that internal targets exist."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.links.append(value)


class TableReader(HTMLParser):
    """Rows of the first ``<table class="...">`` with the wanted class, as lists of cell text."""

    def __init__(self, table_class: str) -> None:
        super().__init__()
        self.table_class = table_class
        self.rows: list[list[str]] = []
        self._inside = False
        self._done = False
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and not self._done:
            self._inside = self.table_class in (dict(attrs).get("class") or "").split()
        elif self._inside and tag == "tr":
            self.rows.append([])
        elif self._inside and tag in {"td", "th"}:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._inside:
            self._inside, self._done = False, True
        elif self._inside and tag in {"td", "th"} and self._cell is not None:
            self.rows[-1].append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def table_rows(page: str, table_class: str) -> list[list[str]]:
    reader = TableReader(table_class)
    reader.feed(page)
    return reader.rows


def without_noscript(page: str) -> str:
    return re.sub(r"<noscript>.*?</noscript>", "", page, flags=re.DOTALL)


# --------------------------------------------------------------------------------------------
# Build output
# --------------------------------------------------------------------------------------------


def test_build_creates_expected_files(built: tuple[Path, BuildReport]) -> None:
    out, report = built
    expected = [
        "index.html",
        "companies/AAA.html",
        "companies/BBB.html",
        "writeups/what-an-alpha-cloud-gpu-hour-earns.html",
        "writeups/depreciation-the-gpu-hour.html",
        "writeups/index.html",
        "stack/index.html",
        *(f"stack/{key}.html" for key in STAGE_KEYS),
        "signals/index.html",
    ]
    for rel in expected:
        assert (out / rel).is_file(), rel
    assert sorted(report.pages) == sorted(expected)
    for rel in ("static/style.css", "static/dashboard.js", ".nojekyll", "data/calls.json"):
        assert (out / rel).is_file(), rel
    counts = (report.companies, report.writeups, report.calls, report.stages, report.signals)
    assert counts == (2, 2, 2, 9, 5)
    assert not report.warnings, report.warnings


COMPANIES_HEADER = [
    "Company",
    "Layer",
    "Period",
    "Revenue",
    "Revenue YoY",
    "Capex",
    "Capex YoY",
    "Capex / revenue",
    "Model",
]


def test_index_header_question_and_one_line_footer(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    index = read(out / "index.html")
    assert "<h1>AI economics</h1>" in index
    question = (
        "What does a dollar of GPU compute earn, who captures it, "
        "and does supply match guided demand?"
    )
    assert f'<p class="question">{question}</p>' in index
    footer = index.split('<footer class="site-footer">', 1)[1].split("</footer>", 1)[0]
    assert footer.count("<p") == 1
    assert (
        "Source: SEC EDGAR · Updated "
        '<time datetime="2026-09-12T11:00:00Z">2026-09-12 11:00 UTC</time> · '
        f'<a href="{REPO_URL}" rel="noopener">GitHub</a>'
    ) in footer


def test_index_is_one_table_of_companies(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    index = read(out / "index.html")
    assert 'class="card"' not in index and "<article" not in index
    rows = table_rows(index, "companies")
    assert rows[0] == COMPANIES_HEADER
    # AAA: 2025Q3 against 2024Q3 (1.4bn / 1.0bn, 2.8bn / 3.5bn), capex / revenue = 2.8 / 1.4.
    # BBB: calendar 2025 against 2024 (100m / 80m, 400m / 300m), capex / revenue = 400 / 100.
    assert rows[1:] == [
        [
            "AAA Alpha Cloud, Inc.",
            "neocloud",
            "2025Q3",
            "$1.4bn",
            "+40.0%",
            "$2.8bn",
            "-20.0%",
            "200.0%",
            "model in progress",
        ],
        [
            "BBB Beta Compute N.V.",
            "neocloud",
            "2025",
            "$100m",
            "+25.0%",
            "$400m",
            "+33.3%",
            "400.0%",
            "model",
        ],
    ]
    assert 'href="companies/AAA.html"' in index and 'href="companies/BBB.html"' in index
    # The sign of a change is carried by a class, so the stylesheet can colour it.
    assert '<td class="num chg pos">+40.0%</td>' in index
    assert '<td class="num chg neg">-20.0%</td>' in index


def test_index_rows_sort_by_layer_then_ticker_and_blank_out_missing_figures(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    layers = {
        "HYB": "hyperscaler",
        "CHP": "chip",
        "ZNC": "neocloud",
        "PWR": "power",  # not a layer the site knows: after the known ones
        "HYA": "hyperscaler",
        "ANC": "neocloud",
    }
    companies = [
        {"ticker": t, "name": f"{t} Corp", "layer": layer, "model_status": "no-model"}
        for t, layer in layers.items()
    ]
    (site / "data" / "companies.json").write_text(
        json.dumps({"as_of": "2026-09-12T11:00:00Z", "companies": companies}), encoding="utf-8"
    )
    # Revenue only, with no year-earlier quarter and no capex series at all.
    (site / "data" / "ANC.json").write_text(
        json.dumps({"reported": {"revenue": {"unit": "USD", "points": [["2025Q2", 5.0e8]]}}}),
        encoding="utf-8",
    )
    out = tmp_path / "build"
    build(
        site_dir=site,
        out_dir=out,
        calls_md=tmp_path / "missing-calls.md",
        stack_dir=tmp_path / "no-stack",
        signals_dir=tmp_path / "no-signals",
    )
    rows = table_rows(read(out / "index.html"), "companies")[1:]
    assert [row[0].split()[0] for row in rows] == ["ANC", "ZNC", "CHP", "HYA", "HYB", "PWR"]
    assert rows[0][2:8] == ["2025Q2", "$500m", MISSING, MISSING, MISSING, MISSING]
    assert rows[1][2:8] == [MISSING] * 6  # no data file
    assert MISSING == "–"


def test_index_lists_writeups_and_calls_when_they_exist(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    index = read(out / "index.html")
    assert '<section id="calls" class="section"><h2>Calls</h2>' in index
    assert '<section id="writeups" class="section"><h2>Writeups</h2>' in index
    assert index.index('id="companies"') < index.index('id="calls"') < index.index('id="writeups"')
    assert 'href="index.html#calls">Calls</a>' in index  # nav
    assert 'href="writeups/index.html">Writeups</a>' in index
    # Writeups newest first, escaped titles and summaries.
    newest = index.index("Depreciation &amp; the GPU-hour")
    older = index.index("What an Alpha Cloud GPU-hour earns")
    assert newest < older
    assert "&lt;angle&gt;" in index and "<angle>" not in index
    # Calls table with the escaped pipe restored and the outcome class; the blank row is dropped.
    assert "example claim: toy &gt; $5bn" in index
    assert "toy &lt; $4bn | example cut" in index
    assert 'class="outcome outcome-wrong"' in index
    calls = table_rows(index, "calls")
    assert calls[0] == ["Date", "Claim", "Falsifying number", "Deadline", "Outcome"]
    assert [row[0] for row in calls[1:]] == ["2026-09-21", "2026-09-22"]
    assert "outcome-untitled" not in index


def test_empty_sections_and_their_nav_links_are_left_out(tmp_path: Path) -> None:
    """No calls and no published writeups: no heading, no "none yet" line, no dead nav link."""
    site, out, _ = make_site(tmp_path)
    for path in (site / "content").glob("*.md"):
        path.unlink()
    no_calls = tmp_path / "no-calls.md"
    no_calls.write_text(CALLS_MD.split("| 2026-09-21", 1)[0], encoding="utf-8")
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=no_calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    assert (report.writeups, report.calls) == (0, 0) and not report.warnings
    for page in ("index.html", "companies/AAA.html", "companies/BBB.html"):
        text = read(out / page)
        for gone in ('id="calls"', 'id="writeups"', 'id="company-writeups"', "<h2>Writeups</h2>"):
            assert gone not in text, (page, gone)
        assert "#calls" not in text and "writeups/index.html" not in text, page
        assert "Companies</a>" in text and "GitHub</a>" in text
    assert "<li>" not in read(out / "writeups" / "index.html")


def test_index_shows_only_newest_five_writeups(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    for day in range(1, 7):
        text = WRITEUP_INDUSTRY.replace("2026-09-25", f"2026-10-{day:02d}").replace(
            '"Depreciation & the GPU-hour"', f"Writeup number {day}"
        )
        (site / "content" / f"2026-10-{day:02d}-n{day}.md").write_text(text, encoding="utf-8")
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    assert report.writeups == 8
    index = read(out / "index.html")
    assert "Writeup number 6" in index and "Writeup number 2" in index
    assert "Writeup number 1" not in index
    assert "Writeup number 1" in read(out / "writeups" / "index.html")


def test_drafts_and_underscore_files_are_skipped(built: tuple[Path, BuildReport]) -> None:
    out, report = built
    everything = "\n".join(read(p) for p in out.rglob("*.html"))
    assert "Draft on hyperscaler capex" not in everything
    assert "Template must never publish" not in everything
    assert not (out / "writeups" / "draft-on-hyperscaler-capex.html").exists()
    assert not (out / "writeups" / "template-must-never-publish.html").exists()
    assert report.writeups == 2


def test_links_are_relative_and_internal_targets_exist(built: tuple[Path, BuildReport]) -> None:
    out, report = built
    pages = list(out.rglob("*.html"))
    assert pages
    for page in pages:
        text = read(page)
        assert 'href="/' not in text and 'src="/' not in text, page
        assert audit_html(text) == [], page
        collector = LinkCollector()
        collector.feed(text)
        for link in collector.links:
            if link.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = (page.parent / link.split("#", 1)[0]).resolve()
            assert target.exists(), f"{page.name} -> {link}"
    assert not any("absolute link" in w for w in report.warnings)


def test_data_is_copied_and_calls_json_written(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    assert json.loads(read(out / "data" / "AAA.json")) == AAA_JSON
    assert json.loads(read(out / "data" / "companies.json")) == COMPANIES_JSON
    calls = json.loads(read(out / "data" / "calls.json"))
    assert calls == [
        {
            "date": "2026-09-21",
            "claim": "example claim: toy > $5bn",
            "falsifying_number": "toy < $4bn | example cut",
            "deadline": "2027-03-31",
            "outcome": "open",
        },
        {
            "date": "2026-09-22",
            "claim": "example claim: toy ratio rises",
            "falsifying_number": "toy ratio below 50%",
            "deadline": "2027-02-28",
            "outcome": "wrong",
        },
    ]


def test_company_page_without_a_built_model(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    page = read(out / "companies" / "AAA.html")
    assert "<title>Alpha Cloud, Inc. (AAA) · AI economics</title>" in page
    # Header: layer, CIK, status badge, latest filing and EDGAR. No workbook exists until the
    # model is built, so a link would be a 404 on GitHub.
    assert '<p class="eyebrow"><span class="layer">neocloud</span> · CIK 0000000001</p>' in page
    assert '<span class="badge badge-pending">model in progress</span>' in page
    assert 'href="https://www.sec.gov/Archives/edgar/data/1/example/' in page
    assert "10-Q · 2026-08-12" in page
    assert "CIK=0000000001" in page and 'rel="noopener">EDGAR</a>' in page
    assert "xlsx" not in page and "Workbook" not in page
    # The badge is the whole model status: no model section, no chart host for outputs.
    assert 'id="model"' not in page and "<h2>Model</h2>" not in page
    assert 'data-charts="outputs"' not in page
    assert "Assumptions" not in page and "Outputs" not in page
    # The element behind a series is named, and only for series that carry one.
    assert "<summary>XBRL elements</summary>" in page
    assert "<li>Revenue: <code>us-gaap:Revenues</code></li>" in page
    assert "Capex: <code>" not in page
    # Server-side table of reported values, formatted bn/m, with negatives.
    assert "<h2>Reported</h2>" in page
    for cell in ("$1.2bn", "$982m", "$1.4bn", "-$100m", "$2.9bn"):
        assert cell in page, cell
    assert "2025Q1" in page and "2025Q3" in page
    assert "<caption>Quarters</caption>" in page
    # Hooks for dashboard.js: relative data-src, inline JSON copy, Chart.js from cdnjs.
    assert 'data-src="../data/AAA.json"' in page
    assert 'data-charts="reported"' in page
    assert '<script type="application/json" data-company>{"ticker":"AAA"' in page
    assert CHART_JS_URL in page
    assert 'href="../static/style.css"' in page
    # Only this company's writeups are listed.
    assert '<section id="company-writeups" class="section"><h2>Writeups</h2>' in page
    assert "What an Alpha Cloud GPU-hour earns" in page
    assert "Depreciation &amp; the GPU-hour" not in page


def kpi_tiles(page: str) -> list[str]:
    return re.findall(r'<div class="kpi">(.*?)</div>', page)


def test_key_figures_tiles_show_latest_value_change_and_period(
    built: tuple[Path, BuildReport],
) -> None:
    out, _ = built
    page = read(out / "companies" / "AAA.html")
    assert '<section id="key-figures" class="section"><h2>Key figures</h2><dl class="kpis">' in page
    assert kpi_tiles(page) == [
        '<dt>Revenue</dt><dd class="kpi-value">$1.4bn</dd>'
        '<dd class="kpi-note"><span class="chg pos">+40.0% YoY</span> · 2025Q3</dd>',
        '<dt>Capex</dt><dd class="kpi-value">$2.8bn</dd>'
        '<dd class="kpi-note"><span class="chg neg">-20.0% YoY</span> · 2025Q3</dd>',
        # The year-earlier quarter was an outflow, so there is no percentage, only the period.
        '<dt>Cash from operations</dt><dd class="kpi-value">$250m</dd>'
        '<dd class="kpi-note">2025Q2</dd>',
        '<dt>Cash</dt><dd class="kpi-value">$1.1bn</dd>'
        '<dd class="kpi-note"><span class="chg pos">+15.0% YoY</span> · 2025Q2</dd>',
    ]  # no long-term debt or PP&E series, so no tile for either
    # An annual filer compares calendar years.
    assert kpi_tiles(read(out / "companies" / "BBB.html")) == [
        '<dt>Revenue</dt><dd class="kpi-value">$100m</dd>'
        '<dd class="kpi-note"><span class="chg pos">+25.0% YoY</span> · 2025</dd>',
        '<dt>Capex</dt><dd class="kpi-value">$400m</dd>'
        '<dd class="kpi-note"><span class="chg pos">+33.3% YoY</span> · 2025</dd>',
    ]


def test_company_page_built_model(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    page = read(out / "companies" / "BBB.html")
    assert '<span class="badge badge-built">model</span>' in page
    workbook_url = html.escape(f"{REPO_URL}/blob/main/models/BBB.xlsx")
    assert f'<a href="{workbook_url}" rel="noopener">Workbook</a>' in page
    # Key figures, then the model (charts, outputs, assumptions), then the filings.
    order = [
        page.index(marker)
        for marker in (
            'id="key-figures"',
            '<section id="model" class="section"><h2>Model</h2>',
            'data-charts="outputs"',
            "<caption>Outputs</caption>",
            "<caption>Assumptions</caption>",
            '<section id="reported"',
        )
    ]
    assert order == sorted(order)
    for cell in ("$500m", "$1.5bn", "55.0%", "60.0%", "2025A", "2026E"):
        assert cell in page, cell
    # Assumptions register: exact input values (not abbreviated), and the bracketed tag that
    # opens a note split off into the Basis column.
    assert "<code>toy_price</code>" in page
    assert table_rows(page, "inputs") == [
        ["Assumption", "Value", "Unit", "Basis", "Source", "Note"],
        [
            "toy_price",
            "$32,000",
            "USD",
            "derived; proposed; range 4 to 6",
            "example source",
            "example note & <caveat>",
        ],
        ["toy_share", "60.0%", "share", "", "assumption", ""],
        ["toy_life", "6", "years", "", "", "plain note [not a tag]"],
    ]
    assert '<td class="basis">derived; proposed; range 4 to 6</td>' in page
    assert "example note &amp; &lt;caveat&gt;" in page.split("<script", 1)[0]
    assert "<caveat>" not in page and "[derived" not in page.split("<script", 1)[0]
    assert "Shares outstanding" in page and "240m" in page
    assert '<span class="muted">no filing</span>' in page  # latest_filing is null
    assert 'id="company-writeups"' not in page  # none about BBB


def test_single_period_outputs_are_one_table_and_no_charts(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    snapshot = json.loads(json.dumps(BBB_JSON))
    snapshot["outputs"]["toy_output"]["points"] = [["2024Q4", 500.0]]
    snapshot["outputs"]["toy_ratio"]["points"] = [["2024Q4", 0.55]]
    (site / "data" / "BBB.json").write_text(json.dumps(snapshot), encoding="utf-8")
    build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    page = read(out / "companies" / "BBB.html")
    model = page.split('<section id="model"', 1)[1].split("</section>", 1)[0]
    assert 'data-charts="outputs"' not in page and 'class="charts"' not in model
    assert "<caption>Outputs · 2024Q4</caption>" in model
    assert table_rows(page, "outputs") == [
        ["Output", "Value", "Unit"],
        ["Toy output", "$500m", "USD m"],
        ["Toy ratio", "55.0%", "%"],
    ]
    assert 'class="series"' not in model  # no table by period beside it
    # The assumptions follow, and the model section holds the two tables and nothing else.
    assert model.index('class="outputs"') < model.index("<caption>Assumptions</caption>")
    assert "<p" not in model
    assert 'data-charts="reported"' in page  # the filings are still charted

    # Outputs that end in different periods: a Period column, not one period in the caption.
    snapshot["outputs"]["toy_ratio"]["points"] = [["2025Q1", 0.55]]
    (site / "data" / "BBB.json").write_text(json.dumps(snapshot), encoding="utf-8")
    build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    page = read(out / "companies" / "BBB.html")
    assert "<caption>Outputs</caption>" in page and 'data-charts="outputs"' not in page
    assert table_rows(page, "outputs") == [
        ["Output", "Period", "Value", "Unit"],
        ["Toy output", "2024Q4", "$500m", "USD m"],
        ["Toy ratio", "2025Q1", "55.0%", "%"],
    ]


def test_outputs_over_several_periods_keep_charts_and_the_table_by_period(
    built: tuple[Path, BuildReport],
) -> None:
    out, _ = built
    page = read(out / "companies" / "BBB.html")
    model = page.split('<section id="model"', 1)[1].split("</section>", 1)[0]
    assert '<div class="charts" data-charts="outputs"></div>' in model
    assert 'class="outputs"' not in model
    rows = table_rows(model, "series")
    assert rows[0] == ["Item", "Unit", "2025A", "2026E"]
    assert rows[1] == ["Toy output", "USD m", "$500m", "$1.5bn"]
    assert "<p" not in model


def test_model_content_is_hidden_until_the_status_says_built(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    companies = json.loads(json.dumps(COMPANIES_JSON))
    companies["companies"][1]["model_status"] = "pending"
    (site / "data" / "companies.json").write_text(json.dumps(companies), encoding="utf-8")
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    page = read(out / "companies" / "BBB.html")
    assert 'id="model"' not in page and "toy_price" not in page.split("<script", 1)[0]
    assert "Toy output" not in page.split("<script", 1)[0]
    assert [w for w in report.warnings if w.startswith("BBB.json carries model outputs")]


def test_status_labels_shown_to_readers(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    companies = json.loads(json.dumps(COMPANIES_JSON))
    companies["companies"].append(
        {"ticker": "CCC", "name": "Gamma Chips Corp", "layer": "chip", "model_status": "no-model"}
    )
    (site / "data" / "companies.json").write_text(json.dumps(companies), encoding="utf-8")
    build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    rows = table_rows(read(out / "index.html"), "companies")[1:]
    assert {row[0].split()[0]: row[-1] for row in rows} == {
        "AAA": "model in progress",
        "BBB": "model",
        "CCC": "reported data",
    }
    # The vocabulary in the JSON is unchanged; only the label a reader sees differs.
    published = json.loads(read(out / "data" / "companies.json"))
    assert [c["model_status"] for c in published["companies"]] == ["pending", "built", "no-model"]


SOURCE_LINE = (
    '<p class="source">SEC XBRL, calendar periods. '
    "Cash-flow quarters derived from year-to-date filings.</p>"
)


def test_reported_section_has_one_source_line_and_a_noscript_note(
    built: tuple[Path, BuildReport],
) -> None:
    out, _ = built
    for ticker in ("AAA", "BBB"):
        page = read(out / "companies" / f"{ticker}.html")
        reported = page.split('<section id="reported"', 1)[1].split("</section>", 1)[0]
        assert reported.count(SOURCE_LINE) == 1
        assert "<noscript>" in reported and "Charts need JavaScript" in reported
        assert "Charts need JavaScript" not in without_noscript(page)
        # Nothing but the source line between the heading and the chart host.
        lead = reported.split("<h2>Reported</h2>", 1)[1].split('<div class="charts"', 1)[0]
        assert without_noscript(lead).strip() == SOURCE_LINE


def test_annual_table_is_captioned_as_calendar_years(built: tuple[Path, BuildReport]) -> None:
    """Annual points are SEC ``CYyyyy`` frames, so "fiscal years" would mislabel a January FYE."""
    out, _ = built
    page = read(out / "companies" / "BBB.html")
    assert "<caption>Calendar years</caption>" in page
    assert "<caption>Quarters</caption>" not in page
    assert "fiscal" not in page.lower()
    assert "$300m" in page and "$400m" in page


def test_company_page_data_only(tmp_path: Path) -> None:
    """``no-model`` (refresh.py's status for companies without a model class) is not "pending"."""
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    companies = {
        "as_of": "2026-09-12T11:00:00Z",
        "companies": [
            {
                "ticker": "CCC",
                "name": "Gamma Chips Corp",
                "layer": "chip",
                "cik": "0000000003",
                "model_status": "no-model",
                "latest_filing": None,
                "has_data": True,
            }
        ],
    }
    (site / "data" / "companies.json").write_text(json.dumps(companies), encoding="utf-8")
    (site / "data" / "CCC.json").write_text(
        json.dumps({"ticker": "CCC", "reported": {}, "outputs": None, "inputs": None}),
        encoding="utf-8",
    )
    out = tmp_path / "build"
    build(
        site_dir=site,
        out_dir=out,
        calls_md=tmp_path / "missing-calls.md",
        stack_dir=tmp_path / "no-stack",
        signals_dir=tmp_path / "no-signals",
    )
    index = read(out / "index.html")
    page = read(out / "companies" / "CCC.html")
    assert 'class="badge badge-no-model">reported data</span>' in index
    assert 'class="badge badge-no-model">reported data</span>' in page
    for text in (index, page):
        assert "model in progress" not in text
    # A data-only company never gets a workbook or a model section.
    assert "xlsx" not in page and "Workbook" not in page
    assert 'id="model"' not in page and 'data-charts="outputs"' not in page
    assert 'rel="noopener">EDGAR</a>' in page
    # No series yet: no tiles, and the reported section carries a status, not a sentence.
    assert 'id="key-figures"' not in page
    assert '<p class="note">No data</p>' in page


def test_index_page_has_no_chart_library(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    assert CHART_JS_URL not in read(out / "index.html")
    assert 'src="static/dashboard.js"' in read(out / "index.html")


def test_writeup_page_renders_markdown(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    page = read(out / "writeups" / "what-an-alpha-cloud-gpu-hour-earns.html")
    assert "<title>What an Alpha Cloud GPU-hour earns · AI economics</title>" in page
    assert '<h2 id="the-question">The question</h2>' in page
    assert '<div class="table-scroll"><table>' in page  # markdown tables get the scroll wrapper
    assert 'href="../companies/AAA.html"' in page
    assert "&lt;angle&gt;" in page
    assert '<li class="tag">unit-economics</li>' in page
    industry = read(out / "writeups" / "depreciation-the-gpu-hour.html")
    assert "<strong>emphasis</strong>" in industry
    assert "· industry" in industry


def test_writeups_index_lists_all_published(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    page = read(out / "writeups" / "index.html")
    assert 'href="../writeups/what-an-alpha-cloud-gpu-hour-earns.html"' in page
    assert 'href="../writeups/depreciation-the-gpu-hour.html"' in page
    assert "Draft on hyperscaler capex" not in page


def test_empty_state_build_succeeds_with_warning(tmp_path: Path) -> None:
    out = tmp_path / "out"
    report = build(
        site_dir=tmp_path / "nothing-here",
        out_dir=out,
        calls_md=tmp_path / "missing-calls.md",
        stack_dir=tmp_path / "no-stack",
        signals_dir=tmp_path / "no-signals",
    )
    assert "index.html" in report.pages and "writeups/index.html" in report.pages
    assert (report.companies, report.writeups, report.calls, report.stages) == (0, 0, 0, 0)
    assert any("companies.json" in w for w in report.warnings), report.warnings
    assert any("missing-calls.md" in w for w in report.warnings), report.warnings
    assert any("stack pages not built" in w for w in report.warnings), report.warnings
    index = read(out / "index.html")
    # A minimal page: the header, the table with its columns and no rows, the footer.
    assert "<h1>AI economics</h1>" in index
    assert table_rows(index, "companies") == [COMPANIES_HEADER]
    assert "<tbody></tbody>" in index
    assert 'id="calls"' not in index and 'id="writeups"' not in index
    assert '<p class="note">' not in index and "<code>" not in index
    assert "Source: SEC EDGAR · Not refreshed · " in index and "<time" not in index
    assert "<li>" not in read(out / "writeups" / "index.html")
    assert (out / ".nojekyll").exists() and (out / "static" / "style.css").exists()
    assert json.loads(read(out / "data" / "calls.json")) == []


# Sentences the redesign removed. The pages carry labels, numbers, source lines and statuses; the
# project and the method are described in the README, not on the site.
BANNED_PHRASES = (
    "three layers",
    "Every reported figure",
    "Every writeup",
    "Every number",
    "Hits and misses",
    "An open financial model",
    "Each one asks",
    "Each writeup",
    "blue cells",
    "Model pending",
    "model pending",
    "model built",
    "data only",
    "No model for",
    "TODO.md",
    "company-facts API",
    "carry the same numbers",
    "none yet",
    "not exported yet",
    "No writeups",
    "No calls",
    "No company data",
    "No reported data yet",
    "pulled yet",
    "refresh.py",
    "uv run",
    "Last refresh",
)


def test_no_generated_page_carries_explanatory_prose(tmp_path: Path) -> None:
    """Every page of three builds: the full fixture, a data-only company, and no data at all."""
    site, full, calls = make_site(tmp_path)
    build(
        site_dir=site,
        out_dir=full,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )

    bare = tmp_path / "bare-site"
    (bare / "data").mkdir(parents=True)
    company = {"ticker": "CCC", "name": "Gamma Chips Corp", "layer": "chip"}
    (bare / "data" / "companies.json").write_text(
        json.dumps({"companies": [company | {"model_status": "no-model", "has_data": True}]}),
        encoding="utf-8",
    )
    build(
        site_dir=bare,
        out_dir=tmp_path / "bare",
        calls_md=tmp_path / "missing-calls.md",
        stack_dir=tmp_path / "no-stack",
        signals_dir=tmp_path / "no-signals",
    )
    build(
        site_dir=tmp_path / "nothing",
        out_dir=tmp_path / "empty",
        calls_md=tmp_path / "x.md",
        stack_dir=tmp_path / "no-stack",
        signals_dir=tmp_path / "no-signals",
    )

    # The full build has the stack index, nine stage pages and the signals index; the other two
    # have neither a stack nor a ledger.
    pages = [p for d in ("build", "bare", "empty") for p in (tmp_path / d).rglob("*.html")]
    assert len(pages) == 17 + 3 + 2
    for page in pages:
        text = read(page)
        for phrase in BANNED_PHRASES:
            assert phrase not in text, f"{page.relative_to(tmp_path)}: {phrase!r}"
        assert "Charts need JavaScript" not in without_noscript(text), page
    # dashboard.js writes status notes into the page, so its strings are held to the same rule.
    script = read(SITE_STATIC_DIR / "dashboard.js")
    for phrase in ("carry the same numbers", "refresh.py", "Charts did not load"):
        assert phrase not in script, phrase


def test_rebuild_is_byte_identical(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    first = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    second = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert first == second


def test_main_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    site, out, calls = make_site(tmp_path)
    argv = ["--site-dir", str(site), "--out", str(out), "--calls", str(calls)]
    argv += ["--stack", str(tmp_path / "stack"), "--signals", str(tmp_path / "signals")]
    code = main(argv)
    assert code == 0
    printed = capsys.readouterr().out
    assert "built 17 pages" in printed and "2 companies" in printed and "9 stages" in printed
    assert "5 signals" in printed
    assert (out / "index.html").exists() and (out / "stack" / "power.html").exists()
    assert (out / "signals" / "index.html").exists()


# --------------------------------------------------------------------------------------------
# Output dir guard: a build deletes <out>/data and <out>/static, which must never be the sources
# --------------------------------------------------------------------------------------------


def site_files(site: Path) -> dict[Path, bytes]:
    return {p.relative_to(site): p.read_bytes() for p in site.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "target",
    [
        "site",
        "site/data",
        "site/content",
        "site/static",
        "site/templates",
        "site/data/x",
        "stack",
        "signals",
        ".",
    ],
)
def test_build_refuses_an_out_dir_that_overlaps_the_sources(tmp_path: Path, target: str) -> None:
    site, _, calls = make_site(tmp_path)
    (site / "static").mkdir()
    (site / "static" / "style.css").write_text("/* source */\n", encoding="utf-8")
    before = site_files(site)
    with pytest.raises(ValueError, match="refusing to build into"):
        build(
            site_dir=site,
            out_dir=tmp_path / target,
            calls_md=calls,
            stack_dir=tmp_path / "stack",
            signals_dir=tmp_path / "signals",
        )
    assert site_files(site) == before
    assert (tmp_path / "signals" / "ledger.csv").is_file()
    assert not (site / "index.html").exists() and not (tmp_path / "index.html").exists()


def test_build_allows_the_default_layout_of_a_build_dir_inside_the_site_dir(tmp_path: Path) -> None:
    site, _, calls = make_site(tmp_path)
    before = site_files(site)
    report = build(
        site_dir=site,
        out_dir=site / "build",
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    assert "index.html" in report.pages and (site / "build" / "data" / "AAA.json").is_file()
    assert {p: b for p, b in site_files(site).items() if p.parts[0] != "build"} == before


def test_build_also_protects_the_repo_site_dir_when_site_dir_is_custom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Templates and static assets fall back to the repo's site dir, so it is guarded as well.

    The repo's site dir is swapped for a temp one: if the guard ever regresses, this test must
    not be the thing that deletes ``site/static``.
    """
    import scripts.build_site as build_site

    site, _, calls = make_site(tmp_path)
    repo_site = tmp_path / "repo" / "site"
    (repo_site / "data").mkdir(parents=True)
    (repo_site / "data" / "AAA.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(build_site, "SITE_DIR", repo_site)
    for target in (repo_site, repo_site.parent):
        with pytest.raises(ValueError, match="refusing to build into"):
            build(
                site_dir=site,
                out_dir=target,
                calls_md=calls,
                stack_dir=tmp_path / "stack",
                signals_dir=tmp_path / "signals",
            )
    assert (repo_site / "data" / "AAA.json").is_file()


def test_out_dir_guard_on_the_real_repo_paths() -> None:
    """Through the pure check only, never ``build``: nothing here can delete a repo file."""
    sources = [SITE_DIR / sub for sub in SOURCE_SUBDIRS]
    for target in (REPO_ROOT, REPO_ROOT.parent, SITE_DIR, SITE_DIR / "data", SITE_STATIC_DIR):
        with pytest.raises(ValueError, match="refusing to build into"):
            _check_out_dir(target, sources)
    _check_out_dir(SITE_DIR / "build", sources)  # the default target
    _check_out_dir(REPO_ROOT / "somewhere-else", sources)


def test_main_cli_reports_a_refused_out_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site, _, calls = make_site(tmp_path)
    argv = ["--site-dir", str(site), "--out", str(site), "--calls", str(calls)]
    argv += ["--stack", str(tmp_path / "stack"), "--signals", str(tmp_path / "signals")]
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    assert "refusing to build into" in capsys.readouterr().err
    assert (site / "data" / "AAA.json").is_file()


# --------------------------------------------------------------------------------------------
# Writeup loading: reserved slug and skipped files
# --------------------------------------------------------------------------------------------


def test_writeup_slug_index_is_reserved_for_the_listing_page(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    text = WRITEUP_INDUSTRY.replace('"Depreciation & the GPU-hour"', "Index")
    (site / "content" / "2026-10-01-index.md").write_text(text, encoding="utf-8")
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    assert report.writeups == 3
    assert len(report.pages) == len(set(report.pages))
    assert "writeups/index-writeup.html" in report.pages
    assert [w for w in report.warnings if "2026-10-01-index.md" in w and "index-writeup" in w]
    listing = read(out / "writeups" / "index.html")
    assert "<h1>Writeups</h1>" in listing
    assert 'href="../writeups/index-writeup.html"' in listing
    assert "<h1>Index</h1>" in read(out / "writeups" / "index-writeup.html")


def test_skipped_writeups_are_named_in_warnings_but_drafts_are_silent(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    files = {
        "no-status.md": "---\ntitle: No status\ndate: 2026-09-01\n---\nbody\n",
        "leading-blank.md": "\n---\ntitle: Leading blank line\nstatus: published\n---\nbody\n",
        "no-frontmatter.md": "# Just markdown\n",
        "odd-status.md": "---\ntitle: Odd\nstatus: final\n---\nbody\n",
        "draft.md": WRITEUP_DRAFT,
    }
    for name, text in files.items():
        (content / name).write_text(text, encoding="utf-8")
    warnings: list[str] = []
    assert load_writeups(content, warnings) == []
    by_file = {w.split(":", 1)[0]: w for w in warnings}
    assert sorted(by_file) == [
        "leading-blank.md",
        "no-frontmatter.md",
        "no-status.md",
        "odd-status.md",
    ]
    assert len(warnings) == 4
    assert "no status in frontmatter" in by_file["no-status.md"]
    assert "no frontmatter found" in by_file["leading-blank.md"]
    assert "no frontmatter found" in by_file["no-frontmatter.md"]
    assert "unknown status 'final'" in by_file["odd-status.md"]


# --------------------------------------------------------------------------------------------
# Unit tests for the pure helpers
# --------------------------------------------------------------------------------------------


def test_parse_frontmatter_basic_and_quoted_values() -> None:
    meta, body = parse_frontmatter(WRITEUP_INDUSTRY)
    assert meta["title"] == "Depreciation & the GPU-hour"
    assert meta["date"] == "2026-09-25"
    assert meta["status"] == "published"
    assert body.strip() == "Body text with **emphasis**."


def test_parse_frontmatter_handles_crlf_bom_and_colons_in_values() -> None:
    text = "\ufeff---\r\ntitle: Alpha Cloud: the S-1 view\r\nStatus: Draft\r\n---\r\nbody\r\n"
    meta, body = parse_frontmatter(text)
    assert meta == {"title": "Alpha Cloud: the S-1 view", "status": "Draft"}
    assert body == "body\r\n"


def test_parse_frontmatter_unquotes_only_a_matching_pair() -> None:
    text = (
        "---\n"
        'title: Alpha Cloud calls it "committed backlog"\n'
        "summary: The call is 'open'\n"
        "slug: 'quoted-slug'\n"
        'tags: " a, b "\n'
        'company: "\n'
        "---\n"
    )
    meta, _ = parse_frontmatter(text)
    assert meta["title"] == 'Alpha Cloud calls it "committed backlog"'
    assert meta["summary"] == "The call is 'open'"
    assert meta["slug"] == "quoted-slug"
    assert meta["tags"] == "a, b"
    assert meta["company"] == '"'  # a lone quote is text, not an empty quoted value


def test_parse_frontmatter_without_fence_returns_text_untouched() -> None:
    assert parse_frontmatter("# Just markdown\n") == ({}, "# Just markdown\n")
    assert parse_frontmatter("") == ({}, "")


def test_parse_calls_escaped_pipe_and_comment_example_ignored() -> None:
    rows = parse_calls(CALLS_MD)
    assert len(rows) == 2
    assert rows[0]["falsifying_number"] == "toy < $4bn | example cut"
    assert rows[1]["outcome"] == "wrong"
    assert all(
        list(r) == ["date", "claim", "falsifying_number", "deadline", "outcome"] for r in rows
    )


def test_parse_calls_ignores_other_tables_and_missing_header() -> None:
    other = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert parse_calls(other) == []
    assert parse_calls("no table here") == []
    header_only = (
        "| date | claim | falsifying number | deadline | outcome |\n|---|---|---|---|---|\n"
    )
    assert parse_calls(header_only) == []
    # Header match is case-insensitive and a short row is padded rather than dropped.
    short = (
        "| Date | Claim | Falsifying Number | Deadline | Outcome |\n"
        "|-|-|-|-|-|\n"
        "| 2026-01-01 | x |\n"
    )
    assert parse_calls(short) == [
        {"date": "2026-01-01", "claim": "x", "falsifying_number": "", "deadline": "", "outcome": ""}
    ]


def test_parse_calls_skips_rows_with_no_content() -> None:
    """The writeup template used to ship a ``| | | |`` row that authors copied into calls.md."""
    md = (
        "| date | claim | falsifying number | deadline | outcome |\n"
        "|---|---|---|---|---|\n"
        "| | | |\n"
        "| 2026-01-01 | example claim | example number | 2026-12-31 | open |\n"
        "|  |  |  |  |  |\n"
        "|\n"
    )
    assert [row["date"] for row in parse_calls(md)] == ["2026-01-01"]


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("What an Alpha Cloud GPU-hour earns", "what-an-alpha-cloud-gpu-hour-earns"),
        ("Depreciation & the GPU-hour", "depreciation-the-gpu-hour"),
        ("  Beta Compute: 20-F, EUR & USD  ", "beta-compute-20-f-eur-usd"),
        ("Déjà vu — émissions", "deja-vu-emissions"),
        ("!!!", "untitled"),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert slugify(title) == slug


@pytest.mark.parametrize(
    ("value", "kwargs", "expected"),
    [
        (1_212_000_000, {}, "$1.2bn"),
        (982_000_000, {}, "$982m"),
        (1_500_000, {}, "$1.5m"),
        (32_000, {}, "$32k"),
        (-100_000_000, {}, "-$100m"),
        (1500.0, {"unit": "USD m"}, "$1.5bn"),
        (32_000, {"compact": False}, "$32,000"),
        (0.08, {"compact": False}, "$0.08"),
        (None, {}, "—"),
        (float("nan"), {}, "—"),
        ("n/a", {}, "—"),
    ],
)
def test_fmt_money(value: object, kwargs: dict[str, object], expected: str) -> None:
    assert fmt_money(value, **kwargs) == expected


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (0.55, "%", "55.0%"),
        (0.6, "share", "60.0%"),
        (1.25, "ratio", "1.25"),
        (2500, "tokens/s", "2,500"),
        (240_000_000, "shares", "240m"),
        (3.5, "x", "3.50x"),
        ("see note", "USD", "see note"),
        (None, "MW", "—"),
    ],
)
def test_fmt_value(value: object, unit: str, expected: str) -> None:
    assert fmt_value(value, unit, compact=False) == expected


# The compact form is what the series tables show, and what formatValue in dashboard.js must
# reproduce for chart ticks and tooltips. The same table sits in a comment in both source files.
# 1.25e9, 100.5 and 0.125 are exact binary ties, where Python rounds to even and a naive
# JavaScript toFixed/Math.round would round up.
FORMAT_VECTORS: list[tuple[float, str, str]] = [
    (1.15e9, "USD", "$1.1bn"),
    (1.25e9, "USD", "$1.2bn"),
    (982000000, "USD", "$982m"),
    (9.95e6, "USD", "$9.9m"),
    (32000, "USD", "$32k"),
    (-100000000, "USD", "-$100m"),
    (1500, "USD m", "$1.5bn"),
    (123.456, "USD", "$123"),
    (100.5, "USD", "$100"),
    (12.5, "USD", "$12.50"),
    (2.75, "USD", "$2.75"),
    (1.5772, "USD", "$1.58"),
    (0.2079, "USD", "$0.21"),
    (0.08, "USD/kWh", "$0.08"),
    (0.5, "USD/M tokens", "$0.50"),
    (0.55, "%", "55.0%"),
    (0.6, "share", "60.0%"),
    (3.5, "x", "3.50x"),
    (0.125, "x", "0.12x"),
    (240000000, "shares", "240m"),
    (2500000, "tokens/s", "2.5m"),
    (2500, "tokens/s", "2,500"),
    (1250, "GPUs", "1,250"),
    (-1250, "GPUs", "-1,250"),
    (18.99485, "months", "18.99"),
]


@pytest.mark.parametrize(("value", "unit", "expected"), FORMAT_VECTORS)
def test_fmt_value_compact_vectors(value: float, unit: str, expected: str) -> None:
    assert fmt_value(value, unit) == expected


def documented_vectors(path: Path) -> list[tuple[float, str, str]]:
    """The ``fmt: value | unit | display`` lines of a source file's comment table."""
    rows = re.findall(r"fmt: (\S+) \| (.*?) \| (.+)$", read(path), flags=re.MULTILINE)
    return [(float(value), unit, shown) for value, unit, shown in rows]


def test_format_vectors_are_documented_identically_in_python_and_javascript() -> None:
    """dashboard.js cannot be run here, so its copy of the table is what keeps it honest."""
    expected = [(float(value), unit, shown) for value, unit, shown in FORMAT_VECTORS]
    assert documented_vectors(REPO_ROOT / "scripts" / "build_site.py") == expected
    assert documented_vectors(SITE_STATIC_DIR / "dashboard.js") == expected


# --------------------------------------------------------------------------------------------
# Display arithmetic: latest point, the same period a year earlier, YoY, capex / revenue
# --------------------------------------------------------------------------------------------

QUARTERLY = {
    "unit": "USD",
    "freq": "Q",
    "points": [["2025Q1", 90.0], ["2025Q2", 100.0], ["2026Q1", 120.0], ["2026Q2", 125.0]],
}
ANNUAL = {"unit": "USD", "freq": "A", "points": [["2023", 50.0], ["2024", 80.0], ["2025", 60.0]]}


def test_latest_point_is_the_last_numeric_period() -> None:
    assert latest_point([("2025Q4", 1.0), ("2026Q1", 2.0)]) == ("2026Q1", 2.0)
    assert latest_point([("2026Q1", 2.0), ("2025Q4", 1.0)]) == ("2026Q1", 2.0)  # by label
    # A null, NaN or text placeholder for the newest period is not a figure.
    assert latest_point([("2025", 7), ("2026", None)]) == ("2025", 7.0)
    assert latest_point([("2025", 7), ("2026", float("nan")), ("2027", "n/a")]) == ("2025", 7.0)
    assert latest_point([]) is None and latest_point([("2026", None)]) is None


def test_value_at() -> None:
    points = [("2025Q2", 100), ("2026Q2", None)]
    assert value_at(points, "2025Q2") == 100.0
    assert value_at(points, "2026Q2") is None and value_at(points, "2024Q2") is None


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("2026Q2", "2025Q2"),  # the same quarter, not the previous one
        ("2026Q1", "2025Q1"),
        ("2025", "2024"),
        ("2026E", None),  # model labels are not calendar periods
        ("2025A", None),
        ("2026Q5", None),
        ("Q2", None),
        ("", None),
    ],
)
def test_prior_year_period(period: str, expected: str | None) -> None:
    assert prior_year_period(period) == expected


@pytest.mark.parametrize(
    ("current", "prior", "expected"),
    [
        (125.0, 100.0, 0.25),
        (80.0, 100.0, -0.2),
        (-100.0, 250.0, -1.4),  # a positive base is a base, whatever the sign of the new figure
        (100.0, 0.0, None),
        (100.0, -50.0, None),  # an outflow of 50 turning into an inflow of 100 is not "-300%"
        (-20.0, -50.0, None),
        (100.0, None, None),
        (None, 100.0, None),
    ],
)
def test_yoy_change(current: float | None, prior: float | None, expected: float | None) -> None:
    assert yoy_change(current, prior) == pytest.approx(expected)


def test_capex_to_revenue() -> None:
    assert capex_to_revenue(2.8e9, 1.4e9) == pytest.approx(2.0)
    assert capex_to_revenue(0.0, 1.4e9) == 0.0
    for capex, revenue in ((1.0, 0.0), (1.0, -5.0), (None, 1.0), (1.0, None)):
        assert capex_to_revenue(capex, revenue) is None


@pytest.mark.parametrize(
    ("change", "text", "direction"),
    [
        (0.177, "+17.7%", "pos"),
        (-0.2, "-20.0%", "neg"),
        (12.345, "+1,234.5%", "pos"),
        (0.0, "0.0%", "flat"),
        (-0.0004, "0.0%", "flat"),  # shown as zero, so neither signed nor coloured
        (None, MISSING, ""),
        (float("inf"), MISSING, ""),
    ],
)
def test_fmt_change_and_direction_agree(change: float | None, text: str, direction: str) -> None:
    assert fmt_change(change) == text
    assert change_direction(change) == direction


def test_series_figure_quarterly_compares_the_same_quarter_a_year_earlier() -> None:
    assert series_figure(QUARTERLY) == Figure("2026Q2", 125.0, "USD", pytest.approx(0.25))
    # 2026Q1 against 2025Q1, not against the quarter before it.
    assert series_figure(QUARTERLY, "2026Q1") == Figure(
        "2026Q1", 120.0, "USD", pytest.approx(1 / 3)
    )


def test_series_figure_annual_compares_the_prior_year() -> None:
    assert series_figure(ANNUAL) == Figure("2025", 60.0, "USD", pytest.approx(-0.25))


def test_series_figure_without_a_comparison_or_without_a_point() -> None:
    assert series_figure(QUARTERLY, "2025Q2") == Figure("2025Q2", 100.0, "USD", None)
    gap = {"unit": "USD", "points": [["2024Q3", 10.0], ["2025Q4", 20.0]]}  # 2024Q4 not filed
    assert series_figure(gap) == Figure("2025Q4", 20.0, "USD", None)
    zero_base = {"unit": "USD", "points": [["2024", 0.0], ["2025", 61.5]]}
    assert series_figure(zero_base) == Figure("2025", 61.5, "USD", None)
    assert series_figure(QUARTERLY, "2030Q1") is None
    for missing in (None, {}, {"points": []}, {"points": [["2025", None]]}, "n/a"):
        assert series_figure(missing) is None


def test_headline_figures_read_capex_at_the_revenue_period() -> None:
    reported = {
        "revenue": QUARTERLY,
        # Capex runs a quarter ahead: the row still shows 2026Q2 for both, so the ratio is like
        # for like.
        "capex": {
            "unit": "USD",
            "points": [["2025Q2", 40.0], ["2026Q2", 50.0], ["2026Q3", 70.0]],
        },
    }
    revenue, capex = headline_figures(reported)
    assert revenue == Figure("2026Q2", 125.0, "USD", pytest.approx(0.25))
    assert capex == Figure("2026Q2", 50.0, "USD", pytest.approx(0.25))
    # Annual capex beside quarterly revenue has no point for the quarter.
    assert headline_figures({"revenue": QUARTERLY, "capex": ANNUAL})[1] is None
    # Without revenue the row falls back to the latest capex point.
    assert headline_figures({"capex": ANNUAL}) == (
        None,
        Figure("2025", 60.0, "USD", pytest.approx(-0.25)),
    )
    assert headline_figures({}) == (None, None) and headline_figures(None) == (None, None)


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        (
            "[derived; proposed; range 4 to 6] Fleet average.",
            ("derived; proposed; range 4 to 6", "Fleet average."),
        ),
        ("  [judgment;  proposed]\nZero on purpose.", ("judgment; proposed", "Zero on purpose.")),
        ("[disclosed]", ("disclosed", "")),
        ("No tag here.", ("", "No tag here.")),
        ("Raised to six years [S-1 p.F-16].", ("", "Raised to six years [S-1 p.F-16].")),
        ("[derived] first [second] tag stays", ("derived", "first [second] tag stays")),
        ("[unclosed tag", ("", "[unclosed tag")),
        ("", ("", "")),
        (None, ("", "")),
    ],
)
def test_split_basis(note: str | None, expected: tuple[str, str]) -> None:
    assert split_basis(note) == expected


def test_fmt_timestamp() -> None:
    assert fmt_timestamp("2026-09-17T13:43:24Z") == "2026-09-17 13:43 UTC"
    assert fmt_timestamp("2026-09-17") == "2026-09-17"
    assert fmt_timestamp("last Tuesday") == "last Tuesday"


def css_block(css: str, selector: str) -> str:
    match = re.search(
        r"^" + re.escape(selector) + r" \{(.*?)^\}", css, flags=re.MULTILINE | re.DOTALL
    )
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_stylesheet_keeps_a_phone_page_from_scrolling_sideways() -> None:
    css = read(SITE_STATIC_DIR / "style.css")
    # A bare URL or a file path in a writeup has no break point of its own.
    assert "overflow-wrap: anywhere" in css_block(css, ".prose")
    # The header nav stays on one line and scrolls inside itself instead of wrapping at 375px.
    nav = css_block(css, ".site-header nav")
    assert "flex-wrap: nowrap" in nav and "overflow-x: auto" in nav
    # Wide tables scroll inside their own box, and the key-figures strip is two columns on a
    # phone (the wider layouts sit in min-width media queries further down).
    assert "overflow-x: auto" in css_block(css, ".table-scroll")
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css_block(css, ".kpis")


def test_stylesheet_sets_numbers_in_tabular_figures_and_colours_the_sign_of_a_change() -> None:
    css = read(SITE_STATIC_DIR / "style.css")
    assert "font-variant-numeric: tabular-nums" in css_block(css, "table")
    assert "text-align: right" in css_block(css, ".num")
    assert "var(--ok)" in css_block(css, ".pos") and "var(--bad)" in css_block(css, ".neg")
    assert "@media (prefers-color-scheme: dark)" in css


def test_audit_html_flags_absolute_links_only() -> None:
    assert audit_html('<a href="companies/AAA.html">x</a><img src="../static/a.png">') == []
    problems = audit_html('<a href="/companies/AAA.html">x</a><script src="/static/d.js"></script>')
    assert len(problems) == 2 and all(p.startswith("absolute link") for p in problems)


def test_content_template_is_draft_and_ends_with_required_sections() -> None:
    template = SITE_CONTENT_DIR / "_template.md"
    meta, body = parse_frontmatter(read(template))
    assert meta["status"] == "draft"
    for key in ("title", "date", "company", "summary", "tags"):
        assert key in meta
    # Placeholders only: the template must not pre-write a thesis about a real company.
    for placeholder in (meta["title"], meta["summary"], meta["tags"]):
        assert not re.search(r"coreweave|nebius|nvidia", placeholder, flags=re.IGNORECASE)
    # The example row lives in a comment; a blank "| | | |" row would be copied into calls.md.
    assert not re.search(r"^\|[ \t|]*$", body, flags=re.MULTILINE)
    headings = re.findall(r"^## (.+)$", body, flags=re.MULTILINE)
    assert headings == [
        "The question",
        "What the model says",
        "Key drivers",
        "Sensitivities",
        "Position",
        "What would prove this wrong",
    ]
    last_section = body.split("## What would prove this wrong", 1)[1]
    assert "| claim | falsifying number | deadline |" in last_section
    assert "calls.md" in last_section


# --------------------------------------------------------------------------------------------
# Stack pages: the chain as one table, one page per stage
# --------------------------------------------------------------------------------------------

STAGES_HEADER = [
    "#",
    "Stage",
    "Unit",
    "Sells",
    "Lead time (years)",
    "Bottleneck",
    "Key figures",
    "Players",
]


def facts(page: str) -> list[tuple[str, str]]:
    """The (label, value) pairs of a stage page's facts list."""
    return re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", page)


def stage_nav(page: str) -> str:
    return page.split('<p class="stage-nav">', 1)[1].split("</p>", 1)[0]


def test_stack_index_lists_all_nine_stages_in_order(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    page = read(out / "stack" / "index.html")
    assert "<title>Stack · AI economics</title>" in page and "<h1>Stack</h1>" in page
    assert '<p class="meta">9 stages</p>' in page
    rows = table_rows(page, "stages")
    assert rows[0] == STAGES_HEADER
    assert [row[0] for row in rows[1:]] == [str(n) for n in range(1, 10)]  # by order, not file
    assert [row[1] for row in rows[1:]] == [
        "Power",
        "Grid",
        "Data centre",
        "Wafers & <packaging>",
        "Memory",
        "Systems",
        "GPU-hours",
        "Tokens",
        "Applications",
    ]
    for key in STAGE_KEYS:
        assert f'href="../stack/{key}.html"' in page, key
    # Power: unit, sells, lead time, score, the first two metrics and the first four players.
    assert rows[1][2:6] == ["MW", "firm electricity", "3", "4"]
    assert rows[1][6] == "1,500,000 MW Example capacity 0.06 USD/kWh Example price"
    assert "Example third metric" not in page
    assert rows[1][7] == "AAA, Gamma <Chips> & Co, PT3, PF4"
    assert "PF5" not in page
    assert '<a href="../companies/AAA.html">AAA</a>' in page  # a company the site knows
    assert "PT3</td>" in page or "PT3, " in page  # one it does not: text, no link
    # A blank lead time or score, and a stage with no figures or players, show an en dash.
    assert rows[9][4:8] == [MISSING, MISSING, MISSING, MISSING]  # applications
    assert rows[3][4:8] == ["1.5", MISSING, MISSING, MISSING]  # datacenter
    assert rows[6][4:8] == ["0.75", "2", "32,000.5 USD per GPU Example GPU price", "BBB"]
    # Names with & and < are escaped, in the table and in the players column.
    assert "Wafers &amp; &lt;packaging&gt;" in page and "<packaging>" not in page
    assert "Gamma &lt;Chips&gt; &amp; Co" in page and "<Chips>" not in page
    # The nav carries the section on every page, relative to that page.
    assert 'href="../stack/index.html">Stack</a>' in page
    assert 'href="stack/index.html">Stack</a>' in read(out / "index.html")
    assert 'href="../stack/index.html">Stack</a>' in read(out / "companies" / "AAA.html")
    index = read(out / "index.html")
    assert index.index("Companies</a>") < index.index("Stack</a>") < index.index("Writeups</a>")


def test_stack_index_lists_consumption_tiers_under_the_chain(
    built: tuple[Path, BuildReport],
) -> None:
    out, _ = built
    page = read(out / "stack" / "index.html")
    assert page.index('id="stages"') < page.index('id="consumption-tiers"')
    assert '<section id="consumption-tiers" class="section"><h2>Consumption tiers</h2>' in page
    assert table_rows(page, "tiers") == [
        ["Tier", "Name", "Examples", "Tokens per user per day", "Revenue model"],
        ["heavy", "Example heavy tier", "agents, long runs", ">1000000", "usage"],
        ["light", "Example light tier", "chat", "5000-50000", "subscription"],
    ]
    assert "&gt;1000000" in page
    # Tables and headings only between the chain and the end of the page body: no prose.
    body = page.split('id="stages"', 1)[1].split("</main>", 1)[0]
    assert "<p" not in body


def test_stage_page_header_primer_figures_conversions_and_players(
    built: tuple[Path, BuildReport],
) -> None:
    out, _ = built
    page = read(out / "stack" / "power.html")
    assert "<title>Power · AI economics</title>" in page
    assert '<p class="eyebrow">Stage 1 of 9</p>' in page
    assert '<h1>Power <span class="ticker">power</span></h1>' in page
    assert '<p class="lede">Example power summary.</p>' in page
    assert facts(page) == [
        ("Unit", "MW"),
        ("Sells", "firm electricity"),
        ("Lead time", "3 years"),
        ("Bottleneck", "4 / 5 · Example bottleneck note."),
        ("Status", '<span class="badge badge-deep">deep</span>'),
    ]  # no "Buys from": power buys from nothing
    # Primer, figures, conversions, players, then the stage nav.
    order = [
        page.index(marker)
        for marker in (
            '<section id="primer" class="section"><h2>Primer</h2><div class="prose">',
            '<section id="figures" class="section"><h2>Figures</h2>',
            '<section id="conversions" class="section"><h2>Conversions</h2>',
            '<section id="players" class="section"><h2>Players</h2>',
            'class="stage-nav"',
        )
    ]
    assert order == sorted(order)
    # The primer goes through the writeup pipeline: heading ids, emphasis, scrolling tables.
    assert '<h2 id="why-power-comes-first">Why power comes first</h2>' in page
    assert "<strong>emphasis</strong>" in page and '<div class="table-scroll"><table>' in page
    assert table_rows(page, "figures") == [
        ["Metric", "Value", "Unit", "As of", "Scope", "Confidence", "Source", "Note"],
        [
            "Example capacity",
            "1,500,000",
            "MW",
            "2026-06",
            "US",
            "high",
            "Example source",
            "Toy value & <note>",
        ],
        [
            "Example price",
            "0.06",
            "USD/kWh",
            "2026Q2",
            "US industrial",
            "medium",
            "Example filing p.3",
            "",
        ],
        [
            "Example third metric",
            "42",
            "GW",
            "2026",
            "world",
            "low",
            "example.com",
            "Only two show on the index",
        ],
    ]
    assert '<a href="https://example.com/power" rel="noopener">Example source</a>' in page
    assert "Example filing p.3</td>" in page  # a source with no URL is text
    assert '<a href="https://example.com/third" rel="noopener">example.com</a>' in page  # no text
    assert "Toy value &amp; &lt;note&gt;" in page and "<note>" not in page
    assert '<td class="confidence confidence-high">high</td>' in page
    assert table_rows(page, "conversions") == [
        ["From → To", "Factor", "Unit", "As of", "Confidence", "Source", "Note"],
        [
            "Power → Grid",
            "0.9",
            "MW connected per MW",
            "2026",
            "medium",
            "Example source",
            "Toy factor",
        ],
    ]
    assert '<a href="../stack/grid.html">Grid</a>' in page
    assert table_rows(page, "players") == [
        ["Company", "Ticker", "Role", "Listed", "Note"],
        ["Alpha Cloud", "AAA", "buyer", "yes", "Example note"],
        ["Gamma <Chips> & Co", "", "turbines", "no", ""],
        ["Player Three", "PT3", "fuel", "yes", ""],
        ["Player Four", "PF4", "nuclear", "no", ""],
        ["Player Five", "PF5", "solar", "no", ""],
    ]
    assert '<a href="https://example.com/aaa" rel="noopener">Alpha Cloud</a>' in page
    assert '<a href="../companies/AAA.html">AAA</a>' in page
    assert "<td>PT3</td>" in page
    assert "Gamma &lt;Chips&gt; &amp; Co" in page and "<Chips>" not in page
    # First stage: no previous link; the chain and the next stage.
    assert stage_nav(page) == (
        '<a href="../stack/index.html">Stack</a>'
        '<a class="next" href="../stack/grid.html">2 Grid →</a>'
    )


def test_stage_pages_omit_empty_sections_and_facts(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    # A conversion is listed at both ends.
    grid = read(out / "stack" / "grid.html")
    assert [row[0] for row in table_rows(grid, "conversions")[1:]] == ["Power → Grid"]
    assert ("Buys from", '<a href="../stack/power.html">Power</a>') in facts(grid)
    for gone in ('id="primer"', 'id="figures"', 'id="players"'):
        assert gone not in grid, gone
    # Nothing about datacenter but the chain: header, facts and nav, no heading, no "none" line.
    page = read(out / "stack" / "datacenter.html")
    for gone in ('id="primer"', 'id="figures"', 'id="conversions"', 'id="players"', "<h2>"):
        assert gone not in page, gone
    assert '<p class="lede">' not in page  # blank summary
    assert facts(page) == [
        ("Unit", "MW of IT load"),
        ("Sells", "rack-ready megawatts"),
        ("Buys from", '<a href="../stack/grid.html">Grid</a>'),
        ("Lead time", "1.5 years"),
        ("Status", '<span class="badge badge-skeleton">skeleton</span>'),
    ]
    # Two suppliers, a fractional lead time, a singular year.
    systems = facts(read(out / "stack" / "systems.html"))
    assert (
        "Buys from",
        '<a href="../stack/silicon.html">Wafers &amp; &lt;packaging&gt;</a> · '
        '<a href="../stack/memory.html">Memory</a>',
    ) in systems
    assert ("Lead time", "0.75 years") in systems and ("Bottleneck", "2 / 5") in systems
    assert ("Lead time", "1 year") in facts(read(out / "stack" / "compute.html"))
    # Last stage: a note without a score, no lead time, no next link.
    last = read(out / "stack" / "applications.html")
    assert ("Bottleneck", "Note without a score.") in facts(last)
    assert "<dt>Lead time</dt>" not in last
    assert stage_nav(last) == (
        '<a class="prev" href="../stack/models.html">← 8 Tokens</a>'
        '<a href="../stack/index.html">Stack</a>'
    )


def test_stage_page_escapes_the_name(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    page = read(out / "stack" / "silicon.html")
    assert "<title>Wafers &amp; &lt;packaging&gt; · AI economics</title>" in page
    assert '<h1>Wafers &amp; &lt;packaging&gt; <span class="ticker">silicon</span></h1>' in page
    assert "<packaging>" not in page
    assert "Wafers &amp; &lt;packaging&gt; →</a>" in read(out / "stack" / "datacenter.html")


def test_primer_section_is_omitted_without_a_primer_file(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    (tmp_path / "stack" / "primers" / "power.md").unlink()
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    assert not report.warnings
    page = read(out / "stack" / "power.html")
    assert 'id="primer"' not in page and "Primer" not in page
    assert 'id="figures"' in page and PRIMER_MD.splitlines()[0].lstrip("# ") not in page


def test_build_without_a_stack_dir_warns_and_leaves_the_stack_out(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "no-stack",
        signals_dir=tmp_path / "signals",
    )
    assert report.stages == 0 and not any(p.startswith("stack/") for p in report.pages)
    assert not (out / "stack" / "index.html").exists()
    assert len(report.warnings) == 1
    assert "stages.csv missing" in report.warnings[0]
    assert "stack pages not built" in report.warnings[0]
    for page in ("index.html", "companies/AAA.html", "writeups/index.html"):
        text = read(out / page)
        assert "Stack</a>" not in text and "stack/index.html" not in text, page
    # The ledger still loads (there is no chain to check its stages against) and the signals
    # index names each stage as text: there is no stage page to link to.
    assert report.signals == 5 and "signals/index.html" in report.pages
    signals = read(out / "signals" / "index.html")
    assert "<td>power</td>" in signals and "stack/power.html" not in signals


def test_malformed_stack_tables_warn_and_leave_that_table_empty(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    stack = tmp_path / "stack"
    header = "stage,metric,value,unit,as_of,scope,source_url,source,confidence,note\n"
    (stack / "metrics.csv").write_text(
        header + "power,Broken,lots,MW,,,,,high,\n", encoding="utf-8"
    )
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=stack,
        signals_dir=tmp_path / "signals",
    )
    assert report.stages == 9 and len(report.warnings) == 1
    assert report.warnings[0].startswith("metrics.csv: row 2 (power / Broken): value 'lots'")
    assert report.warnings[0].endswith("; stack metrics left empty")
    power = read(out / "stack" / "power.html")
    assert 'id="figures"' not in power and 'id="players"' in power  # the rest still renders
    rows = table_rows(read(out / "stack" / "index.html"), "stages")
    assert rows[1][6] == MISSING and rows[1][7] == "AAA, Gamma <Chips> & Co, PT3, PF4"
    # A malformed stages.csv takes the whole stack out, naming the row.
    bad = STAGES_CSV.replace("1,power,", "one,power,")
    (stack / "stages.csv").write_text(bad, encoding="utf-8")
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=stack,
        signals_dir=tmp_path / "signals",
    )
    assert report.stages == 0 and not (out / "stack" / "index.html").exists()
    assert len(report.warnings) == 1
    assert "row 3 (power): order 'one' is not a number" in report.warnings[0]
    assert report.warnings[0].endswith("; stack pages not built")
    assert "Stack</a>" not in read(out / "index.html")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1500000, "1,500,000"),
        (32000.5, "32,000.5"),
        (0.75, "0.75"),
        (3.0, "3"),
        (0.125, "0.12"),  # an exact binary tie rounds to even
        (-1250, "-1,250"),
        (-0.001, "0"),  # never "-0"
        ("42", "42"),
        ("n/a", MISSING),
        (None, MISSING),
        (float("nan"), MISSING),
    ],
)
def test_fmt_number_uses_separators_and_at_most_two_decimals(value: object, expected: str) -> None:
    assert fmt_number(value) == expected
    assert "$" not in fmt_number(value) and "bn" not in fmt_number(value)


def test_stylesheet_lets_stage_names_wrap_and_lays_out_the_facts() -> None:
    css = read(SITE_STATIC_DIR / "style.css")
    assert "white-space: normal" in css_block(css, '.conversions th[scope="row"]')
    assert "grid-template-columns" in css_block(css, ".facts")
    assert "display: block" in css_block(css, ".figure")


# --------------------------------------------------------------------------------------------
# Signals: the ledger on signals/index.html, a stage's rows on its page, a company's on its page
# --------------------------------------------------------------------------------------------

SIGNALS_HEADER = [
    "Date",
    "Kind",
    "Actor",
    "Counterparty",
    "Claim",
    "Value",
    "Confidence",
    "Source",
]
INDEX_SIGNALS_HEADER = [
    "Date",
    "Kind",
    "Stage",
    "Actor",
    "Claim",
    "Value",
    "Confidence",
    "Bears on",
    "Source",
]
ESCAPED_CLAIM = "Says 2 GW of data-centre load &lt;requested&gt; &amp; queued in its territory"


def signals_section(page: str) -> str:
    return page.split('<section id="signals"', 1)[1].split("</section>", 1)[0]


def test_stage_page_lists_its_signals_newest_first(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    power = read(out / "stack" / "power.html")
    assert '<section id="signals" class="section"><h2>Signals</h2>' in power
    rows = table_rows(power, "signals")
    assert rows[0] == SIGNALS_HEADER
    # File order is August, September, July; the page shows September then July.
    assert rows[1:] == [
        [
            "2026-09-01",
            "statement",
            "Utility Co",
            "",
            "Says 2 GW of data-centre load <requested> & queued in its territory",
            "2 GW",
            "reported",
            "Example press release",
        ],
        [
            "2026-07-15",
            "price",
            "Tracker",
            "",
            "Contract power prices flat quarter on quarter",
            MISSING,  # a signal without a number
            "speculated",
            "Example tracker",
        ],
    ]
    assert "Beta commits" not in power  # a compute signal, not power's
    assert ESCAPED_CLAIM in power and "<requested>" not in power
    assert '<td class="confidence confidence-reported">reported</td>' in power
    assert '<td class="confidence confidence-speculated">speculated</td>' in power
    assert (
        '<a href="https://example.com/power-statement" rel="noopener">Example press release</a>'
        in power
    )
    # Players, then signals, then the stage nav; tables only, no prose.
    order = [
        power.index(marker)
        for marker in ('<section id="players"', '<section id="signals"', 'class="stage-nav"')
    ]
    assert order == sorted(order)
    assert "<p" not in signals_section(power)
    compute = table_rows(read(out / "stack" / "compute.html"), "signals")
    assert [row[0] for row in compute[1:]] == ["2026-08-11", "2026-05-01"]
    assert compute[1][3:6] == ["Beta Labs", "Beta commits $4 billion over five years", "4 USD bn"]
    assert compute[2][5] == "300 MW"
    # A stage with no signals: no section, no heading, no "none" line (the nav link stays).
    datacenter = read(out / "stack" / "datacenter.html")
    assert 'id="signals"' not in datacenter and "<h2>Signals</h2>" not in datacenter
    assert 'href="../signals/index.html">Signals</a>' in datacenter


def test_company_page_lists_the_signals_mapped_to_it(built: tuple[Path, BuildReport]) -> None:
    out, _ = built
    aaa = read(out / "companies" / "AAA.html")
    assert '<section id="signals" class="section"><h2>Signals</h2>' in aaa
    rows = table_rows(aaa, "signals")
    assert rows[0] == [*SIGNALS_HEADER[:-1], "Bears on", "Source"]
    # Newest first, and "Bears on" is the text after the ticker, not the whole maps_to.
    assert [(row[0], row[7]) for row in rows[1:]] == [
        ("2026-08-11", "toy_price"),
        ("2026-05-01", "toy_share"),
    ]
    assert rows[1][2:6] == [
        "Alpha Cloud",
        "Beta Labs",
        "Beta commits $4 billion over five years",
        "4 USD bn",
    ]
    assert "AAA: toy_price" not in aaa
    # Rows mapped to the stack, to "watch" or to the other company are not this company's.
    before_json = aaa.split("<script", 1)[0]
    for other in ("Utility Co", "Tracker", "Borrows", "toy_life"):
        assert other not in before_json, other
    # Reported (the filings), then signals (outside the filings), then the writeups.
    order = [
        aaa.index(marker)
        for marker in ('<section id="reported"', '<section id="signals"', 'id="company-writeups"')
    ]
    assert order == sorted(order)
    assert "<p" not in signals_section(aaa)
    # The ticker in maps_to matches whatever its case; a source with no text shows its host.
    bbb = table_rows(read(out / "companies" / "BBB.html"), "signals")
    assert [row[0] for row in bbb[1:]] == ["2026-06-30"]
    assert bbb[1][5:] == ["1.5 USD bn", "confirmed", "toy_life", "example.com"]


def test_signals_index_counts_links_and_escapes(built: tuple[Path, BuildReport]) -> None:
    out, report = built
    assert report.signals == 5
    page = read(out / "signals" / "index.html")
    assert "<title>Signals · AI economics</title>" in page and "<h1>Signals</h1>" in page
    assert '<p class="meta">5 signals · 2 confirmed · 2 reported · 1 speculated</p>' in page
    assert '<section id="ledger" class="section">' in page and "<h2>Ledger</h2>" in page
    rows = table_rows(page, "signals")
    assert rows[0] == INDEX_SIGNALS_HEADER
    assert [row[0] for row in rows[1:]] == [
        "2026-09-01",
        "2026-08-11",
        "2026-07-15",
        "2026-06-30",
        "2026-05-01",
    ]
    # The stage column carries the stage's name, linked to its page.
    assert [row[2] for row in rows[1:]] == ["Power", "GPU-hours", "Power", "Systems", "GPU-hours"]
    assert '<a href="../stack/power.html">Power</a>' in page
    assert '<a href="../stack/compute.html">GPU-hours</a>' in page
    # Bears on: maps_to as written; a leading ticker the site knows is linked, whatever its case.
    assert [row[7] for row in rows[1:]] == [
        "stack: power/demand_pipeline_gw",
        "AAA: toy_price",
        "watch",
        "bbb: toy_life",
        "AAA: toy_share",
    ]
    assert '<a href="../companies/AAA.html">AAA</a>: toy_price' in page
    assert '<a href="../companies/BBB.html">bbb</a>: toy_life' in page
    assert "<td>watch</td>" in page and "<td>stack: power/demand_pipeline_gw</td>" in page
    assert rows[1][5] == "2 GW" and rows[3][5] == MISSING
    assert ESCAPED_CLAIM in page and "<requested>" not in page
    assert '<a href="https://example.com/bbb-loan" rel="noopener">example.com</a>' in page
    # Labels, numbers and links only between the heading and the end of the page body.
    body = page.split('id="ledger"', 1)[1].split("</main>", 1)[0]
    assert "<p" not in body
    # The nav carries the section on every page, relative to that page, after the stack.
    assert 'href="../signals/index.html">Signals</a>' in page
    index = read(out / "index.html")
    assert 'href="signals/index.html">Signals</a>' in index
    assert index.index("Stack</a>") < index.index("Signals</a>") < index.index("Writeups</a>")
    for rel in ("companies/AAA.html", "stack/power.html", "writeups/index.html"):
        assert 'href="../signals/index.html">Signals</a>' in read(out / rel), rel


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (",confirmed,AAA: toy_price,", ",likely,AAA: toy_price,", "confidence must be one of"),
        # An unknown stage proves the ledger is checked against this build's stack.
        ("2026-09-01,statement,power,", "2026-09-01,statement,orbit,", "unknown stage"),
    ],
)
def test_malformed_ledger_warns_and_leaves_signals_out(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    site, out, calls = make_site(tmp_path)
    write_ledger(tmp_path, variant(LEDGER_CSV, old, new))
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    assert report.signals == 0 and report.stages == 9  # the rest of the site is built
    assert len(report.warnings) == 1
    assert report.warnings[0].startswith("ledger.csv: ") and message in report.warnings[0]
    assert report.warnings[0].endswith("; signals left empty")
    assert not (out / "signals" / "index.html").exists()
    assert not any(p.startswith("signals/") for p in report.pages)
    for rel in ("index.html", "companies/AAA.html", "stack/power.html", "stack/compute.html"):
        text = read(out / rel)
        assert 'id="signals"' not in text and "<h2>Signals</h2>" not in text, rel
        assert "Signals</a>" not in text and "signals/index.html" not in text, rel


def test_build_without_a_ledger_has_no_signals_and_no_warning(tmp_path: Path) -> None:
    site, out, calls = make_site(tmp_path)
    build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    assert (out / "signals" / "index.html").exists()
    # No ledger is "none yet", not a problem: no warning, and a rebuild drops the stale page.
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "no-signals",
    )
    assert report.signals == 0 and not report.warnings
    assert not (out / "signals" / "index.html").exists()
    assert not any(p.startswith("signals/") for p in report.pages)
    for rel in ("index.html", "companies/AAA.html", "stack/power.html", "writeups/index.html"):
        text = read(out / rel)
        assert 'id="signals"' not in text and "<h2>Signals</h2>" not in text, rel
        assert "Signals</a>" not in text and "signals/index.html" not in text, rel
    # A ledger with a header and no rows is the same as none.
    write_ledger(tmp_path, LEDGER_CSV.splitlines()[0] + "\n")
    report = build(
        site_dir=site,
        out_dir=out,
        calls_md=calls,
        stack_dir=tmp_path / "stack",
        signals_dir=tmp_path / "signals",
    )
    assert report.signals == 0 and not report.warnings
    assert not (out / "signals" / "index.html").exists()
    assert "Signals</a>" not in read(out / "index.html")


def test_signals_meta_counts_every_level_and_pluralises() -> None:
    assert signals_meta([]) == "0 signals · 0 confirmed · 0 reported · 0 speculated"
    one = [{"confidence": "reported"}]
    assert signals_meta(one) == "1 signal · 0 confirmed · 1 reported · 0 speculated"
    three = [{"confidence": "Confirmed"}, {"confidence": "confirmed"}, {"confidence": "speculated"}]
    assert signals_meta(three) == "3 signals · 2 confirmed · 0 reported · 1 speculated"


def test_stylesheet_rates_ledger_confidence_on_the_stack_scale_and_floors_the_claim() -> None:
    css = read(SITE_STATIC_DIR / "style.css")
    assert "var(--ink)" in css_block(css, ".confidence-confirmed")
    assert "var(--warn)" in css_block(css, ".confidence-speculated")
    assert "min-width" in css_block(css, ".signals .claim")


def test_maps_to_ticker_is_read_the_same_way_everywhere() -> None:
    # The index link, the company-page filter and "Bears on" must agree on a row, whatever the
    # spacing and case of the ticker; a row linked on the index but missing from the company
    # page would be a silent inconsistency.
    from scripts.build_site import _bears_on, _maps_to_html, company_signals

    rows = [{"maps_to": "aaa : toy_price"}, {"maps_to": "BBB:x"}, {"maps_to": "watch"}]
    assert company_signals(rows, "AAA") == [rows[0]]
    assert company_signals(rows, "bbb") == [rows[1]]
    assert _bears_on("aaa : toy_price") == "toy_price"
    assert _bears_on("watch") == "watch"
    html = _maps_to_html("aaa : toy_price", "../", {"AAA"})
    assert 'href="../companies/AAA.html"' in html and html.endswith(": toy_price")
    assert _maps_to_html("stack: compute/price", "../", {"AAA"}) == "stack: compute/price"
